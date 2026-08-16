"""Tenant-facing operations: uploads, chunks, finalize, artifacts.

Every lookup is tenant-scoped in the SQL predicate (``AND tenant_id=?``), so a
foreign or unknown id is indistinguishable from a missing one (404) — existence
itself is private (promise 1).

Each operation opens its own SQLite connection and closes it in a finally.
Finalize is the convergence point of several invariants; see DECISIONS.md:
  * at most one artifact per upload (atomic conditional claim on uploads.state)
  * finalize-vs-GC coordination (acquire_blob under gc_lock)
  * durability ordering (bytes durable before metadata commit)
"""

import hashlib
import sqlite3
import time
import uuid

from . import config, fs, locks, store
from . import db as _db


def _now() -> int:
    return int(time.time())


def _new_upload_id() -> str:
    return "up_" + uuid.uuid4().hex


def _new_artifact_id() -> str:
    return "art_" + uuid.uuid4().hex


def _expected_chunks(total_size: int, chunk_size: int) -> int:
    return (total_size + chunk_size - 1) // chunk_size


def _get_upload_row(conn, tenant_id: str, upload_id: str):
    return conn.execute(
        "SELECT * FROM uploads WHERE upload_id=? AND tenant_id=?",
        (upload_id, tenant_id),
    ).fetchone()


def _rollback(conn) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# --- Upload lifecycle -------------------------------------------------------

def start_upload(cfg, tenant_id: str, name: str, total_size: int, chunk_size: int):
    if total_size < 0 or chunk_size <= 0:
        return (422, {"error": "invalid size"})
    if total_size > config.MAX_ARTIFACT_SIZE:
        return (413, {"error": "artifact too large"})
    if not name or len(name) > config.MAX_NAME_LEN:
        return (422, {"error": "invalid name"})

    conn = _db.open(cfg)
    try:
        upload_id = _new_upload_id()
        conn.execute("BEGIN")
        try:
            conn.execute(
                "INSERT INTO uploads"
                " (upload_id, tenant_id, name, total_size, chunk_size, state, created_at)"
                " VALUES (?,?,?,?,?,'open',?)",
                (upload_id, tenant_id, name, total_size, chunk_size, _now()),
            )
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise
        return (201, {"upload_id": upload_id, "received": []})
    finally:
        conn.close()


def put_chunk(cfg, tenant_id: str, upload_id: str, index: int,
              body: bytes, claimed_sha: str | None):
    conn = _db.open(cfg)
    try:
        up = _get_upload_row(conn, tenant_id, upload_id)
        if up is None:
            return (404, {"error": "not found"})
        expected = _expected_chunks(up["total_size"], up["chunk_size"])
        if index < 0 or index >= expected:
            return (422, {"error": "chunk index out of range"})
        if len(body) > config.MAX_CHUNK_SIZE:
            return (413, {"error": "chunk too large"})
        actual = hashlib.sha256(body).hexdigest()
        if claimed_sha is not None and claimed_sha.lower() != actual:
            return (422, {"error": "chunk sha mismatch"})

        # Serialized per-upload: conflicting-replay resolution is deterministic
        # (first-write-wins) and a finalize cannot race its own late chunks.
        with locks.upload_lock(upload_id):
            existing = conn.execute(
                'SELECT sha256 FROM chunks WHERE upload_id=? AND "index"=?',
                (upload_id, index),
            ).fetchone()
            if existing is not None:
                if existing["sha256"] == actual:
                    return (200, {"status": "ok"})          # idempotent replay
                return (409, {"error": "conflicting chunk"})

            cp = config.chunk_path(cfg, upload_id, index)
            fs.durable_write(cfg.tmp_dir, cp, body)          # durable BEFORE the row
            conn.execute("BEGIN")
            try:
                conn.execute(
                    'INSERT INTO chunks (upload_id, "index", size, sha256)'
                    " VALUES (?,?,?,?)",
                    (upload_id, index, len(body), actual),
                )
                conn.execute("COMMIT")
            except sqlite3.IntegrityError:
                _rollback(conn)
                fs.unlink_quiet(cp)
                existing = conn.execute(
                    'SELECT sha256 FROM chunks WHERE upload_id=? AND "index"=?',
                    (upload_id, index),
                ).fetchone()
                if existing is not None and existing["sha256"] == actual:
                    return (200, {"status": "ok"})
                return (409, {"error": "conflicting chunk"})
        return (201, {"status": "stored"})
    finally:
        conn.close()


def get_upload_state(cfg, tenant_id: str, upload_id: str):
    conn = _db.open(cfg)
    try:
        up = _get_upload_row(conn, tenant_id, upload_id)
        if up is None:
            return None
        received = sorted(
            r[0] for r in conn.execute(
                'SELECT "index" FROM chunks WHERE upload_id=?', (upload_id,)
            )
        )
        return {
            "upload_id": upload_id,
            "received": received,
            "state": up["state"],
            "artifact_id": up["artifact_id"],
        }
    finally:
        conn.close()


def finalize(cfg, tenant_id: str, upload_id: str,
             claimed_sha: str, claimed_size: int):
    conn = _db.open(cfg)
    try:
        up = _get_upload_row(conn, tenant_id, upload_id)
        if up is None:
            return (404, {"error": "not found"})
        if up["state"] == "finalized":
            return (200, {"artifact_id": up["artifact_id"]})     # idempotent

        expected = _expected_chunks(up["total_size"], up["chunk_size"])
        have = {
            r[0] for r in conn.execute(
                'SELECT "index" FROM chunks WHERE upload_id=?', (upload_id,)
            )
        }
        if have != set(range(expected)):
            return (422, {"error": "incomplete upload"})
        if claimed_size != up["total_size"]:
            return (422, {"error": "size mismatch"})

        parts = []
        for i in range(expected):
            try:
                parts.append(fs.read_all(config.chunk_path(cfg, upload_id, i)))
            except FileNotFoundError:
                return (422, {"error": "missing chunk"})
        data = b"".join(parts)
        if len(data) != up["total_size"]:
            return (422, {"error": "assembled size mismatch"})
        H = hashlib.sha256(data).hexdigest()
        if claimed_sha.lower() != H:
            return (422, {"error": "sha mismatch"})
        if len(data) > config.MAX_ARTIFACT_SIZE:
            return (413, {"error": "artifact too large"})

        artifact_id = _new_artifact_id()
        # Per-upload lock first, then gc_lock (strict order; no deadlock).
        with locks.upload_lock(upload_id), locks.gc_lock():
            conn.execute("BEGIN")
            try:
                cur = conn.execute(
                    "UPDATE uploads SET state='finalized', artifact_id=?"
                    " WHERE upload_id=? AND state='open'",
                    (artifact_id, upload_id),
                )
                if cur.rowcount != 1:
                    _rollback(conn)
                    up2 = _get_upload_row(conn, tenant_id, upload_id)
                    if up2 is not None and up2["state"] == "finalized":
                        return (200, {"artifact_id": up2["artifact_id"]})
                    return (409, {"error": "finalize in progress"})
                store.acquire_blob(conn, cfg, H, data)
                conn.execute(
                    "INSERT INTO artifacts"
                    " (artifact_id, tenant_id, name, size, sha256, blob_hash, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (artifact_id, tenant_id, up["name"], len(data), H, H, _now()),
                )
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
        locks.release_upload_lock(upload_id)
        return (200, {"artifact_id": artifact_id})
    finally:
        conn.close()


# --- Artifact reads ---------------------------------------------------------

def get_artifact(cfg, tenant_id: str, artifact_id: str):
    conn = _db.open(cfg)
    try:
        return conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id=? AND tenant_id=?",
            (artifact_id, tenant_id),
        ).fetchone()
    finally:
        conn.close()


def list_artifacts(cfg, tenant_id: str):
    conn = _db.open(cfg)
    try:
        return conn.execute(
            "SELECT artifact_id, name, size, sha256"
            " FROM artifacts WHERE tenant_id=? ORDER BY created_at, artifact_id",
            (tenant_id,),
        ).fetchall()
    finally:
        conn.close()


def delete_artifact(cfg, tenant_id: str, artifact_id: str) -> bool:
    """Delete one artifact's metadata and drop its reference. The blob becomes
    collectable only when no artifact in any tenant still references it.
    """
    conn = _db.open(cfg)
    try:
        conn.execute("BEGIN")
        try:
            row = conn.execute(
                "SELECT blob_hash FROM artifacts WHERE artifact_id=? AND tenant_id=?",
                (artifact_id, tenant_id),
            ).fetchone()
            if row is None:
                _rollback(conn)
                return False
            conn.execute(
                "DELETE FROM artifacts WHERE artifact_id=? AND tenant_id=?",
                (artifact_id, tenant_id),
            )
            conn.execute(
                "UPDATE blobs SET refcount=refcount-1 WHERE content_hash=?",
                (row["blob_hash"],),
            )
            conn.execute("COMMIT")
        except BaseException:
            _rollback(conn)
            raise
        return True
    finally:
        conn.close()
