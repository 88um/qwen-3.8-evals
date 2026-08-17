# DECISIONS.md

## Architecture

**Language:** Python 3 with standard library only. SQLite for metadata (WAL mode, `synchronous=FULL`), filesystem for blob bytes. `ThreadingHTTPServer` provides concurrency — one OS thread per request.

**Storage layout** under `$VAULTDROP_STATE_DIR`:
- `vaultdrop.db` — SQLite database
- `blobs/{hash[0:2]}/{hash}` — content-addressed blob files, sharded by 2-char prefix (256 buckets, ~40 files each at 10K blobs)
- `chunks/{upload_id}/{index}` — temporary chunk files during upload
- `tmp/` — assembly scratch space, cleaned on startup

**ID format:** Upload IDs are `up_<uuid4hex>`, artifact IDs are `art_<uuid4hex>`. Not sequential, not guessable.

## Scale Envelope

| Parameter | Limit | Mechanism |
|---|---|---|
| Max artifact size | 10 GiB | Enforced at `POST /uploads` via `total_size` check |
| Max chunk body | 64 MiB | Enforced at `POST /uploads` via `chunk_size` check and at `PUT` via `Content-Length` check |
| Memory bound | 64 KB buffer | All I/O (chunk write, assembly, download, validation) streams through a fixed 64 KB buffer; no full-artifact buffering anywhere |
| Metadata at 10K | Indexed queries | `idx_artifacts_tenant`, `idx_artifacts_content`, `idx_chunks_upload` indexes; blob directory sharded into 256 buckets |
| No global stalls | Per-hash blob locks, no global lock | Validation iterates blobs without holding any write lock; GC locks one blob hash at a time; concurrent finalizes of different content proceed in parallel |

## Claims Register

| # | Invariant | Mechanism | How to verify |
|---|---|---|---|
| 1 | **No cross-tenant access** | Every query filters by `tenant_id`. Wrong tenant → 404 (not 403). Artifact IDs are UUIDs. Admin endpoints never expose tenant identity. | `test_isolation.py`: 12 tests covering download, delete, upload status, chunk upload, finalize, listing, ID probing, admin endpoint separation |
| 2 | **No corrupt or partial reads** | Artifacts table is populated only after `commit_finalize` succeeds — which requires assembled blob to match the client-declared SHA-256 and size. Download streams from the verified blob file. The artifact record is never written without the blob existing. | `test_basic.py::test_finalize_sha_mismatch`, `test_finalize_size_mismatch` |
| 3 | **No lost data under crash** | **Durability ordering:** blob file is written and `fsync`'d (file + directory) *before* the SQLite transaction that commits the artifact. SQLite runs with `PRAGMA synchronous=FULL` (WAL synced on commit). On crash: if the DB committed, the blob file is durable; if not, recovery resets `finalizing` → `uploading` and the upload is retryable. Chunk files are written to temp, `fsync`'d, then renamed before the DB insert. | `test_crash.py`: crash during upload (resumable), crash after finalize (artifact survives), crash during finalize (state consistent, retryable), crash during GC (referenced data intact), multiple sequential crashes |
| 4 | **Finalize-vs-GC correctness** | Both finalize and GC acquire an in-process per-hash `blob_lock` before touching the blob record or file. Under this lock: finalize writes/verifies the blob file then commits a transaction that inserts/increments the blob refcount; GC re-checks `refcount=0` inside an `IMMEDIATE` transaction then deletes the record, then deletes the file. Because the lock serializes per-hash, there is no window where GC deletes a file that finalize is about to reference. Different hashes proceed concurrently (no global lock). | `test_concurrency.py::test_finalize_vs_gc_race` |
| 5 | **Concurrent chunk correctness** | Per-(upload, index) in-process lock serializes the check-then-write of each chunk slot. Inside the lock: check DB for existing chunk → if exists, compare hash (200 for match, 409 for mismatch) → if not, rename temp file and INSERT. SQLite `IMMEDIATE` transactions serialize the DB writes. | `test_concurrency.py::test_concurrent_chunk_uploads`, `test_concurrent_duplicate_chunks` |
| 6 | **Concurrent finalize — at most one artifact** | `UPDATE uploads SET state='finalizing' WHERE state='uploading'` inside an `IMMEDIATE` transaction. Exactly one caller matches the WHERE clause. Second caller sees `finalizing` → 409, or `finalized` → returns existing artifact ID. | `test_basic.py::test_concurrent_finalize_same_upload` |
| 7 | **Chunk conflict detection** | Primary key `(upload_id, chunk_index)` enforces uniqueness. First writer wins. Replay with same bytes → 200; different bytes → 409. | `test_basic.py::test_chunk_idempotent_replay`, `test_chunk_conflict_replay` |
| 8 | **Late chunks rejected** | Chunk upload checks `upload.state == 'uploading'`; once finalize sets state to `finalizing` or `finalized`, subsequent chunk PUTs return 409. | `test_basic.py::test_chunk_after_finalize` |
| 9 | **Cross-tenant dedup invisible** | Each tenant gets its own artifact record with its own ID. The shared blob is transparent — no timing side-channel (dedup happens at file-rename level, same speed as new write), no enumeration (admin blobs show refcount but not tenant IDs). | `test_isolation.py::test_cross_tenant_dedup_invisible`, `test_dedup_delete_one_tenant_other_survives` |
| 10 | **GC safety — referenced bytes never collected** | GC `DELETE FROM blobs WHERE content_hash=? AND refcount<=0` re-checks inside an `IMMEDIATE` transaction. If a concurrent finalize incremented refcount, the DELETE matches zero rows and skips. After deletion, GC re-checks `blob_exists()` before unlinking the file (guards against a finalize that re-inserted the record between delete and unlink). | `test_concurrency.py::test_finalize_vs_gc_race` |
| 11 | **Recovery cleans up incomplete state** | On startup: `UPDATE uploads SET state='uploading' WHERE state='finalizing'` resets stuck finalizations. Temp files in `tmp/` are cleaned. Orphan chunk directories are harmless (GC or manual cleanup). | `test_crash.py::test_crash_during_finalize_recovery` |

## Honest Disclosures

- **Streaming buffer:** Fixed 64 KB. Under adversarial measurement, RSS will include Python interpreter overhead (~30-40 MB) plus per-thread stack (~8 MB default per thread), but no per-artifact memory scaling.
- **Concurrency model:** Python's GIL means CPU-bound work (SHA-256 hashing during assembly) does not truly parallelize across threads. I/O operations release the GIL. For a CPU-bound validation pass over many large blobs, throughput is limited to one hash at a time. This does not violate the "no global stalls" requirement because validation does not hold any locks during hashing — it only touches the DB briefly per blob to update the `validated` flag.
- **In-process locks vs crash:** The per-hash blob locks and per-chunk locks are in-process `threading.Lock` objects. They protect against concurrent access within one `serve` invocation. After a crash and restart, all locks are fresh — this is correct because there are no concurrent requests during startup recovery.
- **GC file deletion window:** Between GC's DB deletion and file deletion, a concurrent finalize could theoretically re-insert a blob record for the same hash. GC handles this with a post-delete existence check (`blob_exists()` in the DB) before unlinking the file. This check is inside the blob_lock, preventing the TOCTOU race.
