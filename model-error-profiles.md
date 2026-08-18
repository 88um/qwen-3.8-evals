# Model Error Profiles: Qwen vs Opus — what actually goes wrong when they plan and code

**Status:** living document, updated across every completed eval set currently
checked into the repository: three planning sets (`atlas-cutover`,
`pilot-project`, `social-media-auto`) and five coding sets (`vaultdrop`,
`mini-inbox`, `mini-ledger`, `mini-publisher`, `mini-recur`). `cipherboard`,
`erasure-lake`, and `patchbay` currently contain briefs or grader material but
no completed submission results, so they are tracked as pending rather than
treated as evidence.

**Data sources:** the paired analyses in `atlas-cutover/`, `pilot-project/`,
and `social-media-auto/`; the reconciled and follow-up hostile reviews in
`vaultdrop/`; and the checked-in mechanical grader reports under each `mini-*`
set. Sections 1–8 preserve the original atlas-cutover analysis and method;
sections 9–13 add the cross-eval evidence and supersede atlas-only profile and
hypothesis conclusions where they conflict.

**Core caveat, stated once:** almost every identified submission is still one
generation of one model/version/configuration on one task. Versions are not
interchangeable: Opus 4.5, 4.6, 4.8, and 5 differ materially, as do the qwen
effort settings and VaultDrop iterations. The four mini suites keep their
identities masked in the checked-in tree, so they support conclusions about
field parity and grader discrimination, but not Qwen-vs-Opus attribution.
`review-protocol.md` §7 requires n = 2 because single samples cannot separate
capability from draw luck. This remains a *hypothesis file*, not a universal
model ranking.

---

## 1. The measurement problem this file exists to fix

The hostile protocol scores by confirmed findings. A finding requires a
**checkable claim** to attack. Models differ systematically in how many
checkable claims they emit per plan — so raw finding counts and Σ severity
weights partly measure *how much a model dares to say*, not only how often it
is wrong. Three lenses on the same data give three different orderings:

- **Σ severity weights** (the protocol's lens): balanced 91 < low 96 < max 128 < opus 167 — opus looks worst.
- **Confirmed findings per plan:** all four tied at 11 — no signal at all.
- **Findings per 100 checkable claims** (precision lens): opus 5.7 best, max 8.2 worst — opus looks *best*.

None of these is "the truth"; together they describe two different failure
temperaments. That is the subject of this file.

## 2. Claim density (measured)

Proxies counted mechanically from each `plan.md` (grep-based; approximate but
applied identically to all four — see §8 for the exact definitions):

| Plan | Words | SQL stmts | Unique `pg_*` refs | Named tests | Quantities | Claim proxy total | Confirmed findings | Findings /100 claims | Σ weights | Σ weights /100 claims | Avg weight /finding |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| qwen-low | 5,564 | 23 | 6 | 35 | 106 | 170 | 11 | 6.5 | 96 | 56 | 8.7 |
| qwen-balanced | 6,058 | 18 | 6 | 38 | 86 | 148 | 11 | 7.4 | 91 | 61 | 8.3 |
| qwen-max | 5,130 | 20 | 3 | 30 | 81 | 134 | 11 | 8.2 | 128 | 96 | 11.6 |
| opus-4.6 | 6,896 | 34 | 14 | 24 | 120 | 192 | 11 | 5.7 | 167 | 87 | 15.2 |

Read the two right-hand columns together and the shape of the result falls out:

- **Opus makes the most checkable claims (192) and is wrong the least often per
  claim (5.7/100) — but when it is wrong, the errors are the heaviest in the
  field (15.2 avg weight, nearly double qwen's ~9).** Its `pg_*` references
  are also essentially all real facilities (14/14 modulo grep artifacts).
- **qwen-max is the inverse pathology: fewest checkable claims (134, only 3
  `pg_*` references — one of which, `pg_logicalreplication`, doesn't exist)
  and the worst per-claim error rate (8.2/100).** Low density did not mean
  high precision; it meant the same confusion with fewer receipts.
- **qwen-balanced won the protocol not by precision (7.4/100, middling) but by
  keeping its errors *light*** (8.3 avg weight — its failures cluster in loud,
  gate-catchable classes) — see the taxonomy below.

## 3. Error-class taxonomy (all 44 reconciled findings, classified)

Six classes, ordered roughly by *when the error gets caught in real life*:

| Class | Definition | Caught by | Real-world cost |
|---|---|---|---|
| **Fabrication** | invented facility/API/artifact that has never existed | first execution / first doc lookup | low — dies in hour one |
| **Arithmetic** | number that doesn't recompute | any reviewer with a calculator | low |
| **Omission / vagueness** | load-bearing moment left unspecified; hard requirement unengaged | only by adversarial review (nothing to execute) | high — hides bugs entirely |
| **Self-inconsistency** | plan contradicts its own earlier text/DDL | careful cross-section read | medium — loud when hit |
| **Design-logic bug** | fully specified mechanism that fails under an interleave/edge input | staging at best; production at worst | high |
| **Semantic misuse** | real facility, confidently wrong about its behavior | **production, silently** | highest |

Classification of the reconciled findings (IDs from `analysis-fable.md`):

| Class | qwen-low | qwen-balanced | qwen-max | **qwen total (33)** | **opus-4.6 (11)** |
|---|---:|---:|---:|---:|---:|
| Fabrication | 2 (audit endpoint; CONNECT VIA) | 2 (slot-at-LSN; PQ* funcs) | 1 (port-range drain proof) | **5 (15%)** | **0 (0%)** |
| Semantic misuse | 1 (trigger RETURN NULL) | 0 | 4 (origin-filter loop; subscriber triggers; fence semantics; REV copy_data/upsert) | **5 (15%)** | **4 (36%)** (fence RETURN NEW on DELETE; subscriber triggers; REFRESH PUBLICATION; tablesync TRUNCATE) |
| Self-inconsistency | 1 (rollback blocked by own fence) | 2 (seed-vs-gate; missing OS reverse channel) | 2 (unpublished `reservations` vs six-table claim; identity DDL vs prose) | **5 (15%)** | **1 (9%)** (`'draining'` vs own CHECK) |
| Design-logic bug | 3 (delete resurrection; dedup collapse; EDI commit-order) | 5 (sweep release; zero-tolerance comparison; FI-3 budget; sync-switch coupling; dedup ambiguity) | 1 (dedup collapse) | **9 (27%)** | **4 (36%)** (movements-less rollback; batch allocator; ambiguous-commit release; reseed timing) |
| Omission / vagueness | 3 (FWD gate; availability budget; wal_level) | 2 (flip choreography/FWD gate; wal_level) | 1 (FWD gate) | **6 (18%)** | **1 (9%)** (wal_level) |
| Arithmetic | 1 | 0 | 2 | **3 (9%)** | **1 (9%)** |

**The headline:** opus concentrates 72% of its errors in the two classes that
survive to production (semantic misuse + design-logic); qwen spreads its errors
with a third of them (fabrication + omission) in classes that die at typing
time or hide until a hostile reviewer arrives. Opus never invented a facility;
qwen plans did so five times. Opus was wrong about what real facilities *do*
four times — confidently, with the correct names spelled correctly, which is
the most dangerous way to be wrong.

## 4. Atlas-only baseline profiles for planning work

This section records the profile inferred before the other eval sets landed.
The cross-eval update in §11 is the current profile.

### Opus 4.6

**Strengths (both reviewers, independently):**
- **Transition choreography.** Only plan of four with a correct write-flip
  ordering (fence → in-flight drain → CDC lag 0 → disable subscriptions →
  route). Both reviewers' REJECTED lists credit it explicitly.
- **Facility literacy in breadth.** 14 real catalog/function references; the
  most accurate REPLICA IDENTITY analysis; correct exactly-once reasoning via
  apply-position tracking.
- **Operational surface.** Error contracts (custom SQLSTATE → 503 +
  Retry-After), circuit-breaker numbers, pre-flight cron-collision checks,
  numbered operator runbooks with stated pause windows, evidence lines that
  name queryable observables.
- **Highest judgment score from both reviewers** (avg 63.5) — including from
  the cross-vendor reviewer, so it's not family affinity.

**Weaknesses:**
- **Confident semantic misuse of real facilities.** Knows the name, misses the
  behavior (subscriber-trigger firing rules, REFRESH PUBLICATION scope,
  trigger return values on DELETE). These read as true and deploy as bugs.
- **Designed-in wrong decisions.** The batch allocator and the
  movements-less reverse publication weren't slips — they were specified,
  justified with arithmetic, and wrong. Confident specificity survives the
  author's own review and, unchecked, would survive a lazy human one.
- **Severity concentration.** When it fails, it fails in silent classes: its
  errors averaged 15.2 protocol weight vs ~9 for qwen, and it owns the single
  finding both reviewers agreed was CRITICAL (post-rollback reconcile
  corruption).
- **Claims-register exposure.** Writes the most doubled-claim surface; under
  claim-punishing rubrics it structurally underscores its own competence.

### Qwen (all configurations)

**Strengths:**
- **qwen-balanced's decision discipline is the best artifact in the field:**
  strongest choice/rejected/why rigor, honest assumption ledger, fail-closed
  instincts (poison events, review queues — the only plan that never silently
  guesses on ambiguity). Won the protocol ranking on both reviewers' pooled
  correctness.
- **qwen-max's artifact archaeology:** only plan to catch the wal_level
  restart, sequence non-replication in logical decoding, and to preserve the
  misspelled-trigger wart deliberately. Deep reading of provided context.
- **Errors skew loud.** More of qwen's failure mass is fabrication/omission —
  caught at first execution or first review — which is why its per-finding
  severity runs light.

**Weaknesses:**
- **Fabrication under pressure.** When a mechanism is needed and not known,
  qwen invents it (nonexistent libpq calls, nonexistent SQL syntax, a
  nonexistent "existing audit endpoint", a slot-at-LSN capability PostgreSQL
  has never had). Opus produced zero of these.
- **Vagueness at exactly the load-bearing instant.** The flip choreography —
  the hardest three inches of the problem — got one sentence in qwen-balanced
  and no drain gate in any qwen plan. The worst qwen bugs lived in their least
  specified sentences.
- **Effort scaling is non-monotonic.** balanced (mid) beat max (high) on both
  reviewers' scoring; max's extra depth bought artifact insight but also the
  worst per-claim precision in the field. More thinking ≠ more checking.
- **Thinner operational layer.** Fewer named tests executed as runbooks, fewer
  real observables, no error contracts; evidence lines lean on bespoke tooling
  that doesn't exist yet.

## 5. Pre-registered predictions for the *coding* eval sets

These predictions were written from atlas-cutover alone. They are retained as
a pre-registration; §12 evaluates them against the completed coding sets.

- **H1 — qwen's fabrication class collapses in code.** Compilers and tests
  kill invented APIs in the first iteration loop. Expect qwen's plan-vs-code
  quality gap to *narrow*; do not extrapolate plan fabrication rates to code.
- **H2 — opus's semantic-misuse class persists in code.** Code that runs and
  passes its own tests while wrong under concurrency/crash is exactly this
  class (cf. the mini-inbox lost-dirty race that motivated the protocol).
  Expect opus submissions to need the hostile execution review *most*, and to
  look best under it until §3-step-4 boundary attacks land.
- **H3 — qwen's omission class becomes missing code, not wrong code.** Expect
  absent edge-path handling and thin failure recovery rather than broken
  mechanisms; the concealment rule (register omissions) should do more work on
  qwen submissions than the doubling rule.
- **H4 — opus self-inconsistency reappears as cross-file drift** (schema CHECK
  vs code enum, à la `'draining'`). Grep-level cross-checks are high-yield on
  opus submissions.
- **H5 — opus wins the operability/judgment layer in execution evals**
  (README quality, DECISIONS.md, runnability) for the same reason it won plan
  judgment. Watch whether that halo pressures reviewers to under-probe; the
  protocol's "feels right → build a probe" rule is aimed at exactly this.
- **H6 — claims registers will differ in density, not honesty.** Expect opus
  to register more invariants (more doubling exposure) and qwen to register
  fewer, vaguer ones. Compare register row counts before comparing scores.

## 6. Rubric implications (apply to future eval sets)

1. **Report claim density alongside Σ weights** (the §2 table) so terse
   submissions can't win by silence. Findings/100-claims and Σ/100-claims are
   one grep away and change the story materially.
2. **Weight failure loudness.** A claim that fails at first execution
   (fabricated API, non-parsing DDL) should grade below an equal-severity
   claim that fails silently in production. This was the entire content of the
   two reviewers' severity dispute in atlas-cutover; encoding it removes the
   biggest calibration gap.
3. **Enforce the concealment rule on vagueness, not just omission** — a
   claimed mechanism specified too thinly to attack should score as if broken,
   or vagueness becomes the dominant strategy against hostile review.
4. **Keep cross-vendor reviewer pairs.** The Claude-family reviewer was
   *harshest* on the opus plan (initial rank: 4th) and the GPT reviewer rated
   it best on judgment — the bias controls held, in the non-obvious direction.
   Keep them.
5. **n = 2 minimum before any per-model conclusion** — this file included.

## 7. Extension template (used for the cross-eval update below)

For each completed eval set, add: task type (plan/code), per-submission claim
proxies (§8 method), confirmed findings classified by the §3 taxonomy, Σ
weights, per-claim rates, and one paragraph: which hypotheses from §5 this set
confirmed/refuted. Update §4 profiles only when a pattern has appeared in ≥2
sets.

## 8. Method notes (reproducibility)

Claim proxies per plan.md, counted identically for all submissions:
- `SQL stmts`: lines matching `^\s*(CREATE|ALTER|DROP|REVOKE|INSERT INTO|SELECT setval|UPDATE migration)`
- `pg_* refs`: unique matches of `pg_[a-z_]+` (minor artifacts possible, e.g.
  tokens inside placeholders; identical noise floor for all plans)
- `Named tests`: unique matches of test-identifier patterns (`FI-n`, `fi_*`,
  `f-n`, `t-*`, `T-*`, `test_*`)
- `Quantities`: numeric tokens with units (MB/s, GB, ms, /s, min, %, days, …)
- `Claim proxy total` = sum of the four columns. Findings and weights are the
  post-reconciliation ledgers in `atlas-cutover/analysis-fable.md`.

Classification judgment calls: trigger-return-value errors are counted as
*semantic misuse* (real facility, wrong semantics) even where the consequence
was catastrophic (qwen-low L2, opus O3); qwen-balanced's flip-choreography
finding is counted as *omission/vagueness* because the defect is the absence of
specification at the flip, and its seed-at-backfill as *self-inconsistency*
because its own gate contradicts its own seeding. Reclassify with reasons if
you disagree — the taxonomy only works if applied consistently across sets.

---

## 9. Cross-eval evidence map and comparison rules

The repository now has more evidence than the atlas-only profile, but not all
rows are the same kind of measurement. The following distinctions prevent a
large qualitative plan review from being treated as interchangeable with a
deterministic hidden-test score.

| Eval set | Task | Field used here | Measurement | Identity status |
|---|---|---|---|---|
| `atlas-cutover` | plan | 3 qwen effort settings; Opus 4.6 | hostile two-reviewer ledger, reconciled | revealed |
| `pilot-project` | plan | Qwen; Opus; Sonnet | two independent comparative reviews, cross-checked | revealed |
| `social-media-auto` | plan | Qwen 3.8 27B; Opus 4.5/4.6/4.8/5; 3 other Claude variants | two independent comparative reviews, reconciled in prose | revealed |
| `vaultdrop` | code | 3 identified qwen iterations; 2 identified Opus 4.6 iterations; GPT-5.6 Sol as a field baseline | executable probes plus hostile review; reconciliation varies by round | revealed |
| `mini-inbox` | code | 2 submissions | 2 mechanically graded chaos runs each | still masked |
| `mini-ledger` | code | 2 submissions | 2 mechanically graded concurrency runs each | still masked |
| `mini-publisher` | code | 2 submissions | 3 mechanically graded chaos runs each | still masked |
| `mini-recur` | code | 2 submissions | 10 hidden golden cases each | still masked |
| `cipherboard` | plan brief | no submissions/results | no result yet | n/a |
| `erasure-lake` | code brief/grader | no reviewed submissions | no result yet | n/a |
| `patchbay` | plan brief | no submissions/results | no result yet | n/a |

Rules used in the rest of this document:

1. **Do not pool scores across rubrics.** A 90 in VaultDrop is not nine-tenths
   of an 80 in Pilot; only within-set ordering and the underlying findings are
   comparable.
2. **Do not retrofit severity totals where none exist.** Pilot and Social Media
   have cross-checked findings but not an atlas-style confirmed-finding ledger,
   so they update the qualitative taxonomy without invented Σ weights.
3. **Do not unmask by writing style.** The mini suites demonstrate that both
   masked submissions can implement the required mechanisms. Until their
   mapping is recorded, neither result belongs in a Qwen or Opus column.
4. **Separate versions and draws.** Social Media shows a 21–23 point spread
   between Opus 4.5 and Opus 5 under the same reviewers. VaultDrop shows a
   43-point swing between two qwen generations under different versions of
   the brief. A family average would erase the most actionable signal.

## 10. Results by eval set

### 10.1 `pilot-project`: Qwen wins planning, by mechanisms rather than coverage

Both reviewers produced the same order. Their reconciled absolute scores
differ, which is why the raw pair is preserved rather than collapsed into a
single official number.

| Plan | Fable review | GPT review | Mean (descriptive only) | Rank agreement |
|---|---:|---:|---:|---|
| Qwen | 80 | 77 | 78.5 | 1st / 1st |
| Opus | 74 | 63 | 68.5 | 2nd / 2nd |
| Sonnet | 59 | 47 | 53.0 | 3rd / 3rd |

Qwen's win confirms the atlas strength that matters most: it turns safety
requirements into database constraints, transaction boundaries, durable
intents/receipts, chaos tests, and measurable gates. Its deterministic ranking
and truthfulness audit were also cheaper and more falsifiable than an LLM in
the feed-scoring path.

The errors, however, repeat the atlas pattern. Full-auto mode disappeared
without disclosure; the review-authorization token and rendered artifact were
load-bearing but unmodeled; posting enrichment had a consumer but no producer;
and the employer-side post-click ambiguity was reduced but not closed. These
are not hallucinated syntax. They are **omissions at the join between two
otherwise detailed mechanisms**.

Opus was the most product-complete and operable plan, but its core failures
were more dangerous: it mutated the supposedly append-only ledger, could
automatically resubmit after an ambiguous crash, used an invalid partial-index
predicate, relied on browser `storageState` as if it preserved DOM progress,
and priced per-user/job scoring as if it ran only for eventual applications.
This confirms the atlas warning: breadth and correct facility names do not
protect against a confidently wrong lifecycle or unit of work.

**Effect on the atlas profile:** confirms Qwen's structural-invariant strength
and load-bearing omissions; confirms Opus's completeness/operability advantage
and its concentration of risk in fully designed mechanisms.

### 10.2 `social-media-auto`: version effect dominates the family label

The two reviews agree on the complete eight-plan order. The direct Qwen/Opus
slice is:

| Plan | Fable review | GPT review | Mean (descriptive only) | Field rank |
|---|---:|---:|---:|---:|
| Opus 5 | 87 | 81 | 84.0 | 1 |
| Qwen 3.8 27B | 84 | 76 | 80.0 | 3 |
| Opus 4.8 | 73 | 70 | 71.5 | 4 |
| Opus 4.6 | 68 | 62 | 65.0 | 6 |
| Opus 4.5 | 64 | 60 | 62.0 | 7 |

Qwen was the strongest original-round plan and beat three of the four Opus
versions. It again excelled at work isolation, durable scheduling/capacity,
evidence, restore discipline, and an invariant-oriented data model. It again
lost points where described entities and executable artifacts diverged: its
DDL did not load, its central outbox was absent, content-rights evidence was
asserted rather than modeled, the cleanup dispatch record did not exist, and
its cleanup design relied on unsupported API behavior.

Opus 5 won because it improved the exact class that hurt earlier versions: it
committed markers before both external publish boundaries, held a permanent
attempt fence across uncertainty and success, treated cleanup as browser
automation rather than inventing an API, reconciled billing from canonical
state, and handled DST with round-trip checks and dual uniqueness. This is
strong evidence that the weaknesses are correctable design habits, not a fixed
family ceiling.

Yet Opus 5 still reproduced the family-level drift: contradictory quota timing,
non-executable DDL, a grant matrix that forbade its own walkthrough writes, an
undefined `admin_sessions` entity, and a deletion path blocked by its own FKs.
Earlier Opus versions added false DST semantics, unsupported cleanup behavior,
and attempt fences whose predicates excluded the state they claimed to hold.

Every one of the eight plans had at least one schema execution error, missing
referenced object, or walkthrough/schema contradiction. All also left some
part of post-crash Instagram receipt recovery, deletion, tenant binding, or
editable frozen content unresolved. The family distinction is therefore
secondary to a field-wide one: **plans are much better at naming state machines
than at proving that every schema predicate and external contract implements
them.**

**Effect on the atlas profile:** refutes any simple "Qwen plans better" or
"Opus plans better" conclusion. Qwen 3.8 beat Opus through 4.8; Opus 5 beat
Qwen. It confirms the recurring error classes on both sides and shows that
version/checking discipline can outweigh family.

### 10.3 `vaultdrop`: execution reverses twice, exposing draw variance

VaultDrop is the only broad coding set with revealed identities and hostile
execution review. Scores marked "single" have not had the same two-reviewer
reconciliation as the round-1 pair.

| Brief/round | Submission | Score used | Review status | Headline result |
|---|---|---:|---|---|
| round 1 | Qwen 3.8 27B | 90 | reconciled; 77 under unresolved alternate severity | correct core coordination; small boundary gaps |
| round 1 | Opus 4.6 | 56 | reconciled; 31 under alternate severity ledger | silent corrupt reads, replay/size gaps, weak crash tests |
| current brief, v2 | Qwen 3.8 27B v2 | 47 | reconciled; about 35 under alternate severities | one-line dead GC guard enabled CRITICAL crash data loss |
| current brief, v2 | Opus 4.6 v2 | 72 | single Fable review | core lock protocol held; corrupt-read defect repeated |
| current brief, v3 | Qwen v3 | 82 | single Fable review | crash/GC and scale held; read integrity and HTTP framing failed |
| current brief | GPT-5.6 Sol | 90 / 66.5 | two independent reviews, not reconciled | reviewers disagree on a concurrent read-integrity finding |

The GPT-5.6 Sol row is retained as field context but excluded from the two
profiles. Its 23.5-point reviewer gap is itself useful evidence: the Fable pass
did not find the read check/use race that the GPT pass reproduced, and no
checked-in reconciliation resolves it. It must not be converted into a single
score by averaging.

Round 1 is the clearest counterexample to the atlas extrapolation that Opus
would win coding operability. Qwen used a deliberately coarse coordination
mechanism, backed it with stronger crash and race tests, re-hashed at the read
boundary, and won unanimously. Opus shipped a clean, idiomatic service whose
own 21 tests passed, but its named crash tests did not enter the claimed crash
windows, its finalize-vs-GC test was sequential, and its read path trusted
write-time validation. The worst defect survived compilation and the model's
own suite exactly as H2 predicted.

The v2 round prevents overfitting to that result. Qwen replaced simple locking
with a more ambitious lock-free-ish resurrection/quarantine protocol. A
truthy database cursor was mistaken for an affected-row count, voiding the
guard. The normal path self-healed, so ordinary race probes missed the bug;
only the crash interleave exposed permanent data loss. Opus retained its
simpler per-hash lock, passed the coordination attacks, and fixed most v1 API
defects. It still failed to verify bytes on download, repeating the same silent
integrity gap after it had already been found in v1.

Qwen v3 recovered to 82 with a different `collecting`-state protocol that
survived the hostile battery and the scale envelope. Its remaining failures
were again at boundaries: serving corrupted stored bytes, committing response
headers before verification, and desynchronizing keep-alive framing on reject
paths. Its 18-test suite was rated the strongest in the project, but contained
no test for either confirmed defect.

The claims-register counts also falsify the atlas density prediction. Qwen's
identified registers contain 12, 14, and 13 rows; Opus's contain 13 and 11.
There is no stable family direction. What matters is whether a row's proposed
probe reaches the dangerous interleave, not how many rows exist.

**Effect on the atlas profile:** confirms that invented facilities largely
disappear once code must run; confirms silent semantic/design failures in code;
refutes a stable Opus coding-operability advantage; and shows that qwen's
within-model draw/approach variance can be larger than the round-1 family gap.

### 10.4 The four `mini-*` suites: bounded implementation parity

All checked-in mini submissions score 100. Because the IDs remain masked,
these results must stay in model-1/model-2 form.

| Eval | model-1 | model-2 | Adversarial coverage represented by checked-in reports |
|---|---:|---:|---|
| `mini-recur` | 100 | 100 | both pass all 10 hidden DST/edit/timezone cases |
| `mini-ledger` | 2/2 runs at 100 | 2/2 runs at 100 | zero violations across 12 concurrency, idempotency, prefix, and durability counters |
| `mini-inbox` | 2/2 runs at 100 | 2/2 runs at 100 | 240 emitted events total, 12 total SIGKILLs, exact post-settle truth |
| `mini-publisher` | 3/3 runs at 100 | 3/3 runs at 100 | 300 messages, 294 total SIGKILLs, zero duplicates/false terminals/loss/stuck rows |

The meaningful residual signal is in `mini-inbox`: model-1 made 68 canonical
calls and incurred zero 429s; model-2 made 98 calls and incurred 21 429s. Both
still converged exactly, so this is an efficiency/rate-shaping difference, not
a correctness failure. It also illustrates why binary PASS should retain its
diagnostic counters.

These suites strongly reject a claim that either contestant simply cannot
implement concurrency serialization, authoritative reconciliation,
at-most-once delivery, or DST semantics under a 30-minute bounded task. They
do **not** reject the VaultDrop findings: the minis provide the authoritative
audit endpoint, constrained state machine, fixed interface, and targeted
grader that broad-system plans often leave unspecified. Narrow mechanism
competence and whole-system closure are different measurements.

## 11. Updated model profiles across identified evals

### Qwen 3.8 27B family/configurations

**Most repeatable strengths:**

- **Structural invariant instinct.** Across Atlas, Pilot, Social Media, and
  strong VaultDrop draws, qwen reaches for conditional writes, partial unique
  indexes, durable intents/receipts, explicit state transitions, and
  fail-closed recovery. This is the most stable positive pattern.
- **Proof-oriented implementation.** Its best submissions pair mechanisms
  with adversarial tests: Pilot's chaos gates, Social Media's evidence and
  restore discipline, VaultDrop v1's real crash/race tests, and v3's scale and
  interleave suite.
- **Good performance under bounded executable contracts.** The revealed
  VaultDrop round-1 win is large, and both anonymous mini contestants clear
  every functional grader. There is no evidence that qwen's planning
  fabrications translate into chronic compile-time failure.
- **Useful simplicity when it chooses it.** The coarse-lock VaultDrop v1
  protocol was easier to prove than the later novel design and beat a more
  polished implementation.

**Most repeatable weaknesses:**

- **Load-bearing entities disappear between prose and artifact.** Authorization
  tokens, rendered files, enrichment producers, outboxes, rights records, and
  cleanup dispatches recur as described-but-unmodeled components.
- **DDL and boundary code receive less verification than the core algorithm.**
  Social Media's schema did not execute; VaultDrop's residual failures cluster
  in body limits, response framing, read verification, and configuration
  behavior after the concurrency core passes.
- **External capability invention remains a planning risk.** Unsupported
  PostgreSQL and Instagram mechanisms recur. Compilation removes the literal
  fabrication class, but not the tendency to assume a needed external
  capability exists.
- **Ambition raises variance.** Atlas balanced beat max; VaultDrop's simplest
  qwen protocol scored 90, a novel v2 protocol scored 47, and a corrected v3
  returned to 82. More mechanism is not more proof.

**Best use:** generate the enforcement skeleton, constraints, adversarial test
matrix, and a deliberately simple first implementation. Require a second pass
whose only job is to execute every migration, enumerate every prose noun in the
schema, and attack HTTP/provider boundaries.

### Opus family (version-sensitive)

**Most repeatable strengths:**

- **Architecture and product completeness.** Pilot and every Social Media
  review credit Opus with broader workflow coverage, better operator surfaces,
  and more explicit deployment/process decisions.
- **Transition choreography can be excellent.** Atlas's only correct flip
  sequence and Opus 5's two committed Instagram boundary markers are the best
  examples in their fields.
- **Breadth of facility knowledge and operational detail.** Opus usually names
  more real facilities, error contracts, observables, runbooks, roles, and
  recovery states. Later versions turn that breadth into much better results.
- **Conservative coordination can survive execution.** The retained per-hash
  VaultDrop lock held in both Opus rounds, including where qwen v2's novel
  scheme failed.

**Most repeatable weaknesses:**

- **Semantic confidence exceeds behavioral verification.** Real APIs and
  states are named correctly but used incorrectly: PostgreSQL trigger and
  replication behavior in Atlas, browser restoration and auto-resubmit in
  Pilot, DST/index/API semantics in Social Media, and write-time validation
  treated as a guarantee of read-time integrity in VaultDrop.
- **Schema/prose/walkthrough drift.** Invalid DDL, missing entities, predicates
  that exclude the state they claim to fence, grants that forbid required
  writes, and tests that do not enter their named window recur across all three
  planning sets and VaultDrop.
- **Silent integrity gaps repeat.** VaultDrop's corrupt-read defect survived
  v1 review into v2. This is more concerning than a fabricated symbol because
  the service builds, its own tests pass, and the wrong bytes are served.
- **Version effects are large.** Opus 5 wins Social Media; Opus 4.5 and 4.6 are
  near the bottom of the same field. Treating "Opus" as one stable profile is
  empirically wrong here.

**Best use:** generate architecture options, lifecycle choreography,
operational contracts, and product-completeness checks. Require executable
schema tests and independent semantic probes for every facility or external
API the plan treats as load-bearing. Never accept a named test as evidence
without verifying that its timing reaches the claimed window.

### Comparative conclusion

The strongest supported statement is narrower than either family stereotype:

- Qwen more consistently supplies **structural enforcement and adversarial
  proof intent**, but drops nouns and boundary cases and can become brittle
  when it attempts a novel protocol.
- Opus more consistently supplies **architecture, choreography, completeness,
  and operations**, but is more likely to turn a real facility into a
  confidently wrong guarantee and to let prose drift away from executable
  schema/tests.
- Either can win. Qwen wins Pilot and VaultDrop round 1; Opus 5 wins Social
  Media and Opus wins the VaultDrop v2 head-to-head. The mini suites show no
  functional separation while identities remain masked.

The natural pairing suggested by the evidence is therefore reciprocal review,
not a fixed author/reviewer hierarchy: use Opus to challenge missing product
surface and lifecycle choreography; use qwen to challenge whether the database
and tests actually make the lifecycle unavoidable; then use an execution-only
reviewer to distrust both.

## 12. Verdict on the six pre-registered coding hypotheses

| Hypothesis | Verdict | Evidence |
|---|---|---|
| H1 — qwen fabrication collapses in code | **Supported, with a boundary caveat** | Identified qwen VaultDrop submissions build and run; the core failures are runtime logic or missing validation, not nonexistent imports/APIs. The anonymous mini field also clears every build/run floor. External-capability assumptions remain in plans. |
| H2 — Opus semantic misuse persists in code | **Supported** | Opus's `validated` state was not consulted by downloads; write-time validation was claimed as "no corrupt reads"; several named crash tests never entered their window. These failures survived its own green suite. |
| H3 — qwen omission becomes missing code | **Partially supported** | Read re-verification, wire-limit enforcement, and boundary cases were absent or disclosed in multiple qwen iterations. But v2's worst failure was an implemented design-logic/semantic bug, so omission is not the whole coding profile. |
| H4 — Opus self-inconsistency becomes cross-file drift | **Supported** | VaultDrop's claims register, `validated` field, download handler, and tests disagreed about the integrity guarantee. Planning sets add many schema/walkthrough examples. |
| H5 — Opus wins coding operability/judgment | **Refuted as a family rule** | Qwen v1 had stronger crash tests and clearer invariant factoring; qwen v3's suite was rated strongest in the project. Opus v2 remained clean/readable, so the trait is draw- and version-dependent. |
| H6 — claims-register density differs by family | **Refuted in revealed code** | Qwen registers: 12/14/13 rows. Opus registers: 13/11. Density changes by submission; probe quality predicts more than row count. |

Three better hypotheses emerge from the full suite:

- **H7 — version, effort setting, and chosen design strategy can outweigh
  family.** Already supported by Atlas, Social Media, and VaultDrop; repeat
  generations are needed to estimate how often.
- **H8 — bounded mechanisms converge; system closure differentiates.** The
  mini suites are ties, while broad plan/code evals separate on missing
  entities, external contracts, migration executability, and boundary
  composition.
- **H9 — external ambiguous outcomes are a field-wide hard class.** Pilot,
  Social Media, mini-publisher, and VaultDrop all turn on what happens between
  a committed marker and an external side effect. Models perform well when an
  authoritative audit contract is given; they often invent proof from absence
  when it is not.

## 13. Evaluation and rubric changes justified by the full suite

1. **Add an identity manifest after reveal.** Keep blinded directories during
   grading, then commit a small mapping file. Without it, the mini suites
   cannot update model-specific profiles even though their results are strong.
2. **Require two generations per model/version on broad tasks.** VaultDrop's
   qwen scores of 90, 47, and 82 make single-draw family claims indefensible.
3. **Make schema execution a mechanical floor for plan evals.** All eight
   Social Media plans had a schema/object contradiction. A fresh-database
   migration check should happen before qualitative credit for DDL mechanisms.
4. **Add a prose-noun-to-artifact audit.** Extract named tables, states, queues,
   tokens, files, roles, and endpoints; require each to resolve to DDL/code or
   an explicit future-phase/disclosure entry. This directly targets qwen's
   recurring missing-entity class and Opus's walkthrough drift.
5. **Verify test reach, not test name.** Instrument that the kill/race actually
   lands between the two relevant commits. VaultDrop showed green tests whose
   names promised a crash window they never entered.
6. **Keep diagnostic counters even on PASS.** `mini-inbox`'s 0 versus 21 429s
   is real design information hidden by equal 100s. Correctness remains the
   gate; efficiency and recovery behavior should be reported separately.
7. **Require contract evidence for external absence.** If retry safety depends
   on "not found," the plan must name the authoritative API, pagination and
   consistency semantics, unique match key, and a real-account contract test.
   Otherwise uncertainty must be terminal/fail-closed.
8. **Do not compare raw verbosity or claim counts across domains.** Retain the
   atlas claim-density analysis within that set, but use claims-register rows,
   test reach, and resolved-artifact rates for code. The same grep proxy is not
   valid for SQL migration plans, social-product plans, and executables.

The next highest-value evidence is not another large single plan. It is: reveal
the mini identity mapping; run a second generation on Pilot or Social Media;
complete `erasure-lake` with the same two-reviewer protocol; and use
`cipherboard`/`patchbay` to test whether the missing-entity and external-
contract patterns survive outside PostgreSQL-heavy state machines.
