"""VaultDrop metadata store: schema, per-thread connections, crash recovery."""

import os
import sqlite3
import threading

from . import core

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    upload_id   TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    total_size  INTEGER NOT NULL CHECK (total_size >= 0),
    chunk_size  INTEGER NOT NULL CHECK (chunk_size > 0),
    state       TEXT NOT NULL CHECK (state IN ('active', 'finalized')),
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_tenant ON uploads (tenant_id);

CREATE TABLE IF NOT EXISTS chunks (
    upload_id    TEXT NOT NULL,
    "index"      INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    size         INTEGER NOT NULL,
    PRIMARY KEY (upload_id, "index")
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    upload_id   TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    sha256      TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_tenant ON artifacts (tenant_id);

CREATE TABLE IF NOT EXISTS blobs (
    content_hash TEXT PRIMARY KEY,
    size         INTEGER NOT NULL,
    path         TEXT NOT NULL,
    refcount     INTEGER NOT NULL DEFAULT 0 CHECK (refcount >= 0),
    state        TEXT NOT NULL CHECK (state IN ('active', 'pending', 'deleted')),
    validated    INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blobs_gc ON blobs (state, refcount);
"""

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=FULL",
    "PRAGMA busy_timeout=15000",
)


class _ConnBox(threading.local):
    def __init__(self):
        self.conn = None


def _open(state_dir):
    conn = sqlite3.connect(core.db_path(state_dir), isolation_level=None,
                            check_same_thread=False)
    for p in _PRAGMAS:
        conn.execute(p)
    return conn


class Database:
    """Per-thread SQLite connections over one WAL database."""

    def __init__(self, state_dir):
        self.state_dir = state_dir
        self._box = _ConnBox()

    def conn(self):
        if self._box.conn is None:
            self._box.conn = _open(self.state_dir)
        return self._box.conn

    def migrate(self):
        core.ensure_layout(self.state_dir)
        c = self.conn()
        c.executescript(SCHEMA)

    # -- transaction helper --------------------------------------------------
    def txn(self, fn):
        """Run fn(conn) inside an IMMEDIATE write transaction."""
        c = self.conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            out = fn(c)
            c.execute("COMMIT")
            return out
        except Exception:
            try:
                c.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise

    def read(self, sql, args=()):
        cur = self.conn().execute(sql, args)
        return cur.fetchall()

    def read_one(self, sql, args=()):
        cur = self.conn().execute(sql, args)
        return cur.fetchone()


def migrate(state_dir):
    Database(state_dir).migrate()


# --- Crash recovery ----------------------------------------------------------
def recover(state_dir, db):
    """Sweep partial/orphaned state left by SIGKILL, purge tombstones.

    Runs before serve accepts traffic. Every sweep target is, by construction,
    bytes no committed artifact references (see DECISIONS.md, durability).
    """
    sd = state_dir

    # 1. Assemble temp files (finalize crashed between write and claim).
    tmpd = core.assemble_tmp_dir(sd)
    for name in os.listdir(tmpd) if os.path.isdir(tmpd) else ():
        core.unlink_quiet(os.path.join(tmpd, name))

    # 2. Chunk temp files (PUT crashed between write and rename).
    croot = os.path.join(sd, "chunks")
    orphan_chunk_files = []
    if os.path.isdir(croot):
        for uid in os.listdir(croot):
            udir = os.path.join(croot, uid)
            if not os.path.isdir(udir):
                continue
            tdir = core.chunk_tmp_dir(sd, uid)
            if os.path.isdir(tdir):
                for name in os.listdir(tdir):
                    core.unlink_quiet(os.path.join(tdir, name))
                os.rmdir(tdir) if _empty(tdir) else None
            for name in os.listdir(udir):
                if name.isdigit():
                    orphan_chunk_files.append((uid, int(name), os.path.join(udir, name)))

    # 3. Chunk files without committed rows (and rows without files).
    c = db.conn()
    for uid, idx, path in orphan_chunk_files:
        row = c.execute(
            'SELECT 1 FROM chunks WHERE upload_id=? AND "index"=?', (uid, idx)
        ).fetchone()
        if row is None:
            core.unlink_quiet(path)
    # rows whose backing file vanished (filesystem anomaly): drop the row so
    # the upload stays resumable via re-PUT.
    for (uid, idx) in db.read('SELECT upload_id, "index" FROM chunks'):
        if not os.path.exists(core.chunk_path(sd, uid, idx)):
            c.execute('DELETE FROM chunks WHERE upload_id=? AND "index"=?', (uid, idx))

    # 4. Upload rows that vanished take their chunk rows/files with them.
    for (uid,) in db.read("SELECT upload_id FROM chunks WHERE upload_id NOT IN (SELECT upload_id FROM uploads)"):
        for f in os.listdir(core.chunks_dir(sd, uid)) if os.path.isdir(core.chunks_dir(sd, uid)) else ():
            core.unlink_quiet(os.path.join(core.chunks_dir(sd, uid), f))
        c.execute("DELETE FROM chunks WHERE upload_id=?", (uid,))

    # 5. Blob sweep: orphan bytes (no row), tombstones, quarantines.
    broot = os.path.join(sd, "blobs")
    if os.path.isdir(broot):
        for shard in os.listdir(broot):
            sdir = os.path.join(broot, shard)
            if not os.path.isdir(sdir):
                continue
            for name in os.listdir(sdir):
                path = os.path.join(sdir, name)
                if name.endswith(".gcq.") or ".gcq." in name:
                    core.unlink_quiet(path)
                    continue
                row = c.execute(
                    "SELECT state FROM blobs WHERE content_hash=?", (name,)
                ).fetchone()
                if row is None:
                    core.unlink_quiet(path)
                elif row[0] == "deleted":
                    core.unlink_quiet(path)
                    c.execute("DELETE FROM blobs WHERE content_hash=?", (name,))
                else:
                    # active/pending: bytes must be present and full-size.
                    b = c.execute(
                        "SELECT size FROM blobs WHERE content_hash=?", (name,)
                    ).fetchone()
                    if b is not None and os.path.getsize(path) != b[0]:
                        c.execute("UPDATE blobs SET validated=0 WHERE content_hash=?", (name,))

    # 6. Purge any remaining tombstone rows (their bytes are unreferenced).
    c.execute("DELETE FROM blobs WHERE state='deleted'")

    # 7. Consolidate WAL.
    try:
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.Error:
        pass


def _empty(path):
    try:
        return not os.listdir(path)
    except OSError:
        return True
