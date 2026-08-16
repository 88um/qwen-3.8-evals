# Erasure Lake evaluation

This directory is a holdout planning evaluation for comparing engineering-plan quality across
models. The candidate-facing materials are:

- `product.md`
- `candidate-prompt.md`

Do not provide `grader-guide.md` to candidates. If candidates operate in this shared
workspace, explicitly repeat that it and other submissions are off-limits.

## Recommended run matrix

Run Qwen 3.8 27B, Opus 4.5, Sonnet 4.5, Opus 4.6, and Sonnet 4.6 once each in fresh
contexts. Each model creates its own directory directly under `erasure-lake/` using its exact
model ID and writes `<model-id>/plan.md`, matching the other planning evals in this workspace.

Use identical:

- candidate prompt and product brief;
- maximum output budget;
- time limit;
- tool and internet access;
- instructions about primary-source documentation;
- retry policy when a generation fails for infrastructure reasons.

## Expected artifact

Each model directory should contain only:

```text
plan.md
```

The plan has a 12,000-word cap. Record latency and token usage outside the evaluated plan.

## Grading protocol

1. Copy or rename completed model directories to anonymous labels before graders see them.
2. Give `product.md`, `grader-guide.md`, and the anonymized plans to two graders from different
   model families.
3. Each grader completes a full review before seeing the other review.
4. Mechanically reconcile verifiable findings; average judgment components.
5. Preserve both rankings if the graders still disagree.
6. Reveal the model-ID directory mapping only after scores and reconciliation are final.

Store reviews under `reviews/` as `grader-a.md`, `grader-b.md`, and `reconciliation.md`.

## Interpreting the set

Report:

- each model's total and section scores;
- paired model differences within the same prompt;
- grader disagreement before reconciliation;
- defects shared across model families versus submission-specific failures.

Do not merge these results into the older projects until the holdout analysis is complete.
