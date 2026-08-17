"""Database access: schema bootstrap, per-thread connections, crash recovery.

Durability model: SQLite in WAL mode with synchronous=FULL (the WAL file is
fsynced on every commit). All write transactions are short (no filesystem I/O
inside a transaction); the ordering guarantees in DECISIONS.md depend on that.
"""

import os
import sqlite3
import threading

import config

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def _migrations_dir():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "migrations")


def connect():
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=config.SQLITE_BUSY_TIMEOUT_S,
        isolation_level=None,  # autocommit; explicit BEGIN IMMEDIATE for writes
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=%d" % int(config.SQLITE_BUSY_TIMEOUT_S * 1000))
    return conn


def get_conn():
    """Per-thread connection (sqlite3 connections are not thread-safe)."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = connect()
        _local.conn = conn
    return conn


def bootstrap():
    """Create state layout, apply schema, run crash recovery. Idempotent."""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        for d in (config.STATE_DIR, config.BLOBS_DIR, config.CHUNKS_DIR, config.STAGING_DIR):
            os.makedirs(d, exist_ok=True)
        conn = connect()
        try:
            with open(os.path.join(_migrations_dir(), "0001_initial.sql")) as f:
                conn.executescript(f.read())
            recover(conn)
        finally:
            conn.close()
        _initialized = True


def recover(conn):
    """Crash recovery sweep. Runs single-threaded at startup, before serving.

    1. uploads stuck in 'finalizing' (SIGKILL between state transition and
       commit) revert to 'open'; the client may finalize again.
    2. blobs stuck in 'collecting' (SIGKILL inside a GC pass): file still on
       disk -> revert to 'active' (refcount was 0 and increments are blocked
       while collecting, so it is collectable again); file gone -> delete row.
    3. Orphan sweep: byte-store files with no blob row, and chunk files with
       no chunk row, are removed. Orphans are the residue of crashes between a
       filesystem link and its metadata commit; they are never served because
       nothing references them.
    """
    cur = conn.cursor()

    cur.execute("UPDATE uploads SET state='open' WHERE state='finalizing'")

    cur.execute("SELECT hash FROM blobs WHERE state='collecting'")
    for row in cur.fetchall():
        if os.path.exists(blob_file_path(row["hash"])):
            cur.execute(
                "UPDATE blobs SET state='active', gc_pass_id=NULL WHERE hash=?",
                (row["hash"],),
            )
        else:
            cur.execute("DELETE FROM blobs WHERE hash=?", (row["hash"],))

    _sweep_orphans(cur)
    conn.commit()


def _sweep_orphans(cur):
    known = set()
    for (h,) in cur.execute("SELECT hash FROM blobs"):
        known.add(h)
    for sub in os.listdir(config.BLOBS_DIR):
        subpath = os.path.join(config.BLOBS_DIR, sub)
        if not os.path.isdir(subpath):
            continue
        for name in os.listdir(subpath):
            if name not in known:
                try:
                    os.unlink(os.path.join(subpath, name))
                except FileNotFoundError:
                    pass

    rows = {}
    for up, idx in cur.execute("SELECT upload_id, chunk_index FROM upload_chunks"):
        rows.setdefault(up, set()).add(idx)
    for up in os.listdir(config.CHUNKS_DIR):
        uppath = os.path.join(config.CHUNKS_DIR, up)
        if not os.path.isdir(uppath):
            continue
        valid = rows.get(up, set())
        for name in os.listdir(uppath):
            if name.endswith(".tmp"):
                try:
                    os.unlink(os.path.join(uppath, name))
                except FileNotFoundError:
                    pass
                continue
            try:
                idx = int(name)
            except ValueError:
                continue
            if idx not in valid:
                try:
                    os.unlink(os.path.join(uppath, name))
                except FileNotFoundError:
                    pass


def blob_file_path(content_hash):
    """Two-level fan-out: blobs/<hh>/<hash>. Keeps any directory small."""
    return os.path.join(config.BLOBS_DIR, content_hash[:2], content_hash)


def chunk_file_path(upload_id, index):
    return os.path.join(config.CHUNKS_DIR, upload_id, str(index))


def fsync_dir(path):
    """fsync a directory so a rename/link inside it is durable."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
