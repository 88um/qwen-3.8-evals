"""Resumable chunked uploads and the finalization state machine.

Chunk placement protocol (crash- and race-safe):
- Body is streamed to a private tmp file while its SHA-256 is computed.
- The chunk row (metadata) is committed first; the tmp file is then linked
  into place. A link onto an existing destination is never an overwrite: the
  existing file wins, and the response is decided by comparing hashes. The
  only residue a crash can leave is an orphan file, which is swept at startup
  and self-heals on the client's retry.

Finalization protocol: per-upload state machine (open -> finalizing ->
finalized) arbitrated by conditional UPDATEs; assembly streams chunks into a
staging file while hashing; the artifact row, the upload state transition,
and the blob accounting land in ONE IMMEDIATE transaction. See blobs.py for
the GC-coordination half.
"""

import hashlib
import os
import time
import uuid

import blobs
import config
import db


class UploadError(Exception):
    def __init__(self, status, message, consumed=0):
        self.status = status
        self.message = message
        self.consumed = consumed  # body bytes already read; lets the HTTP
        # layer drain the remainder before responding (keep-alive hygiene)
        super().__init__(message)


def _ceil_chunks(total_size, chunk_size):
    return (total_size + chunk_size - 1) // chunk_size if chunk_size else 0


def start_upload(conn, tenant_id, name, total_size, chunk_size):
    if not isinstance(name, str) or not (1 <= len(name) <= config.MAX_NAME_LEN):
        raise UploadError(400, "name must be a string of 1..%d chars" % config.MAX_NAME_LEN)
    if not isinstance(total_size, int) or isinstance(total_size, bool) or total_size < 0:
        raise UploadError(400, "total_size must be a non-negative integer")
    if total_size > config.MAX_ARTIFACT_SIZE:
        raise UploadError(413, "total_size exceeds max artifact size (%d)" % config.MAX_ARTIFACT_SIZE)
    if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
        raise UploadError(400, "chunk_size must be a positive integer")
    if chunk_size > config.MAX_CHUNK_SIZE:
        raise UploadError(400, "chunk_size exceeds max chunk size (%d)" % config.MAX_CHUNK_SIZE)
    upload_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO uploads(id, tenant_id, name, total_size, chunk_size, state, created_at)"
        " VALUES(?,?,?,?,?,'open',?)",
        (upload_id, tenant_id, name, total_size, chunk_size, blobs.now()),
    )
    return upload_id


def get_upload(conn, tenant_id, upload_id):
    row = conn.execute(
        "SELECT id, state FROM uploads WHERE id=? AND tenant_id=?", (upload_id, tenant_id)
    ).fetchone()
    if row is None:
        return None
    received = [r["chunk_index"] for r in conn.execute(
        "SELECT chunk_index FROM upload_chunks WHERE upload_id=? ORDER BY chunk_index",
        (upload_id,),
    )]
    return {"upload_id": upload_id, "received": received, "state": row["state"]}


def _read_upload(conn, tenant_id, upload_id):
    row = conn.execute(
        "SELECT id, tenant_id, total_size, chunk_size, state FROM uploads WHERE id=?",
        (upload_id,),
    ).fetchone()
    if row is None or row["tenant_id"] != tenant_id:
        raise UploadError(404, "not found")
    return row


def store_chunk(conn, tenant_id, upload_id, index, body_file, declared_sha, content_length):
    """Streams the request body into the chunk store. Returns None on success.

    Raises UploadError with the appropriate status otherwise. Reads are bounded
    by the remaining Content-Length so keep-alive connections never block on a
    short final read.
    """
    row = _read_upload(conn, tenant_id, upload_id)
    if row["state"] == "finalized":
        raise UploadError(409, "upload already finalized")
    chunk_count = _ceil_chunks(row["total_size"], row["chunk_size"])
    if index >= chunk_count:
        raise UploadError(400, "chunk index out of range")
    if declared_sha is None:
        raise UploadError(400, "missing X-Chunk-SHA256 header")
    if len(declared_sha) != 64:
        raise UploadError(400, "X-Chunk-SHA256 must be 64 hex chars")

    limit = min(row["chunk_size"], config.MAX_CHUNK_SIZE)
    updir = os.path.join(config.CHUNKS_DIR, upload_id)
    os.makedirs(updir, exist_ok=True)
    tmp_path = os.path.join(updir, "%d.%s.tmp" % (index, uuid.uuid4().hex[:8]))
    h = hashlib.sha256()
    size = 0
    oversize = False
    try:
        with open(tmp_path, "wb") as tmp:
            remaining = content_length
            while remaining > 0:
                n = min(config.IO_BLOCK, remaining)
                block = body_file.read(n)
                got = len(block)
                if got == 0:
                    break
                size += got
                remaining -= got
                if size > limit:
                    oversize = True
                    break
                tmp.write(block)
                h.update(block)
        if oversize:
            raise UploadError(413, "chunk exceeds size limit", consumed=size)
        db.fsync_file(tmp_path)
    except UploadError:
        _unlink_quiet(tmp_path)
        raise
    except OSError:
        _unlink_quiet(tmp_path)
        raise UploadError(500, "storage I/O failure", consumed=size)

    computed = h.hexdigest()
    if computed != declared_sha.lower():
        _unlink_quiet(tmp_path)
        raise UploadError(400, "chunk digest mismatch", consumed=content_length)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT OR IGNORE INTO upload_chunks(upload_id, chunk_index, size, sha256)"
            " VALUES(?,?,?,?)",
            (upload_id, index, size, computed),
        )
        stored = conn.execute(
            "SELECT sha256 FROM upload_chunks WHERE upload_id=? AND chunk_index=?",
            (upload_id, index),
        ).fetchone()["sha256"]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    if stored != computed:
        _unlink_quiet(tmp_path)
        raise UploadError(409, "conflicting chunk already stored at this index",
                          consumed=content_length)

    dst = db.chunk_file_path(upload_id, index)
    try:
        os.link(tmp_path, dst)
        db.fsync_dir(updir)
    except FileExistsError:
        pass
    except OSError:
        _unlink_quiet(tmp_path)
        raise UploadError(500, "storage I/O failure")
    _unlink_quiet(tmp_path)
    return None


def _unlink_quiet(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def _rollback_finalizing(conn, upload_id):
    conn.execute(
        "UPDATE uploads SET state='open' WHERE id=? AND state='finalizing'", (upload_id,)
    )


def _assembly(conn, upload_id, total_size, chunk_size):
    """Streams chunks 0..K-1 into a staging file. Returns (path, digest, size)."""
    chunk_count = _ceil_chunks(total_size, chunk_size)
    staging = os.path.join(config.STAGING_DIR, "%s.%s.tmp" % (upload_id, uuid.uuid4().hex[:8]))
    h = hashlib.sha256()
    size = 0
    try:
        with open(staging, "wb") as out:
            for index in range(chunk_count):
                src = db.chunk_file_path(upload_id, index)
                if not os.path.exists(src):
                    raise _MissingChunk(index)
                with open(src, "rb") as f:
                    while True:
                        block = f.read(config.IO_BLOCK)
                        if not block:
                            break
                        out.write(block)
                        h.update(block)
                        size += len(block)
        db.fsync_file(staging)
    except _MissingChunk:
        _unlink_quiet(staging)
        raise
    return staging, h.hexdigest(), size


class _MissingChunk(Exception):
    pass


def finalize(conn, tenant_id, upload_id, declared_sha256, declared_size):
    """Returns (status, payload). At most one artifact ever results from an
    upload; concurrent finalizers converge on the winner's artifact id."""
    row = conn.execute(
        "SELECT id, tenant_id, name, total_size, chunk_size, state, artifact_id"
        " FROM uploads WHERE id=?",
        (upload_id,),
    ).fetchone()
    if row is None or row["tenant_id"] != tenant_id:
        return 404, {"error": "not found"}
    if row["state"] == "finalized":
        if row["artifact_id"] is None:
            return 409, {"error": "upload was finalized; its artifact has been deleted"}
        return 200, {"artifact_id": row["artifact_id"]}
    if declared_sha256 is None or len(declared_sha256) != 64:
        return 400, {"error": "sha256 must be 64 hex chars"}
    if not isinstance(declared_size, int) or isinstance(declared_size, bool) or declared_size < 0:
        return 400, {"error": "size must be a non-negative integer"}
    if declared_size != row["total_size"]:
        return 422, {"error": "declared size does not match upload"}

    conn.execute("BEGIN IMMEDIATE")
    cur = conn.cursor()
    cur.execute(
        "UPDATE uploads SET state='finalizing' WHERE id=? AND state='open'", (upload_id,)
    )
    won = cur.rowcount == 1
    conn.commit()

    if not won:
        return _finalize_loser(conn, upload_id)

    try:
        return _finalize_winner(conn, row, declared_sha256)
    except blobs.FinalizeBlocked:
        _rollback_finalizing(conn, upload_id)
        return 500, {"error": "finalization blocked by in-flight GC; retry"}
    except UploadError as e:
        _rollback_finalizing(conn, upload_id)
        return e.status, {"error": e.message}
    except Exception:
        import sys, traceback
        traceback.print_exc(file=sys.stderr)
        _rollback_finalizing(conn, upload_id)
        return 500, {"error": "internal error during finalization"}


def _finalize_loser(conn, upload_id):
    deadline = time.monotonic() + config.FINALIZE_LOSER_TIMEOUT_S
    while True:
        state = conn.execute(
            "SELECT state, artifact_id FROM uploads WHERE id=?", (upload_id,)
        ).fetchone()
        if state["state"] == "finalized":
            return 200, {"artifact_id": state["artifact_id"]}
        if state["state"] == "open":
            return 409, {"error": "concurrent finalization failed; retry"}
        if time.monotonic() > deadline:
            return 500, {"error": "concurrent finalization did not complete; retry"}
        time.sleep(config.FINALIZE_LOSER_POLL_MS / 1000.0)


def _finalize_winner(conn, row, declared_sha256):
    upload_id = row["id"]
    tenant_id = row["tenant_id"]
    try:
        staging, digest, size = _assembly(
            conn, upload_id, row["total_size"], row["chunk_size"]
        )
    except _MissingChunk:
        _rollback_finalizing(conn, upload_id)
        return 422, {"error": "chunks incomplete"}
    except OSError:
        _rollback_finalizing(conn, upload_id)
        return 500, {"error": "storage I/O failure"}
    if digest != declared_sha256.lower() or size != row["total_size"]:
        _unlink_quiet(staging)
        _rollback_finalizing(conn, upload_id)
        return 422, {"error": "content does not match declared digest"}

    artifact_id = uuid.uuid4().hex
    while True:
        try:
            mode = blobs.decide_ingest_mode(conn, digest, staging)
        except blobs.FinalizeBlocked:
            _unlink_quiet(staging)
            _rollback_finalizing(conn, upload_id)
            return 500, {"error": "finalization blocked by in-flight GC; retry"}
        try:
            blobs.commit_finalization(
                conn, upload_id, tenant_id, row["name"], digest, size, mode, artifact_id
            )
            break
        except blobs._IncrementLost:
            continue
        except Exception:
            import sys, traceback
            traceback.print_exc(file=sys.stderr)
            _unlink_quiet(staging)
            _rollback_finalizing(conn, upload_id)
            return 500, {"error": "internal error during finalization"}
    if mode == "increment":
        _unlink_quiet(staging)
    return 200, {"artifact_id": artifact_id}


def delete_artifact(conn, tenant_id, artifact_id):
    """Returns (status, payload)."""
    row = conn.execute(
        "SELECT id, tenant_id, blob_hash FROM artifacts WHERE id=?", (artifact_id,)
    ).fetchone()
    if row is None or row["tenant_id"] != tenant_id:
        return 404, {"error": "not found"}
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE uploads SET artifact_id=NULL WHERE artifact_id=?", (artifact_id,)
        )
        conn.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
        blobs.decrement_refcount(conn, row["blob_hash"])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return 204, {}


def list_artifacts(conn, tenant_id):
    return [
        {
            "artifact_id": r["id"],
            "name": r["name"],
            "size": r["size"],
            "sha256": r["sha256"],
        }
        for r in conn.execute(
            "SELECT id, name, size, sha256 FROM artifacts"
            " WHERE tenant_id=? ORDER BY created_at, id",
            (tenant_id,),
        )
    ]


def get_artifact(conn, tenant_id, artifact_id):
    """Returns the artifact row (with blob path) or None."""
    row = conn.execute(
        "SELECT id, blob_hash, size FROM artifacts WHERE id=? AND tenant_id=?",
        (artifact_id, tenant_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "path": db.blob_file_path(row["blob_hash"]),
        "size": row["size"],
        "sha256": None,
    }
