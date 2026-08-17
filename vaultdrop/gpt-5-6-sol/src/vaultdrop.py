#!/usr/bin/env python3
"""VaultDrop: a crash-conscious, multi-tenant artifact store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sqlite3
import sys
import threading
import time
from contextlib import closing
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, Iterator
from urllib.parse import urlsplit


MAX_ARTIFACT_SIZE = 10 * 1024 * 1024 * 1024
MAX_CHUNK_SIZE = 32 * 1024 * 1024
MAX_JSON_SIZE = 64 * 1024
IO_BLOCK = 1024 * 1024
ID_RE = re.compile(r"^[0-9a-f]{32}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
INPUT_HASH_RE = re.compile(r"^[0-9A-Fa-f]{64}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    total_size INTEGER NOT NULL CHECK(total_size >= 0),
    chunk_size INTEGER NOT NULL CHECK(chunk_size > 0),
    state TEXT NOT NULL CHECK(state IN ('uploading','finalizing','finalized')),
    expected_sha256 TEXT,
    expected_size INTEGER,
    artifact_id TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS uploads_tenant_idx ON uploads(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS chunks (
    upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
    size INTEGER NOT NULL CHECK(size >= 0),
    sha256 TEXT NOT NULL,
    PRIMARY KEY(upload_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS blobs (
    content_hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL CHECK(size >= 0),
    refcount INTEGER NOT NULL CHECK(refcount >= 0),
    validated INTEGER NOT NULL CHECK(validated IN (0,1)),
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    upload_id TEXT NOT NULL UNIQUE REFERENCES uploads(id),
    name TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    sha256 TEXT NOT NULL REFERENCES blobs(content_hash),
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS artifacts_tenant_idx
    ON artifacts(tenant_id, created_at, id);
CREATE INDEX IF NOT EXISTS artifacts_hash_idx ON artifacts(sha256);
"""


class ApiError(Exception):
    def __init__(self, status: int, message: str, *, close: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.close = close


def fsync_dir(path: Path) -> None:
    """Persist directory entry changes on POSIX filesystems."""
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def unlink_if_present(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


class LockPool:
    """Stable, per-key locks. The bounded keyspace is acceptable at 10k+ blobs."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def for_key(self, key: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(key, threading.RLock())


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.db_path = root / "vaultdrop.sqlite3"
        self.upload_root = root / "uploads"
        self.blob_root = root / "blobs"
        self.trash_root = root / "gc-trash"
        self.blob_locks = LockPool()
        self.validation_lock = threading.Lock()
        self.tenant_tokens: dict[str, str] = {}
        self.admin_token = ""

    def prepare(self, load_auth: bool = True) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.upload_root.mkdir(exist_ok=True)
        self.blob_root.mkdir(exist_ok=True)
        self.trash_root.mkdir(exist_ok=True)
        with self.db() as db:
            db.executescript(SCHEMA)
            db.commit()
        fsync_dir(self.root)
        self.recover()
        if load_auth:
            self.load_auth()

    def db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    def load_auth(self) -> None:
        auth_path = self.root / "tenants.json"
        try:
            with auth_path.open("r", encoding="utf-8") as f:
                doc = json.load(f)
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"cannot read tenants.json: {exc}") from exc
        tenants = doc.get("tenants")
        admin = doc.get("admin_token")
        if not isinstance(tenants, list) or not isinstance(admin, str) or not admin:
            raise RuntimeError("invalid tenants.json")
        by_token: dict[str, str] = {}
        for item in tenants:
            if not isinstance(item, dict):
                raise RuntimeError("invalid tenant entry")
            tenant_id, token = item.get("id"), item.get("token")
            if not isinstance(tenant_id, str) or not tenant_id:
                raise RuntimeError("invalid tenant id")
            if not isinstance(token, str) or not token or token in by_token:
                raise RuntimeError("invalid or duplicate tenant token")
            by_token[token] = tenant_id
        if admin in by_token:
            raise RuntimeError("admin token must differ from tenant tokens")
        self.tenant_tokens = by_token
        self.admin_token = admin

    def recover(self) -> None:
        """Repair only states left at explicit crash boundaries."""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE uploads SET state='finalized'
                   WHERE artifact_id IS NOT NULL
                     AND EXISTS (SELECT 1 FROM artifacts a WHERE a.id=uploads.artifact_id)"""
            )
            db.execute(
                """UPDATE uploads SET state='uploading', expected_sha256=NULL,
                                      expected_size=NULL, artifact_id=NULL
                   WHERE state='finalizing'"""
            )
            db.execute(
                """UPDATE blobs SET refcount=(
                       SELECT COUNT(*) FROM artifacts a
                       WHERE a.sha256=blobs.content_hash)"""
            )
            db.commit()

        # A GC rename precedes its metadata commit. Restore only if a reference won.
        for entry in self.trash_root.iterdir():
            match = re.match(r"^([0-9a-f]{64})\.[0-9a-f]+\.gc$", entry.name)
            if not match or not entry.is_file():
                continue
            digest = match.group(1)
            with self.db() as db:
                row = db.execute(
                    "SELECT refcount FROM blobs WHERE content_hash=?", (digest,)
                ).fetchone()
            if row is not None and row["refcount"] > 0:
                target = self.blob_path(digest)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    os.replace(entry, target)
                    fsync_dir(target.parent)
                else:
                    unlink_if_present(entry)
            else:
                unlink_if_present(entry)
        fsync_dir(self.trash_root)

        # Remove incomplete request temporaries and chunk files lacking a DB commit.
        with self.db() as db:
            for parent, _, files in os.walk(self.upload_root):
                p = Path(parent)
                for name in files:
                    path = p / name
                    if ".tmp." in name:
                        unlink_if_present(path)
                        continue
                    if not name.endswith(".chunk"):
                        continue
                    upload_id = p.parent.name if p.name == "chunks" else ""
                    try:
                        index = int(name[:-6])
                    except ValueError:
                        continue
                    found = db.execute(
                        """SELECT u.state FROM chunks c JOIN uploads u ON u.id=c.upload_id
                           WHERE c.upload_id=? AND c.chunk_index=?""",
                        (upload_id, index),
                    ).fetchone()
                    if found is None or found["state"] == "finalized":
                        unlink_if_present(path)

        # Blob installs happen before metadata commits. Such uncommitted files are safe
        # to remove, while missing zero-reference rows are safe to forget.
        with self.db() as db:
            for first in self.blob_root.iterdir():
                if not first.is_dir():
                    continue
                for path in first.iterdir():
                    if ".tmp." in path.name:
                        unlink_if_present(path)
                        continue
                    if not path.name.endswith(".blob"):
                        continue
                    digest = path.name[:-5]
                    if not HASH_RE.fullmatch(digest):
                        continue
                    found = db.execute(
                        "SELECT 1 FROM blobs WHERE content_hash=?", (digest,)
                    ).fetchone()
                    if found is None:
                        unlink_if_present(path)

    def upload_dir(self, upload_id: str) -> Path:
        return self.upload_root / upload_id

    def chunk_path(self, upload_id: str, index: int) -> Path:
        return self.upload_dir(upload_id) / "chunks" / f"{index}.chunk"

    def cleanup_chunk_files(self, upload_id: str) -> None:
        directory = self.upload_dir(upload_id) / "chunks"
        try:
            for path in directory.iterdir():
                if path.is_file():
                    unlink_if_present(path)
            fsync_dir(directory)
        except FileNotFoundError:
            pass

    def blob_path(self, digest: str) -> Path:
        # 256 fanout directories keep a 10k-blob store at about 39 entries/dir,
        # while also bounding the number of directory fsyncs in a full GC pass.
        return self.blob_root / digest[:2] / f"{digest}.blob"

    def create_upload(self, tenant: str, name: str, total: int, chunk: int) -> str:
        upload_id = secrets.token_hex(16)
        directory = self.upload_dir(upload_id) / "chunks"
        directory.mkdir(parents=True)
        fsync_dir(directory.parent)
        fsync_dir(self.upload_root)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """INSERT INTO uploads
                   (id,tenant_id,name,total_size,chunk_size,state,created_at)
                   VALUES(?,?,?,?,?,'uploading',?)""",
                (upload_id, tenant, name, total, chunk, time.time_ns()),
            )
            db.commit()
        return upload_id

    def upload_status(self, tenant: str, upload_id: str) -> dict:
        with self.db() as db:
            row = db.execute(
                "SELECT id,state FROM uploads WHERE id=? AND tenant_id=?",
                (upload_id, tenant),
            ).fetchone()
            if row is None:
                raise ApiError(404, "upload not found")
            received = [
                r[0]
                for r in db.execute(
                    "SELECT chunk_index FROM chunks WHERE upload_id=? ORDER BY chunk_index",
                    (upload_id,),
                )
            ]
        return {"upload_id": upload_id, "received": received, "state": row["state"]}

    def put_chunk(
        self,
        tenant: str,
        upload_id: str,
        index: int,
        expected_hash: str,
        source: BinaryIO,
        length: int,
    ) -> None:
        with self.db() as db:
            upload = db.execute(
                """SELECT total_size,chunk_size,state FROM uploads
                   WHERE id=? AND tenant_id=?""",
                (upload_id, tenant),
            ).fetchone()
        if upload is None:
            raise ApiError(404, "upload not found", close=True)
        if upload["state"] != "uploading":
            raise ApiError(409, "upload is not accepting chunks", close=True)
        count = (upload["total_size"] + upload["chunk_size"] - 1) // upload["chunk_size"]
        if index < 0 or index >= count:
            raise ApiError(422, "chunk index out of range", close=True)
        wanted = min(
            upload["chunk_size"], upload["total_size"] - index * upload["chunk_size"]
        )
        if length != wanted:
            raise ApiError(422, "incorrect chunk length", close=True)

        directory = self.chunk_path(upload_id, index).parent
        temp = directory / f"{index}.tmp.{secrets.token_hex(12)}"
        actual = hashlib.sha256()
        remaining = length
        try:
            with temp.open("xb") as out:
                while remaining:
                    block = source.read(min(IO_BLOCK, remaining))
                    if not block:
                        raise ApiError(400, "request body ended early", close=True)
                    out.write(block)
                    actual.update(block)
                    remaining -= len(block)
                out.flush()
                os.fsync(out.fileno())
            digest = actual.hexdigest()
            if digest != expected_hash:
                raise ApiError(422, "chunk checksum mismatch")

            final = self.chunk_path(upload_id, index)
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    """SELECT u.state,c.size,c.sha256
                       FROM uploads u LEFT JOIN chunks c
                         ON c.upload_id=u.id AND c.chunk_index=?
                       WHERE u.id=? AND u.tenant_id=?""",
                    (index, upload_id, tenant),
                ).fetchone()
                if current is None:
                    db.rollback()
                    raise ApiError(404, "upload not found")
                if current["size"] is not None:
                    same = current["size"] == length and current["sha256"] == digest
                    db.rollback()
                    if not same:
                        raise ApiError(409, "conflicting chunk replay")
                    return
                if current["state"] != "uploading":
                    db.rollback()
                    raise ApiError(409, "upload is not accepting chunks")
                os.replace(temp, final)
                fsync_dir(directory)
                db.execute(
                    "INSERT INTO chunks(upload_id,chunk_index,size,sha256) VALUES(?,?,?,?)",
                    (upload_id, index, length, digest),
                )
                db.commit()
        finally:
            unlink_if_present(temp)

    def _freeze_upload(
        self, tenant: str, upload_id: str, declared_hash: str, declared_size: int
    ) -> sqlite3.Row | dict:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM uploads WHERE id=? AND tenant_id=?",
                (upload_id, tenant),
            ).fetchone()
            if row is None:
                db.rollback()
                raise ApiError(404, "upload not found")
            if row["state"] == "finalized":
                artifact = db.execute(
                    "SELECT 1 FROM artifacts WHERE id=? AND tenant_id=?",
                    (row["artifact_id"], tenant),
                ).fetchone()
                if (
                    artifact is not None
                    and row["expected_sha256"] == declared_hash
                    and row["expected_size"] == declared_size
                ):
                    db.rollback()
                    return {"artifact_id": row["artifact_id"]}
                db.rollback()
                raise ApiError(409, "upload already finalized")
            if row["state"] == "finalizing":
                db.rollback()
                raise ApiError(409, "upload finalization in progress")
            if declared_size != row["total_size"]:
                db.rollback()
                raise ApiError(422, "declared size does not match upload")
            expected_count = (
                (row["total_size"] + row["chunk_size"] - 1) // row["chunk_size"]
            )
            stats = db.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM chunks WHERE upload_id=?",
                (upload_id,),
            ).fetchone()
            if stats["n"] != expected_count or stats["bytes"] != row["total_size"]:
                db.rollback()
                raise ApiError(422, "upload is incomplete")
            db.execute(
                """UPDATE uploads SET state='finalizing',expected_sha256=?,expected_size=?
                   WHERE id=?""",
                (declared_hash, declared_size, upload_id),
            )
            db.commit()
            return row

    def _unfreeze(self, upload_id: str) -> None:
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                """UPDATE uploads SET state='uploading',expected_sha256=NULL,
                                      expected_size=NULL
                   WHERE id=? AND state='finalizing'""",
                (upload_id,),
            )
            db.commit()

    def finalize(
        self, tenant: str, upload_id: str, declared_hash: str, declared_size: int
    ) -> str:
        frozen = self._freeze_upload(tenant, upload_id, declared_hash, declared_size)
        if isinstance(frozen, dict):
            return frozen["artifact_id"]

        target = self.blob_path(declared_hash)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Persist a newly-created hash-prefix directory before metadata can point
        # through it to a committed blob.
        fsync_dir(self.blob_root)
        temp = target.parent / f"{declared_hash}.tmp.{secrets.token_hex(12)}"
        digest = hashlib.sha256()
        written = 0
        try:
            with temp.open("xb") as out:
                count = (
                    (frozen["total_size"] + frozen["chunk_size"] - 1)
                    // frozen["chunk_size"]
                )
                for index in range(count):
                    with self.chunk_path(upload_id, index).open("rb") as chunk:
                        while True:
                            block = chunk.read(IO_BLOCK)
                            if not block:
                                break
                            out.write(block)
                            digest.update(block)
                            written += len(block)
                out.flush()
                os.fsync(out.fileno())
            if written != declared_size or digest.hexdigest() != declared_hash:
                self._unfreeze(upload_id)
                raise ApiError(422, "artifact checksum or size mismatch")

            artifact_id = secrets.token_hex(16)
            # Every finalize performs the same streamed write, fsync, and atomic replace,
            # including a dedup hit. This both equalizes the public path and avoids a
            # check-then-use race with GC.
            with self.blob_locks.for_key(declared_hash):
                os.replace(temp, target)
                fsync_dir(target.parent)
                with self.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    state = db.execute(
                        "SELECT state FROM uploads WHERE id=? AND tenant_id=?",
                        (upload_id, tenant),
                    ).fetchone()
                    if state is None or state["state"] != "finalizing":
                        db.rollback()
                        raise RuntimeError("finalization state changed unexpectedly")
                    db.execute(
                        """INSERT INTO blobs(content_hash,size,refcount,validated,created_at)
                           VALUES(?,?,0,1,?)
                           ON CONFLICT(content_hash) DO UPDATE SET
                               size=excluded.size, validated=1,
                               created_at=excluded.created_at""",
                        (declared_hash, declared_size, time.time_ns()),
                    )
                    db.execute(
                        """INSERT INTO artifacts
                           (id,tenant_id,upload_id,name,size,sha256,created_at)
                           VALUES(?,?,?,?,?,?,?)""",
                        (
                            artifact_id,
                            tenant,
                            upload_id,
                            frozen["name"],
                            declared_size,
                            declared_hash,
                            time.time_ns(),
                        ),
                    )
                    db.execute(
                        "UPDATE blobs SET refcount=refcount+1 WHERE content_hash=?",
                        (declared_hash,),
                    )
                    db.execute(
                        """UPDATE uploads SET state='finalized',artifact_id=?
                           WHERE id=?""",
                        (artifact_id, upload_id),
                    )
                    db.commit()
            try:
                self.cleanup_chunk_files(upload_id)
            except OSError:
                # Committed artifacts do not depend on chunks. Startup recovery
                # retries this storage-only cleanup after a crash or I/O hiccup.
                pass
            return artifact_id
        except ApiError:
            raise
        except Exception:
            self._unfreeze(upload_id)
            raise
        finally:
            unlink_if_present(temp)

    def artifact_row(self, tenant: str, artifact_id: str) -> sqlite3.Row:
        with self.db() as db:
            row = db.execute(
                """SELECT a.* FROM artifacts a
                   WHERE a.id=? AND a.tenant_id=?""",
                (artifact_id, tenant),
            ).fetchone()
        if row is None:
            raise ApiError(404, "artifact not found")
        return row

    def open_verified_artifact(
        self, tenant: str, artifact_id: str
    ) -> tuple[sqlite3.Row, BinaryIO]:
        # Initial lookup obtains the hash used for lock selection. It is repeated
        # under the lock so deletion cannot create a lookup/open gap.
        row = self.artifact_row(tenant, artifact_id)
        digest = row["sha256"]
        with self.blob_locks.for_key(digest):
            row = self.artifact_row(tenant, artifact_id)
            try:
                f = self.blob_path(digest).open("rb")
            except FileNotFoundError:
                raise ApiError(500, "artifact bytes unavailable")
        try:
            actual = hashlib.sha256()
            size = 0
            while True:
                block = f.read(IO_BLOCK)
                if not block:
                    break
                actual.update(block)
                size += len(block)
            if size != row["size"] or actual.hexdigest() != digest:
                f.close()
                with self.db() as db:
                    db.execute(
                        "UPDATE blobs SET validated=0 WHERE content_hash=?", (digest,)
                    )
                raise ApiError(500, "artifact failed integrity verification")
            f.seek(0)
            return row, f
        except Exception:
            if not f.closed:
                f.close()
            raise

    def list_artifacts(self, tenant: str) -> list[dict]:
        with self.db() as db:
            rows = db.execute(
                """SELECT id,name,size,sha256 FROM artifacts
                   WHERE tenant_id=? ORDER BY created_at,id""",
                (tenant,),
            ).fetchall()
        return [
            {
                "artifact_id": r["id"],
                "name": r["name"],
                "size": r["size"],
                "sha256": r["sha256"],
            }
            for r in rows
        ]

    def delete_artifact(self, tenant: str, artifact_id: str) -> None:
        row = self.artifact_row(tenant, artifact_id)
        digest = row["sha256"]
        with self.blob_locks.for_key(digest):
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                current = db.execute(
                    "SELECT sha256 FROM artifacts WHERE id=? AND tenant_id=?",
                    (artifact_id, tenant),
                ).fetchone()
                if current is None:
                    db.rollback()
                    raise ApiError(404, "artifact not found")
                db.execute(
                    "DELETE FROM artifacts WHERE id=? AND tenant_id=?",
                    (artifact_id, tenant),
                )
                changed = db.execute(
                    """UPDATE blobs SET refcount=refcount-1
                       WHERE content_hash=? AND refcount>0""",
                    (digest,),
                ).rowcount
                if changed != 1:
                    db.rollback()
                    raise RuntimeError("blob refcount invariant violated")
                db.commit()

    def gc(self) -> dict:
        with self.db() as db:
            candidates = [
                (r["content_hash"], r["size"])
                for r in db.execute(
                    "SELECT content_hash,size FROM blobs WHERE refcount=0 ORDER BY content_hash"
                )
            ]
        collected = 0
        freed = 0
        # Small durable batches avoid 10k SQLite fsyncs while keeping the global
        # writer-lock interval short. Sorted hashes also cluster directory fsyncs.
        for offset in range(0, len(candidates), 64):
            batch = candidates[offset : offset + 64]
            locks = [self.blob_locks.for_key(digest) for digest, _ in batch]
            for lock in locks:
                lock.acquire()
            renamed: list[tuple[int, Path]] = []
            removed = 0
            try:
                with self.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    touched_dirs: set[Path] = set()
                    for digest, _candidate_size in batch:
                        row = db.execute(
                            "SELECT refcount,size FROM blobs WHERE content_hash=?",
                            (digest,),
                        ).fetchone()
                        if row is None or row["refcount"] != 0:
                            continue
                        trash = self.trash_root / f"{digest}.{secrets.token_hex(8)}.gc"
                        source = self.blob_path(digest)
                        try:
                            os.replace(source, trash)
                            renamed.append((row["size"], trash))
                            touched_dirs.add(source.parent)
                        except FileNotFoundError:
                            pass
                        db.execute(
                            "DELETE FROM blobs WHERE content_hash=? AND refcount=0",
                            (digest,),
                        )
                        removed += 1
                    for directory in touched_dirs:
                        fsync_dir(directory)
                    if renamed:
                        fsync_dir(self.trash_root)
                    db.commit()
                collected += removed
                for size, trash in renamed:
                    unlink_if_present(trash)
                    freed += size
                if renamed:
                    fsync_dir(self.trash_root)
            finally:
                for lock in reversed(locks):
                    lock.release()
        return {"scanned": len(candidates), "collected": collected, "bytes_freed": freed}

    def list_blobs(self) -> list[dict]:
        with self.db() as db:
            rows = db.execute(
                """SELECT content_hash,refcount,size,validated FROM blobs
                   ORDER BY content_hash"""
            ).fetchall()
        return [
            {
                "content_hash": r["content_hash"],
                "refcount": r["refcount"],
                "size": r["size"],
                "validated": bool(r["validated"]),
            }
            for r in rows
        ]

    def validate(self) -> dict:
        if not self.validation_lock.acquire(blocking=False):
            raise ApiError(409, "validation already running")
        try:
            with self.db() as db:
                snapshots = db.execute(
                    "SELECT content_hash,size,created_at FROM blobs"
                ).fetchall()
            results: list[tuple[int, str, int]] = []
            for snapshot in snapshots:
                digest = snapshot["content_hash"]
                # Hold the keyed lock only through open. An open POSIX fd survives a
                # GC unlink and is an immutable snapshot across atomic replacement.
                with self.blob_locks.for_key(digest):
                    try:
                        f = self.blob_path(digest).open("rb")
                    except FileNotFoundError:
                        f = None
                good = False
                if f is not None:
                    opened_stat = os.fstat(f.fileno())
                    with f:
                        actual = hashlib.sha256()
                        size = 0
                        while True:
                            block = f.read(IO_BLOCK)
                            if not block:
                                break
                            actual.update(block)
                            size += len(block)
                    good = snapshot["size"] == size and actual.hexdigest() == digest
                    # A concurrent finalize can atomically replace an identical hash.
                    # If it did, its own verification is newer than this snapshot.
                    with self.blob_locks.for_key(digest):
                        try:
                            current_stat = self.blob_path(digest).stat()
                            replaced = (
                                current_stat.st_dev != opened_stat.st_dev
                                or current_stat.st_ino != opened_stat.st_ino
                            )
                        except FileNotFoundError:
                            replaced = False
                        if replaced:
                            good = True
                results.append((1 if good else 0, digest, snapshot["created_at"]))
            # One brief metadata commit rather than one durable commit per blob.
            with self.db() as db:
                db.execute("BEGIN IMMEDIATE")
                db.executemany(
                    """UPDATE blobs SET validated=?
                       WHERE content_hash=? AND created_at=?""",
                    results,
                )
                db.commit()
            valid = sum(flag for flag, _, _ in results)
            return {
                "scanned": len(snapshots),
                "valid": valid,
                "invalid": len(snapshots) - valid,
            }
        finally:
            self.validation_lock.release()


class VaultServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], store: Store):
        super().__init__(address, Handler)
        self.store = store


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VaultDrop/1"

    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _token(self) -> str | None:
        value = self.headers.get("Authorization", "")
        if not value.startswith("Bearer "):
            return None
        return value[7:]

    def tenant(self) -> str:
        token = self._token()
        tenant = self.store.tenant_tokens.get(token or "")
        if tenant is None:
            raise ApiError(401, "unauthorized", close=True)
        return tenant

    def admin(self) -> None:
        if self._token() != self.store.admin_token:
            raise ApiError(403, "admin authorization required", close=True)

    def json_body(self) -> dict:
        if self.headers.get("Transfer-Encoding") is not None:
            raise ApiError(400, "Transfer-Encoding is not supported", close=True)
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ApiError(411, "Content-Length required", close=True)
        try:
            length = int(raw_length)
        except ValueError:
            raise ApiError(400, "invalid Content-Length", close=True)
        if length < 0 or length > MAX_JSON_SIZE:
            raise ApiError(413, "JSON body too large", close=True)
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ApiError(400, "request body ended early", close=True)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "invalid JSON")
        if not isinstance(value, dict):
            raise ApiError(400, "JSON body must be an object")
        return value

    def send_json(self, status: int, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self, method: str) -> None:
        path = urlsplit(self.path).path
        parts = [p for p in path.split("/") if p]

        if method == "GET" and path == "/health":
            self.send_json(200, {"status": "ok"})
            return

        if method == "POST" and path == "/uploads":
            tenant = self.tenant()
            body = self.json_body()
            name, total, chunk = body.get("name"), body.get("total_size"), body.get("chunk_size")
            if not isinstance(name, str) or not name or len(name) > 1024:
                raise ApiError(422, "name must be 1-1024 characters")
            if isinstance(total, bool) or not isinstance(total, int) or not 0 <= total <= MAX_ARTIFACT_SIZE:
                raise ApiError(422, "total_size must be between 0 and 10 GiB")
            if isinstance(chunk, bool) or not isinstance(chunk, int) or not 1 <= chunk <= MAX_CHUNK_SIZE:
                raise ApiError(422, "chunk_size must be between 1 byte and 32 MiB")
            upload_id = self.store.create_upload(tenant, name, total, chunk)
            self.send_json(201, {"upload_id": upload_id, "received": []})
            return

        if len(parts) == 2 and parts[0] == "uploads" and ID_RE.fullmatch(parts[1]):
            tenant = self.tenant()
            if method == "GET":
                self.send_json(200, self.store.upload_status(tenant, parts[1]))
                return

        if (
            method == "PUT"
            and len(parts) == 4
            and parts[0] == "uploads"
            and ID_RE.fullmatch(parts[1])
            and parts[2] == "chunks"
        ):
            tenant = self.tenant()
            if self.headers.get("Transfer-Encoding") is not None:
                raise ApiError(400, "Transfer-Encoding is not supported", close=True)
            try:
                index = int(parts[3])
            except ValueError:
                raise ApiError(404, "not found", close=True)
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise ApiError(411, "Content-Length required", close=True)
            try:
                length = int(raw_length)
            except ValueError:
                raise ApiError(400, "invalid Content-Length", close=True)
            if length < 0 or length > MAX_CHUNK_SIZE:
                raise ApiError(413, "chunk exceeds 32 MiB", close=True)
            checksum = self.headers.get("X-Chunk-SHA256", "")
            if not INPUT_HASH_RE.fullmatch(checksum):
                raise ApiError(400, "invalid X-Chunk-SHA256", close=True)
            self.store.put_chunk(tenant, parts[1], index, checksum.lower(), self.rfile, length)
            self.send_json(200, {"received": index})
            return

        if (
            method == "POST"
            and len(parts) == 3
            and parts[0] == "uploads"
            and ID_RE.fullmatch(parts[1])
            and parts[2] == "finalize"
        ):
            tenant = self.tenant()
            body = self.json_body()
            digest, size = body.get("sha256"), body.get("size")
            if not isinstance(digest, str) or not INPUT_HASH_RE.fullmatch(digest):
                raise ApiError(422, "sha256 must be hexadecimal")
            if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_ARTIFACT_SIZE:
                raise ApiError(422, "invalid size")
            artifact_id = self.store.finalize(tenant, parts[1], digest.lower(), size)
            self.send_json(200, {"artifact_id": artifact_id})
            return

        if method == "GET" and path == "/artifacts":
            self.send_json(200, self.store.list_artifacts(self.tenant()))
            return

        if len(parts) == 2 and parts[0] == "artifacts" and ID_RE.fullmatch(parts[1]):
            tenant = self.tenant()
            if method == "GET":
                row, f = self.store.open_verified_artifact(tenant, parts[1])
                with f:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(row["size"]))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    shutil.copyfileobj(f, self.wfile, length=IO_BLOCK)
                return
            if method == "DELETE":
                self.store.delete_artifact(tenant, parts[1])
                self.send_json(200, {"deleted": True})
                return

        if method == "POST" and path == "/admin/gc":
            self.admin()
            self.send_json(200, self.store.gc())
            return
        if method == "GET" and path == "/admin/blobs":
            self.admin()
            self.send_json(200, self.store.list_blobs())
            return
        if method == "GET" and path == "/admin/validate":
            self.admin()
            self.send_json(200, self.store.validate())
            return
        raise ApiError(404, "not found")

    def _handle(self, method: str) -> None:
        try:
            self.dispatch(method)
        except ApiError as exc:
            if exc.close:
                self.close_connection = True
            self.send_json(exc.status, {"error": exc.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            self.log_error("unhandled error: %r", exc)
            try:
                self.send_json(500, {"error": "internal server error"})
            except (BrokenPipeError, ConnectionResetError):
                pass

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


def usage() -> None:
    print("usage: vaultdrop {serve|migrate}", file=sys.stderr)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"serve", "migrate"}:
        usage()
        return 2
    raw_root = os.environ.get("VAULTDROP_STATE_DIR")
    if not raw_root:
        print("VAULTDROP_STATE_DIR is required", file=sys.stderr)
        return 2
    store = Store(Path(raw_root).resolve())
    try:
        store.prepare(load_auth=argv[1] == "serve")
    except Exception as exc:
        print(f"startup failed: {exc}", file=sys.stderr)
        return 1
    if argv[1] == "migrate":
        return 0
    try:
        port = int(os.environ.get("PORT", ""))
        if not 1 <= port <= 65535:
            raise ValueError
    except ValueError:
        print("PORT must be an integer from 1 to 65535", file=sys.stderr)
        return 2
    server = VaultServer(("0.0.0.0", port), store)

    def stop(_signum: int, _frame: object) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
