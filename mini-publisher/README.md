# mini-publisher — Coding Eval Kit (Operator Guide)

Head-to-head implementation eval: each model builds a crash-safe, at-most-once
delivery daemon against a deliberately unreliable fake receiver, then a chaos
harness SIGKILLs the daemon repeatedly and grades the outcome against the
receiver's ground-truth log. The harness is the grader — no LLM judgment in the
core score, so grader self-preference is out of the picture.

## Files

| File | Who sees it |
|---|---|
| `spec.md` | Given to every model (the assignment) |
| `receiver.py` | Given to every model (frozen — they must not modify it) |
| `grader.py` | **Operator only.** Never show it to a contestant. |
| `results/` | Created by the grader (logs + `report.json` per run) |

All three harness files were authored by the eval operator side (Fable), frozen
before any contestant starts. Note for the blinded record: Fable is not a
contestant in this eval.

## Protocol (fairness checklist)

1. Create one masked directory per model, e.g. `submissions/model-a`,
   `submissions/model-b`, `submissions/model-c` (randomize the assignment and
   keep the mapping out of every transcript, per the workspace blinding policy).
2. Copy **only** `spec.md` and `receiver.py` into each. (The spec's shared-
   workspace rules assume this layout: submissions may run concurrently, so each
   one is told to stay inside its own directory and use its own receiver port.)
3. Kick each model off with the same prompt, filling in only the masked ID:
   > Your submission ID is model-a. Implement the product described in spec.md,
   > working entirely inside submissions/model-a/ per the spec's shared-
   > workspace rules — other submissions share this machine. receiver.py is a
   > fake external service provided for development — run it on your own port
   > to test, but do not modify it. Your deliverable is ./publisher at your
   > submission root, exactly matching the interface contract, plus your own
   > tests and NOTES.md. You have about 30 minutes; correctness under crashes
   > matters more than anything else.
4. Same time budget, same machine class, no mid-run coaching. If a model asks a
   clarifying question, the only answer is "follow spec.md."
5. When all runs finish, grade sequentially on one machine (the harness is
   timing-sensitive; don't grade in parallel):

```bash
cd mini-publisher
# quick contract check first (reliable receiver, ~2 kills, 5 messages, ~2 min):
python3 grader.py submissions/model-a --smoke
# the real thing (3 runs x ~16 min each per submission):
python3 grader.py submissions/model-a submissions/model-b submissions/model-c
```

## Reading the results

- **PASS** requires zero duplicates, zero false status claims, zero lost
  records, zero stuck messages across the run. Score starts at 100; penalties
  per defect (duplicate −20, false claim / lost −10, stuck −5, gave-up −3,
  persistent enqueue failure −5).
- **`duplicates` is the headline.** It means the model blind-retried across an
  ambiguous outcome or put its commit point on the wrong side of the HTTP call —
  the exact failure class the plan evals graded on paper.
- `report.json` in each `results/<name>/run-<i>/` has per-message final states
  vs ground-truth accepted counts; `daemon.log` shows the submission's own
  output across every kill/restart.
- `total_deliver_requests` is a soft efficiency signal: a correct implementation
  reconciles via `/audit` instead of spraying retries.
- An **ABORTED** run (daemon can't stay up 2 s across 5 straight starts, or no
  executable `./publisher`) scores 0 — "didn't run" is a result too.

## Judgment-layer add-on (optional)

The harness scores correctness only. If you also want the qualitative dimension
(code clarity, NOTES.md honesty about the crash-window design, test quality),
apply the two-grader protocol from `../grader-instructions.md` on top — but keep
it a separate score; don't blend it into the harness number.

## Known limits

- Kill timing is wall-clock random even with a fixed seed, so runs are not
  bit-reproducible; seeds fix the receiver's outcome sequence and the kill
  cadence distribution. Three runs per submission is the default to smooth this.
- The receiver's `/audit` is deliberately reliable — the eval tests whether the
  model *uses* an authoritative reconciliation source correctly, not whether it
  can survive without one. A follow-up hard mode could make `/audit` laggy.
- macOS/Linux only (process-group SIGKILL semantics).
