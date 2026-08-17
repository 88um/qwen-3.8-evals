# VaultDrop v2 round — opus-4-6-v2 — Hostile Execution Review (Reviewer: Fable)

Independent review of `opus-4-6-v2` under `review-protocol.md` and the revised
`product.md` (scale envelope in the contract). Same probe battery as
`qwen-3-8-27b-v2` (see `analysis-fable-v2.md`), including the phase-aware and
remnant-construction attacks added after that round's reconciliation.
Generation provenance: produced headless from the spec alone in an isolated
directory (no access to the review protocol, prior submissions, or analyses);
n = 1 generation; single reviewer pending reconciliation.

## Verdict

| Submission | Total | Correctness (55) | Data model (20) | Isolation (10) | Tests (8) | Judgment (7) |
|---|---:|---:|---:|---:|---:|---:|
| opus-4-6-v2 | **72 / 100** | 34.7 | 16.5 | 9.5 | 5.5 | 5.5 |

Six confirmed findings (−37), dominated by one repeat offender: the corrupt-read
gap, claimed in the register and this time **not** disclosed → doubled. The
crash-coordination core — the place opus-4-6 lost 50 points — is **correct and
verified**, including under the remnant attacks that broke opus-4-6.

## 1. Mechanical floor — PASS, clean

Builds/runs from README exactly (stdlib Python; `migrate`/`serve` per
contract). Own suite: **45/45 × 4 consecutive full runs** — no flake. (Its
error paths have the same drain weakness as opus-4-6's, but its tests use
small bodies, so its suite never trips over it.)

## 2. Probe evidence

**Standard suite: 17/18 meaningful passes.** Highlights: conflicting replay →
`409` with first bytes preserved (**the v1 overwrite bug is fixed** — temp-file
staging, hash compare under a per-(upload,index) lock, only the temp is
discarded); byte-identical 404s; 12/12 finalize-vs-GC race rounds; concurrent
finalize → one artifact (two `200`s + two clean `409`s — a stated, coherent
choice); finalize-200 survives `kill -9`; empty artifact OK.

**Scale envelope: all contract minimums verified.**

| Probe | Result |
|---|---|
| 10 GiB accepted; 10 GiB+1 rejected; 64 MiB stated chunk cap (≥ envelope's 32 MiB) honored, exact-32 MiB chunk accepted + finalized | PASS |
| 2 GiB round-trip, RSS watched | PASS — **peak 28 MiB resident** |
| Finalize/download mid-`/admin/validate` | PASS — 3 ms / 1 ms against a 1.3 s validation; no stall |
| 10,000 blobs | PASS — listings ≤0.2 s, GC instant |
| `kill -9` mid-GC (400 collectable + 40 live), restart | PASS — 0/40 live damaged; metadata↔disk **exactly** consistent |
| **Dedup timing, extended 20-round probe** | **Register row 9 holds** — novel 3.21 ms vs dup 3.41 ms (ratio 0.94), fully overlapping distributions. (A 6-round probe suggested 1.27×; the larger sample shows noise. Recorded so the second reviewer doesn't re-chase.) |
| Corrupt blob → validate flags → download again | **FAIL** — still `200` with corrupt bytes (O2-1) |
| 33 MiB body → observable rejection status | **FAIL** — connection reset instead (O2-2) |

## 3. Findings log

### O2-1 · CONFIRMED · HIGH ×2 → −24
**Same-length corrupted blob served `200`, including after `/admin/validate`
flags it.** Downloads stream from the blob path with no content check and never
consult `validated` ([server.py:348-376](opus-4-6-v2/src/server.py#L348-L376);
[db.py:239-245](opus-4-6-v2/src/db.py#L239-L245) faithfully records the
mismatch that nothing reads). Register row 2 claims "No corrupt or partial
reads" on write-time verification alone, and — unlike opus-4-6 and unlike this
model line's v1 — the Honest Disclosures section says nothing about the read
path, so the honesty exemption does not apply: **doubled**. Third submission
in a row with this exact defect; one `validated` conditional from mitigated.

### O2-2 · CONFIRMED · MEDIUM → −5
**Error responses on paths with unread bodies arrive as connection resets for
large bodies.** All wire-limit and state checks fire before the body is read
([server.py:141-167](opus-4-6-v2/src/server.py#L141-L167)) and `_send_error`
sets `close_connection` without draining — good enough to prevent keepalive
desync (better than opus-4-6), not to deliver the status: a 33 MiB oversize PUT
and a 4 MiB late chunk both yield `EPIPE` at the client; the same probes get
clean `400`/`409` with small bodies. Undoubled: the register promises the
enforcement mechanism (which holds — no oversize byte is ever accepted), not a
wire-observable status, so nothing claimed is broken. Enforcement real, error
semantics wrong.

### O2-3 / O2-4 · CONFIRMED · LOW → −2 each
**The named crash-during-finalize and crash-during-GC tests kill after the
operation has already returned.** `test_crash_during_finalize_recovery` kills
at 0.1 s against a ~3 ms finalize of a 2 KB artifact; `test_crash_during_gc`
kills at 0.05 s against a millisecond two-blob GC pass. Both are written to
hold at any landing point (they branch on observed state), which is the right
shape — but they do not reach the windows their names claim. Same grading as
the equivalent opus-4-6 findings (V2-3/V2-4), for consistency.

### O2-5 · CONFIRMED · LOW → −2
Malformed JSON → `500` with the raw parser message leaked
(`{'error': 'Expecting property name enclosed in double quotes: …'}`) — the
catch-all in `_dispatch` swallows it ([server.py:79-82](opus-4-6-v2/src/server.py#L79-L82)).
Typed-field validation is otherwise correct (`"abc"` as total_size → clean 400).

### O2-6 · CONFIRMED (by trace) · LOW → −2
`_read_json` reads the full `Content-Length` into memory with no cap
([server.py:52-57](opus-4-6-v2/src/server.py#L52-L57)) — a multi-GB JSON body
is buffered before parsing. Memory-DoS vector; same grading as round-1
model-1's F1-3.

### REJECTED (attacked, held)
- **Finalize-vs-GC** (rows 4/10): per-hash lock held across blob-file effects
  *and* the `BEGIN IMMEDIATE` commit on both sides, refcount re-check and
  post-delete `blob_exists` re-check inside the lock — every interleaving
  traced, 12 live race rounds, clean kill-9-mid-GC with exact metadata↔disk
  agreement. **Rowcounts are actually checked** (`cur.rowcount == 1` /
  `> 0`) — the opus-4-6 truthiness bug has no analogue here, and the remnant
  attacks that broke opus-4-6 find no purchase: crash orphans here are
  files-without-rows (harmless), never rows-without-files.
- **Durable `finalizing` state + startup reset** (rows 3/11): crash mid-finalize
  leaves a durable, resettable state; verified by their test and my trace.
- **Chunk conflict/replay** (rows 5/7): the v1 overwrite bug is structurally
  fixed. Verified live.
- **Timing side-channel** (row 9): holds under the extended probe — the
  unconditional-assembly design does equalize the dominant cost.
- **Scale claims** (envelope table): all verified (28 MiB RSS, no stalls,
  10k responsive).

### Judgment notes (not scored)
Orphan blob files (crash between GC's row-delete and unlink, or between blob
placement and commit) are never reclaimed and — unlike v1 — not disclosed;
`ChunkLocks`/`BlobLocks` registries grow without pruning; README refers the
reader to `product.md` for the API, which is not part of the submission; an
absent `admin_token` key in tenants.json would make the empty bearer token an
admin (unreachable under the contract's seed file, noted for completeness).

## 4. Category scoring

- **Correctness (55):** 100 − 37 = 63% → **34.7**.
- **Data model & mechanism (20): 16.5.** The v1 lock protocol, kept and
  correctly re-verified, now with durable finalize state, post-finalize chunk
  reclamation (opus-4-6 retains chunks forever), streaming, and sharding.
  Docked: no schema CHECK constraints, orphan files unreclaimed and
  undisclosed.
- **Tenant isolation (10): 9.5.** Everything probed holds, including the
  timing claim under a 20-round attack.
- **Test quality (8): 5.5.** 45 tests, 4 clean runs, real concurrency races;
  but the two named crash tests don't reach their windows, and nothing tests
  the scale envelope (no RSS/10k probe shipped, unlike opus-4-6).
- **Architecture/clarity/operability (7): 5.5.** Clean, small, readable;
  genuinely honest GIL and in-process-lock disclosures; docked for the
  undisclosed read gap, the JSON-handling nits, and the drain behavior.

## 5. Round-2 standing (both v2 submissions, this reviewer's ledgers)

| Submission | Fable score | Status |
|---|---:|---|
| opus-4-6-v2 | **72** | single-reviewer, reconciliation pending |
| qwen-3-8-27b-v2 | **47** (35 under GPT's severities) | reconciled with GPT |

Same spec, same probes, opposite failure profiles. opus-4-6 reached for a novel
lock-free coordination scheme and shipped a one-line bug that voids it exactly
under SIGKILL, taking a CRITICAL ×2; opus-4-6-v2 kept its v1 lock-based
protocol — which survived hostile review both rounds — and spent its effort
meeting the scale envelope and fixing its v1 API-layer bugs (chunk overwrite:
fixed; declared-size enforcement: fixed; timing channel: closed and verified).
What it did *not* fix is the one defect it shipped in v1 and got hit for then
too: downloads that never verify bytes — this time without even the
disclosure. Both v2 submissions would benefit from the same one-conditional
remediation, and both share the drain-on-error weakness, suggesting the new
spec should probably name "error responses must be observable for maximal-size
bodies" explicitly next round.

Caveats: n = 1 per model; opus-4-6-v2 unreconciled; crash model is `kill -9`.
