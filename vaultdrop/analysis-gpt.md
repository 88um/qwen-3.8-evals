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
