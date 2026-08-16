# Grader Instructions

Instructions for any model grading anonymized engineering-plan submissions in this
workspace. These exist because LLM graders exhibit **self-preference bias** — favoring
output that resembles their own style, even when submissions are masked — and because a
single grader reconciling disputes gives that grader the tiebreak on exactly the
disagreements where its bias is in play. The rubric therefore pushes as much scoring
weight as possible onto claims that can be mechanically checked, and splits authority
between at least two graders from different model families.

---

## 1. Blinding rules

1. Submissions are anonymized (`model-1`, `model-2`, …) deliberately. **Never attempt to
   identify which model produced a submission**, in the analysis or in passing. Do not
   speculate, even with hedges.
2. **Never persist identity mappings** to memory, notes, or any file — even if a mapping
   is revealed in conversation after grading.
3. Assume any submission might be your own output or your family's. Grade accordingly:
   if a passage "feels right" or "feels natural," treat that as a warning sign to check
   the underlying claim, not as evidence of quality.
4. Do not let stylistic familiarity move a score in either direction. Style is scored
   only where the brief explicitly makes it a criterion (e.g., readability requirements),
   and then against the brief's words, not your preferences.

## 2. Two-grader protocol

1. At least **two graders from different vendors** evaluate every submission. Each
   completes a full independent evaluation **before** reading the other's.
2. After both evaluations exist, reconcile — but the reconciliation rules differ by
   claim type:
   - **Verifiable claims** (Section 4 checklist): resolved by performing the check. The
     check's outcome is binding on both graders. A finding neither grader can verify is
     recorded as `PLAUSIBLE — UNVERIFIED` and moves no score.
   - **Judgment calls** (design quality, honesty, tradeoff reasoning): **averaged across
     graders**, not adjudicated. Neither grader may override the other's judgment score
     with a rationale, however well-argued — "defensible rationale for the ranking I
     already produced" is the exact shape residual self-preference takes.
3. If final rankings still disagree after reconciliation, **report both orderings** with
   each grader's rationale. Do not force a single order by letting one grader arbitrate.
4. Every reconciliation decision is logged: finding, check performed, outcome
   (`CONFIRMED` / `REJECTED` / `UNVERIFIED`), score delta, and which grader originated it.

## 3. Score composition

Each section's score is split into two explicitly labeled components:

- **Verifiable component (target ≥ 70% of section weight):** binary or stepped checks
  from the Section 4 checklist that apply to that section. These should produce the same
  score from any competent grader.
- **Judgment component (≤ 30%):** design quality, appropriateness to constraints,
  honesty of self-assessment. This is the only component subject to averaging under the
  two-grader protocol.

Sections whose nature is mostly judgment (e.g., tradeoffs/assumptions sections) keep the
split but shrink: cap their weight in the overall score rather than letting judgment
weight grow. The overall weighting should follow the brief's stated priorities (for the
current eval: the invariant enforcement map is weighted highest).

## 4. Mechanical verification checklist

Run every applicable check on every submission. Each is pass/fail with evidence quoted.

**Schema fidelity**
- [ ] DDL executes top-to-bottom in a clean database (trace it; forward references,
      missing extensions, and invalid syntax are failures).
- [ ] Every table, column, index, role, and function referenced anywhere in the plan
      (walkthroughs, invariant map, security section) exists in the DDL.
- [ ] Every constraint claim is executed in your head against **every** state the claim
      is about — including terminal/success states and uncertain/ambiguous states. An
      index predicate that excludes a state it claims to block is a failure of the
      claim, regardless of how precisely the mechanism is named.
- [ ] Declared grants/permissions permit every write the plan's own walkthroughs perform.

**External-world claims**
- [ ] Every named API endpoint exists in the provider's public documentation. Undocumented
      endpoints presented as settled operations are failures; honest acknowledgment of a
      capability gap is not.
- [ ] Every library behavior claim (DST handling, dedup semantics, ORM behavior, etc.) is
      checked against that library's documentation.
- [ ] Every dependency is currently supported: no EOL runtimes, unsupported framework
      majors, or deprecated libraries in a greenfield plan.
- [ ] Platform capability claims are accurate (e.g., "managed" databases are actually
      managed offerings with the claimed backup/restore behavior).

**Internal consistency**
- [ ] Arithmetic checks out (costs, rate limits, capacity, storage). Note the direction
      of each error, but an error in the conservative direction is still an error.
- [ ] The same mechanism is specified identically everywhere it appears. Two mutually
      exclusive specifications for one mechanism score as having no answer, not as
      having either answer.
- [ ] Failure walkthroughs only reference states, columns, and transitions the schema
      defines, and their recovery paths can actually execute against the declared
      constraints.

## 5. Anti-bias scoring rules

1. **Mechanisms over prose.** A claim is credited only if the enforcing construct is
   named, defined, and covers the claim. Assurances, however well-written, score zero on
   mechanism criteria.
2. **A precisely named mechanism that does not enforce its claim scores *worse* than an
   admitted gap.** False precision survives review; admitted gaps invite fixes.
3. **Do not reward volume.** Counts of invariants, walkthroughs, or risks beyond the
   brief's requirement earn nothing by themselves; each item is scored on whether its
   mechanism holds. Breadth with broken depth must not outscore narrow correctness.
4. **Do not penalize brevity** where the brief's requirement is met.
5. **Quote before you judge.** Every finding cites the submission's own text or DDL.
   A finding you cannot anchor to a quote is not a finding.
6. **Symmetric skepticism.** Apply the same adversarial reading to the plans you rank
   highest as to those you rank lowest. Before finalizing, re-read the top-ranked plan
   specifically hunting for the defect classes you found in the others.
7. **Check your dispersion.** If your scores cluster by writing style (e.g., every
   plan with a particular structural voice lands high), re-verify the low-scored
   outliers' mechanisms first — family-level affinity shows up as systematic underreading
   of the stylistic outsider.

## 6. Output format

For each submission:
- Section scores with the verifiable/judgment split shown.
- Strengths and weaknesses as quoted, checkable findings — each tagged with the check
  that confirms it.
- A one-line verdict.

For the field:
- A rankings table, cross-model observations, and (after reconciliation) the log of
  confirmed/rejected/unverified findings with score deltas.
- If graders disagree on final ordering: both orderings, side by side, with rationales.
