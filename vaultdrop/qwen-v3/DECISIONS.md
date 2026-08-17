# DECISIONS.md — VaultDrop (qwen-v3)

## Architecture

One process (`serve`), threaded HTTP (`ThreadingHTTPServer`), one SQLite
connection per thread (WAL, `synchronous=FULL`, autocommit with explicit
short `BEGIN IMMEDIATE` write transactions). All persistent state lives under
`$VAULTDROP_STATE_DIR`:

- `blobs/<hh>/<hash>` — content-addressed byte store, two-level fan-out, one
  file per distinct byte-content, shared across tenants for efficiency.
- `chunks/<upload_id>/<index>` — per-upload chunk store.
- `staging/` — transient finalize-assembly files.
- `vaultdrop.db` — `uploads`, `upload_chunks`, `artifacts`, `blobs`,
  `gc_passes`.

Data paths: a chunk PUT streams the body into a private tmp file while
hashing it, commits the chunk row, then links the tmp into place (a link never
overwrites). Finalize walks a per-upload state machine, streams chunks into a
staging file while hashing, and on digest match places the bytes (link or
refcount increment) and lands the artifact row, the upload-state transition,
and the blob accounting in **one** IMMEDIATE transaction. GC runs in three
phases: mark (short write txn) → unlink files (no lock held) → scoped delete
(short write txn). Validate is streaming re-hashes plus single-row flag
updates.

## Claims register

| # | Claim | Exact mechanism | Reviewer check |
|---|---|---|---|
| 1 | No cross-tenant access, ever | Every tenant-scoped lookup is one indexed equality predicate (`tenant_id=?`): `_read_upload`, `get_upload`, `get_artifact`, `list_artifacts`. Foreign/unknown ids return the same `404` as nonexistent ones (no existence leaks). Admin endpoints answer `404` to tenant tokens, `401` to unknown tokens. Dedup sharing is metadata-invisible: artifacts are per-tenant rows; the admin surface exposes only counts/hashes, never tenant identity. | Cross-tenant probes: foreign/unknown artifact & upload ids → identical `404`; listings contain only own rows; identical-content uploads yield distinct artifact ids with no timing/response distinguishability. |
| 2 | No corrupt or partial reads | An artifact row exists only after the atomic finalization commit (claim 5), and bytes are placed **before** the commit that references them (claim 6), so a committed row always has complete bytes. Downloads stream only committed rows. GC never deletes `active` rows (claim 7). | Crash-during-finalize test; digest equality asserted on every download in every test. |
| 3 | Crash during chunk write is resumable, no partial chunk counted | Placement order: stream body→private tmp (fsynced) → chunk-row commit (`BEGIN IMMEDIATE`) → `os.link` into place. Crash before commit leaves an orphan tmp (swept at startup); crash between commit and link leaves a row with no file — assembly then reports missing chunks and the client's retry heals it (the retry's link supplies the file). A link onto an existing destination never overwrites. | `test_crash_during_chunk_write`, `test_crash_chunk_self_heal` (deterministic row-without-file residue). |
| 4 | Conflicting chunk replay → 409, first bytes intact | `INSERT OR IGNORE` + readback comparison inside one IMMEDIATE transaction; mismatch → `409` and the tmp is unlinked. The existing file is never touched (links never overwrite). | `test_chunk_idempotent_and_conflict`. |
| 5 | Concurrent finalize ⇒ at most one artifact | Per-upload state machine `open→finalizing→finalized` arbitrated by a conditional UPDATE (`WHERE id=? AND state='open'`, rowcount check) inside one short IMMEDIATE transaction. Losers poll (bounded) and return the winner's `artifact_id`; a failed winner rolls back to `open` and losers get a retryable `409`. | `test_concurrent_finalize_single_artifact`, `test_concurrent_finalizes_distinct_content`. |
| 6 | Durability ordering (a `2xx` finalize survives `kill -9`) | (a) Bytes are fsynced before any metadata commit that references them: tmp fsync before the chunk-row commit; staging fsync before the finalization commit; blob link + directory fsync before the finalization commit. (b) SQLite WAL with `synchronous=FULL` fsyncs the WAL on every commit. (c) The finalization commit is ONE `BEGIN IMMEDIATE` transaction, ordered: blob accounting (insert or increment) → artifact row → upload-state transition. Crash before commit ⇒ at most orphan files, which nothing references (never served) and startup sweeps; crash after ⇒ the complete artifact is durable. | `test_crash_during_finalize`: after restart, either a complete artifact (digest-verified download) or none; upload re-finalizable. |
| 7 | Finalize vs GC: the reference wins | `blobs.state ∈ {active, collecting}`. GC phase 1 (short write txn) marks `state='active' AND refcount=0` rows as `collecting` tagged with a `gc_pass_id`. Refcount increments apply **only** to `active` rows (`UPDATE … WHERE hash=? AND state='active'`; rowcount≠1 ⇒ retry). A finalize that finds its digest `collecting` blocks until that pass completes (bounded wait, then retryable `500`). GC phase 2 unlinks the marked files with **no lock held**; phase 3 deletes only rows matching `gc_pass_id=? AND state='collecting' AND refcount=0`. Hence a concurrent finalize is either already counted (refcount>0 ⇒ never marked) or waits behind the pass; its bytes survive. | `test_finalize_vs_gc_race`: GC launched against refcount-0 content while a finalize references it; bytes survive, artifact completes. |
| 8 | Crash during GC: no referenced bytes lost, no partial delete served | Phase-3 deletion is scoped to the pass's own `gc_pass_id` plus re-verified state and refcount. Crash mid-pass leaves `collecting` rows; startup recovery: file still present ⇒ revert to `active` (refcount was 0 and increments were blocked while collecting, so it is collectable again); file gone ⇒ delete the row. Either way nothing referenced is missing and no half-deleted blob is servable (files are unlinked before rows are deleted). | `test_crash_during_gc`. |
| 9 | Per-request memory bounded (streaming, not buffering) | Every byte movement streams in fixed 1 MiB blocks (`IO_BLOCK`): PUT body→tmp, assembly→staging, download→wire, validate→hash. Peak per-request memory is a constant number of blocks plus small metadata, independent of artifact size. | `test_scale_streaming_memory`: 2 GiB round-trip with RSS polling; peak stays far under the 512 MiB ceiling. |
| 10 | No global stalls; concurrent finalizes overlap | There is no process-wide lock on any data path. Writers serialize only inside short IMMEDIATE transactions (SQLite busy-timeout 30 s; every write txn is milliseconds of metadata, never filesystem I/O). GC holds write locks only in its two short phases; validate never holds a write lock across a read (pure file reads + single-row updates). Finalizes of distinct uploads overlap fully; same-content races converge via claim 5/7. | `test_concurrent_finalizes_distinct_content` (wall-clock overlap), `test_scale_metadata_responsiveness` (finalize/download latency with validate running). |
| 11 | Metadata responsive at 10k blobs | Two-level blob fan-out (`blobs/<hh>/`, 256 dirs) keeps directories small; tenant-scoped and hash lookups hit indexes (`artifacts(tenant_id)`, `blobs(hash)`, `upload_chunks(upload_id, chunk_index)`); GC/validate are streaming scans with short transactions. | `test_scale_metadata_responsiveness`: 10k stored blobs — listings, lookups, GC, validate all complete in single-digit seconds. |
| 12 | Stated wire limits are enforced | `MAX_CHUNK_SIZE = 32 MiB`: enforced mid-stream on every PUT (`413` after a bounded drain) and at upload start (`chunk_size` > limit ⇒ `400`). `MAX_ARTIFACT_SIZE = 10 GiB`: enforced at `POST /uploads` (`413`). JSON bodies bounded at 1 MiB. All values at/above the scale-envelope minimums. | Over-limit probes: oversized chunk body ⇒ `413`; oversized `total_size` ⇒ `413`. |
| 13 | Keep-alive framing never desyncs | Request-body reads are bounded by remaining `Content-Length` (no blocking short-final-read). Early-reject paths drain the bounded unread remainder **before** responding; if the remainder exceeds a bounded cap they drain the cap and close the connection. | `test_late_chunk_cannot_mutate` (`409` mid-body, connection reusable); repeated PUTs over one connection. |

## Honest notes

- The tenant registry is read once at startup; token rotation requires a
  restart.
- Validate **flags** digest mismatches (`validated` column, counts in the
  response); it does not repair, and downloads do not re-verify digests on the
  wire (single-pass streaming). Integrity is established at finalize time and
  monitored thereafter via the admin surface.
- All coordination waits are bounded and end in retryable errors, never
  indefinite: finalize-losers 600 s, GC-wait 300 s, orphan-settle 2 s.
- Same-content finalize races: the second placer waits for the winner's blob
  row; if none appears within the settle window (winner crashed after linking,
  before committing), its orphan is replaced by the retry's own bytes —
  identical by construction, since both are addressed by the same hash.
