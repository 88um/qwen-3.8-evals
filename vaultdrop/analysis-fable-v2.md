# VaultDrop v2 round — Hostile Execution Review (Reviewer: Fable)

Independent review of submission `qwen-3-8-27b-v2` under `review-protocol.md` and
the **revised** `product.md` (scale envelope now part of the interface
contract). All work in a scratch copy; findings graded CONFIRMED / PLAUSIBLE /
REJECTED; only CONFIRMED findings move the correctness score. Scores from this
round are **not directly comparable to round 1** — the brief is materially
harder (scale envelope), and this is again n = 1 generation.

## Verdict (reconciled — see §6; supersedes the original verdict below)

| Submission | Reconciled total | Fable independent | GPT independent |
|---|---:|---:|---:|
| qwen-3-8-27b-v2 | **47 / 100** (≈35 under GPT's severity ledger) | 83 | 34 |

The reconciliation with `analysis-gpt.md` changed this review materially: GPT
found, and I have independently verified, a **CRITICAL data-loss defect I
missed** (F-V2-GC below) — a dead race-guard in GC (`txn()` returns a truthy
cursor, not a rowcount) that lets GC quarantine a *live, just-referenced* blob,
after which a SIGKILL before the restore plus the recovery sweep's
unconditional quarantine deletion permanently destroys a committed artifact's
only bytes. My original verdict's claim that the finalize-vs-GC machinery
"verified correct under attack" was **wrong**: my trace assumed the guard read
the rowcount, and my live probes never hit the crash window because the
no-crash path self-heals. Original independent review preserved below with
errata marked.

### Original independent verdict (Fable, pre-reconciliation — superseded)

| Submission | Total | Correctness (55) | Data model (20) | Isolation (10) | Tests (8) | Judgment (7) |
|---|---:|---:|---:|---:|---:|---:|
| qwen-3-8-27b-v2 | 83 / 100 | 42.9 | 18 | 9.5 | 6.5 | 6 |

Two confirmed findings (−22 total): an HTTP-layer respond-without-drain bug
that makes large-body error statuses unobservable (and flakes its own suite),
and the disclosed no-re-hash-on-read gap under which corrupted stored bytes are
served — even after `/admin/validate` has flagged them. ~~Every scale-envelope
claim (12–14) and the entire lock-free finalize-vs-GC machinery verified
correct under attack.~~ **[ERRATUM: the finalize-vs-GC implementation is
broken in its crash window — see §6, F-V2-GC. The scale-envelope claims 12–14
do stand.]**

## 1. Mechanical floor — PASS, with a documented flake

Builds/runs from README exactly as documented (`migrate`, `serve`, stdlib-only).
**Own suite: 31 tests; my first run FAILED (1 error), three subsequent full runs
passed 31/31.** The failing test (`test_late_chunk_after_finalize`) dies on a
client-side broken pipe caused by a real server defect (finding V2-1), at
roughly 1-in-4 incidence. Ruling: floor **PASS** — the submission builds, runs,
and its suite passes on repetition; the nondeterminism is scored as a finding
and as test quality, not as ABORTED. A strict reading of §6 ("its own tests do
not pass" — observed once) could rule otherwise; flagged for the second
reviewer rather than decided unilaterally.

## 2. Probe evidence

**Standard hostile suite (same 19 probes as round 1): 17 pass.** Notable
passes: conflicting replay → `409` with first bytes preserved on disk;
byte-identical 404s for foreign vs unknown IDs; 12/12 live finalize-vs-GC race
rounds; 4-way concurrent finalize → one artifact; finalize-200 survives
`kill -9`; dedup timing ratio **1.04×** (novel 2.7 ms vs dup 2.6 ms — the
equalization claim holds). Failures: P6 corrupt-blob served (→ V2-2), P7
oversize rejection arrives as connection reset (→ V2-1).

**Scale-envelope probes (new this round):**

| Probe | Result |
|---|---|
| 10 GiB declaration accepted; 10 GiB+1 and 33 MiB chunk_size rejected `422` | PASS |
| Exact 32 MiB chunk accepted on the wire and finalized | PASS |
| 33 MiB body rejected **with observable 413** | **FAIL** — enforcement fires but client sees broken pipe (V2-1) |
| 2 GiB round-trip, RSS watched | PASS — **peak 31 MiB resident** (bound: 512) |
| Finalize/download issued mid-`/admin/validate` | PASS — 3 ms / 1 ms vs 1.3 s validation; no stall |
| 10,000 blobs: populate, list, `/admin/blobs`, GC | PASS — all sub-second after 17 s populate |
| `kill -9` mid-GC over 400 collectable + 40 live blobs, restart | PASS — 0/40 live damaged; metadata↔disk **exactly** consistent; no stray quarantine files; nothing left to collect |
| Corrupt blob → `/admin/validate` flags it → download again | **FAIL** — still `200` with corrupt bytes (V2-2) |

(A first mid-GC probe run showed a 404 on one artifact; traced to my own
probe deleting it in its GC-fodder batch — probe bug, not submission defect.
The rerun above with correct bookkeeping is the evidence of record.)

## 3. Findings log

### V2-1 · CONFIRMED · MEDIUM ×2 (register-claimed check fails as written) → −10
**Error responses on request paths with unread bodies are sent before draining,
so clients writing bodies larger than the socket buffers get a connection
reset instead of the status code.** [store.py:79-80](qwen-3-8-27b-v2/src/vaultdrop/store.py#L79-L80)
raises the late-chunk `409` (and the cross-tenant `404`, and the mid-stream
`413`/`422` at [store.py:104-107](qwen-3-8-27b-v2/src/vaultdrop/store.py#L104-L107))
before the request body is consumed; [httpd.py](qwen-3-8-27b-v2/src/vaultdrop/httpd.py)
writes the error and neither drains `rfile` nor forces `close_connection`.
Repro: 1 KiB late chunk → clean `409`; 4 MiB late chunk → `EPIPE`. 33 MiB
chunk → `EPIPE`, never the `413` that register row 11 promises for exactly
this reviewer check ("exceed each stated limit on the wire") — hence the
doubling. This same defect flakes the submission's own
`test_late_chunk_after_finalize` (~1 in 4 full-suite runs). Mitigating: the
*data-level* invariants all hold — oversize bytes are never accepted, late
chunks never mutate anything; what breaks is the observability of the
rejection. Remediation is small: bounded drain (or `Connection: close`) before
error responses.

### V2-2 · CONFIRMED · HIGH (disclosed → not doubled) → −12
**A corrupted stored blob is served `200` with corrupt bytes — including after
the submission's own validator has flagged it.** Downloads check blob *state*
and byte-length but never content ([httpd.py:159-176](qwen-3-8-27b-v2/src/vaultdrop/httpd.py#L159-L176));
`validate_pass` sets `validated=0` on mismatch
([store.py:396-399](qwen-3-8-27b-v2/src/vaultdrop/store.py#L396-L399)) and the
download path ignores the flag. Repro (S9): bit-flip a blob → `/admin/validate`
reports `mismatched=1` → `GET /artifacts/{id}` → `200`, corrupt body. Register
row 3 titles the invariant "No partial/corrupt reads," but DECISIONS.md's
limitations section discloses precisely this gap ("Downloads do **not**
re-hash; integrity rests on write-time verification…"), so per the protocol's
honesty rule it scores at base severity, undoubled. Severity HIGH for
consistency with the identical defect in round 1 (same-length corruption is
silent; truncation *is* caught by the size pre-check and mid-stream abort).
One-conditional remediation: refuse to serve `validated=0` blobs.

### REJECTED (attacked, held — recorded to stop re-chasing)
- ~~**Finalize-vs-GC lost reference / lock-free state machine**~~ **[ERRATUM —
  this rejection was wrong; reclassified CONFIRMED CRITICAL at reconciliation,
  see §6 F-V2-GC.]** My original trace of `_claim_recover` / `_claim_resurrect`
  ([store.py:191-292](qwen-3-8-27b-v2/src/vaultdrop/store.py#L191-L292)) against
  GC's sequence ([store.py:332-376](qwen-3-8-27b-v2/src/vaultdrop/store.py#L332-L376))
  assumed the tombstone guard `if not changed` observed the UPDATE's rowcount.
  It does not — `Database.txn` returns the (always-truthy) cursor — so GC
  proceeds to quarantine even when it lost the race. The 24 live race rounds
  and the kill-9-mid-GC probe all passed because the no-crash path self-heals
  via the recheck-restore and none of my kills landed inside the
  rename-to-restore window of a lost race. Correct in the common path;
  crash-unsafe in exactly the window the brief targets.
- **Conflicting-replay overwrite** — checked under per-(upload,index) lock
  before any placement; disk verified untouched after `409` (P2), including
  under concurrent conflicting PUTs (its own test).
- **Concurrent finalize duplicate artifacts** — `UNIQUE(artifacts.upload_id)`
  + retry loop returns the winner's ID; 4-way race → one artifact.
- **Memory bound / stall / metadata scale** (register rows 12–14) — 31 MiB
  peak RSS on a 2 GiB round-trip; validation stalls nothing; 10k blobs
  sub-second. (One caveat: my concurrent-finalize overlap timing was
  disk-bandwidth-bound and thus inconclusive on its own; the no-stall probe
  plus the absence of any shared lock in the code settles row 13.)
- **Dedup timing channel** (row 2) — 1.04× measured; claim holds as written.
- **Crash durability** — finalize-200 survives `kill -9`; deterministic
  recovery-remnant tests cover every crash class the design can leave behind.

### Judgment-layer notes (not scored as findings)
- Staged chunks are retained after finalize (re-finalize support) — a
  permanent ~2× storage cost per artifact that, unlike round 1's equivalent,
  is **not disclosed** in the limitations section.
- Abandoned uploads never expire (chunks accumulate indefinitely).
- `ThreadingHTTPServer` remains a non-production HTTP substrate, though the
  per-request socket timeout (60 s) closes the worst of it.

## 4. Category scoring

- **Correctness (55):** 100 − 22 = 78% → **42.9**.
- **Data model & mechanism (20): 18.** The blob state machine
  (`active/pending/deleted` + quarantine + resurrection) is the strongest
  mechanism design of the three submissions I have reviewed in this project —
  it solves the finalize-vs-GC problem without any shared lock and survived
  every attack constructed against it. Schema encodes its invariants (CHECKs,
  `UNIQUE(upload_id)`, `(state, refcount)` index). Docked for the undisclosed
  chunk-retention cost and unbounded abandoned-upload growth.
- **Tenant isolation (10): 9.5.** Byte-identical 404s, UUIDv4 IDs, sealed
  admin surface, and a timing-equalization design that measurably works
  (1.04×). Nothing confirmed against it.
- **Test quality (8): 6.5.** Genuinely adversarial: real mid-operation
  SIGKILLs (chunk write, finalize, GC — with a metadata↔disk exact-consistency
  assertion), a live finalize-vs-GC race loop, concurrent conflicting-replay
  coverage, wire-limit tests, and a shipped scale probe. Docked for the flaky
  suite (once in four full runs) — whose root cause is the submission's own
  V2-1 — and for the late-chunk test being the flake vector rather than
  catching the bug it trips over.
- **Architecture, clarity, operability (7): 6.** Streaming stdlib design that
  actually hits 31 MiB resident on multi-GiB work, sharded layout, clean
  module boundaries, accurate README, mostly-honest limitations. Docked for
  the drain bug's operational surface and the stdlib HTTP substrate.

## 5. Read against round 1 (context, not a ranking)

Not a like-for-like comparison — the v2 brief adds the scale envelope round 1
was never graded against — but the trajectory is what this round was run to
observe. The v2 submission kept every discipline that won round 1
(first-write-wins chunks, timing equalization, honest limitations,
crash-remnant sweeps, adversarial self-testing) while abandoning round 1's
defensive trades: the 64 MiB cap, whole-artifact buffering, and the global GC
lock are gone, replaced by a 10 GiB streaming path at 31 MiB resident and a
lock-free coordination scheme that is *more* verifiable than the coarse lock
it replaced, not less. Both round-1 architectural objections (scale ceiling,
global serialization) are answered outright. The cost of the added ambition:
one new HTTP-layer bug (V2-1) that a body-draining error path would have
avoided, and the same read-integrity gap every submission so far has chosen —
now at least disclosed, flagged by its own validator, and one conditional away
from closed.

Caveats: n = 1 generation; single reviewer (repros and line-cited traces
provided throughout for the second reviewer); crash model exercised is the
brief's `kill -9`, not power loss.

---

## 6. Reconciliation with the second review (`analysis-gpt.md`, v2 section)

Per protocol §6: findings checked against the confirmation bar, judgment
averaged, surviving severity splits reported side by side. Every GPT finding
was independently re-verified before adoption.

### F-V2-GC · ADOPTED FROM GPT · CONFIRMED · CRITICAL ×2 → −50 · *(missed in my independent pass)*
**GC can quarantine a live, just-referenced blob; SIGKILL before the restore
plus the recovery sweep permanently destroys a committed artifact's only
bytes.** Three defects chain:
1. **Dead race guard.** `Database.txn` returns `fn(c)`'s result — the SQLite
   *cursor*, which is always truthy — not the rowcount
   ([db.py:90-97](qwen-3-8-27b-v2/src/vaultdrop/db.py#L90-L97)). So when GC's
   conditional tombstone `UPDATE … WHERE state='pending' AND refcount=0`
   affects **zero rows** (a concurrent `_claim_recover` won and made the blob
   `active`/`refcount=1` with a committed artifact), the `if not changed:
   continue` at [store.py:360](qwen-3-8-27b-v2/src/vaultdrop/store.py#L360)
   never fires — verified directly: `bool(cursor)` is `True` at `rowcount=0`.
2. GC then renames the **live canonical blob** to `*.gcq.*`
   ([store.py:362-364](qwen-3-8-27b-v2/src/vaultdrop/store.py#L362-L364)). In
   the no-crash path the state recheck restores it — which is why all 24 of my
   live race rounds passed. A SIGKILL between rename and restore leaves the
   remnant: `active/refcount=1` row + committed artifact + bytes only at the
   quarantine path.
3. **Recovery destroys the evidence.** The startup sweep deletes every
   `*.gcq.*` file unconditionally
   ([db.py:176-180](qwen-3-8-27b-v2/src/vaultdrop/db.py#L176-L180)) and never
   reconciles quarantines against active rows.

My verification run of the deterministic remnant: pre-restart row
`('active', 1)`, quarantine present → post-restart: quarantine **deleted**,
canonical absent, artifact row intact, download `500` forever — and, one step
worse than GPT reported, **a retry-finalize returns `200` with the existing
artifact ID while its bytes remain unrecoverable** (the upload is `finalized`,
so the client is told all is well). Data loss of a committed artifact, claimed
enforced by register rows 5/8/9 → CRITICAL, doubled. Adopted at GPT's grading
without dispute.

### Corrupt-read finding (my V2-2 = GPT V2-1) · severity split reported
Same facts, same repro (both of us confirmed the flagged-blob case). Split:
mine **HIGH −12, undoubled** — DECISIONS.md's limitations section explicitly
discloses "downloads do not re-hash," and the protocol's honesty rule exempts
disclosed gaps from doubling; GPT **CRITICAL ×2 −50**, arguing disclosure
cannot waive promise 2 against register row 3's claim. Unresolved; both
recorded. (Same split as round 1; it affects magnitude only.)

### Adopted from GPT after verification
- **V2-3 / V2-4 · LOW −2 each:** the crash-during-chunk-write and
  crash-during-GC tests routinely kill *after* the target operation has
  already returned 200 (fixed sleeps vs sub-0.1 s operations). Corroborated by
  my own mid-GC probe, where a 400-blob GC pass completed before an 0.08 s
  kill. My original test-quality praise ("real mid-op kills") was too
  generous; tests score reduced accordingly.
- **V2-5 · LOW −2:** the validator cannot count a missing blob file —
  `streaming_hash_files` converts `FileNotFoundError` into `ApiError(422)`
  ([core.py:97-100](qwen-3-8-27b-v2/src/vaultdrop/core.py#L97-L100)) while
  `validate_pass` catches only `FileNotFoundError`
  ([store.py:389-391](qwen-3-8-27b-v2/src/vaultdrop/store.py#L389-L391)).
  Verified live: with one blob file missing, `GET /admin/validate` returns
  `422 "stored chunk missing; re-upload required"` instead of `missing: 1`.

### Standing from my review (not in GPT's ledger)
- **Respond-without-drain (my V2-1) · MEDIUM ×2 −10:** GPT's review does not
  mention it; the live repros stand (33 MiB → broken pipe instead of the
  register-promised 413; own-suite flake). Carried into the reconciled ledger;
  open for GPT to contest.

### Reconciled ledger and score

Fable severities: −10 (drain) −12 (corrupt read) −50 (GC data loss) −2 −2 −2
(tests ×2, validator) = **−78** → correctness base **22** → 12.1/55.
GPT severities (corrupt read at −50): −116 → base **0** → 0/55.

Judgment categories averaged (Fable revised / GPT → mean): data model 14/13.5
→ 13.75 (the state-machine *idea* remains the best mechanism design in the
project; its implementation fails at the guard and the recovery protocol);
isolation 9.5/9.0 → 9.25; tests 5.5/5.5 → 5.5; architecture 6.0/6.0 → 6.0.

| Ledger | Correctness (55) | Averaged categories (45) | Total |
|---|---:|---:|---:|
| Fable severities | 12.1 | 34.5 | **47** |
| GPT severities | 0 | 34.5 | **≈35** |

### What this does to the round's conclusion
The trajectory finding from §5 survives in weakened form: the v2 generation
really did re-architect to meet the scale envelope (claims 12–14 all hold, at
31 MiB RSS), and kept round 1's isolation and honesty discipline. But the
correctness conclusion inverts: the new lock-free coordination — the very
mechanism I called "more verifiable than the coarse lock it replaced" — ships
with a one-line truthiness bug that voids its central safety guard in exactly
the SIGKILL window this protocol exists to probe, and neither its own tests
nor my probes reached that window; only GPT's phase-aware remnant construction
did. Repair order (per GPT, endorsed): fix the rowcount check and make
recovery quarantine-aware first; gate downloads on `validated` and hash before
headers second; make crash tests phase-aware third.

Both reviewers' rankings agree in direction: the v2 submission, under the v2
spec, currently scores far below round 1's winners under either severity
ledger. n = 1 generation, as ever.
