# mini-ledger — Coding Eval Kit (Operator Guide)

Concurrency-correctness eval: each model builds an HTTP credit-ledger service;
the harness fires barrier-synchronized racing requests (duplicate holds, a
12-thread drain of a 5-credit account, charge-vs-release races, verbatim
replays), SIGKILLs the server once, then replays the submission's own ledger
dump prefix-by-prefix to prove every invariant. This is the TOCTOU test —
"check then insert" passes every sequential test and fails here.

**Load profile: light.** ~45 s per run, a few dozen requests, ≤12 short-lived
threads, one kill/restart. The grader self-nices (env `GRADER_NICE`, default
10). Validated: a correct BEGIN-IMMEDIATE reference scores 100/PASS; a
check-then-act variant scores 0 with 20 duplicate holds caught.

## Files

| File | Who sees it |
|---|---|
| `spec.md` | Given to every model |
| `grader.py`, `results/` | **Operator only** |

## Protocol

1. `submissions/<masked-id>/` per model; copy in **only** `spec.md`.
2. Kickoff prompt:
   > Your submission ID is model-a. Implement the product described in
   > spec.md, working entirely inside submissions/model-a/ per the spec's
   > shared-workspace rules. Your deliverable is ./ledger at your submission
   > root, exactly matching the interface contract, plus your own tests and
   > NOTES.md. You have about 30 minutes; correctness under true concurrency
   > is everything — assume every request races with a twin.
3. Grade: `python3 grader.py submissions/model-a submissions/model-b ...`
   (default 2 runs each; sequential, ~90 s per submission).

## Reading results

PASS requires zero violations. The violation names in the run summary map
straight to mechanisms: `dup_hold`/`negative_available` → no storage-layer
serialization; `dup_terminal` → charge/release race unguarded;
`replay_appended` → no idempotency keys; `durability_loss`/`append_only` →
commit discipline; `false_claim` → responses disagree with the ledger;
`balance_mismatch` → derived state drifts from the ledger. Full per-run
detail in `results/<name>/run-<i>/report.json`, server stderr in `server.log`.
