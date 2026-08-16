"""Content-addressed blob store: acquisition (finalize), GC, validation, cleanup.

This module is where the two hardest invariants live:

* finalize-vs-GC coordination (promise 4) — acquire_blob and gc_pass both run
  under locks.gc_lock(), held across the DB transaction AND the filesystem
  effects, so a GC decision and a finalize reference-claim can never interleave.
* durability ordering — a blob file is staged+fsync'd+renamed (durable) BEFORE
  the metadata transaction commits; a durable finalize therefore implies durable
  bytes.

See DECISIONS.md for the full claims register and how each is checked.
"""

import hashlib
import os

from . import config, fs, locks
from . import db as _db


class BlobIntegrityError(Exception):
    pass


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def acquire_blob(conn, cfg, content_hash: str, data: bytes) -> None:
    """Ensure `data` (whose sha256 is content_hash) is durably stored and add one
    reference to it. MUST be called under locks.gc_lock() and inside the caller's
    metadata transaction.

    Timing equalization: both the dedup (increment) path and the create path spend
    the dominant cost — staging the full bytes to a temp file and fsync'ing it — so
    a tenant cannot infer cross-tenant dedup from finalize latency. Residual
    differences (metadata ops) are disclosed in DECISIONS.md.
    """
    tmp = fs.stage_write(cfg.tmp_dir, data)          # write(S) + fsync
    bp = config.blob_path(cfg, content_hash)
    row = conn.execute(
        "SELECT size FROM blobs WHERE content_hash=?", (content_hash,)
    ).fetchone()

    if row is not None and bp.exists():
        # Existing, intact blob: take a reference, discard the staged copy.
        conn.execute(
            "UPDATE blobs SET refcount=refcount+1 WHERE content_hash=?",
            (content_hash,),
        )
        fs.unlink_quiet(tmp)
        return

    # Create (or recreate after a missing-file anomaly): place bytes durably.
    fs.durable_rename(tmp, bp)
    if not bp.exists():
        raise BlobIntegrityError(content_hash)
    if row is None:
        conn.execute(
            "INSERT INTO blobs (content_hash, size, refcount) VALUES (?, ?, 1)",
            (content_hash, len(data)),
        )
    else:
        # Row present but file was missing: file restored; add the reference.
        conn.execute(
            "UPDATE blobs SET refcount=refcount+1 WHERE content_hash=?",
            (content_hash,),
        )


def gc_pass(cfg) -> dict:
    """One synchronous garbage-collection pass. Collects blobs with refcount=0.

    Ordering: all row deletions in ONE transaction, committed, THEN file unlinks —
    all under gc_lock(). A crash before commit rolls back (nothing collected); a
    crash after commit leaves orphan files (no row) that startup cleanup reclaims.
    GC never touches a blob any artifact references (refcount>0).
    """
    conn = _db.open(cfg)
    try:
        with locks.gc_lock():
            conn.execute("BEGIN")
            try:
                total = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
                rows = conn.execute(
                    "SELECT content_hash, size FROM blobs WHERE refcount = 0"
                ).fetchall()
                for r in rows:
                    conn.execute(
                        "DELETE FROM blobs WHERE content_hash=? AND refcount=0",
                        (r["content_hash"],),
                    )
                conn.execute("COMMIT")
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                raise

            freed = 0
            for r in rows:
                bp = config.blob_path(cfg, r["content_hash"])
                if fs.unlink_quiet(bp):
                    freed += r["size"]
            fs.fsync_dir(cfg.blobs_dir)

        return {"scanned": total, "collected": len(rows), "bytes_freed": freed}
    finally:
        conn.close()


def validate_pass(cfg) -> dict:
    """Re-hash every stored blob; flag mismatches/missing. Advisory only.

    Runs under gc_lock() so a concurrent GC cannot make a blob look missing.
    """
    conn = _db.open(cfg)
    try:
        with locks.gc_lock():
            rows = conn.execute(
                "SELECT content_hash, size FROM blobs"
            ).fetchall()
            ok = mismatch = missing = 0
            for r in rows:
                H = r["content_hash"]
                bp = config.blob_path(cfg, H)
                try:
                    data = fs.read_all(bp)
                except FileNotFoundError:
                    missing += 1
                    conn.execute(
                        "UPDATE blobs SET validated=3 WHERE content_hash=?", (H,)
                    )
                    continue
                if _sha256_hex(data) == H and len(data) == r["size"]:
                    ok += 1
                    conn.execute(
                        "UPDATE blobs SET validated=1 WHERE content_hash=?", (H,)
                    )
                else:
                    mismatch += 1
                    conn.execute(
                        "UPDATE blobs SET validated=2 WHERE content_hash=?", (H,)
                    )
        return {
            "scanned": len(rows),
            "ok": ok,
            "mismatch": mismatch,
            "missing": missing,
        }
    finally:
        conn.close()


def cleanup_orphans(cfg) -> dict:
    """Reclaim files with no metadata row (crashed staged writes, GC'd files whose
    unlink was interrupted). Runs at startup before the server accepts traffic.
    """
    conn = _db.open(cfg)
    removed = {"blobs": 0, "chunks": 0, "tmp": 0}
    try:
        with locks.gc_lock():
            valid_blobs = {
                r[0] for r in conn.execute("SELECT content_hash FROM blobs")
            }
            for name in os.listdir(cfg.blobs_dir):
                if name not in valid_blobs:
                    if fs.unlink_quiet(cfg.blobs_dir / name):
                        removed["blobs"] += 1
            fs.fsync_dir(cfg.blobs_dir)

            valid_chunks = {
                (r[0], r[1])
                for r in conn.execute('SELECT upload_id, "index" FROM chunks')
            }
            for up in os.listdir(cfg.chunks_dir):
                updir = cfg.chunks_dir / up
                if not updir.is_dir():
                    continue
                for name in os.listdir(updir):
                    try:
                        idx = int(name)
                    except ValueError:
                        continue
                    if (up, idx) not in valid_chunks:
                        if fs.unlink_quiet(updir / name):
                            removed["chunks"] += 1

            for name in os.listdir(cfg.tmp_dir):
                if fs.unlink_quiet(cfg.tmp_dir / name):
                    removed["tmp"] += 1
    finally:
        conn.close()
    return removed
