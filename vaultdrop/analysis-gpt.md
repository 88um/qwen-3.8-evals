# VaultDrop hostile execution review — GPT report with final reconciliation

Date: 2026-08-16

## Final reconciled ranking

| Rank | Submission | Reconciled score | Alternative under GPT severities | Fable independent | GPT independent |
|---:|---|---:|---:|---:|---:|
| 1 | **Model 1** | **90 / 100** | **77 / 100** | 93 | 78.4 |
| 2 | **Model 2** | **56 / 100** | **31 / 100** | 69 | 28.7 |

The ranking is unanimous across both independent reviews and every unresolved severity choice: **Model 1 first, Model 2 second**.

The reconciled totals use Fable's severity ledger, the merged set of mechanically verified findings, and averaged non-correctness categories. The alternative totals preserve GPT's harsher unresolved severities rather than forcing agreement, as required by `review-protocol.md` §6. The full per-finding reconciliation is also recorded in `analysis-fable.md` §8.

## Mechanical floor

Both submissions clear the floor:

| Check | Model 1 | Model 2 |
|---|---|---|
| `migrate` and `serve` | PASS | PASS through the documented wrapper |
| Candidate-authored tests | **22/22 passed** | **21/21 passed** |
| Claims register | PASS, 1,049 words | PASS, 1,263 words |
| Exact primary build command | PASS | LOW finding: `go build -o vaultdrop .` collides with the wrapper |

## Reconciled scoring

Correctness uses `max(0, 100 − confirmed finding weights)` and is scaled to the brief's 55-point correctness category. The remaining 45 points are averaged across the two independent reviews.

| Submission | Correctness | Data model | Isolation | Tests | Architecture | Total |
|---|---:|---:|---:|---:|---:|---:|
| Model 1 | 91% → 50.1 | 17.15 | 9.05 | 7.55 | 6.15 | **90** |
| Model 2 | 47% → 25.9 | 14.25 | 7.05 | 4.25 | 5.05 | **56** |

Under the unresolved GPT severity ledger, Model 1's correctness becomes 67% and Model 2's becomes 0%; the same averaged categories then produce **77** and **31**.

## Final finding ledger — Model 1

### Confirmed and scored

| Finding | Severity | Weight | Evidence |
|---|---|---:|---|
| Accepted upload can be impossible to complete | MEDIUM, concealment | −5 | `start_upload` accepts `total_size=chunk_size=20 MiB`, but PUT caps every body at 16 MiB. Live repro: POST 201; only legal chunk PUT 413. `model-1/src/vaultdrop/uploads.py:55-61,90-94`. |
| Malformed scalar input becomes 500 | LOW | −2 | `{"total_size":"abc"}` reaches unguarded `int()` conversion and becomes `500 internal: ValueError`. `model-1/src/vaultdrop/httpd.py:114-121`. |
| Body is buffered before limit enforcement | LOW | −2 | `_body` reads the complete declared body before the 16 MiB check. `model-1/src/vaultdrop/httpd.py:53-55`. |

Reconciled Model 1 correctness: `100 − 5 − 2 − 2 = 91`.

### Reclassified or rejected

- **PLAUSIBLE — residual dedup timing, zero score.** Both reviewers measured small branch differences, but neither demonstrated a usable classifier. The register's actual claim—bounded, best-effort, and not a clean signal—holds as written.
- **DROPPED — missing parent-directory fsync.** The earlier trace incorrectly claimed a completed `rename()`/`mkdir()` could be lost merely by killing the userspace process. SIGKILL does not discard the kernel's filesystem cache. The issue remains a power-loss/kernel-crash hardening note, outside the brief's explicit process-crash probes. GPT's original −24 dissent is retained only in the alternative score.
- **REJECTED — conflicting replay overwrite.** Model 1 checks under its per-upload lock before mutation, and its test proves the first bytes still finalize and download.
- **REJECTED — finalize-vs-GC race.** The global lock spans the metadata decision and filesystem effect on both sides.
- **REJECTED — corrupt read.** Model 1 hashes and length-checks the complete blob before sending status or bytes.

## Final finding ledger — Model 2

### Confirmed and scored under the reconciled severity ledger

| Finding | Reconciled severity | Weight | Evidence |
|---|---|---:|---|
| Corrupt blob served with HTTP 200 | HIGH ×2 | −24 | Download streams without checking hash, length, or validation state. Same-length corruption was returned as 200 bytes. `model-2/handlers.go:375-385`. |
| Conflicting replay overwrites the first chunk | MEDIUM, concealment | −5 | Existing-chunk path calls `WriteChunk`, which renames over the canonical file before comparing hashes. Repro: `Hello`, conflicting `World` → 409, then original finalize → 422. `model-2/handlers.go:151-175`; `model-2/blob.go:84-90`. |
| Cross-tenant dedup timing channel | MEDIUM, concealment | −5 | Existing content skips rename and directory fsync. Independent probes measured 1.55× and 1.53× novel/dedup median ratios. `model-2/blob.go:143-157`. |
| Finalize ignores upload `total_size` | MEDIUM, concealment | −5 | A five-byte declaration accepted one ten-byte chunk and finalized it with 200 when the finalize body claimed ten. `model-2/handlers.go:322-327`. |
| Crash-during-chunk test never starts PUT | LOW | −2 | Test creates the upload and immediately SIGKILLs. `model-2/vaultdrop_test.go:927-942`. |
| Finalize-vs-GC test is sequential | LOW | −2 | GC completes before finalize begins. `model-2/vaultdrop_test.go:726-739`. |
| Dependencies are not vendored | LOW | −2 | `modernc.org/sqlite` and transitive modules are absent from a `vendor/` tree despite the brief's instruction. |
| Claimed `BEGIN IMMEDIATE` is absent | LOW | −2 | Default `BeginTx` plus `SELECT 1` is not immediate; single-connection serialization preserves convergence through another mechanism. `model-2/store.go:196-205`. |
| Stated 10 MB wire limit is unenforced | LOW | −2 | `WriteChunk` performs unbounded `io.Copy` from the request body. A 17 MiB body was accepted. `model-2/blob.go:71-89`. |
| Validator cannot detect orphan files | LOW | −2 | It iterates DB rows only; an orphan has no row. `model-2/gc.go:58-79`. |
| README primary build command fails | LOW | −2 | `go build -o vaultdrop .` fails because `vaultdrop` already exists as a non-object wrapper. |

Reconciled Model 2 correctness: `100 − (24 + 5 + 5 + 5 + 2 + 2 + 2 + 2 + 2 + 2 + 2) = 47`.

### Dropped or rejected

- **DROPPED — parent-directory fsync gaps.** Same flawed SIGKILL trace as Model 1. Missing parent fsync and swallowed sync errors remain valid power-loss hardening concerns, but move zero score under the stated process-crash model.
- **REJECTED — finalize-vs-GC deletion race.** The per-hash lock plus post-lock DB recheck closes both orderings and survived repeated live races.
- **REJECTED — duplicate artifact creation.** Single-connection database serialization and transactional state recheck converge concurrent finalizers.

## Surviving severity splits

The facts below are agreed; only their protocol weights remain unresolved.

| Model 2 finding | Fable severity | GPT severity |
|---|---:|---:|
| Corrupt bytes returned with 200 | HIGH ×2 = −24 | CRITICAL ×2 = −50 |
| Replay overwrites first-written staging bytes | MEDIUM concealment = −5 | HIGH ×2 = −24 |
| Observable cross-tenant timing channel | MEDIUM concealment = −5 | CRITICAL = −25 |

Fable's corrupt-read severity reflects that the trigger requires out-of-band filesystem corruption. GPT's severity follows the table's literal inclusion of “silent corruption.” For replay overwrite, Fable limits blast radius to recoverable upload staging and notes that the register claims only conflict detection; GPT reads atomic first-write preservation as part of the registered chunk mechanism. For timing, Fable treats the leak as existence of already-known bytes; GPT treats any promise-1 existence oracle as CRITICAL.

Per protocol, these splits are reported side by side. They change magnitude, not order: the reconciled ledger yields **90 vs 56**, and the GPT alternatives yield **77 vs 31**.

## Final verdict

Model 1 wins because its critical checks sit on the safe side of mutation and response boundaries: conflicts are decided before writes, reads are verified before 200, and its crash/concurrency tests actually reach the states they name. Model 2 has a genuinely sound per-hash finalize/GC protocol and better streaming scalability, but it mutates before resolving conflicts, returns bytes before verifying integrity, leaks dedup state through timing, and overstates several tests and mechanisms. Those confirmed differences dominate the numerical ranking.

---

## Addendum — scale-oriented v2 submission

Review target: `qwen-3-8-27b-v2/` against the revised scale-aware `product.md`.
This is a new, single-reviewer hostile pass; it has not been reconciled with the
other reviewer and does not replace the earlier two-submission reconciliation.

### Bottom line

The revision made **good scale architecture choices**, and the scale probe passes
comfortably. It did not, however, make the core safety architecture correct. Two
confirmed defects break promises 2–4: same-length corrupt bytes are served with
HTTP 200, and a reachable finalize/GC/SIGKILL interleaving permanently loses the
only bytes for a committed artifact. Both contradict explicit claims-register
rows, so the protocol's doubling rule applies.

Architectural concept: **7.5/10**. Hostile execution score under GPT's literal
CRITICAL severities: **34/100 provisional**. If both critical findings are instead
weighted HIGH, the alternative is **59/100**. The large difference is severity,
not disagreement about either defect.

### Mechanical floor and measured scale

| Check | Result |
|---|---|
| README-only run / `migrate` / `serve` | PASS |
| Candidate-authored tests | **31/31 passed** in 5.2 s |
| Dependencies | PASS: Python stdlib only |
| Claims register | PASS: 14 rows and explicit scale/durability sections |
| 10,000-artifact listing | PASS: 0.06 s |
| GC over 5,000 candidates | PASS: 0.84 s |
| Validation over 5,000 blobs | PASS: 0.37 s |
| Four concurrent 64 MiB finalizes | PASS: 0.11 s |
| 256 MiB round-trip service RSS | PASS: 27.2 MiB peak |

The scale decisions are substantively sound: all large byte paths stream in 1 MiB
blocks; blob paths are hash-sharded; SQLite WAL permits readers during writes;
assembly occurs outside the short claim transaction; distinct chunks/finalizes do
not share a global application lock; GC and validation iterate rather than loading
the full store. This is a real improvement, not scale language pasted over a
small-object implementation.

### Confirmed finding ledger

| ID | Finding | Severity / weight | Claim | Confirmation |
|---|---|---:|---|---|
| V2-1 | Same-length corrupted blob is served with `200`, including after validation marks it invalid | **CRITICAL x2 = -50** | #3, “No partial/corrupt reads” | Live repro below; `httpd.py:159-186`, `store.py:378-404` |
| V2-2 | GC can quarantine a newly referenced active blob; SIGKILL then makes recovery delete its only bytes | **CRITICAL x2 = -50** | #5, #8, #9 | Step-numbered interleaving and deterministic crash-remnant repro below; `store.py:346-375`, `db.py:169-198` |
| V2-3 | Crash-during-chunk test commonly kills after the PUT returned 200 | **LOW = -2** | test-quality finding | Exact test setup produced `thread_alive=False` and a completed 200 before its 0.12 s kill |
| V2-4 | Crash-during-GC test kills after GC returned 200 | **LOW = -2** | test-quality finding | Exact 20 x 1 MiB setup completed GC in 0.0036 s; at the test's 0.08 s kill point the worker had returned 200 |
| V2-5 | Validator's missing-file branch catches the wrong exception and cannot flag a missing blob | **LOW, concealment = -2** | required validator behavior omitted from register | Line trace: `core.py:97-100` converts `FileNotFoundError` to `ApiError(422)`, while `store.py:389-395` catches only `FileNotFoundError` |

Strict correctness: `max(0, 100 - 50 - 50 - 2 - 2 - 2) = 0%`.

#### V2-1 live repro

1. Upload and finalize a 65,536-byte artifact.
2. Modify one byte of its blob in place without changing the file length.
3. Call `/admin/validate`: it returns `200`, `mismatched: 1` and sets
   `validated=0`.
4. Download the artifact: the service returns `200`, the original content length,
   and a SHA-256 different from the artifact's declared digest.

Observed result:

```text
validate_status=200 mismatched=1
download_status=200 same_length=true digest_matches=false
```

The implementation checks only `stat().st_size` before sending the 200 headers.
It neither hashes the file nor consults `validated`. The honest limitation saying
downloads do not re-hash is useful disclosure, but it cannot waive promise 2 and
directly contradicts registered claim #3.

#### V2-2 confirmed interleaving

Precondition: content exists as `pending`, `refcount=0`, after its final artifact
was deleted. A new upload of the same bytes is ready to finalize.

1. GC's open cursor selects the pending candidate at `store.py:346-353`.
2. Finalize runs `_claim_recover` at `store.py:209-223,269-279`; its transaction
   changes the blob to `active`, increments `refcount` to 1, inserts the artifact,
   and returns 200.
3. GC executes its conditional tombstone `UPDATE` at `store.py:355-359`. It
   affects **zero rows**, because the blob is now active.
4. `Database.txn` returns the SQLite cursor object, not its `rowcount`
   (`db.py:90-97`). A cursor is truthy, so `if not changed` at `store.py:360`
   does not stop GC.
5. GC renames the live canonical blob to `*.gcq.*` at `store.py:362-364`.
6. SIGKILL lands before the active-state check restores it at
   `store.py:368-371`.
7. On restart, recovery unconditionally deletes every quarantine file at
   `db.py:176-180`; it neither restores quarantined bytes for active rows nor
   detects active rows whose canonical file is absent.
8. The committed artifact and `active/refcount=1` row remain, but every download
   now returns `500 blob bytes unavailable`. The assembled finalize temp was
   already removed, so retry cannot repair the committed artifact.

A deterministic remnant reproduction—active blob row with `refcount=1`, committed
artifact, and its only bytes at the GC quarantine path—produced:

```text
precrash_row=('active', 1)
after restart: download_status=500 canonical_exists=false quarantine_exists=false
```

This is exactly the adversarial SIGKILL window the brief requires. The state-machine
idea is reasonable; the implementation and recovery protocol do not preserve it.

### Judgment scoring

| Category | Score | Assessment |
|---|---:|---|
| Correctness under concurrency/crash | 0.0 / 55 | Two claimed promise-class failures zero the strict correctness ledger |
| Data model and mechanism design | 13.5 / 20 | Good schema, transaction boundaries, sharding, streaming and intended state machine; cursor-result and recovery semantics invalidate the hardest mechanism |
| Tenant isolation | 9.0 / 10 | Tenant predicates, uniform 404s, per-tenant artifact IDs and unconditional assembly are strong; timing equalization remains approximate |
| Test quality | 5.5 / 8 | Broad API/concurrency/recovery coverage and a useful scale probe; two named crash tests do not reach their claimed kill windows, and neither critical defect is covered |
| Architecture, clarity, operability | 6.0 / 7 | Clear stdlib implementation and unusually good documentation; critical guarantees are overclaimed |
| **Provisional total** | **34 / 100** | Single-reviewer strict score; reconciliation pending |

Severity sensitivity:

| Corrupt read | GC crash loss | Result |
|---|---|---:|
| CRITICAL x2 | CRITICAL x2 | **34** |
| HIGH x2 | CRITICAL x2 | **45** |
| HIGH x2 | HIGH x2 | **59** |

### Rejected or zero-score concerns

- **REJECTED — scale envelope failure.** The provided scale probe passed all of
  its asserted thresholds, and code inspection supports bounded memory through
  the 10 GiB path.
- **REJECTED — global finalize serialization.** Large assembly and hashing are
  outside SQLite write transactions; only the short metadata claim serializes.
- **REJECTED — replay overwrite.** Same-index mutation is guarded, the committed
  row is checked before placement, and conflicting content leaves disk untouched.
- **REJECTED — duplicate artifact from concurrent finalize.** The unique upload
  constraint plus transactional refcount rollback converges both callers on one
  artifact ID.
- **PLAUSIBLE, zero score — residual dedup timing.** Novel content performs final
  placement/fsync while a dedup hit does not, but a 4-pair 128 MiB probe measured
  only a 1.03x median difference. That is not a demonstrated classifier.

### Architectural verdict and repair order

The revised submission chose the right broad shape for the scale envelope. Its
failure is not “Qwen added needless enterprise complexity”; it is that the two
most important safety boundaries still rely on assumptions the implementation
does not enforce.

1. **Repair GC before anything else.** Test `cursor.rowcount == 1` before touching
   bytes. Recovery must reconcile quarantine files with the blob row: restore a
   quarantine for `active/refcount>0`, delete it only for a committed tombstone,
   and explicitly detect active rows missing canonical bytes.
2. **Verify before committing a download response.** Open the blob, hash and
   length-check it before sending status/headers, then rewind the same open file
   descriptor and stream it. At minimum, `validated=0` must make download fail,
   but that alone does not detect corruption occurring between validation passes.
3. **Make crash tests phase-aware.** Add deterministic failpoints/barriers after
   quarantine rename, after blob placement, and before metadata commit. Kill only
   after the child confirms it reached the target phase; fixed sleeps are not
   evidence of a mid-operation crash.
4. **Use operation-specific hashing errors.** The general chunk helper should not
   translate a missing blob into a chunk-specific 422 that bypasses the validator's
   missing-file accounting.

With those repairs, this architecture could score above the original compact
submission because its streaming, sharding, lock granularity and scale behavior
are substantially better. In its current form, the architectural direction is
good but the core promises are not yet trustworthy.
