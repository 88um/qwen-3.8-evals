# Hostile Execution Review Protocol

The rules for scoring **working software submissions** (not plans) when the score
comes from two independent reviewers instead of a pre-frozen grader. It exists
because at this capability level automated harnesses saturate — every mini-kit run
scored 100 while a real convergence bug went uncaught — so the discriminating
signal has to come from adversarial reading. The danger of judgment-based scoring
is reviewer bias (a Claude grading a Claude-family field, a reviewer preferring its
own style), so this protocol pins score to **confirmed findings**, caps the
subjective layer, and splits authority between two graders from different vendors.

Use alongside `grader-instructions.md` (blinding rules, two-grader reconciliation,
anti-bias scoring) — this document specializes those rules for executable work.

---

## 1. What every submission ships

```
<masked-id>/
  README.md         how to build and run (build command, runtime deps)
  DECISIONS.md      ≤1,500 words: architecture + the claims register (§2)
  src/ (or equiv)   the implementation
  migrations/       schema, if any
  tests/            candidate-authored tests
```

The brief for each project defines an **interface contract** (an executable name,
its subcommands, env vars, HTTP endpoints) so any submission can be built and
driven identically regardless of language. A submission that does not honor the
contract is scored under §6 ABORTED.

## 2. The claims register (the review's target list)

`DECISIONS.md` must contain a **claims register**: a table of every invariant the
submission asserts it enforces, each with the mechanism behind it.

| # | Invariant claimed | Enforcing mechanism (table / constraint / lock / ordering / fsync) | How to check |
|---|---|---|---|

The register is not decoration — it is the contract the review attacks. Two
consequences follow, and they are the core of this protocol:

- **A claimed invariant the reviewer breaks costs double.** A named mechanism that
  does not hold is worse than an admitted gap, exactly as in the plan evals: false
  precision survives review until someone executes it.
- **An invariant the brief required but the register omits is a concealment
  finding** (see §4 severity), scored whether or not the code happens to enforce
  it — silence about a hard requirement is not neutral.

An honest "we did not implement X / X is best-effort" in DECISIONS.md is *not*
penalized under the double rule; it is graded as a normal scope gap. The register
rewards honesty and punishes overclaiming.

## 3. The review pass (each reviewer, independently, before scoring)

Complete all of these before writing a single score. Do them in a scratch copy,
never the submission dir.

1. **Build and run it** from README alone. If the README is insufficient, note it
   (an operability finding) and recover using the interface contract.
2. **Run its own tests.** Record pass/fail counts. Tests that assert behavior the
   code does not enforce (green but meaningless) are a finding.
3. **Trace every claims-register row** through schema and code. For each: is the
   named mechanism actually present, and does it cover *every* state the claim is
   about — including terminal, uncertain, and post-crash states? (This is where
   the mini-inbox lost-dirty race lived: a mechanism correct for the common path,
   silently absent for one interleaving.)
4. **Attack the boundaries.** Inspect every transaction boundary, every
   filesystem atomic-rename, every fsync/durability claim, every place two
   operations race, every cross-tenant access path. Build targeted probes for the
   weaknesses you find — this is adaptive: probe the architecture in front of you,
   not a reference design.
5. **Run the crash/concurrency probes** the brief names as minimums, plus any you
   derive. SIGKILL where the brief requires it.
6. **Assess architecture, maintainability, operability** — the judgment layer,
   scored separately and capped (§5).
7. **Separate confirmed defects from plausible concerns** (§4). Only confirmed
   findings move the correctness score.

## 4. Findings: the confirmation bar

Every finding is one of three grades. **The confirmation bar is the heart of this
protocol** — it is what keeps hostile review from degrading into taste.

- **CONFIRMED** — backed by *either* a runnable repro (a script/command the other
  reviewer can execute to observe the defect) *or* a step-numbered interleaving
  trace against specific line numbers in the submission, concrete enough that the
  other reviewer can verify it by reading. Only CONFIRMED findings move the
  correctness score. "I think this races" is not a finding; "here is the interleave
  that makes it race, at these lines" is.
- **PLAUSIBLE** — a real concern you could not confirm in the time budget. Recorded,
  visible to the other reviewer, but moves **zero** score. It may become confirmed
  during reconciliation if the other reviewer reproduces it.
- **REJECTED** — you investigated a suspected defect and the code is correct.
  Recorded too (it is evidence the mechanism holds, and stops the other reviewer
  re-chasing it).

### Severity (per confirmed finding)

| Severity | Meaning | Base weight |
|---|---|---:|
| CRITICAL | data loss, cross-tenant leak, silent corruption, money/promise-1-class violation | −25 |
| HIGH | invariant breakable under a realistic interleave; crash-unsafe durability | −12 |
| MEDIUM | correctness gap on an edge path; wrong error semantics; missing enforcement with narrow blast radius | −5 |
| LOW | operability, weak test, minor spec deviation | −2 |

**Doubling rule:** if the broken invariant appears in the claims register as
enforced, the weight doubles. **Concealment rule:** a hard requirement absent from
the register is scored at its severity as if broken, even if the code enforces it
(the model doesn't get credit for a guarantee it didn't know it was making).

Correctness score = max(0, 100 − Σ confirmed weights). This is ≥70% of the total.

## 5. The judgment layer (capped, averaged, never adjudicated)

The remaining ≤30% covers architecture quality, code clarity, maintainability,
test quality, DECISIONS.md honesty, and operational realism. Rules:

- Scored on a fixed rubric the brief provides, not open-ended impression.
- **Averaged across the two reviewers, never argued.** Neither reviewer may
  override the other's judgment score with a rationale — "well-argued defense of my
  own number" is the exact shape of self-preference bias.
- A judgment claim that is actually checkable (e.g. "this abstraction is unused")
  must be demoted to a §4 finding and confirmed, not scored as taste.

## 6. Mechanical floor and reconciliation

- **ABORTED (score 0):** does not build, does not run from the contract, or its own
  tests do not pass. "Didn't run" is a result. This is the one automated gate that
  survives from the harness era.
- **Independent first:** each reviewer completes §3–§5 and scores *before* seeing
  the other's review. No exceptions — simultaneous-blind is what makes the
  reconciliation meaningful.
- **Reconciliation:**
  - *Correctness findings* are resolved by the confirmation bar. A PLAUSIBLE from
    one reviewer that the other reproduces becomes CONFIRMED and scores. A CONFIRMED
    the other reviewer shows is REJECTED (the repro doesn't reproduce, the trace has
    a flaw) is dropped. Findings are facts; they are checked, not voted on.
  - *Judgment scores* are averaged, per §5.
  - *Disagreement that survives* (different severity on a confirmed finding, split
    correctness totals) is **reported, not forced** into one number — both scores,
    side by side, with rationale, exactly as `grader-instructions.md` requires.
- **Log everything:** every finding with grade, severity, repro/trace, doubling and
  concealment flags, which reviewer originated it, and its reconciliation outcome
  (CONFIRMED / REJECTED / PLAUSIBLE-unresolved). The log is the deliverable, not
  just the number.

## 7. Blinding and authorship (bias controls specific to execution)

- Masked IDs, randomized per project; no identity speculation; no persisting
  mappings (per `grader-instructions.md` and the workspace blinded-eval policy).
- **Starter-code authorship** (for brownfield projects with a provided starter):
  the grader who authored the project brief authors its starter app; the *other*
  grader reviews and signs off on the starter before it is frozen. Rationale: the
  author of a starter knows it intimately and imprints a style contestants build
  against — the cross-check keeps that from tilting the field.
- **n = 2 generations per model per project.** Generation variance at 1.5–5k lines
  is larger than in plan evals; a single sample cannot tell a capability from a
  lucky draw. Score each generation; report both and the spread.
- Treat "the submission feels right / reads like good code" as a prompt to build a
  probe, never as evidence. The bias channel is affinity for your own style; the
  antidote is the confirmation bar.
