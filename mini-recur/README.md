# mini-recur — Coding Eval Kit (Operator Guide)

Pure-correctness eval: each model builds a `./recur` CLI that materializes
recurring schedule occurrences across DST transitions, rule edits, and
timezone changes. Graded against hidden golden scenarios — deterministic,
seconds to grade, **near-zero system load** (no daemons, no chaos loops).

This isolates the sharpest discriminator from the plan evals: correct DST
handling and first-writer-wins slot semantics, which only the top-ranked plan
got right on paper.

## Files

| File | Who sees it |
|---|---|
| `spec.md` | Given to every model |
| `examples/` | Given to every model (2 worked scenarios with expected output) |
| `make_goldens.py` | **Operator only** — the ground-truth generator |
| `goldens/` | **Operator only** — the 10 hidden grading scenarios |
| `grader.py`, `results/` | **Operator only** |

Hidden scenario coverage: spring-forward gap (US + Berlin), fall-back
ambiguity (earlier offset), edit that steps over a date, same-day edit dedup,
timezone-change re-mint trap, window boundary inclusion/exclusion.

## Protocol

1. `submissions/<masked-id>/` per model; copy in **only** `spec.md` and
   `examples/`. Randomize assignment; keep the mapping out of transcripts.
2. Kickoff prompt:
   > Your submission ID is model-a. Implement the product described in
   > spec.md, working entirely inside submissions/model-a/ per the spec's
   > shared-workspace rules. Your deliverable is ./recur at your submission
   > root matching the interface contract exactly, plus your own tests and
   > NOTES.md. Your output must reproduce examples/ exactly. You have about
   > 30 minutes; exact correctness on timezone edge cases is everything.
3. Grade (seconds, negligible load):

```bash
cd mini-recur && python3 grader.py submissions/model-a submissions/model-b ...
```

## Reading results

Score is % of hidden scenarios passed; `results/<name>.json` lists which
failed by name. The scenario names say what broke: a model failing only
`spring-gap`/`berlin-spring-gap` mishandles nonexistent times; failing
`fallback-earlier-offset` picked the wrong fold; failing the `edit-*`/
`tz-remint-trap` scenarios missed first-writer-wins semantics.

If goldens ever need regenerating (`python3 make_goldens.py`), regenerate
BEFORE any submission starts — never between submissions of the same round.
