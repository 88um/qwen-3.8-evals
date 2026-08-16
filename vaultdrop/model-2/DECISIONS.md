# DECISIONS.md — VaultDrop Architecture

## Technology choices

**Go + SQLite (WAL) + filesystem blobs.** Single binary, no external dependencies
beyond the Go toolchain. Pure-Go SQLite driver (`modernc.org/sqlite`) for maximum
portability. Single DB connection (`SetMaxOpenConns(1)`) avoids per-connection
PRAGMA drift and eliminates SQLite lock contention — write serialization is
inherent to SQLite anyway.

## Data model

**SQLite tables:** `uploads`, `chunks`, `blobs`, `artifacts`.

- `blobs` is keyed by `content_hash` (SHA-256 of full artifact bytes) with a
  `refcount` column. Dedup is implicit: two tenants storing identical bytes share
  one blob entry with `refcount=2`.
- `artifacts` ties a tenant-scoped ID to a `content_hash`. Tenant isolation comes
  from filtering every artifact query by `tenant_id` and returning 404 (never 403)
  for mismatches — existence itself is private.
- `chunks` records received chunk metadata per upload. The chunk *file* is the
  source of truth for data; the DB row is the source of truth for "received."

**Filesystem layout:**

```
$VAULTDROP_STATE_DIR/
  vaultdrop.db          SQLite database (WAL mode)
  chunks/{upload_id}/   per-upload chunk files by index
  blobs/{hash[0:2]}/    content-addressed blob files (2-char prefix dirs)
```

## Durability ordering

**Finalize protocol** (the critical path for promise 3):

1. Assemble all chunks into a temp file in `blobs/`, computing SHA-256.
2. Verify hash and size match the client's claim (422 on mismatch).
3. Acquire per-hash mutex (`blobLocks[contentHash]`).
4. Atomic rename temp → `blobs/{prefix}/{hash}`, fsync file and parent directory.
5. Single `BEGIN IMMEDIATE` transaction:
   - Re-check upload state is `uploading` (guards concurrent finalize).
   - `INSERT INTO blobs ... ON CONFLICT DO UPDATE SET refcount = refcount + 1`.
   - `INSERT INTO artifacts`.
   - `UPDATE uploads SET state='finalized', artifact_id=?`.
   - `COMMIT` (SQLite fsyncs the WAL with `synchronous=FULL`).
6. Release per-hash mutex.
7. Return 200 with `artifact_id`.

**Why this ordering is crash-safe:** The blob file is durable on disk (step 4)
before the metadata commit (step 5). If SIGKILL arrives between 4 and 5, the blob
file is an orphan (harmless, cleaned by future GC) and the upload is still
`uploading` — the client retries finalize. If SIGKILL arrives after 5, both the
blob and the DB are durable. A 200 is only returned after step 5's COMMIT, so
promise 3 holds: a 200 on finalize survives `kill -9`.

**Crash recovery at startup:** any upload stuck in `finalizing` state (a
transitional state within the DB transaction) is reset to `uploading`.

## Finalize-vs-GC coordination

This is the hardest invariant (promise 4). The race: GC finds a blob with
refcount ≤ 0 and deletes it; concurrently, a finalize is about to create a new
artifact referencing the same content hash.

**Protocol:**

- **GC** (in `POST /admin/gc`):
  1. Single `BEGIN IMMEDIATE` transaction: find all blobs with `refcount ≤ 0`,
     `DELETE` their records, `COMMIT`. Returns the list of hashes.
  2. For each hash: acquire `blobLocks[hash]`, re-check whether a blob record
     exists in the DB (a concurrent finalize may have re-created it). Only if no
     record exists, delete the blob file. Release lock.

- **Finalize** (steps 3–6 above): holds `blobLocks[contentHash]` while writing the
  blob file and committing the DB transaction.

**Why this is correct:** The per-hash mutex serializes finalize's file-write + DB
commit against GC's re-check + file-delete for the same content hash. If finalize
runs first, GC's re-check sees refcount > 0 and skips deletion. If GC runs first
and deletes the file, finalize writes a fresh blob file from the assembled chunks.
They cannot interleave because the mutex prevents it.

## Concurrent finalize

Two clients calling `POST /uploads/{id}/finalize` simultaneously: the DB
transaction (step 5) uses `UPDATE uploads SET state='finalizing' WHERE id=? AND
state='uploading'` with `rows_affected` check. Exactly one succeeds; the other
sees `state='finalized'`, reads the existing `artifact_id`, and returns it. At most
one artifact is created; both callers get a 200 with the same `artifact_id`.

## Chunk upload concurrency

Chunks arrive concurrently, duplicated, and out of order. The handler:

1. Reads the body while streaming to a temp file, computing SHA-256.
2. Verifies against the `X-Chunk-SHA256` header (400 on mismatch).
3. If chunk already exists in DB with same hash → 200 (idempotent).
4. If chunk exists with different hash → 409 (conflict).
5. Otherwise: atomic rename temp file to `chunks/{upload_id}/{index}`, then
   `INSERT INTO chunks ... ON CONFLICT DO NOTHING`.
6. If the INSERT was a no-op (concurrent insert won), compare hashes.

Atomic file rename + `ON CONFLICT DO NOTHING` means concurrent same-content
uploads are safe (idempotent) and different-content uploads are detected (409).

## Tenant isolation

- Every query that touches artifacts or uploads filters by `tenant_id`.
- Cross-tenant access returns 404 (not 403) — existence is private.
- Dedup is invisible: two tenants storing identical bytes each have distinct
  `artifact_id`s; neither can observe the other's existence.
- Admin endpoints (`/admin/blobs`) expose only `refcount` and `content_hash` —
  never tenant IDs.

---

## Claims register

| # | Invariant | Mechanism | How to check |
|---|-----------|-----------|-------------|
| 1 | No cross-tenant access | All DB queries filter by `tenant_id`; mismatches return 404 | `TestCrossTenantArtifactAccess`, `TestCrossTenantUploadProbing` — access t1's artifact/upload with t2's token, verify 404 |
| 2 | No corrupt or partial reads | Finalize verifies assembled SHA-256 + size against client claim; only returns artifact_id after DB commit; download streams from content-addressed blob | `TestFullUploadDownload` — round-trip verify; `TestFinalizeSHA256Mismatch` — 422 on wrong hash |
| 3 | No lost bytes under crash | Blob file fsync'd + dir fsync'd before DB commit; DB uses `synchronous=FULL` WAL; 200 only after COMMIT | `TestCrashAfterFinalizeRestart` — SIGKILL after finalize, restart, verify artifact survives |
| 4 | GC never collects referenced bytes | Per-hash mutex serializes finalize and GC; GC re-checks blob existence after acquiring lock before deleting file | `TestFinalizeVsGC` — delete artifact, prepare concurrent upload of same content, GC, then finalize succeeds |
| 5 | Chunk idempotency | DB `ON CONFLICT DO NOTHING` + hash comparison; atomic file writes via temp+rename | `TestChunkIdempotent`, `TestConcurrentDuplicateChunks` |
| 6 | Chunk conflict detection | Compare stored SHA-256 against incoming; 409 on mismatch | `TestChunkConflict` |
| 7 | At most one artifact per finalize | `UPDATE ... WHERE state='uploading'` with `rows_affected` check inside `BEGIN IMMEDIATE` | `TestConcurrentFinalize` — 5 concurrent finalizes, all get same artifact_id |
| 8 | Late chunk rejected | Chunk handler checks `upload.State == "uploading"` | `TestChunkAfterFinalize` |
| 9 | Dedup invisible to tenants | Separate artifact IDs; cross-tenant download returns 404; admin sees refcount only | `TestCrossTenantDedup` — same content, different artifact IDs, one blob with refcount=2 |
| 10 | Crash during chunk write | Atomic temp+fsync+rename; partial chunk never treated as complete | `TestCrashDuringChunkWrite` — SIGKILL, restart, upload resumable |
| 11 | Resumable upload after crash | Upload state persists in SQLite; chunks survive on disk | `TestCrashDuringChunkWrite` — after restart, received chunks intact, finalize succeeds |
| 12 | GC safe under crash | GC deletes DB records in one transaction, then deletes files; if SIGKILL between: orphan files remain (harmless), no referenced data lost | Structural: GC transaction is atomic; file deletion is idempotent |
| 13 | Validation detects corruption | `GET /admin/validate` re-hashes every blob file, flags mismatches | `TestValidation` |

### Not implemented / best-effort

- **Upload expiry/cleanup:** abandoned uploads (never finalized) accumulate chunks
  on disk indefinitely. A production system would expire them after a timeout.
- **Orphan blob file cleanup:** if SIGKILL occurs between blob file write and DB
  commit, or between GC DB delete and file delete, orphan files remain. They waste
  space but do not affect correctness. Validation can detect them.
- **Concurrent GC calls:** two simultaneous `POST /admin/gc` calls are safe (both
  use `BEGIN IMMEDIATE`, so they serialize) but the second may find nothing to
  collect.
