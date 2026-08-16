"""VaultDrop store: uploads, chunk writes, finalize/claim, GC, validation."""

import os
import sqlite3
import threading
import time
import uuid

from . import core
from .db import Database

_CLAIM_MAX_ATTEMPTS = 5


class Store:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.db = Database(state_dir)
        self._index_locks = {}
        self._index_locks_guard = threading.Lock()

    # ------------------------------------------------------------------ utils
    def _index_lock(self, upload_id, index):
        key = (upload_id, index)
        with self._index_locks_guard:
            lk = self._index_locks.get(key)
            if lk is None:
                lk = threading.Lock()
                self._index_locks[key] = lk
            return lk

    @staticmethod
    def _now():
        return int(time.time())

    # ---------------------------------------------------------------- uploads
    def create_upload(self, tenant_id, name, total_size, chunk_size):
        if not isinstance(name, str) or not name:
            raise core.ApiError(422, "name must be a non-empty string")
        if not isinstance(total_size, int) or isinstance(total_size, bool) \
                or total_size < 0 or total_size > core.MAX_ARTIFACT_SIZE:
            raise core.ApiError(422, "total_size out of range [0, %d]" % core.MAX_ARTIFACT_SIZE)
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) \
                or chunk_size <= 0 or chunk_size > core.MAX_CHUNK_SIZE:
            raise core.ApiError(422, "chunk_size out of range (0, %d]" % core.MAX_CHUNK_SIZE)
        upload_id = uuid.uuid4().hex
        self.db.txn(lambda c: c.execute(
            "INSERT INTO uploads (upload_id, tenant_id, name, total_size, chunk_size, state, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (upload_id, tenant_id, name, total_size, chunk_size, "active", self._now()),
        ))
        os.makedirs(core.chunks_dir(self.state_dir, upload_id), exist_ok=True)
        return upload_id

    def _get_upload(self, tenant_id, upload_id):
        return self.db.read_one(
            "SELECT upload_id, tenant_id, name, total_size, chunk_size, state "
            "FROM uploads WHERE upload_id=? AND tenant_id=?",
            (upload_id, tenant_id),
        )

    def describe_upload(self, tenant_id, upload_id):
        up = self._get_upload(tenant_id, upload_id)
        if up is None:
            raise core.ApiError(404, "not found")
        received = [r[0] for r in self.db.read(
            'SELECT "index" FROM chunks WHERE upload_id=? ORDER BY "index"', (upload_id,))]
        return {"upload_id": upload_id, "received": received, "state": up[5]}

    @staticmethod
    def _expected_indices(total_size, chunk_size):
        return (total_size + chunk_size - 1) // chunk_size

    # ----------------------------------------------------------------- chunks
    def put_chunk(self, tenant_id, upload_id, index, body_stream, declared_hash, clen):
        up = self._get_upload(tenant_id, upload_id)
        if up is None:
            raise core.ApiError(404, "not found")
        if up[5] == "finalized":
            raise core.ApiError(409, "upload finalized")
        total_size, chunk_size = up[3], up[4]
        if not isinstance(index, int) or isinstance(index, bool) \
                or index < 0 or index >= self._expected_indices(total_size, chunk_size):
            raise core.ApiError(422, "chunk index out of range")
        expected_size = min(chunk_size, total_size - index * chunk_size)
        if not isinstance(declared_hash, str) or not core.valid_sha256_hex(declared_hash):
            raise core.ApiError(422, "bad X-Chunk-SHA256")

        # Stream the body to a temp file while hashing; enforce size caps.
        tmpd = core.chunk_tmp_dir(self.state_dir, upload_id)
        os.makedirs(tmpd, exist_ok=True)
        tmp_path = os.path.join(tmpd, "%d.%s" % (index, uuid.uuid4().hex))
        h = core._hasher()
        count = 0
        remaining = clen
        try:
            with open(tmp_path, "wb") as out:
                while remaining > 0:
                    b = body_stream.read(min(core.STREAM_BUF, remaining))
                    if not b:
                        break
                    remaining -= len(b)
                    count += len(b)
                    if count > core.MAX_CHUNK_SIZE:
                        raise core.ApiError(413, "chunk exceeds %d bytes" % core.MAX_CHUNK_SIZE)
                    if count > expected_size:
                        raise core.ApiError(422, "chunk size %d != expected %d" % (count, expected_size))
                    out.write(b)
                    h.update(b)
            if count != expected_size:
                raise core.ApiError(422, "chunk size %d != expected %d" % (count, expected_size))
            if h.hexdigest() != declared_hash.lower():
                raise core.ApiError(422, "chunk sha256 mismatch")
        except BaseException:
            core.unlink_quiet(tmp_path)
            raise

        # Serialize same-index writes; replays resolve against the committed row.
        with self._index_lock(upload_id, index):
            row = self.db.read_one(
                'SELECT content_hash FROM chunks WHERE upload_id=? AND "index"=?',
                (upload_id, index),
            )
            if row is not None:
                digest = h.hexdigest()
                if digest == row[0]:
                    core.unlink_quiet(tmp_path)
                    return True          # exact replay
                core.unlink_quiet(tmp_path)
                raise core.ApiError(409, "conflicting chunk at index %d" % index)
            final_path = core.chunk_path(self.state_dir, upload_id, index)
            core.atomic_place(tmp_path, final_path)
            self.db.txn(lambda c: c.execute(
                'INSERT INTO chunks (upload_id, "index", content_hash, size) VALUES (?,?,?,?)',
                (upload_id, index, h.hexdigest(), count),
            ))
        return True

    # --------------------------------------------------------------- finalize
    def finalize(self, tenant_id, upload_id, declared_sha, declared_size):
        up = self._get_upload(tenant_id, upload_id)
        if up is None:
            raise core.ApiError(404, "not found")
        total_size, chunk_size, state = up[3], up[4], up[5]
        if not isinstance(declared_size, int) or isinstance(declared_size, bool) \
                or declared_size != total_size:
            raise core.ApiError(422, "size does not match upload total_size")
        if not isinstance(declared_sha, str) or not core.valid_sha256_hex(declared_sha):
            raise core.ApiError(422, "bad sha256")

        if state == "finalized":
            row = self.db.read_one(
                "SELECT artifact_id FROM artifacts WHERE upload_id=?", (upload_id,))
            if row is not None:
                return row[0]
            # artifact was deleted afterwards; chunks are retained, so the
            # upload may be finalized again (new artifact, same bytes).

        n = self._expected_indices(total_size, chunk_size)
        present = self.db.read_one(
            "SELECT COUNT(*) FROM chunks WHERE upload_id=?", (upload_id,))[0]
        if present != n:
            raise core.ApiError(422, "incomplete upload: %d/%d chunks" % (present, n))

        # Assemble: stream chunks in order into a temp file, hashing en route.
        # Done unconditionally (dedup hit or miss) so finalize latency is
        # independent of whether the bytes are already stored (isolation).
        tmpd = core.assemble_tmp_dir(self.state_dir)
        tmp_path = os.path.join(tmpd, "assemble.%s" % uuid.uuid4().hex)
        try:
            with open(tmp_path, "wb") as sink:
                chunk_paths = [core.chunk_path(self.state_dir, upload_id, i) for i in range(n)]
                digest, _ = core.streaming_hash_files(chunk_paths, sink=sink,
                                                       expected_size=total_size)
            if digest != declared_sha.lower():
                raise core.ApiError(422, "artifact sha256 mismatch")
            return self._claim(tenant_id, up, declared_sha.lower(), tmp_path)
        finally:
            if tmp_path is not None:
                core.unlink_quiet(tmp_path)

    def _claim(self, tenant_id, up, sha, tmp_path):
        """Attach verified bytes (tmp_path) to the blob store and commit the
        artifact. Resolves the finalize-vs-GC race via the blob state machine
        (active/pending/deleted) under SQLite write-transaction serialization.
        """
        upload_id, total_size, name = up[0], up[3], up[2]
        artifact_id = uuid.uuid4().hex
        blob_final = core.blob_path(self.state_dir, sha)

        for _attempt in range(_CLAIM_MAX_ATTEMPTS):
            state = self.db.read_one(
                "SELECT state FROM blobs WHERE content_hash=?", (sha,))
            state = state[0] if state else None

            if state is None:
                if tmp_path is not None:
                    os.makedirs(os.path.dirname(blob_final), exist_ok=True)
                    core.atomic_place(tmp_path, blob_final)
                    tmp_path = None
                try:
                    self.db.txn(self._claim_insert(sha, total_size, blob_final,
                                                    tenant_id, upload_id, name,
                                                    artifact_id))
                    return artifact_id
                except sqlite3.IntegrityError:
                    continue          # raced another claimant; retry below

            if state in ("active", "pending"):
                try:
                    changed = self.db.txn(self._claim_recover(sha, total_size,
                                                              tenant_id, upload_id,
                                                              name, artifact_id))
                except sqlite3.IntegrityError as e:
                    if "artifacts.upload_id" in str(e):
                        winner = self.db.read_one(
                            "SELECT artifact_id FROM artifacts WHERE upload_id=?",
                            (upload_id,))
                        if winner is not None:
                            return winner[0]   # coherent: both callers, one artifact
                    continue
                if changed:
                    return artifact_id
                continue              # raced to deleted; retry -> resurrect

            # state == 'deleted': GC tombstoned this content. Stored bytes are
            # gone; our assembled bytes are complete and verified, so resurrect.
            if tmp_path is not None:
                os.makedirs(os.path.dirname(blob_final), exist_ok=True)
                core.atomic_place(tmp_path, blob_final)
                tmp_path = None
            try:
                self.db.txn(self._claim_resurrect(sha, total_size, blob_final,
                                                  tenant_id, upload_id, name,
                                                  artifact_id))
                return artifact_id
            except sqlite3.IntegrityError as e:
                if "artifacts.upload_id" in str(e):
                    winner = self.db.read_one(
                        "SELECT artifact_id FROM artifacts WHERE upload_id=?",
                        (upload_id,))
                    if winner is not None:
                        return winner[0]
                continue

        raise core.ApiError(500, "claim contention; retry finalize")

    @staticmethod
    def _claim_insert(sha, size, path, tenant_id, upload_id, name, artifact_id):
        def fn(c):
            c.execute(
                "INSERT INTO blobs (content_hash, size, path, refcount, state, validated, created_at) "
                "VALUES (?,?,?,1,'active',1,?)",
                (sha, size, path, int(time.time())),
            )
            Store._insert_artifact(c, tenant_id, upload_id, name, size, sha, artifact_id)
        return fn

    @staticmethod
    def _insert_artifact(c, tenant_id, upload_id, name, size, sha, artifact_id):
        c.execute(
            "INSERT INTO artifacts (artifact_id, tenant_id, upload_id, name, size, sha256, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (artifact_id, tenant_id, upload_id, name, size, sha, int(time.time())),
        )
        c.execute("UPDATE uploads SET state='finalized' WHERE upload_id=?", (upload_id,))

    @staticmethod
    def _claim_recover(sha, size, tenant_id, upload_id, name, artifact_id):
        def fn(c):
            cur = c.execute(
                "UPDATE blobs SET state='active', refcount=refcount+1 "
                "WHERE content_hash=? AND state IN ('active', 'pending')",
                (sha,),
            )
            if cur.rowcount != 1:
                return False
            Store._insert_artifact(c, tenant_id, upload_id, name, size, sha, artifact_id)
            return True
        return fn

    @staticmethod
    def _claim_resurrect(sha, size, path, tenant_id, upload_id, name, artifact_id):
        def fn(c):
            c.execute("DELETE FROM blobs WHERE content_hash=? AND state='deleted'", (sha,))
            c.execute(
                "INSERT INTO blobs (content_hash, size, path, refcount, state, validated, created_at) "
                "VALUES (?,?,?,1,'active',1,?)",
                (sha, size, path, int(time.time())),
            )
            Store._insert_artifact(c, tenant_id, upload_id, name, size, sha, artifact_id)
        return fn

    # ---------------------------------------------------------------- artifacts
    def get_artifact(self, tenant_id, artifact_id):
        row = self.db.read_one(
            "SELECT artifact_id, tenant_id, name, size, sha256 FROM artifacts "
            "WHERE artifact_id=? AND tenant_id=?",
            (artifact_id, tenant_id),
        )
        if row is None:
            raise core.ApiError(404, "not found")
        return row

    def list_artifacts(self, tenant_id):
        return self.db.read(
            "SELECT artifact_id, name, size, sha256 FROM artifacts "
            "WHERE tenant_id=? ORDER BY created_at, artifact_id",
            (tenant_id,),
        )

    def delete_artifact(self, tenant_id, artifact_id):
        def fn(c):
            row = c.execute(
                "SELECT sha256 FROM artifacts WHERE artifact_id=? AND tenant_id=?",
                (artifact_id, tenant_id),
            ).fetchone()
            if row is None:
                return False
            c.execute("DELETE FROM artifacts WHERE artifact_id=?", (artifact_id,))
            c.execute("UPDATE blobs SET refcount=refcount-1 WHERE content_hash=?", (row[0],))
            return True
        return self.db.txn(fn)

    # ------------------------------------------------------------------ admin
    def list_blobs(self):
        return self.db.read(
            "SELECT content_hash, refcount, size, validated FROM blobs "
            "WHERE state != 'deleted' ORDER BY content_hash",
        )

    def gc_pass(self):
        """Synchronous garbage-collection pass.

        Candidates: blobs with refcount=0. Marking (active->pending) and the
        per-candidate deletion decision are separate write transactions, so a
        concurrent finalize's recovery update (pending->active) always wins or
        loses atomically; deletion additionally quarantines the bytes before
        unlinking and re-checks state, so a resurrect that lands between the
        tombstone commit and the unlink is restored (content-identical).
        """
        self.db.txn(lambda c: c.execute(
            "UPDATE blobs SET state='pending' WHERE refcount=0 AND state='active'"
        ))
        scanned = collected = bytes_freed = 0
        cur = self.db.conn().execute(
            "SELECT content_hash, size, path FROM blobs "
            "WHERE refcount=0 AND state='pending'")
        while True:
            row = cur.fetchone()
            if row is None:
                break
            sha, size, path = row
            scanned += 1
            changed = self.db.txn(lambda c: c.execute(
                "UPDATE blobs SET state='deleted' "
                "WHERE content_hash=? AND state='pending' AND refcount=0",
                (sha,),
            ))
            if not changed:
                continue
            qname = path + ".gcq." + uuid.uuid4().hex
            try:
                os.rename(path, qname)
            except FileNotFoundError:
                collected += 1          # bytes already gone (crash between steps)
                continue
            state = self.db.read_one("SELECT state FROM blobs WHERE content_hash=?", (sha,))
            state = state[0] if state else "deleted"
            if state == "active":
                os.replace(qname, path)  # resurrected: restore identical bytes
            else:
                core.unlink_quiet(qname)
                collected += 1
                bytes_freed += size
        return {"scanned": scanned, "collected": collected, "bytes_freed": bytes_freed}

    def validate_pass(self):
        """Synchronous validation: re-hash stored bytes, update the flag."""
        scanned = matched = mismatched = missing = 0
        cur = self.db.conn().execute(
            "SELECT content_hash, size, path, validated FROM blobs WHERE state != 'deleted'")
        while True:
            row = cur.fetchone()
            if row is None:
                break
            sha, size, path, _validated = row
            scanned += 1
            try:
                digest, got = core.streaming_hash_files([path])
            except FileNotFoundError:
                missing += 1
                self.db.txn(lambda c: c.execute(
                    "UPDATE blobs SET validated=0 WHERE content_hash=?", (sha,)))
                continue
            if got != size or digest != sha:
                mismatched += 1
                self.db.txn(lambda c: c.execute(
                    "UPDATE blobs SET validated=0 WHERE content_hash=?", (sha,)))
            else:
                matched += 1
                if _validated != 1:
                    self.db.txn(lambda c: c.execute(
                        "UPDATE blobs SET validated=1 WHERE content_hash=?", (sha,)))
        return {"scanned": scanned, "matched": matched,
                "mismatched": mismatched, "missing": missing}
