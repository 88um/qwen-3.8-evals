# mini-inbox — Coding Eval Kit (Operator Guide)

The hardest of the three kits: each model builds a webhook consumer that must
converge to a billing provider's exact truth despite duplicated, delayed,
out-of-order event delivery, SIGKILLs mid-stream, and a rate limit that
forbids brute-force re-fetching. The correct architecture (events as pings +
budgeted canonical reconciliation) has to be *discovered* — trusting payloads
in arrival order fails, and the grader proves it mechanically.

**Load profile: light.** Two small HTTP servers plus the consumer, 3 kills,
mostly sleeps; ~3–4 min per run, 2 runs default. Self-nices (`GRADER_NICE`).
Validated both directions: a dirty-flag + budgeted-sweep reference passes
100/PASS; a payload-trusting last-write-wins variant fails with a stale
account and `canonical_calls=0` on its scorecard.

## Files

| File | Who sees it |
|---|---|
| `spec.md` | Given to every model |
| `provider.py` | Given to every model (frozen — do not let them modify it) |
| `grader.py`, `results/` | **Operator only** |

The provider's `/truth` endpoint is token-protected per run (`TRUTH_TOKEN`
env set by the grader), so contestants cannot cheat by reading truth even if
they probe the provider — the spec also forbids it outright.

## Protocol

1. `submissions/<masked-id>/` per model; copy in **only** `spec.md` and
   `provider.py`.
2. Kickoff prompt:
   > Your submission ID is model-a. Implement the product described in
   > spec.md, working entirely inside submissions/model-a/ per the spec's
   > shared-workspace rules. provider.py is the fake provider — run your own
   > instance on your own port for development; do not modify it. Your
   > deliverable is ./inbox at your submission root, exactly matching the
   > interface contract, plus your own tests and NOTES.md. You have about 30
   > minutes; exact convergence under hostile delivery is everything.
3. Grade: `python3 grader.py submissions/model-a submissions/model-b ...`

## Reading results

PASS = exact convergence on every event-touched account. The per-run line
carries the design signals: `mismatched` names the accounts still wrong
(stale-payload architectures die here); `canonical_calls`/`429s` show whether
the model found reconciliation and budgeted it (0 calls = trusted payloads;
hundreds + many 429s = no budget discipline); `settled_after` shows
convergence speed. Full detail in `results/<name>/run-<i>/report.json`,
consumer stderr in `consumer.log`.

## Suggested eval order across the three kits

mini-recur (seconds, pure correctness) → mini-ledger (~2 min, concurrency)
→ mini-inbox (~8 min, architecture discovery). Grade one kit at a time;
never run two kits' graders simultaneously.
