# VaultDrop — DECISIONS.md

Architecture, limits, and the claims register. Every row states the invariant, the
exact mechanism that enforces it, and how a reviewer checks it. The two mechanisms
the review hits hardest — durability ordering and finalize-vs-GC coordination —
are stated explicitly below the register.

## Architecture (summary)

Single-process Python (standard library only). SQLite in WAL mode holds metadata
under `$VAULTDROP_STATE_DIR/vaultdrop.db`; the filesystem holds bytes:
`blobs/<sha256>` (content-addressed, immutable), `chunks/<upload>/<i>` (staged
upload chunks), `tmp/` (durable-write staging). One worker thread per HTTP
connection (`ThreadingHTTPServer`). Each operation opens its own SQLite connection
(clean file-descriptor lifecycle under load) with explicit transaction control
(`isolation_level=None`). Two in-process locks, acquired in a strict order
(per-upload first, then GC), coordinate concurrency. Every durable byte goes
through `fs`: stage to temp → fsync file → atomic rename into place → fsync dir, so
a file appears at its final path fully written or not at all.

## Limits (enforced at the HTTP boundary)

- `MAX_CHUNK_SIZE` = 16 MiB per PUT body → `413`
- `MAX_ARTIFACT_SIZE` = 64 MiB total → `413`
- `MAX_NAME_LEN` = 512 → `422`

## Claims register

| # | Invariant (promise) | Exact mechanism | Reviewer check |
|---|---|---|---|
| 1 | No cross-tenant read / list / confirm-existence | Every tenant lookup carries `AND tenant_id=?` in the SQL predicate (`uploads._get_upload_row`, `get_artifact`, `list_artifacts`, `delete_artifact`); a foreign or unknown id is indistinguishable from a missing one ⇒ `404` | Request another tenant's `artifact_id`/`upload_id` ⇒ `404`, byte-identical to a random unknown id |
| 2 | Admin surface unreachable with a tenant token | `httpd._route` admin branch requires role `admin`, else `403` | Call `/admin/*` with a tenant token ⇒ `403` |
| 3 | Cross-tenant dedup invisible & inaccessible | Content addressing lives only in `blobs` (keyed by `content_hash`, no tenant column); each tenant's artifact is its own `artifacts` row pointing at the shared blob via `blob_hash` | `t1`,`t2` store identical bytes ⇒ distinct `artifact_id`s; `/admin/blobs` shows one blob, `refcount` 2; neither tenant can see the other |
| 4 | Dedup timing side-channel bounded (best-effort) | `store.acquire_blob` stages + fsyncs the **full** bytes on both the increment (dedup) and create paths, so the dominant cost is equalized; only residual metadata-op differences remain | Time finalize of novel vs already-stored content ⇒ small, non-deterministic delta, not a clean signal |
| 5 | No corrupt or partial reads | Read-boundary check in `httpd._download`: re-hash + length-check the stored bytes **before** sending any byte; mismatch/missing ⇒ `500` | Corrupt a file under `blobs/` and download ⇒ `500`, never partial bytes |
| 6 | No half-written chunk served | Chunk PUT durable-writes the file **before** inserting its row (`uploads.put_chunk`); a chunk counts as received iff its row exists | SIGKILL mid-PUT ⇒ restart ⇒ chunk absent (resumable) or complete; never partial |
| 7 | No lost / double-counted bytes under crash | Durability ordering (below) + startup `store.cleanup_orphans` reclaims files with no metadata row | SIGKILL mid-finalize / mid-GC ⇒ restart ⇒ every prior artifact readable; no orphan served |
| 8 | Finalize atomicity (≤ 1 artifact per upload) | Conditional `UPDATE uploads SET state='finalized' WHERE state='open'` + `rowcount` check inside the transaction; a loser returns the winner's `artifact_id` or a clear conflict | Two concurrent finalizes ⇒ exactly one `artifact_id`; both callers get a coherent answer |
| 9 | GC never collects referenced bytes | GC selects only `refcount=0` rows; `refcount` is incremented under `gc_lock` at finalize and decremented at delete; row-deletes are committed before file-unlinks | Delete one of two cross-tenant copies ⇒ GC leaves the blob (`refcount` 1); the surviving artifact stays readable |
| 10 | GC crash-safe | `gc_lock` held across the transaction **and** the file effects; crash pre-commit ⇒ rollback (nothing collected); post-commit ⇒ orphan files reclaimed at startup | SIGKILL mid-GC ⇒ restart ⇒ no referenced bytes missing, no partially-deleted blob served |
| 11 | Concurrent chunk writes safe | Per-upload lock serializes writes; first-write-wins; exact replay ⇒ `200`, conflicting bytes ⇒ `409` | Hammer one index with concurrent PUTs ⇒ one canonical byte-set; conflicts ⇒ `409` |
| 12 | Late chunk cannot mutate a finalized artifact | Finalize requires **all** chunk indices present before it proceeds; afterward every index already holds a canonical byte-set, so a late PUT is idempotent (`200`) or conflicting (`409`) under the per-upload lock — it cannot add or overwrite bytes | PUT after finalize ⇒ committed artifact bytes unchanged |

## Durability ordering (explicit)

A unit of durable work is: (1) stage the bytes to `tmp/` and fsync the file;
(2) atomically rename into the final path and fsync the directory; (3) commit the
metadata transaction. Steps (1)–(2) strictly precede (3). Consequence: whenever
metadata asserts "this exists," the bytes are already durable on disk. A `kill -9`
between (2) and (3) leaves durable bytes with no row — an orphan, reclaimed at
startup — never a row pointing at absent bytes. Applied to chunk PUT
(`uploads.put_chunk`) and to finalize's blob acquisition (`store.acquire_blob`).
GC is the reverse: rows are deleted and committed **first**, then files are
unlinked, so a crash leaves safe orphans, never a referenced-but-missing blob.

## Finalize-vs-GC coordination (explicit)

`locks.gc_lock()` is held across **both** the metadata transaction and the
filesystem effects in `store.acquire_blob` (finalize) and `store.gc_pass` (GC),
with strict lock ordering (per-upload lock first, then `gc_lock`). A GC's
decision-to-collect and a finalize's reference-claim therefore cannot interleave
between decision and effect: while a finalize holds `gc_lock` and increments a
refcount inside its transaction, a concurrent GC blocks on `gc_lock`; after the
finalize commits, GC observes `refcount > 0` and skips the blob. The reference
always wins.

## Known limitations (honest)

- **Dedup timing side-channel** (row 4) is bounded, not eliminated. A precisely
  instrumented adversary may still extract a weak signal from residual metadata-op
  differences.
- **Chunk retention:** a finalized upload's staged chunks are retained, not
  reclaimed, to keep the finalize crash-safety simple. This is a bounded
  per-upload space inefficiency (bytes temporarily present as both chunks and
  blob), not a correctness or isolation flaw. Candidate hardening: orphan-safe
  post-finalize chunk reclamation plus rejecting chunk PUTs on finalized uploads.
- **Validation** (`validated` flags, `GET /admin/validate`) is advisory and does
  not gate reads; the read-boundary check (row 5) is what gates reads.
- **Single-node** only; no replication or high availability.
