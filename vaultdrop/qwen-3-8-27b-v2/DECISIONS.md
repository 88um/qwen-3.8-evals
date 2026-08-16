# VaultDrop (model-1) — Design Decisions

## Architecture

One Python 3 process (`serve`), stdlib only. Metadata in SQLite (WAL,
`synchronous=FULL`, per-thread connections, explicit `BEGIN IMMEDIATE` write
transactions). Bytes on the filesystem under `$VAULTDROP_STATE_DIR`:

- `chunks/<upload>/<index>` — committed chunk bytes (atomic temp→rename)
- `blobs/<h[:2]>/<h>` — content-addressed byte store, sharded into 256 dirs
- `tmp/`, `chunks/<u>/.tmp/` — transient assembly/write temps

HTTP via stdlib `ThreadingHTTPServer` (one worker thread per connection).
Every multi-byte operation streams at 1 MiB granularity; nothing buffers a
whole chunk, artifact, or response.

**Blob state machine** (the core concurrency device): every blob row is
`active`, `pending`, or `deleted`. `active` = bytes present, refcount ≥ 0.
`pending` = GC candidate, bytes still present. `deleted` = tombstone; bytes
unlinked. Transitions happen only inside serialized write transactions, so
competing transitions (finalize recovery vs GC deletion) win or lose
atomically — there is no check-then-act gap.

## Claims register

| # | Claimed invariant | Exact mechanism | Reviewer check |
|---|---|---|---|
| 1 | No cross-tenant read/list/confirm | Every tenant lookup is `WHERE id=? AND tenant_id=?`; misses return the identical `404 {"error":"not found"}`; IDs are UUIDv4 | Probe a foreign tenant's real IDs and a random UUID; compare status+body byte-for-byte |
| 2 | Dedup sharing invisible | Dedup happens only at the byte layer; each tenant gets its own artifact row. Finalize assembles unconditionally (full chunk read + temp write + hash) whether or not the bytes are already stored, so latency is independent of dedup outcome; response shapes are uniform | Time finalize with vs without pre-stored identical content; diff response shapes |
| 3 | No partial/corrupt reads | Artifact bytes reach `blobs/` only via atomic rename after complete write + fsync; GC deletion is gated on `refcount=0` inside its own transaction, so a referenced blob is never deleted; download pre-checks `stat` size against the declared size before streaming and a short read aborts the response | Download while GC/validate run; SHA-256 the response |
| 4 | Crash durability ordering | Ordered: (1) temp bytes fully written, fsynced; (2) atomic rename + directory fsync; (3) metadata `COMMIT` with `synchronous=FULL`. Metadata never commits ahead of durable bytes; bytes never commit ahead of metadata | SIGKILL between each ordered step; restart; retry must succeed or find the complete artifact |
| 5 | No lost/double-counted bytes under crash | Chunk write: temp+fsync+atomic rename, then row insert (row follows bytes); finalize: claim transaction inserts blob+artifact+state-flip atomically; startup sweep removes every remnant class (temps, orphan finals, orphan blob bytes, tombstones, quarantines) | `tests/test_crash.py` (timing) + `tests/test_recovery.py` (deterministic remnants) |
| 6 | Chunk replay integrity | Per-`(upload,index)` in-process mutex serializes same-index writes; under the lock the committed row decides: identical digest → `200`, differing → `409` with disk untouched; first-written bytes are never overwritten | Concurrent conflicting PUTs: exactly one winner; finalize verifies against committed bytes |
| 7 | Finalize atomicity / single artifact | Blob claim + artifact insert + upload state flip in one `BEGIN IMMEDIATE` transaction; `UNIQUE(artifacts.upload_id)` bounds artifacts per upload to one; a conflicting loser rolls back and returns the winner's `artifact_id` (both callers coherent) | Two concurrent finalizes of one upload → one artifact, both callers get its ID |
| 8 | Finalize-vs-GC: the reference wins | Finalize recovery: `UPDATE blobs SET state='active', refcount=refcount+1 WHERE state IN ('active','pending')` in its own transaction — atomically beats or loses to GC's deletion decision (`UPDATE ... SET state='deleted' WHERE state='pending' AND refcount=0`). If GC wins, finalize resurrects: its already-assembled verified bytes are renamed into place and the tombstone replaced in one transaction. GC's unlink window is closed by quarantine: bytes are renamed to `*.gcq.<nonce>` before unlink, state is re-checked, and a resurrect that landed in the window is restored (content-identical, since the path is a function of the hash) | Race finalize of refcount-0 content against repeated GC passes (`test_finalize_vs_gc_race`); crash-remnant probes |
| 9 | GC collects only unreferenced bytes | Candidates strictly `refcount=0`; deletion decision re-verified inside its own transaction (`state='pending' AND refcount=0`); unlink only after the tombstone commits | GC during live downloads; `test_gc_during_live_download`; crash-remnant probes |
| 10 | Admin surface tenant-proof | Admin endpoints require the exact `admin_token`; tenant tokens get `401` | Call admin endpoints with tenant tokens |
| 11 | Stated limits enforced on the wire | `MAX_CHUNK_SIZE=32 MiB`: PUT bodies streamed with caps — `>32 MiB` → `413`, longer-than-expected → `422`; declared `chunk_size>32 MiB` or `total_size>10 GiB` → `422` at upload creation | Exceed each stated limit on the wire |
| 12 | Bounded memory (<512 MiB/operation) | Streaming at 1 MiB granularity end-to-end: PUT→temp, assembly→temp, download→response, validate→hash; no whole-artifact buffering anywhere | Measure RSS during a multi-GiB round trip (`tests/scale_probe.py`) |
| 13 | No global stalls | Locks: per-`(upload,index)` mutex held only across rename+row-insert (µs–ms). GC/validate: one short marking transaction, then per-blob short transactions + streaming reads; concurrent finalizes overlap because assembly is parallel streaming IO and claim transactions are brief; WAL serves concurrent readers | Finalize/download issued mid-validate/GC completes without waiting; concurrent distinct-content finalizes overlap |
| 14 | Metadata scale (10k blobs) | Blob paths sharded into 256 directories (no flat mega-directory); GC/validate scans hit `(state, refcount)` / shard-local walks and iterate via `fetchone` (bounded memory) | `tests/scale_probe.py`: listing/GC/validate at 10k blobs in single-digit seconds |

## Durability ordering (explicit)

For every durable write the order is fixed and crash-safe at every cut:

1. **Bytes**: written to a temp file, fully flushed, `fsync`ed.
2. **Placement**: atomic `rename` temp→final, then directory `fsync`.
   Crash here ⇒ orphan bytes at the final path with no metadata row.
3. **Metadata**: single `BEGIN IMMEDIATE … COMMIT` (blob row / chunk row /
   artifact row), `synchronous=FULL`. Crash here ⇒ nothing committed.

Startup recovery maps every cut to a resumable state: orphan bytes without
rows are deleted (no committed artifact can reference rowless bytes);
tombstones with/without bytes are purged; partial temps are deleted; chunk
rows without files are dropped (the upload accepts a re-PUT). Because the
threat model is `kill -9` (OS intact), write-completion already survives;
the fsync ordering additionally hardens against power loss for metadata and
directory entries.

## Finalize-vs-GC coordination (explicit)

The two writers never share a lock; correctness comes from (a) serialized
state transitions — SQLite write transactions make the recovery UPDATE and
the deletion decision mutually exclusive, exactly one commits first — and
(b) content identity — every byte that can ever appear at
`blobs/<h[:2]>/<h>` is SHA-256-verified to be exactly `h`, so restore/overwrite
operations inside the GC unlink window are corruption-free. A finalize can
therefore never succeed against deleted bytes (its claim targets only
`active`/`pending`, and the resurrection path supplies freshly assembled,
verified bytes), and GC can never delete bytes a committed artifact
references (deletion requires `refcount=0` re-verified in-transaction).

## Scale envelope

- Limits: chunk PUT ≤ 32 MiB, artifact ≤ 10 GiB (both enforced, claim 11).
- Memory: streaming path named in claim 12; measured in `tests/scale_probe.py`.
- Lock granularity: claim 13; validation/GC hold no cross-operation locks.
- Layout: claim 14.

## Honest limitations

- Downloads do **not** re-hash; integrity rests on write-time verification,
  GC fencing, and the size pre-check. On-demand re-hashing exists
  (`/admin/validate`).
- Timing equalization (claim 2) is approximate: the residual delta between
  dedup-hit and dedup-miss finalize is sub-millisecond metadata work.
- Same-index chunk writes serialize on an in-process mutex (single-process
  assumption of the contract); distinct indices parallelize fully.
- `tenants.json` is read once at startup; no hot reload.
- Sustained pathological write contention could delay a claim up to the
  SQLite `busy_timeout` (15 s); not observed under the tested races.
- Single-node by contract; no replication/HA.
