# VaultDrop — Hostile Execution Review (Reviewer: Fable)

Independent review of submissions `model-1` and `model-2` under `review-protocol.md`
and the `product.md` brief. All work was done in a scratch copy; every finding
below is graded CONFIRMED / PLAUSIBLE / REJECTED per §4 of the protocol, and only
CONFIRMED findings move the correctness score. Sections 1–7 are the original
independent review, preserved unchanged for the record; **§8 is the
reconciliation with the second independent review (`analysis-gpt.md`)** and
supersedes the headline numbers below.

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
