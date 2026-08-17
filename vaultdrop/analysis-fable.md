# VaultDrop — Hostile Execution Review (Reviewer: Fable)

Independent review of submissions `model-1` and `model-2` under `review-protocol.md`
and the `product.md` brief. All work was done in a scratch copy; every finding
below is graded CONFIRMED / PLAUSIBLE / REJECTED per §4 of the protocol, and only
CONFIRMED findings move the correctness score. Sections 1–7 are the original
independent review, preserved unchanged for the record; **§8 is the
reconciliation with the second independent review (`analysis-gpt.md`)** and
supersedes the headline numbers below.

> **Editorial note (2026-08-16):** §§1–8 review the round-1 field, whose masked
> IDs `model-1`/`model-2` have since been revealed and renamed to
> `qwen-3-8-27b`/`opus-4-6`. A **new, unrelated submission** was later dropped
> under the reused masked ID `model-1/`; its independent review is **§9** at the
> end of this file. Any mention of "model-1" in §§1–8 refers to the round-1
> submission, not the new one.

---

## Verdict and ranking (reconciled — see §8)

| Rank | Submission | Reconciled total | Fable independent | GPT independent |
|---:|---|---:|---:|---:|
| **1** | **model-1** | **90 / 100** (77 under GPT's unresolved M1-1 severity) | 93 | 78.4 |
| **2** | **model-2** | **56 / 100** (31 under GPT's severity ledger) | 69 | 28.7 |

**The ranking is unanimous across both reviewers and every severity
resolution: model-1 first, model-2 second.** The reconciled totals use this
reviewer's severity ledger with both reviews' verified findings merged and
judgment scores averaged; where a severity split survives reconciliation, the
alternative total is shown rather than forced into one number (protocol §6).

### Original independent scores (Fable, pre-reconciliation)

| Rank | Submission | Total | Correctness (55) | Data model (20) | Isolation (10) | Tests (8) | Judgment (7) |
|---:|---|---:|---:|---:|---:|---:|---:|
| 1 | model-1 | 93 / 100 | 52.8 | 18 | 9 | 7.5 | 6 |
| 2 | model-2 | 69 / 100 | 36.9 | 15 | 7 | 5 | 5.5 |

Both submissions clear the mechanical floor (§6): both build, run from the
interface contract, and pass their own test suites (model-1: 22/22 pytest;
model-2: 21/21 `go test`). The gap between them is driven by three confirmed
model-2 findings — one doubled under the claims-register rule — plus weaker
crash-test coverage.

---

## 1. Mechanical floor

**model-1** (Python 3, stdlib only): `./vaultdrop migrate` and `./vaultdrop serve`
work exactly as documented; server self-initializes schema and reclaims orphans
before accepting traffic. Own suite: **22 passed** (functional, isolation,
concurrency, crash).

**model-2** (Go + `modernc.org/sqlite`): the README's primary build command
(`go build -o vaultdrop .`) **fails verbatim** — the shipped `vaultdrop` wrapper
script occupies the output name and Go refuses to overwrite a non-object file
(finding F2-7, LOW). Recovered trivially with a different output name; the
wrapper (`go run .`) also works. Own suite: **21 passed**. PASS with an
operability note.

## 2. Probe results (both submissions, identical harness)

19 probes driven purely through the interface contract, plus 2 follow-ups.
model-1: **19/19 pass**. model-2: **16/19** — failures at P2 (conflicting-replay
byte preservation), P6 (corrupt blob served), P7 (stated chunk limit unenforced).
Shared passes worth noting: byte-identical 404s for foreign vs unknown IDs;
dedup refcount lifecycle; 12/12 rounds of a live finalize-vs-GC race; concurrent
finalize convergence to one artifact; finalize-200 surviving `kill -9` + restart;
empty artifact handling.

---

## 3. Findings log — model-2

### F2-2 · CONFIRMED · HIGH ×2 (claims-register doubling) → −24
**A corrupted stored blob is served with `200` and the corrupted bytes — even
after the submission's own validator has flagged it.**
Claims register row 2 asserts "No corrupt or partial reads." The named mechanism
(finalize-time hash verification) covers only the write path; nothing gates the
read path. Repro (probe P6 + follow-up):
1. Upload + finalize any artifact; locate `blobs/<h[0:2]>/<hash>` in the state dir.
2. Flip one byte in that file.
3. `GET /artifacts/{id}` → `200`, correct `Content-Length`, wrong bytes. Silent.
4. `GET /admin/validate` → `{"total":1,"valid":0,"invalid":1}` — corruption detected.
5. `GET /artifacts/{id}` again → still `200` with corrupted bytes. The
   `validated` flag ([gc.go:70-78](model-2/gc.go#L70-L78)) is never consulted by
   [handlers.go:375-385](model-2/handlers.go#L375-L385).

Promise 2 is unconditional ("returns the exact bytes whose SHA-256 equals the
declared digest, **or it fails**"), and the product ships a validation endpoint
precisely because it contemplates stored-content corruption. Severity HIGH
rather than CRITICAL because the trigger is out-of-band filesystem corruption,
not an API-reachable interleaving; doubled because the register claims the
invariant as enforced. (Contrast: model-1 re-hashes at the read boundary and
returned `500` on the same probe.)

### F2-1 · CONFIRMED · MEDIUM → −5 (concealment: brief-required, register-silent)
**A conflicting chunk replay returns `409` but silently overwrites the
first-written bytes on disk.**
The brief's §3 explicitly requires "the first-written bytes are not silently
overwritten." In the chunk-exists path, [handlers.go:158-176](model-2/handlers.go#L158-L176)
"drains the body" by calling `WriteChunk`, which streams the new body to a temp
file and **renames it over the final chunk path**
([blob.go:86](model-2/blob.go#L86)) before the hash comparison. Repro (probe P2):
1. `PUT .../chunks/0` with bytes A → `200`. 2. `PUT .../chunks/0` with bytes B →
`409`, but `chunks/{upload}/0` now contains B (verified on disk). 3. Upload the
remaining chunks of A-content and finalize with sha(A…) → **`422 "sha256 or size
mismatch"`** — the correct client can no longer finalize until it re-uploads
chunk 0. The same overwrite fires from an unsynchronized concurrent duplicate
PUT (no per-upload/per-chunk lock exists), and against any not-yet-finalized
upload, including mid-assembly. The end-to-end finalize hash check prevents a
corrupt *artifact* (blast radius = own upload staging, recoverable), hence
MEDIUM. The register (row 6) claims only the `409`, not preservation — a
brief-required invariant absent from the register (concealment rule applies; no
doubling since preservation was never claimed).

### F2-3 · CONFIRMED · MEDIUM → −5 (concealment: brief-required, register-silent)
**Cross-tenant dedup is inferable from finalize timing.**
Brief §3: a tenant "cannot infer from `POST /uploads`/finalize timing or
responses that t1 already stored that content." Probe P12 (6 alternating rounds,
2 MiB payloads): median finalize latency 11.2 ms for novel content vs 7.2 ms for
content another tenant already stored — a stable **1.55×** ratio. Mechanism: the
dedup path skips `rename` + two directory fsyncs
([blob.go:149-157](model-2/blob.go#L149-L157)), which dominate at this size.
Register row 9 covers ID/listing/404 invisibility but is silent on the timing
channel. (model-1 measured 1.14× on the identical probe — inside noise — because
it deliberately stages + fsyncs the full bytes on both paths and disclosed the
residual channel honestly.)

### F2-4 · CONFIRMED · LOW → −2
**The stated 10 MB max-chunk-size is not enforced on the wire.** A 17 MiB body
for a declared 1 MiB-chunk upload is accepted (`200`) and written to disk
(probe P7); nothing bounds the request body (`io.Copy` from `r.Body`,
[blob.go:73](model-2/blob.go#L73), no `MaxBytesReader`). `MaxChunkSize` is only
checked against the *declared* `chunk_size` at upload creation. Stated limit ≠
enforced limit; unbounded disk-write vector.

### F2-5 · CONFIRMED (by trace) · LOW → −2
**DECISIONS.md and the register (row 7) claim finalize runs in a single
`BEGIN IMMEDIATE` transaction; no such statement exists in the code.**
[store.go:197-204](model-2/store.go#L197-L204) opens a default deferred
transaction and runs `SELECT 1`, which takes no write lock. The concurrency
invariant *holds anyway* — `SetMaxOpenConns(1)` serializes all DB work on one
connection — so this scores as false mechanism precision, not a broken
invariant. (The `rows_affected` guard, which is the load-bearing part of row 7,
is present and was verified under 4-way concurrent finalize.)

### F2-6 · CONFIRMED (by trace) · LOW → −2
**"Validation can detect them [orphan blob files]" (DECISIONS.md, best-effort
section) is false.** `RunValidation` iterates DB `blobs` rows only
([gc.go:58-66](model-2/gc.go#L58-L66)); an orphan file has no row and is
invisible to it. Checkable claim, demoted to a finding per §5.

### F2-7 · CONFIRMED · LOW → −2
README's primary build command fails verbatim (see §1). Operability.

**REJECTED (mechanism verified correct, stops re-chasing):**
- *Finalize-vs-GC coordination.* The per-hash mutex + post-lock DB re-check
  ([gc.go:27-48](model-2/gc.go#L27-L48), [handlers.go:329-348](model-2/handlers.go#L329-L348))
  survived trace analysis of all orderings and 12 live race rounds (P8). The
  reference wins in every interleave I could construct.
- *Concurrent finalize.* One artifact, coherent answers, 4-way race (P9) + own test.
- *Finalize durability ordering.* Blob fsync + dir fsync before COMMIT
  (`synchronous=FULL`); finalize-200 survived `kill -9` (P10 + own test).
- *`RecoverCrash` resetting `finalizing`* is dead code (the state never commits
  mid-transaction) but harmless.

**PLAUSIBLE (recorded, zero score):** none outstanding.

Correctness deductions model-2: 24+5+5+2+2+2+2 = **42** → base 58/100.

---

## 4. Findings log — model-1

### F1-1 · CONFIRMED · LOW → −2 (honest disclosed gap; no doubling)
**Residual dedup timing channel, exactly as disclosed.** Register row 4 claims
the channel is "bounded, best-effort," with the full-byte staging + fsync on
both paths as the equalizer, and DECISIONS.md discloses the residual. Probe P12
measured 1.14× (3.6 ms vs 3.1 ms) — consistent with the claim ("small,
non-deterministic delta, not a clean signal"). Scored as a normal scope gap
against the brief's stricter ideal, per the protocol's honesty rule.

### F1-2 · CONFIRMED · LOW → −2
**Malformed scalar JSON yields `500`, not a 4xx.** `POST /uploads` with
`{"total_size": "abc"}` → `500 {"error":"internal: ValueError"}` — the `int()`
coercions at [httpd.py:114-121](model-1/src/vaultdrop/httpd.py#L114-L121) throw
outside the `JSONDecodeError` handler. Wrong error semantics on a trivially
reachable input (proper malformed JSON and negative sizes are handled correctly:
`400` / `422`).

### F1-3 · CONFIRMED (by trace) · LOW → −2
**Request bodies are fully buffered in RAM before any size check.**
[httpd.py:53-55](model-1/src/vaultdrop/httpd.py#L53-L55) reads the entire
`Content-Length`; the 16 MiB chunk cap is applied only afterwards in
`put_chunk`. A hostile client can commit multi-GB allocations per connection.
Memory-exhaustion vector; deliberately not exercised live.

**REJECTED:**
- *Lock-registry pruning race.* `release_upload_lock`
  ([locks.py:37-44](model-1/src/vaultdrop/locks.py#L37-L44)) pops the per-upload
  lock after finalize, so later PUTs can hold *different* lock objects. Traced
  every interleave: post-finalize, every in-range index already has a `chunks`
  row (finalize requires completeness), so racers converge on the read-only
  `200`/`409` paths; the conditional `UPDATE … WHERE state='open'` and the
  `(upload_id, index)` primary key backstop the rest. No invariant breaks.
- *Finalize-vs-GC.* `gc_lock` held across both the transaction and the file
  effects on both sides ([store.py:30-70, 72-111](model-1/src/vaultdrop/store.py#L30-L111));
  12 live race rounds passed (P8).
- *Conflicting replay.* First-write-wins verified on disk (P2: file still holds
  the first bytes after a `409`), matching register row 11.
- *Read-boundary integrity.* Corrupt-blob probe → `500`, never corrupt bytes
  (register row 5 verified exactly as written).
- *Crash safety.* Durable-write ordering (temp → fsync → rename → dir-fsync →
  commit, `synchronous=FULL`) verified in trace and by P10 + the submission's own
  genuinely mid-operation SIGKILL tests.

**Disclosed, not scored as findings** (honest scope gaps in DECISIONS.md,
weighed in the judgment layer): staged chunks retained after finalize (bounded
space cost); validation advisory-only (superseded by the read-boundary check);
64 MiB artifact cap (stated; small for an "artifact storage" product).

Correctness deductions model-1: 2+2+2 = **6** → base 94/100.

---

## 5. Category scoring

**Correctness under concurrency & crash (55).** Deductions landing in this
category: model-1 −4 (F1-2, F1-3) → 96% → **52.8**. model-2 −33 (F2-1, F2-2,
F2-4, F2-5) → 67% → **36.9**.

**Data model & mechanism design (20).** model-1 **18**: schema with CHECK
constraints and a strict state lifecycle; one shared durable-write primitive;
clean rows-then-files GC ordering with startup orphan reclamation; the entire
finalize-vs-GC problem reduced to one coarse lock (simple, provably
interleave-free, at some throughput cost). model-2 **15**: good schema (FKs,
indexes, prefix-sharded blob dirs, streaming design that scales to 10 GB
artifacts); the per-hash-lock GC protocol is genuinely correct; but the
chunk-write path has no write-once discipline (root cause of F2-1), there is no
startup reclamation of orphans, and one claimed mechanism doesn't exist as
described (F2-5).

**Tenant isolation depth (10).** Both: tenant-scoped SQL predicates everywhere,
byte-identical 404s (verified), admin surface exposes counts only, both deny
admin endpoints to tenant tokens. model-1 **9** (−2 for the disclosed residual
timing channel, which its design actively minimizes). model-2 **7** (confirmed
1.55× timing oracle, F2-3).

**Test quality (8).** model-1 **7.5**: crash tests land SIGKILLs genuinely
mid-operation (including a real crash-during-GC test over 700 collectable
blobs), invariants written to hold at every landing point; a live finalize-vs-GC
race; conflicting-replay test asserts byte preservation — precisely the
invariant model-2's suite never checks. model-2 **5**: broad and real HTTP-level
coverage, but `TestCrashDuringChunkWrite` kills the server *before any chunk is
sent* (the name overstates it; no kill ever lands mid-write), there is no
crash-during-GC test (register row 12 admits "structural" only), `TestFinalizeVsGC`
is sequential rather than racing, and `TestChunkConflict` checks the `409` but
not preservation — the exact spot its bug lives.

**Architecture, clarity, operability (7).** model-1 **6**: stdlib-only, sharply
factored modules whose docstrings state the invariant each enforces; accurate
README; costs: whole-artifact-in-memory design (justifiable only because of the
64 MiB cap) and full re-hash on every download. model-2 **5.5**: idiomatic,
readable Go, single static binary, streaming I/O with sensible large-file
limits; costs: broken README build command, unenforced stated limits, two false
documentation claims, minor test-file cruft.

---

## 6. Limitations of this review

- **n = 1 generation per model.** The protocol calls for two generations per
  model per project; a single sample cannot separate capability from a lucky or
  unlucky draw. Scores above are for these generations.
- Single reviewer pass; correctness findings carry repro commands or line-cited
  traces so the second reviewer can verify or refute each one. Judgment scores
  (§5 rows) are to be averaged, not argued, at reconciliation.
- Crash probes used process-level `SIGKILL` (the brief's stated model), not
  power-loss simulation; fsync claims were verified by trace, not torn-write
  injection.
- Timing-channel measurements are macOS/APFS medians over 6 rounds; absolute
  numbers will vary by platform, though the asymmetry mechanism (skipped
  rename/dir-fsyncs) is structural.

## 7. Bottom line

**model-1 (93)** ships the stronger submission on exactly the axes this brief
weights: every claims-register row survived hostile tracing and live probing,
its two hardest mechanisms (durability ordering, finalize-vs-GC) are simple
enough to verify exhaustively, its register is honest about what it doesn't
fully deliver, and its confirmed defects are three LOWs at the HTTP boundary.
**model-2 (69)** is a well-built service with a correct GC/finalize protocol and
better large-file scalability, but it broke one claimed invariant (corrupt reads
— doubled), silently violates a brief-required behavior its own conflict test
looks straight past (replay overwrite), leaks dedup through timing, and its
crash tests don't reach the states they name.

---

## 8. Reconciliation with the second review (`analysis-gpt.md`)

Per protocol §6: correctness findings are resolved by the confirmation bar
(checked, not voted); judgment scores are averaged, never argued; severity
disagreements that survive are reported side by side. Every cross-reviewer
finding below was re-verified against the code or re-run live before its
reconciliation outcome was assigned.

### 8.1 Per-finding outcomes

**model-1**

| Finding | Origin | Reconciliation outcome |
|---|---|---|
| M1-1 chunk-dir parent fsync missing (HIGH ×2, −24) | GPT | **DROPPED — trace flaw.** Step 5 of the trace claims a *SIGKILL* "can retain the row while losing the not-durably-linked upload directory." It cannot: a completed `rename()`/`mkdir()` lives in the kernel page cache and survives process death unconditionally; missing directory fsyncs only matter under **power loss / kernel crash**, which is outside the brief's stated crash model (§3 names SIGKILL for every crash probe; §4 says "a 200 on finalize must survive `kill -9`"). model-1's register rows 6–7 and its durability section are explicitly SIGKILL-scoped, and hold as scoped. Recorded as a legitimate **power-loss hardening note**, zero score. GPT's dissent (−24) is preserved in the alternative total. |
| M1-2 accepted-but-impossible upload: `chunk_size` > 16 MiB accepted at `POST /uploads` (MEDIUM, −5) | GPT | **CONFIRMED — adopted.** Re-verified live: `total_size=chunk_size=20 MiB` → `201`; the only legal chunk body → `413`; the upload can never be completed. No cap check exists at [uploads.py:55-61](model-1/src/vaultdrop/uploads.py#L55-L61). Not register-claimed → undoubled. **−5.** |
| F1-1 residual dedup timing (was LOW −2) | Fable | **Reclassified PLAUSIBLE-unresolved, 0.** Both reviewers probed it independently (Fable: 1.14× median; GPT: 2.060 vs 2.027 ms, heavily overlapping) and neither demonstrated a usable classifier. Register row 4's claim ("bounded, not a clean signal") holds exactly as written; the honest disclosure stands. Converges with GPT's PLAUSIBLE grading. |
| F1-2 malformed JSON scalar → 500 (LOW −2) | Fable | **Stands** (uncontested; trivially reproducible). |
| F1-3 body buffered in RAM before limit check (LOW −2) | Fable | **Stands** (uncontested; line-traced). |

Reconciled model-1 correctness ledger: 100 − 5 − 2 − 2 = **91**.
(GPT-alternative, with M1-1 at −24: 67.)

**model-2**

| Finding | Origin | Reconciliation outcome |
|---|---|---|
| Corrupt blob served `200` (F2-2 / M2-1) | Both | **CONFIRMED by both, identical repro; doubled by both.** Severity split survives: Fable HIGH ×2 = **−24** (trigger requires out-of-band file corruption, not API-reachable); GPT CRITICAL ×2 = −50 (the table's literal "silent corruption" class). Reported, not forced. |
| Conflicting replay overwrites first-written bytes (F2-1 / M2-2) | Both | **CONFIRMED by both, identical repro.** Severity split survives: Fable MEDIUM via concealment = **−5** (register row 6 never actually claims preservation — it claims only the 409, which works; blast radius is own-upload staging, recoverable; no corrupt artifact possible past the finalize hash gate); GPT reads row 6 + row 5's "atomic file writes" as a claimed-and-broken invariant → HIGH ×2 = −24. Reported. |
| Dedup timing channel (F2-3 / M2-5) | Both | **Facts converged:** the register is silent on a brief-required promise-1 behavior (concealment agreed), and Fable's probe demonstrated the practical classifier (1.55× median) that GPT scored without demonstrating. Severity split survives: Fable MEDIUM = **−5** (leak reveals only existence-of-already-known-bytes); GPT CRITICAL = −25 (promise-1-class per the table). Reported. |
| Finalize ignores declared `total_size` (M2-3, MEDIUM concealment −5) | GPT | **CONFIRMED — adopted.** Re-verified live: declare `total_size=5`, PUT 10 bytes, finalize with `size=10` → `200` and an artifact. Only `req.Size` is checked at [handlers.go:322-327](model-2/handlers.go#L322-L327). **−5.** |
| Parent-dir fsync gaps (M2-4, HIGH ×2 −24) | GPT | **DROPPED** — same trace flaw as M1-1, applied identically for consistency: unreachable under the brief's `kill -9` model. Power-loss hardening note (including `syncDir` swallowing errors at [blob.go:199-206](model-2/blob.go#L199-L206)), zero score. |
| `TestCrashDuringChunkWrite` never starts a PUT (M2-6, LOW −2) | GPT (Fable had it as prose) | **CONFIRMED — adopted into the ledger** per §3.2 (green-but-meaningless tests are findings). **−2.** |
| `TestFinalizeVsGC` sequential, not racing (M2-7, LOW −2) | GPT (Fable had it as prose) | **CONFIRMED — adopted.** **−2.** |
| Dependencies not vendored (M2-8, LOW −2) | GPT | **CONFIRMED — adopted.** The brief says "vendor anything else into the submission"; `modernc.org/sqlite` is fetched from the module cache/network. Builds here only because the host had it. **−2.** |
| "BEGIN IMMEDIATE" documented, absent (F2-5 / M2-9, LOW −2) | Both | **Agreed, −2.** |
| Stated 10 MB chunk limit unenforced (F2-4, LOW −2) | Fable | **Stands.** Distinct from M2-3 (README's global cap vs the brief-required per-upload declared size), though the same missing-enforcement family — GPT's M2-3 repro in fact rode through this hole. **−2.** |
| "Validation can detect orphan files" false (F2-6, LOW −2) | Fable | **Stands** (uncontested; line-traced). |
| README build command fails verbatim (F2-7, LOW −2) | Fable | **Stands** — corroborated by GPT's own floor table, which silently substituted `go build -o vaultdrop-built .`. **−2.** |

Reconciled model-2 correctness ledger (Fable severities):
24 + 5 + 5 + 5 + 2 + 2 + 2 + 2 + 2 + 2 + 2 = **53** → base **47**.
(GPT severities: −50 −24 −25 alone exceed 100 → base **0**.)

### 8.2 Reconciled scores

Correctness applies each reviewer's ledger to the 55-point category; the four
remaining categories are averaged across the two reviews per §5 (Fable / GPT →
mean): model-1 data model 18/16.3 → 17.15, isolation 9/9.1 → 9.05 (F1-1's
reclassification to PLAUSIBLE is absorbed here), tests 7.5/7.6 → 7.55,
architecture 6/6.3 → 6.15; model-2 data model 15/13.5 → 14.25, isolation
7/7.1 → 7.05, tests 5/3.5 → 4.25, architecture 5.5/4.6 → 5.05.

| Submission | Correctness (55) | Averaged categories (45) | **Reconciled total** | Alt. under GPT severities |
|---|---:|---:|---:|---:|
| model-1 | 91% → 50.1 | 39.9 | **90** | 77 |
| model-2 | 47% → 25.9 | 30.6 | **56** | 31 |

### 8.3 What changed and what remains open

Versus my independent report: model-1 93 → **90** (adopted GPT's
accepted-but-impossible-upload finding at MEDIUM; moved the timing residual to
PLAUSIBLE — a wash of −3 net with category averaging). model-2 69 → **56**
(adopted GPT's declared-size bypass, unvendored deps, and both
weak-crash-test findings; averaged their harsher test/architecture judgment).

Dropped from GPT's ledger with cause: M1-1 and M2-4 (−24 each) — the only two
findings whose traces do not survive the brief's own crash model. Every other
GPT finding reproduced.

Unresolved severity splits, reported per §6 rather than forced: corrupt-read
(HIGH ×2 vs CRITICAL ×2), replay-overwrite (MEDIUM-concealment vs HIGH ×2),
timing-channel (MEDIUM vs CRITICAL) — all on model-2, all affecting magnitude
only. Under every resolution of every open split, including GPT's maximal one,
the ranking is unchanged: **1. model-1, 2. model-2.**

---

# §9 — New `model-1` submission — Hostile Execution Review (Reviewer: Fable, 2026-08-16)

Independent review of the **new** submission at `model-1/` (single-file Python,
1,038 lines + 5-test suite; unrelated to the round-1 submission of the same
masked ID) under `review-protocol.md` and the **current** `product.md` (scale
envelope in the interface contract, as in the v2 round). All work in a scratch
copy; findings graded CONFIRMED / PLAUSIBLE / REJECTED; only CONFIRMED findings
move the correctness score. **Single-reviewer, n = 1 generation, unreconciled**
— repros and line-cited traces are provided throughout for the second reviewer.

## 9.0 Verdict and ranking

| | Total | Correctness (55) | Data model (20) | Isolation (10) | Tests (8) | Judgment (7) |
|---|---:|---:|---:|---:|---:|---:|
| model-1 (new) | **90 / 100** | 51.2 | 18 | 9.5 | 5 | 6 |

Numeric rank across every submission reviewed in this project (prior rounds at
their reconciled totals; this one Fable-independent, pending reconciliation):

| Rank | Submission | Score | Brief version |
|---:|---|---:|---|
| **1** | **model-1 (new, §9)** | **90** (independent) | current (scale envelope) |
| 2 | `qwen-3-8-27b` (round 1) | 90 reconciled (93 independent) | round-1 (no envelope) |
| 3 | `opus-4-6` (round 1) | 56 reconciled (69 independent) | round-1 |
| 4 | `qwen-3-8-27b-v2` | 47 reconciled (83 independent) | current |

The tie at 90 is broken in the new submission's favor deliberately: it posts
the same number under a **materially harder contract** — a 10 GiB streaming
envelope it actually meets (38 MiB peak RSS on a 2 GiB round-trip) where the
round-1 winner's 90 was earned with a 64 MiB cap and whole-artifact buffering
that the current brief would score as contract violations. This is the first
submission in the project to survive the full crash/concurrency battery with
**zero confirmed findings above the HTTP boundary layer**: every one of its 16
claims-register rows held under attack, including both mechanisms this
protocol hits hardest (finalize-vs-GC coordination and finalize durability).
Rank is provisional per protocol §7 (n = 1, single reviewer).

## 9.1 Mechanical floor — PASS

- `./vaultdrop migrate` exits 0, is idempotent, creates schema + directory
  tree; `serve` runs foreground on `$PORT`, self-initializes, and fails with a
  clean actionable error (exit 1) when `tenants.json` is absent.
- **Own suite: 5 tests, 4/4 full runs green (~2 s), no flake.** The suite is
  small but every test is genuine (see 9.4): the mid-chunk SIGKILL test really
  lands mid-body (512 KiB of 2 MiB sent, then `kill -9`).
- DECISIONS.md: 1,172 words (≤1,500 ✓), 16-row claims register covering every
  §3 brief scenario **and** the scale envelope — no concealment candidates
  found: durability ordering and finalize-vs-GC coordination are stated
  explicitly, as the brief demands.

## 9.2 Probe evidence

~60 probes in seven stages, driven through the interface contract. Highlights
(full scripts preserved in the review scratchpad, `stage_a`–`stage_g4`):

| Probe | Result |
|---|---|
| Out-of-order/duplicate concurrent chunks; assembly; round-trip | PASS |
| Conflicting replay → `409`, **first bytes verified intact on disk**; exact replay → `200` | PASS |
| Foreign vs unknown ID: byte-identical `404` bodies (uploads, artifacts, delete) | PASS |
| Tenant token on admin → `403`; admin token on tenant routes → `401`; bogus → `403` | PASS |
| Input validation: malformed JSON, string/bool/negative/oversize sizes → typed `4xx` (round-1's 500-on-scalar class is fixed); truncated body → `400`; chunked TE → `400`; >64 KiB JSON → `413` | PASS |
| Finalize semantics: bad hash `422` then retry `200`; idempotent re-finalize returns same ID; mismatched re-finalize `409`; empty (0-byte) artifact | PASS |
| 4-way concurrent finalize: statuses (200,200,409,409), one artifact, single winner ID | PASS |
| **Finalize-vs-GC race** (refcount-0 blob, GC vs identical finalize), 14 rounds | **14/14 — reference wins** |
| SIGKILL mid-chunk PUT → restart: `received: []`, resumable, zero stray files | PASS |
| **Randomized SIGKILL sweep across the finalize window, 10 rounds** (256 MiB artifacts, kill at 20–750 ms): every round lands in "committed + downloadable + state finalized" or "rolled back + retryable"; both outcomes observed | **10/10** |
| SIGKILL immediately after finalize `200` ×3 → restart → downloadable | 3/3 |
| **SIGKILL mid-GC** (400 collectable + 40 live blobs, kill at ~35% of pass): 0/40 live damaged; **metadata↔disk exactly consistent** (every rc>0 row has its file, no file lacks a row); trash empty; follow-up GC completes the remainder | PASS |
| Corrupt (bit-flip) blob → `500` JSON, never bytes; still refused after validate; truncation → `500`; deleted file → `500` + **validator counts missing files** (the v2 gap, fixed); identical re-upload **repairs** the blob for both tenants | PASS |
| Dedup timing channel: novel 5.27 ms vs dup 4.60 ms median = **1.146×** over 8 rounds — consistent with the register's "no explicit timing branch, not a noninterference guarantee" disclosure | PASS (as claimed) |
| 10 GiB / 32 MiB declared limits accepted at maximum, rejected above (`422`) | PASS |
| **2 GiB round-trip, RSS watched: peak 38 MiB resident** (bound: 512) | PASS |
| No-stall: download/finalize issued while validation hashes a 2 GiB store: 1.0 ms / 0.5 ms vs 0.65 ms / 0.4 ms unloaded (≪ 2×) | PASS |
| 10,000 blobs: populate 16.6 s; list 0.04 s; `/admin/blobs` 0.02 s; validate 0.77 s; GC of 5,000 in 0.85 s; survivor integrity intact | PASS |
| 33 MiB chunk body → `413` | **FAIL for blocking clients** (→ F3-1) |

One probe artifact for the record: a first 10k-scale run died with client-side
`EADDRNOTAVAIL` — my harness exhausting ephemeral ports (one TCP connection per
request), not a server defect; the keep-alive rerun above is the evidence of
record. Similarly, an apparent live-blob miscount in the mid-GC probe traced to
my own probe's bookkeeping (undeleted artifacts from earlier stages), exactly
the class of probe bug §2 of the v2 review warned about.

## 9.3 Findings log

### F3-1 · CONFIRMED · MEDIUM → −5
**Rejections issued before the body is read (late-chunk `409`, oversize `413`,
out-of-range/wrong-length `422`, `404`, `401`) are sent and then the connection
is closed without draining, so blocking send-then-read clients with large
bodies observe a connection error instead of the status.** Repro: 33 MiB body
via Python `http.client` → `BrokenPipeError`, deterministically, never the
promised `413`; a 1 MiB late chunk hit the same `EPIPE` intermittently (once
across runs). The `close=True` errors are raised before the body is consumed
([vaultdrop.py:333-344](model-1/src/vaultdrop.py#L333-L344),
[vaultdrop.py:905-906](model-1/src/vaultdrop.py#L905-L906)) and the handler
neither drains `rfile` nor lingers
([vaultdrop.py:967-973](model-1/src/vaultdrop.py#L967-L973)). Mitigations that
cap severity: the response is **always written before close** — a raw-socket
client that reads while sending received the full `409`/`413` bytes in every
trial (verified at byte level), so curl-class clients observe correctly — and
every data-level invariant held under the late-PUT storm (artifact byte-exact
afterwards). **Undoubled:** register row 5's named mechanism (state freeze →
`409`) holds and the status is emitted; what breaks is delivery to one client
class. Contrast v2's V2-1 (MEDIUM ×2): that server neither drained *nor
closed* and its own suite flaked; this one's deliberate close discipline is
half the fix. Flagged for the second reviewer: if you read register row 5's
"late PUTs return 409" as promising *observability* to any conformant client,
this doubles to −10. Remediation: bounded drain (Content-Length is already
known and capped) before error responses.

### F3-2 · CONFIRMED · LOW → −2
**No socket or request timeouts anywhere.** Neither the handler class nor any
accept path sets a timeout ([vaultdrop.py:796-805](model-1/src/vaultdrop.py#L796-L805);
no `settimeout`/`timeout` in the file), and body reads block indefinitely
([vaultdrop.py:836](model-1/src/vaultdrop.py#L836),
[vaultdrop.py:353](model-1/src/vaultdrop.py#L353)). Repro: open a PUT, send
headers declaring a body, send nothing — the server thread and fd stay pinned
for as long as the socket lives (verified; service remains responsive to other
clients meanwhile). A trickle of hostile/broken clients accumulates threads
and fds without bound. Operability; not register-claimed.

### F3-3 · PLAUSIBLE · 0
**Burst-load resident memory transiently exceeded the envelope number.** 15k
rapid empty `GET /artifacts` requests at 24-way keep-alive concurrency drove
RSS 31 → **684 MiB**, recovering to 122 MiB at idle; artifact-creation waves
peaked lower (≈500 MiB) and also receded. No *confirmed* violation: the
envelope and register row 16 bind **single operations**, and the operation the
brief names (multi-GiB round-trip) peaked at 38 MiB. Recorded because a
reviewer who reads the 512 MiB ceiling as a service-wide bound will fail it
under sustained metadata load. Likely contributor, for the second reviewer:
per-request SQLite connections are never explicitly closed — reclamation rides
on CPython GC ([vaultdrop.py:140-147](model-1/src/vaultdrop.py#L140-L147);
`contextlib.closing` is imported and never used, suggesting abandoned intent).

### REJECTED (attacked, held — recorded to stop re-chasing)
- **Finalize-vs-GC coordination** (register row 11). Per-hash lock held across
  blob replace + reference commit on the finalize side
  ([vaultdrop.py:498-541](model-1/src/vaultdrop.py#L498-L541)); GC re-checks
  `refcount=0` inside its write transaction under the same sorted-batch locks,
  renames to durable trash before the metadata delete, unlinks only after
  commit ([vaultdrop.py:660-704](model-1/src/vaultdrop.py#L660-L704)). Traced
  all orderings — the global acquisition order (sorted blob-locks → DB writer)
  is consistent across finalize/delete/GC/download, so no deadlock — and
  14/14 live race rounds passed. Unlike v2's F-V2-GC, the race guard reads
  actual row state, not a cursor truthiness.
- **Crash safety of all three subsystems.** Chunk: temp → fsync → rename +
  dir-fsync + manifest insert in one `BEGIN IMMEDIATE` commit
  ([vaultdrop.py:346-393](model-1/src/vaultdrop.py#L346-L393)). Finalize:
  staging fsync → prefix-dir persist → atomic replace → dir fsync → single
  `synchronous=FULL` commit of blob+artifact+refcount+state. GC: trash rename
  durable before row delete, trash unlink after commit. Recovery
  ([vaultdrop.py:175-261](model-1/src/vaultdrop.py#L175-L261)) resets
  `finalizing`, recomputes refcounts from artifacts, restores or discards
  trash by reference state, and removes uncommitted chunk/blob files —
  verified live by the randomized kill sweep, the post-200 kills, and the
  mid-GC kill's exact metadata↔disk agreement.
- **Read-boundary integrity** (row 8): full streaming re-hash before headers,
  from an already-open fd, lookup repeated under the content lock; corrupt /
  truncated / missing all refuse as JSON `500`. Content-Length doubling of
  read I/O is disclosed in the README.
- **Conflicting replay** (row 4): first manifest row wins inside the write
  transaction; rename only for the winner; disk verified.
- **At-most-one artifact** (row 6): `uploads.state` CAS + `UNIQUE(upload_id)`;
  4-way race converged; losers got a coherent `409`/same-ID answer.
- **Isolation depth** (rows 1–2): tenant-scoped predicates on every query,
  128-bit random IDs, byte-identical 404s (verified), admin surface exposes
  counts only, and the dedup path does the full staged write + fsync + replace
  even on a hit — measured 1.146×, matching the disclosure.
- **Scale envelope** (rows 13–16): all four held under direct measurement
  (table above). Concurrent distinct 192 MiB finalizes overlapped (0.46 s for
  three vs 0.57 s serialized estimate — disk-bound but not serialized, and no
  shared lock exists on that path).

### Judgment-layer notes (not scored)
- Abandoned uploads never expire; staged chunks for unfinalized uploads
  accumulate indefinitely (undisclosed). Finalized uploads' chunks *are*
  cleaned up — v2's undisclosed 2× retention cost is gone.
- `LockPool` never prunes (one `RLock` per content hash ever seen) — disclosed
  in-code as an accepted bound.
- `ThreadingHTTPServer` remains a non-production substrate; combined with
  F3-2 it is the operational soft spot of an otherwise robust design.
- Admin token compared with `!=` rather than a constant-time compare —
  theoretical, out of the brief's threat model.

## 9.4 Category scoring

- **Correctness (55):** 100 − 5 (F3-1) − 2 (F3-2) = 93% → **51.2**. Nothing
  above the HTTP boundary layer was confirmed; promises 1–4 all held under
  live attack.
- **Data model & mechanism (20): 18.** Schema encodes its invariants (CHECKed
  states and refcounts, `UNIQUE(upload_id)`, FK from artifacts to blobs,
  covering indexes); 256-way prefix sharding sized against both the 10k-file
  and the GC-fsync problem; one durable-write discipline reused by chunk,
  finalize, and GC-trash paths; startup recovery that reconciles *both*
  directions (files without rows, rows without files) and recomputes
  refcounts. The trash-rename GC protocol is simpler than v2's state machine
  and, unlike it, survived its crash window. Docked: GC-reliant DB connection
  lifecycle, no upload expiry.
- **Tenant isolation (10): 9.5.** Everything verified; the residual timing
  delta is disclosed, measured small (1.146×), and structurally minimized by
  the equalized write path. Same posture as the strongest prior submissions.
- **Test quality (8): 5.** Five tests, all honest: the mid-chunk SIGKILL
  genuinely lands mid-body; the concurrency test races duplicate chunks *and*
  finalizes; corruption and post-finalize-durability restarts are real. But
  the two hardest crash classes — **crash during finalize and crash during
  GC — have no test at all**, there is no finalize-vs-GC race test, no
  wire-limit test (which would have caught F3-1), and the conflict test
  asserts the `409` without checking on-disk preservation. The brief's stated
  minimum is met; the suite verifies far less than the register claims.
- **Architecture, clarity, operability (7): 6.** A 1,038-line stdlib-only
  single file that reads like a design document — comments state the invariant
  each block enforces, README limits match enforced limits exactly, register
  maps 1:1 to code. Costs: F3-2's operational surface, GC-dependent connection
  cleanup, single-file packaging at the edge of comfortable.

**Total: 51.2 + 18 + 9.5 + 5 + 6 = 89.7 → 90/100.**

## 9.5 Limitations of this review

- Single reviewer, n = 1 generation, unreconciled — protocol §7 requires two
  generations and §6 a second blind review before these numbers are final.
  Every CONFIRMED finding above carries a repro; F3-1's severity reading and
  F3-3's envelope interpretation are the two explicit open questions.
- Crash model exercised is the brief's `kill -9`; fsync ordering was verified
  by trace, not power-loss injection. (Per the §8.1 M1-1 precedent, missing
  parent-dir fsyncs would be power-loss hardening notes, not findings — none
  were needed here; the submission fsyncs parent directories at every rename.)
- The 10 GiB ceiling was probed by declaration and by a 2 GiB physical
  round-trip, not a full 10 GiB transfer; the streaming path is
  size-independent by construction (fixed 1 MiB buffers throughout).
- No identity speculation: this submission is scored as `model-1`, full stop.

## 9.6 Bottom line

This is the strongest submission the project has produced. It keeps every
discipline that distinguished the round-1 winner — first-write-wins chunks,
equalized dedup timing, read-boundary re-hashing, honest register — while
meeting the scale envelope that submission never faced (38 MiB resident on
multi-GiB work, no global locks, sub-second 10k-blob operations), and its
crash story survived a randomized kill sweep, a mid-GC kill over 440 blobs,
and 14 live finalize-vs-GC races without a scratch. Its two confirmed defects
live at the HTTP boundary (error-status delivery to blocking clients; missing
timeouts), both small, both mechanically fixable. The gap between the depth of
its mechanisms and the thinness of its 5-test suite is the one place ambition
visibly outran verification — the register makes 16 claims and the suite
checks perhaps six.

---

# §10 — `qwen-v3` submission — Hostile Execution Review (Reviewer: Fable, 2026-08-16)

Independent review of the submission at `qwen-v3/` (six-module Python package,
~1,260 source lines + 18-test suite) under `review-protocol.md` and the
**current** `product.md` (scale envelope in the interface contract). All work
in a scratch copy; findings graded CONFIRMED / PLAUSIBLE / REJECTED; only
CONFIRMED findings move the correctness score. **Single-reviewer, n = 1
generation, unreconciled** — repros and line-cited traces provided throughout
for the second reviewer.

## 10.0 Verdict and ranking

| | Total | Correctness (55) | Data model (20) | Isolation (10) | Tests (8) | Judgment (7) |
|---|---:|---:|---:|---:|---:|---:|
| qwen-v3 | **82 / 100** | 41.8 | 17.5 | 9.5 | 7.5 | 6 |

Numeric rank across every submission reviewed in this project (prior rounds at
their reconciled totals; the two most recent Fable-independent, pending
reconciliation):

| Rank | Submission | Score | Brief version |
|---:|---|---:|---|
| 1 | `model-1` (new, §9) | 90 (independent) | current (scale envelope) |
| 2 | `qwen-3-8-27b` (round 1) | 90 reconciled | round-1 (no envelope) |
| **3** | **`qwen-v3` (§10)** | **82 (independent)** | current (scale envelope) |
| 4 | `opus-4-6` (round 1) | 56 reconciled | round-1 |
| 5 | `qwen-3-8-27b-v2` | 47 reconciled | current (scale envelope) |

**Among the three submissions graded against the harder current brief, qwen-v3
is a clear second** (model-1 90 > qwen-v3 82 > qwen-3-8-27b-v2 47). It fully
clears the scale envelope that sank v2 — 38 MiB resident on a 2 GiB round-trip,
no validation stall, sub-second 10k-blob operations — and its crash and
finalize-vs-GC machinery survived the full hostile battery without a single
correctness failure. What separates it from the 90-scoring `model-1` is two
confirmed defects at the read/HTTP boundary: it serves corrupted stored bytes
(a disclosed gap), and it desyncs keep-alive framing on its non-chunk reject
paths (a register-claimed invariant, broken → doubled). The raw 82 understates
it relative to the round-1 90s, which were earned before the scale envelope
existed; on a same-brief basis this is the second-strongest submission the
project has produced. Rank is provisional per §7 (n = 1, single reviewer).

## 10.1 Mechanical floor — PASS

- `./vaultdrop migrate` exits 0, is idempotent, builds the full state tree
  (`blobs/ chunks/ staging/ vaultdrop.db`); `serve` runs foreground on `$PORT`,
  self-initializes, and runs crash recovery before accepting traffic.
- **Own suite: 18 tests, green (~24 s), no flake observed.** Coverage is broad
  and genuinely adversarial (see 10.4): real mid-operation SIGKILLs for chunk
  write, finalize (256 MiB mid-assembly), and GC (20k-blob mid-unlink); a live
  finalize-vs-GC race; concurrent duplicate-index chunks; cross-tenant dedup
  invisibility; a 2 GiB streaming RSS-ceiling test; 10k-blob responsiveness.
- DECISIONS.md: 1,288 words (≤1,500 ✓), 13-row claims register covering every
  §3 scenario and the scale envelope, plus an honest-notes section that
  discloses the read-path integrity gap, the restart-to-rotate-tokens
  limitation, and the bounded coordination waits.

## 10.2 Probe evidence

~70 probes across seven stages, driven through the interface contract (scripts
`v3_a`, `v3_desync{,2,3}`, `v3_bc`, `v3_e`, `v3_fg`, `v3_misc` in the review
scratchpad). Highlights:

| Probe | Result |
|---|---|
| Out-of-order/duplicate concurrent chunks; assembly; round-trip | PASS |
| Conflicting replay → `409`, **first bytes verified intact on disk** (`os.link` never overwrites); exact replay → `200` | PASS |
| Foreign vs unknown ID: byte-identical `404` (uploads, artifacts, delete, finalize) | PASS |
| Admin surface: tenant token → `404` (no existence leak), unknown → `401`, admin token on tenant routes → `401` | PASS |
| Input validation: string/bool/negative/oversize `total_size`, zero/oversize `chunk_size`, missing name → typed `4xx` | PASS |
| Wire limits: `total_size` > 10 GiB → `413`; `chunk_size` > 32 MiB → `400`; **33 MiB chunk body → observable `413`** (drained; blocking clients see it, unlike model-1's EPIPE) | PASS |
| Finalize semantics: mismatch `422` then resume; idempotent re-finalize → same ID; declared-size mismatch → `422`; deleted-then-refinalize → `409` | PASS |
| Empty (0-byte) artifact round-trip | PASS |
| 4-way concurrent finalize → all `200` with **one** shared artifact ID (losers poll and return the winner's), one artifact row | PASS |
| **Finalize-vs-GC race** (refcount-0 blob, GC vs identical finalize), 14 rounds | **14/14 — reference wins** |
| Distinct concurrent finalizes overlap (3 × 192 MiB): 0.23 s vs ~0.36 s serialized estimate, no shared lock | PASS |
| Dedup timing channel: novel 2.54 ms vs dup 2.42 ms = **1.05×** over 8 rounds | PASS |
| SIGKILL mid-chunk → restart: `received: []`, resumable, zero stray `.tmp` | PASS |
| **Randomized SIGKILL across the finalize window, 10 rounds** (256 MiB): every round committed+downloadable or rolled-back(`open`)+retryable | **10/10** |
| SIGKILL immediately after finalize `200` ×3 → durable | 3/3 |
| **SIGKILL mid-GC** (6,000 collectable + 40 live, kill mid-mark/unlink): 0/40 live damaged; **metadata↔disk exactly consistent** (0 rc>0 rows missing files, 0 orphan files); validate clean; follow-up GC completes | PASS |
| 2 GiB round-trip, RSS watched: **peak 38 MiB** (bound 512) | PASS |
| No-stall: download/finalize mid-`/admin/validate` over the 2 GiB store: 1.0 ms / 1.3 ms vs 0.45 ms unloaded | PASS |
| 10,000 blobs: populate 8 s; list 0.02 s; `/admin/blobs` 0.01 s; validate 0.51 s; GC of 5,000 in 0.86 s; peak RSS 71 MiB | PASS |
| Burst load (15k `/health` + 15k `/artifacts`, 24-way): RSS flat 27→30 MiB (per-thread connection reuse; **no burst-RSS issue** — improves on model-1's §9 F3-3) | PASS |
| Corrupt/truncated/missing stored blob → download outcome | **FAIL** (→ V3-1) |
| Keep-alive framing on non-chunk reject paths | **FAIL** (→ V3-2) |

## 10.3 Findings log

### V3-1 · CONFIRMED · HIGH → −12 (disclosed → not doubled)
**A corrupted stored blob is served `200` with the corrupted bytes — including
after the submission's own validator has flagged it; a truncated or missing
blob hangs the client instead of failing cleanly.** Downloads stream the file
straight to the wire with the declared `Content-Length` and no digest/length
verification ([httpd.py:271-289](qwen-v3/src/httpd.py#L271-L289)). Repro
(`v3_fg`):
1. Finalize any artifact; bit-flip one byte in `blobs/<hh>/<hash>`.
2. `GET /artifacts/{id}` → `200`, correct `Content-Length`, **corrupted bytes**.
3. `GET /admin/validate` → `{"scanned":1,"ok":0,"mismatch":1}` — detected.
4. `GET /artifacts/{id}` again → still `200`, still corrupted. `validate_pass`
   sets `validated=0` ([blobs.py:248-249](qwen-v3/src/blobs.py#L248-L249)) and
   the download path never consults it.
5. Truncate the blob → the server sends `200` + full `Content-Length`, delivers
   the short body, and **stalls** (client `IncompleteRead`/timeout). Delete the
   file → `200` header already committed, then 0 bytes + close.

Promise 2 is unconditional ("returns the exact bytes whose SHA-256 equals the
declared digest, **or it fails**"), and the product ships `/admin/validate`
precisely because it contemplates stored-content corruption. Register row 2
titles the invariant "No corrupt or partial reads," **but the honest-notes
section explicitly discloses** "downloads do not re-verify digests on the wire
(single-pass streaming)." Per the protocol's honesty rule a disclosed gap
scores at base severity, **undoubled**. Severity HIGH (not CRITICAL): the
trigger is out-of-band filesystem corruption, not an API-reachable interleave.
This is the single axis on which the §9 `model-1` submission is strictly
better — it re-hashes at the read boundary and returns `500`. The
truncation/missing-file **hang** is an aggravating facet of the same root cause
(headers committed before the file is opened or sized), noted here rather than
scored separately. Remediation is one conditional: refuse to stream a
`validated=0` blob, and verify size before sending headers. **Severity split
flagged for the second reviewer:** a reviewer who holds that disclosure cannot
waive promise 2 against a register row that positively claims the invariant
would grade this CRITICAL ×2 (as GPT did on the identical class in §8/§6);
under that ledger correctness falls by an additional ~38 points.

### V3-2 · CONFIRMED · MEDIUM ×2 (claims-register doubling) → −10
**Keep-alive framing desyncs on every non-chunk early-reject path**, breaking
register row 13 ("Keep-alive framing never desyncs"; mechanism: "Early-reject
paths drain the bounded unread remainder **before** responding"). The drain
helper `_reject_with_body` ([httpd.py:96-107](qwen-v3/src/httpd.py#L96-L107))
is wired **only** into `_put_chunk`. The auth failure
([httpd.py:171-172](qwen-v3/src/httpd.py#L171-L172)), the admin-vs-tenant
`404` ([httpd.py:166-168](qwen-v3/src/httpd.py#L166-L168)), and the
JSON-endpoint `400`/invalid-body rejects
([httpd.py:202-203](qwen-v3/src/httpd.py#L202-L203)) all call `self._error(...)`
with the request body still unread. Repro (`v3_desync3`, two-phase, framed by
`Content-Length`):
1. On one socket, send `PUT /uploads/{id}/chunks/0` with `Authorization:
   Bearer nobody` and a 4 KiB body → `401`.
2. Reuse the socket for `GET /health` → the server parses the leftover body as
   the next request line and returns **`501 Unsupported method
   ('zzzz…GET')`** — the client's legitimate request gets a garbage response and
   the connection is poisoned.
3. The same fires for `POST /admin/gc` with a tenant token (`404`) and `POST
   /uploads` with a body the JSON layer rejects (`400`).

The chunk path itself was verified to **hold** (a `409`/`413` chunk reject
drains and the next request on the socket succeeds), so row 13's own cited
reviewer-check passes — but the claim's scope ("never desyncs") is broken on
the paths it forgot to wire. Register-claimed and broken → **doubled**. MEDIUM
base: the blast radius is one client's own reused connection on an error path,
recoverable by reconnecting; single-node, so there is no proxy and no
request-smuggling path to another tenant. **Alternative reading for the second
reviewer:** LOW ×2 = −4 if the desync is judged pure operability. Remediation:
route the auth/admin/JSON rejects through the same bounded-drain-or-close
discipline the chunk path already uses.

### V3-3 · CONFIRMED · LOW → −2
**`GET /uploads/{id}` lists a chunk index as `received` when its bytes are
absent, after the crash window row 3 itself describes.** Placement commits the
chunk row *before* `os.link`s the file into place
([uploads.py:140-163](qwen-v3/src/uploads.py#L140-L163)); a crash in between —
or the deterministic residue the submission's own `test_crash_chunk_self_heal`
constructs — leaves a row with no file. `get_upload`
([uploads.py:61-71](qwen-v3/src/uploads.py#L61-L71)) reports that index in
`received` purely from the row. Repro (`v3_a` A11): inject the row-without-file
residue → `GET /uploads/{id}` returns `received:[0]`, and `finalize` returns
`422 "chunks incomplete"`. A resumable client that trusts `received` (the
normal contract for a resumable upload) will skip re-PUTting index 0 and loop
on `422`; only a client that blindly re-PUTs heals. Promise 3 ("no partial
chunk is treated as complete") is in tension with this — the *finalize* is
safe, but the *resumability signal* misreports. Row 3 discloses the
row-without-file state and the healing mechanism, so LOW/undoubled; **flagged
as a possible MEDIUM** for a reviewer who reads `received` as a hard
resumability contract. Remediation: verify file presence when building
`received`, or link before committing the row.

### REJECTED (attacked, held — recorded to stop re-chasing)
- **Finalize-vs-GC coordination** (rows 5/7). The blob state machine
  (`active`/`collecting` + `gc_pass_id`) coordinates without an in-process
  lock: GC marks `active∧refcount=0 → collecting` in a short txn, unlinks with
  no lock, then deletes only `gc_pass_id=? ∧ state='collecting' ∧ refcount=0`;
  refcount increments target `state='active'` only, and a finalize that finds
  its digest `collecting` waits for the row to clear
  ([blobs.py:114-225](qwen-v3/src/blobs.py#L114-L225)). Traced every ordering —
  the increment's `rowcount != 1` retry and the deletion's re-verified predicate
  close the windows — and 14/14 live race rounds plus the mid-GC kill passed.
  (This is the mechanism v2's F-V2-GC got wrong via a dead guard; here the guard
  reads real rowcounts, and recovery reverts `collecting→active` only when the
  file survives.)
- **Crash safety, all three subsystems.** Chunk: tmp fsync → row commit → link
  → dir fsync. Finalize: staging fsync → blob link + dir fsync → one
  `BEGIN IMMEDIATE` commit of blob+artifact+upload-state (`synchronous=FULL`).
  GC: files unlinked before rows deleted; `collecting` rows reconciled at
  startup. Verified by the 10/10 randomized finalize-window sweep, 3/3 post-200
  kills, and the mid-GC kill's exact metadata↔disk agreement (0 missing, 0
  orphan).
- **Concurrent finalize / at-most-one artifact** (row 5): conditional
  `UPDATE…WHERE state='open'` arbitration; 4-way race → one artifact, losers
  return the winner's ID.
- **Conflicting replay** (row 4): `INSERT OR IGNORE` + readback compare; `os.link`
  never overwrites; first bytes verified intact on disk after `409`.
- **Isolation depth** (row 1): tenant-scoped predicates, UUID4 IDs,
  byte-identical 404s, admin surface counts-only, 1.05× dedup timing.
- **Scale envelope** (rows 9-12): all held under direct measurement — 38 MiB
  RSS on 2 GiB, no validation stall, 10k operations sub-second, wire limits
  enforced and (for the oversize chunk body) observably so.

### PLAUSIBLE / judgment notes (not scored)
- **`serve` with a missing or malformed `tenants.json` starts anyway** with an
  empty registry (every request → `401`), because `load_tenant_registry`
  swallows `OSError`/`ValueError` ([httpd.py:31-47](qwen-v3/src/httpd.py#L31-L47)).
  The brief guarantees the harness writes the file first, so this is not a
  scored contract violation, but it is a silent-misconfiguration wart — the §9
  `model-1` submission fails fast with a clear error instead.
- Abandoned uploads and their chunk files never expire.
- Finalize under sustained GC contention returns a retryable `500` after a
  bounded wait (300 s) rather than blocking indefinitely — disclosed, but a
  `500` for a coordination stall is a coarse signal.

## 10.4 Category scoring

- **Correctness (55):** 100 − 12 (V3-1) − 10 (V3-2) − 2 (V3-3) = 76% → **41.8**.
  Promises 3 and 4 held under the full crash/concurrency battery; the deductions
  are the read-path integrity gap (promise 2) and the HTTP-framing/`received`
  edges.
- **Data model & mechanism (20): 17.5.** Schema encodes its invariants (state
  CHECKs, `refcount≥0`, `(state,refcount)` GC index, FKs); `os.link`-based
  placement gives write-once idempotency for free; the `collecting`-state GC
  protocol is a genuinely correct lock-free-ish coordination that survived
  every attack; startup recovery reconciles both directions (orphan files,
  rows-without-files, stuck `collecting`/`finalizing`). Docked half a point
  more than model-1's 18 for the read path committing headers before verifying
  the file, and the bounded-wait-then-`500` finalize under GC contention.
- **Tenant isolation (10): 9.5.** Byte-identical 404s, sealed admin surface,
  measured 1.05× dedup timing; nothing confirmed against it.
- **Test quality (8): 7.5 — the strongest suite in the project.** 18 tests that
  land real mid-operation SIGKILLs at all three crash sites (including a
  256 MiB mid-assembly finalize kill and a 20k-blob mid-unlink GC kill), a live
  finalize-vs-GC race, duplicate-index concurrency, conflict **byte-preservation**
  assertions, and both scale-envelope probes (2 GiB RSS ceiling, 10k
  responsiveness). Docked 0.5 for fixed-sleep crash timing (could occasionally
  land post-op on a faster host) and for the suite having no test that reaches
  its own two confirmed defects (no corrupt-blob download test, no keep-alive
  reuse test).
- **Architecture, clarity, operability (7): 6.** Clean six-module split with
  docstrings that state each invariant, accurate README, honest DECISIONS
  notes, a real per-request HTTP timeout (300 s — better than model-1's none),
  and per-thread connection reuse that avoids the burst-RSS growth model-1
  showed. Costs: the read-path header-before-verify wart, the desync gap, and
  the silent empty-registry startup.

**Total: 41.8 + 17.5 + 9.5 + 7.5 + 6 = 82.3 → 82/100.**

## 10.5 Limitations of this review

- Single reviewer, n = 1 generation, unreconciled — §7 requires two generations
  and §6 a second blind review before these numbers are final. Every CONFIRMED
  finding carries a repro; the two explicit open questions are V3-1's severity
  (HIGH-disclosed vs CRITICAL-doubled) and V3-2's severity (MEDIUM×2 vs LOW×2).
- Crash model exercised is the brief's `kill -9`; fsync ordering verified by
  trace, not power-loss injection (per the §8.1 precedent, missing parent-dir
  fsyncs would be power-loss notes, not findings; the submission fsyncs
  directories at every link/rename).
- The 10 GiB ceiling was probed by declaration and a 2 GiB physical round-trip;
  the streaming path is size-independent by construction (fixed 1 MiB buffers).
- No identity speculation: scored as `qwen-v3`, full stop.

## 10.6 Bottom line

qwen-v3 is the second-strongest submission the project has produced and a clear
second among the three graded against the scale envelope. Its crash and
finalize-vs-GC story is airtight — 10/10 randomized finalize-window kills, a
mid-GC kill over 6,000 blobs with exact metadata↔disk consistency, 14/14 live
reference-wins-GC races — and it meets the full envelope (38 MiB resident on
2 GiB, no stalls, sub-second at 10k blobs) that broke the v2 generation. Its
test suite is the most adversarial in the field. It falls short of the
90-scoring `model-1` on exactly one axis that matters and two smaller ones: it
serves corrupted stored bytes where `model-1` re-hashes and fails (a gap it
honestly discloses), and it lets keep-alive framing desync on the auth/admin/
JSON reject paths its own register promised would never desync. Both are
one-conditional fixes at the read/HTTP boundary; neither touches the core
storage invariants, which held.
