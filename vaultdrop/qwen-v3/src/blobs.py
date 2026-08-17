"""The byte store: content-addressed blobs, ingest, garbage collection,
validation.

Invariants (see DECISIONS.md claims register):
- A blob row in state 'active' is never deleted by GC; GC only ever deletes
  rows it itself transitioned to 'collecting' in the current pass, and only if
  they are still 'collecting' with refcount 0 at deletion time.
- Refcount increments apply only to rows in state 'active'. Collecting rows
  are frozen: no increment can land on them, so GC's final deletion re-check
  (state='collecting' AND refcount=0) is sound.
- Filesystem links (bytes placement) always happen BEFORE the metadata commit
  that references them. Crash residue is an orphan file, which is never
  served (nothing references it) and is swept at startup.
"""

import hashlib
import os
import time

import config
import db


class FinalizeBlocked(Exception):
    """A required coordination wait timed out; finalize must roll back."""


def now():
    return int(time.time())


def stream_hash_file(path):
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            block = f.read(config.IO_BLOCK)
            if not block:
                break
            h.update(block)
            size += len(block)
    return h.hexdigest(), size


def stream_copy(src_path, dst_path):
    """Streaming copy; also returns (sha256hex, bytes_copied) of the source."""
    h = hashlib.sha256()
    size = 0
    with open(src_path, "rb") as sf, open(dst_path, "wb") as df:
        while True:
            block = sf.read(config.IO_BLOCK)
            if not block:
                break
            df.write(block)
            h.update(block)
            size += len(block)
    db.fsync_file(dst_path)
    return h.hexdigest(), size


def _row_state(conn, digest):
    row = conn.execute("SELECT state FROM blobs WHERE hash=?", (digest,)).fetchone()
    return None if row is None else row["state"]


def _wait_for_gc_to_release(conn, digest):
    """Block until an in-flight GC pass deletes the collecting row for digest.

    The GC pass's remaining phases (file unlink + short deletion transaction)
    complete in milliseconds-to-seconds; we simply wait for the row to go.
    """
    deadline = time.monotonic() + config.GC_WAIT_TIMEOUT_S
    while True:
        if _row_state(conn, digest) is None:
            return
        if time.monotonic() > deadline:
            raise FinalizeBlocked("GC pass holding %s did not complete" % digest)
        time.sleep(config.GC_WAIT_POLL_MS / 1000.0)


def _settle_concurrent_or_new(conn, digest, staging_path):
    """Link the staged bytes into the byte store.

    Returns 'new' on success. If the destination already exists without a blob
    row (a concurrent same-content finalize that has not committed yet), wait
    for its row and switch to the increment path; a destination that exists
    with no row after the settle window is crash residue and is replaced.
    """
    dst = db.blob_file_path(digest)
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.link(staging_path, dst)
        db.fsync_dir(os.path.dirname(dst))
        return "new"
    except FileExistsError:
        pass
    deadline = time.monotonic() + config.ORPHAN_SETTLE_MS / 1000.0
    while True:
        state = _row_state(conn, digest)
        if state == "active":
            return "increment"
        if state is None and time.monotonic() > deadline:
            try:
                os.unlink(dst)
            except FileNotFoundError:
                pass
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.link(staging_path, dst)
            db.fsync_dir(os.path.dirname(dst))
            return "new"
        time.sleep(0.01)


def decide_ingest_mode(conn, digest, staging_path):
    """Choose how the finalization commit will account for the bytes.

    Returns 'new' (staging file already linked into the store; commit must
    insert the blob row) or 'increment' (bytes already resident; commit must
    bump the refcount). Raises FinalizeBlocked if a GC pass holds the content
    and does not release within the timeout.
    """
    state = _row_state(conn, digest)
    if state == "collecting":
        _wait_for_gc_to_release(conn, digest)
        state = None
    if state is None:
        return _settle_concurrent_or_new(conn, digest, staging_path)
    if state == "active":
        return "increment"
    raise FinalizeBlocked("unexpected blob state")


def commit_finalization(conn, upload_id, tenant_id, name, digest, size, mode, artifact_id):
    """The atomic finalization commit: artifact row + upload state transition
    + blob accounting, in one IMMEDIATE transaction. Crash before commit ->
    nothing happened (at most an orphan file); crash after commit -> the full
    artifact is durable.
    """
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    try:
        # Blob accounting lands first: the artifact row references it.
        if mode == "new":
            cur.execute(
                "INSERT INTO blobs(hash, size, refcount, state, validated, created_at)"
                " VALUES(?,?,1,'active',1,?)",
                (digest, size, now()),
            )
        else:
            cur.execute(
                "UPDATE blobs SET refcount=refcount+1 WHERE hash=? AND state='active'",
                (digest,),
            )
            if cur.rowcount != 1:
                raise _IncrementLost()
        cur.execute(
            "INSERT INTO artifacts(id, tenant_id, name, size, sha256, blob_hash, created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (artifact_id, tenant_id, name, size, digest, digest, now()),
        )
        cur.execute(
            "UPDATE uploads SET state='finalized', artifact_id=?, finalized_at=?"
            " WHERE id=? AND state='finalizing'",
            (artifact_id, now(), upload_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise


class _IncrementLost(Exception):
    pass


def gc_pass(conn):
    """One synchronous garbage-collection pass.

    Phase 1 (short write txn): mark active refcount-0 blobs as collecting.
    Phase 2 (no lock held): unlink the marked files.
    Phase 3 (short write txn): delete the marked rows, re-verifying state and
    refcount. Concurrent finalizes are either already counted (refcount>0,
    never marked) or blocked on the collecting row until this pass finishes.
    """
    cur = conn.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute("INSERT INTO gc_passes(started_at) VALUES(?)", (now(),))
    pass_id = cur.lastrowid
    cur.execute(
        "UPDATE blobs SET state='collecting', gc_pass_id=?"
        " WHERE state='active' AND refcount=0",
        (pass_id,),
    )
    candidates = cur.execute(
        "SELECT hash, size FROM blobs WHERE gc_pass_id=?", (pass_id,)
    ).fetchall()
    conn.commit()

    bytes_freed = 0
    dirs = set()
    for row in candidates:
        path = db.blob_file_path(row["hash"])
        try:
            os.unlink(path)
            bytes_freed += row["size"]
            dirs.add(os.path.dirname(path))
        except FileNotFoundError:
            pass
    for d in dirs:
        db.fsync_dir(d)

    collected = 0
    cur.execute("BEGIN IMMEDIATE")
    cur.execute(
        "DELETE FROM blobs WHERE gc_pass_id=? AND state='collecting' AND refcount=0",
        (pass_id,),
    )
    collected = cur.rowcount
    cur.execute(
        "UPDATE gc_passes SET finished_at=?, scanned=?, collected=?, bytes_freed=?"
        " WHERE id=?",
        (now(), len(candidates), collected, bytes_freed, pass_id),
    )
    conn.commit()
    return {"scanned": len(candidates), "collected": collected, "bytes_freed": bytes_freed}


def validate_pass(conn):
    """Re-hash every active blob; flag rows whose bytes no longer match their
    address. Pure file reads plus one short UPDATE per changed row: never
    holds a write lock across a read, so unrelated operations are unaffected.
    """
    scanned = ok = mismatch = 0
    for row in conn.execute("SELECT hash, size, validated FROM blobs WHERE state='active'"):
        scanned += 1
        path = db.blob_file_path(row["hash"])
        try:
            digest, size = stream_hash_file(path)
        except FileNotFoundError:
            digest, size = None, -1
        good = digest == row["hash"] and size == row["size"]
        if good:
            ok += 1
            if row["validated"] == 0:
                conn.execute("UPDATE blobs SET validated=1 WHERE hash=?", (row["hash"],))
        else:
            mismatch += 1
            if row["validated"] == 1:
                conn.execute("UPDATE blobs SET validated=0 WHERE hash=?", (row["hash"],))
    return {"scanned": scanned, "ok": ok, "mismatch": mismatch}


def decrement_refcount(conn, digest):
    conn.execute(
        "UPDATE blobs SET refcount=refcount-1 WHERE hash=? AND refcount>0", (digest,)
    )
