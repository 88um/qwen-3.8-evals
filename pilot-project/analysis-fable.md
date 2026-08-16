# Pilot Engineering Plans — PM Review and Ranking

Reviewer: product manager (author of `product.md`).
Inputs: `product.md` (the brief), `engineering-plan-standard.md` (the authoring standard all three plans follow), and three plans: `opus/plan.md`, `sonnet/plan.md`, `qwen/plan.md`.

Grading follows the brief's own weighting: the invariant enforcement map and failure-mode behavior count most, because the plan will be adversarially reviewed by engineers who operate a system with this exact behavior. A beautiful plan with a hole in no-double-submit or credit safety loses to a plainer plan without one.

---

## Verdict

*(Scores revised after cross-review against `analysis-gpt.md` — see the Addendum at the end of this document. The ranking did not change; every score moved down.)*

| Rank | Plan | Score | Grade | One-line summary |
|---|---|---|---|---|
| **1** | **Qwen** | **80/100** | **A−** | The tightest mechanisms and the strongest proof story in the section that matters most; loses points for silently omitting a required product feature (full-auto mode), for an unmodeled rendered-artifact/authorization layer, and for a residual duplicate-submission path in the post-click crash window. |
| **2** | **Opus** | **74/100** | **B** | The most complete product coverage and the most operable design, undermined by real correctness flaws an adversarial reviewer would find: a mutated "append-only" ledger, an auto-resubmit crash-recovery path that can double-submit, and DDL that isn't executable as claimed (invalid index predicate, three prose-only schema elements). |
| **3** | **Sonnet** | **59/100** | **C+** | Honest, well-written, and self-aware — but it has demonstrable holes in *three* money/catastrophe paths (negative balances under concurrent holds; double-submit after a post-click crash; a webhook double-credit race on a column with no UNIQUE constraint), the largest ops surface, the most feature misses, and a median cost over budget. |

The ordering was not close between #1/#2 and #3. Between Qwen and Opus it was closer than the grades suggest: Opus is the better *product* plan, Qwen is the better *engineering* plan, and the brief explicitly weights engineering rigor on the invariants most heavily.

---

## How the three plans relate

All three converge on the same broad shape — and the convergence is itself a signal that the shape is right:

- PostgreSQL 16 as the correctness substrate (all three explicitly rejected SQLite for `FOR UPDATE SKIP LOCKED` / row locking, with nearly identical reasoning).
- Immutable, versioned profile snapshots pinned by foreign key from documents and applications. All three get profile version pinning fully right.
- A partial unique index enforcing "one active application per user per job." All three.
- Form-structure fingerprint captured at review, recomputed before submit, block on mismatch. All three.
- EEO data physically quarantined in its own table, never serialized into prompts, exact-match fill only. All three, with credible tests.
- Playwright over Puppeteer/Selenium; SSE over WebSockets; Stripe webhook idempotency keyed on event/payment ID.

Where they diverge is exactly where the grade divides: **what happens in the ugly corners** — the machine dying between "click submit" and "record proof," concurrent credit holds, whether the truthfulness audit is a mechanism or a request, and whether the queue lives inside or outside the transactional boundary.

---

## 1. Qwen — Rank 1, Grade A−

### What it gets right

**The invariant map is the best of the three, and it's not close.** Every row names a structural artifact and a specifically writable test. Three mechanisms stand out as genuinely better designs than the competitors':

- **Credit safety lives in the schema, not in code discipline.** Two partial unique indexes on the append-only ledger — `ledger_one_hold_per_app` and `ledger_one_terminal_per_app` (at most one of {charge, release} per application) — make double-charge *structurally impossible*, backed by a conditional `UPDATE … WHERE balance >= :amt` and a `CHECK (balance >= 0)`. Even if every code path raced, Postgres refuses the second terminal entry. Opus enforces this in transaction logic; Sonnet doesn't enforce it at all (see below). This is the difference between "the code is careful" and "the database won't allow it."
- **The submit path has a paper trail on both sides of the click.** `submission_intents` (UNIQUE per application, inserted *before* the employer submit control is clicked) plus `submission_receipts` (UNIQUE, inserted in the same transaction as the flip to `submitted`) means every crash window leaves inspectable evidence of exactly how far the attempt got. No other plan bounds the "did we actually submit?" ambiguity this precisely.
- **Truthfulness is a deterministic audit, not an LLM opinion.** Generated lines are `{text, source_refs[]}`; `auditDocument()` verifies each ref resolves against the pinned snapshot and that every named skill, credential, date, or number appears (normalized) in the referenced field. Failing lines are *dropped*; a document with failing required lines *fails* rather than publishing. The evidence is a mutation property test — mutate any field of 20 golden profiles and assert at least one line gets flagged — which actually tests the auditor's recall mechanically. This is the strongest answer in any plan to promise #1.

**Failure modes are resolved conservatively and honestly.** Machine B dying mid-submission produces `failed / lease_expired` within one watchdog sweep, with the credit hold released in the same transaction, and retry is *explicit and dedup-guarded* (`dedupCheck()` revisits the employer's confirmation page and gates on a user confirm dialog if evidence of a prior receipt exists). Qwen is the only plan that never auto-resubmits after an ambiguous crash. Given the brief calls a double-submit "catastrophic" and a stuck application merely "almost as bad," failing safe with a manual retry is the correct priority ordering — and Qwen flags the UX cost as explicit tradeoff #2 rather than hiding it.

**The proof story is a launch gate, not a test list.** A chaos suite that SIGKILLs the browser worker and Postgres at random points across the submit window, ≥100 runs required before launch, asserting employer-side receipt count ≤1, zero orphan holds, zero stuck states. 1,000 randomized interleavings for the credit ledger. Weekly *automated* restore drills with schema/row-count diff reports written to `backups_log` — recurring proof instead of a one-time checkbox. This is exactly what §7 of the brief asked for and the others only approximate.

**Deterministic ranking was the right product call.** No LLM in the ranking path: a weighted factor function (skills 0.50, location 0.25, arrangement 0.15, type 0.05, salary 0.05) with template explanations assembled from factor deltas. It directly implements the brief's location rules (ambiguous location → neutral score, stays visible with a flag — the "don't hide jobs on weak inference" requirement, which Qwen alone addresses explicitly), it's regression-testable (golden orderings, zero mandatory-pair inversions, Kendall τ gate), and it costs nothing per feed refresh. The others put LLM calls or embeddings in the ranking path without confronting the fan-out cost.

**Best budget engineering.** Full arithmetic at median ($0.034), P95 ($0.082), and worst-realistic ($0.110 — shown *because* it exceeds budget, to justify the cap), with an enforced dispatcher cap at $0.08 soft / $0.10 hard and defined over-budget behavior. Monthly projection ($43–70) with 3× headroom. Model promotion gates (golden corpora, judge rubric, cost ≤1.5× incumbent) plus nightly drift re-runs and 2% shadow scoring make "quality is measured, not asserted" true.

**Best compliance with the authoring standard.** Qwen is the only plan that handles the standard's own R10 example correctly: it explicitly notes `storageState({path})` persists cookies/localStorage but *not* DOM state, and designs around that limitation instead of assuming it away. Both other plans fall into exactly this trap (see below).

### What it gets wrong

- **Full-auto mode is missing, and the omission is undisclosed.** §3.4 of the brief requires a "full auto" mode: submit authorization granted up front, per job, at queue time, with unmistakable labeling and self-blocking on low confidence. Qwen's review gate requires "the review-authorization token minted by the user's explicit submit click" — full stop. There is no `auto_submit` flag in the `applications` DDL, no queue-time grant anywhere in the plan, and — worse — no entry in §11 (tradeoffs) acknowledging the omission. The standard's R11 is blunt: "A reviewer finding an undisclosed weakening treats it as concealment." This is the single biggest defect in the strongest plan, and it's a *product requirement* miss, which stings more coming from the plan that nailed everything else. (The "low-confidence required answers self-block to review" language in §6.5 gestures at the feature but no mechanism delivers it.)
- **OTP waits hold a browser slot.** The wait pins one of only 4 Chromium context slots for up to 300 s. The brief explicitly asked that OTP handling not become "a resource or reliability problem." Three concurrent OTP waits would throttle the entire submission pipeline to one slot. Qwen mitigates (capacity alarm at ≥3 occupied, per-employer TTL overrides) and its choice is *technically honest* — it declined the storageState-park-and-resume trick precisely because storageState doesn't preserve DOM state — but it accepted a real capacity coupling that the other two at least attempted to avoid. A dedicated OTP-wait slot budget (e.g., waits don't count against the 4 fill slots, capped separately) would have answered the brief better.
- **Model tiers are unnamed.** "Cheap tier / strong tier, OpenAI-compatible, versions pinned in config" with unit prices — but no actual model names. Every other decision in the plan is committed to the level of a function name; the single most scrutinized decision in 2026 ("which model?") is abstracted. The promotion-gate machinery partially excuses this (any pin must pass the gates), but the brief said commit, and Opus/Sonnet both committed.
- **Posting enrichment is unspecified.** `postings.requirements` ("hard/soft requirement strings, normalized skills") drives the entire scorer, yet no task in the §6.1 task map produces it. Ingestion via Greenhouse JSON endpoints yields title/location, not structured requirements. Something extracts them — nothing in the plan says what, which is exactly the kind of "the system enriches postings" gap the standard bans.
- Smaller items: board auto-discovery (brief: "seeded + automatically discovered") exists only as a `discovered_by` column with no discovery mechanism; `application_events.seq` via `MAX(seq)+1` invites serialization contention under concurrent writers (it fails loudly per the UNIQUE, but a per-application advisory lock or a plain bigserial would be cleaner); two machines is more ops surface than one for a non-engineer-adjacent operator, though the crash-domain justification is genuinely good.

---

## 2. Opus — Rank 2, Grade B+

### What it gets right

**The most complete product plan.** Opus is the only plan that fully delivers every §3 feature: full-auto mode with an `auto_submit` flag *and* sensitive-field detection (consent/background-check questions block auto-submit regardless of confidence — the only plan to implement that specific brief requirement), data export, share/bookmark filtered feed URLs, persistent notifications with unread state, an admin role with an audit log, and account deletion with an anonymized `deletion_audit` record. If you diff the three plans against §3 of the brief line by line, Opus has the fewest gaps.

**The most operable deployment for this operator.** One Hetzner box, Docker Compose, three containers, no Redis, Postgres as the only stateful system, htmx instead of a SPA build pipeline. Every choice is argued from the one-operator constraint, and the arguments hold. The "how browser work doesn't starve everything else" section (process isolation + cgroup memory caps + explicit context pool + scraper backpressure when queue depth >20) is the clearest of the three.

**Structurally sound core mechanics.** `SKIP LOCKED` claiming with a reaper, partial unique index for one-active-application, form snapshot + hash drift detection with the full structure stored for diffing (a nice stronger-than-required call), SERIALIZABLE credit holds with a `CHECK (available_credits >= 0)` backstop, webhook idempotency via `ON CONFLICT DO NOTHING RETURNING`. The failure-mode walkthroughs mostly name real mechanisms, and the assumptions table (A1–A12) is the best-organized of the three.

**Truthfulness has a real structural layer.** Generated resume JSON carries `source_path` references into the profile, checked by a validator — same family of mechanism as Qwen's, ahead of Sonnet's.

### What it gets wrong

- **The ledger is mutated, which breaks the ledger.** Charge and release are implemented as `UPDATE credit_ledger SET entry_type='charge' WHERE reference_id=$appId AND entry_type='hold'`. The plan says "single source of truth is ledger" — but an append-only ledger that gets UPDATEd isn't one. After a charge, the user's visible history no longer contains the hold; `balance_after` values on later rows silently stop reconstructing; the §10 daily reconciliation ("sum ledger entries vs. balance table") is now checking against a rewritten history. To be fair, the mutation accidentally provides idempotency (a second charge matches zero rows) — but the right fix is Qwen's: append a terminal entry and let a partial unique index forbid a second one. In the most heavily weighted section, this is a self-inflicted wound.
- **Crash recovery can double-submit.** §5.1 step 12: on recovery, if no "application received" confirmation is found and the form is blank, the worker *re-fills and submits automatically*. Confirmation detection relies on "site-specific selectors in `board_config`" — i.e., per-employer heuristics that will be wrong for some employers, some redesigns, some locales. When detection misses on an application that actually went through, Opus resubmits: the exact catastrophic failure the brief opens with. §11.3 discloses the *re-fill* risk but not the *detection-miss* risk, so the disclosure doesn't cover the dangerous half. Qwen's fail-and-ask design and even Sonnet's re-review design are safer here; Opus alone crosses the line without a human.
- **Truthfulness Layer 3 doesn't block.** Free-text bullets that fail the embedding-similarity check are "logged and reviewed in weekly quality audits." A fabricated paraphrase — the hardest case, the one Layers 1–2 can't catch — ships to the user and gets looked at within a week. For the product's #1 promise, flag-and-review is monitoring where the standard demands prevention. (Minimum fix: hold flagged documents for user-visible review rather than publishing.)
- **LLM match scoring has an unexamined fan-out.** Match scoring is a Sonnet call per user–job pair, cached in `match_scores`. The cost table books it once per *application* ($0.0036) — but the feed needs scores for the whole candidate set. 200 active users × even 2,000 relevant postings × $0.0036 is ~$1,400 before anyone applies, and the brief's ingest rate is 5,000–20,000 postings/day. The standard's R5 says an estimate that doesn't multiply by actual call count is wrong; this one doesn't. There is presumably a pre-filter, but the plan never says so.
- **The OTP park-and-resume mechanism is asserted, not demonstrated.** `browserContext.storageState({path})` persists cookies and localStorage, not the DOM of a half-submitted form with an OTP field showing. The authoring standard uses *this exact API* as its R10 example of a capability that must be qualified. Opus doesn't qualify it. The flow may work for employers where the OTP challenge is a URL-addressable page with server-side session state; it will silently not work elsewhere, and the plan doesn't say which reality it's designed for.
- Smaller items: model naming is internally inconsistent (chooses "Claude Sonnet 4," prices "Claude 3.5 Sonnet"); in-memory rate limits reset on restart (acknowledged); board discovery is manual admin seeding only; subscriptions dropped (properly flagged in §11.5 — fine, the brief said "packs or a subscription").

---

## 3. Sonnet — Rank 3, Grade B−

### What it gets right

**The best candor in the field.** Sonnet is the only plan that computed its cost honestly and reported it over budget ($0.137 median vs the $0.10 target), then showed its mitigation arithmetic instead of massaging the estimate. Its §11 tradeoffs (drift detection ignores dropdown-option changes; flat 1-credit pricing is unfair across job complexity; single-server has no failover) and its 20-item assumptions list are the most genuinely self-critical writing across all three documents. The brief rewards flagged weaknesses over hidden ones, and Sonnet flagged the most.

**The audit-as-adversary framing is right, even if the mechanism is weak.** The two-stage generate-then-audit pipeline, with the auditor prompted to assume the draft is lying and required to cite sources, plus a synthetic-violation recall suite (50 deliberate fabrications, 100% catch required, re-run weekly) — that's a real evaluation culture, and the §12 justification ("single-model 'don't lie' is a request, not a guarantee") shows correct instincts.

**Good chaos-testing culture.** Nightly chaos scenarios (duplicate tasks, DB kill, webhook replay, browser hangs), a 100-run pre-launch race test, a 20-run crash-recovery gate, and — uniquely — a *post-launch* daily monitor query for duplicate `submitted` events. The pre-launch checklist committed to the repo with checkmarks is a nice operator-shaped touch.

**Defense-in-depth on the submit transition.** Optimistic `row_version` locking plus the partial unique index plus `FOR UPDATE` in the critical path is redundant by design and cheap, and the plan argues that redundancy well.

### What it gets wrong

- **Credit safety has a textbook TOCTOU hole.** The hold flow is: `SELECT SUM(amount)` to check the balance, then a separate `INSERT` of the hold. No lock, no conditional update, no balance column with a CHECK constraint — nothing serializes two concurrent holds for the *same user on different applications*. A user with 1 credit queuing two applications simultaneously passes both balance checks and goes negative. The brief names this exact scenario ("Balances can never go negative … including under … concurrent activity"), and the plan's own load test ("5 credits, queue 10 concurrently, assert exactly 5 holds") asserts an outcome that no mechanism enforces — the test would fail intermittently and the intermittency would be the bug report. In the section weighted most heavily, this is disqualifying for the top spot on its own.
- **The crash window after the submit click is unhandled.** Scenario 1 carefully kills the worker "after filling form, *before* HTTP POST." But if the crash lands after the employer accepted the POST and before `submission_proof` is written, the cleanup task resets the application to `ready_for_review`, the user re-approves, and the system submits again. Unlike Opus (confirmation detection) and Qwen (`dedupCheck()` + confirm dialog), Sonnet has *no employer-side evidence check anywhere in recovery*. The narration "requeue resets to pre-submit state … without risking double-submit" is true only for the half of the crash window the scenario chose to test. An adversarial reviewer asks about the other half in the first five minutes.
- **Truthfulness is enforced by an LLM's opinion.** There is no structural provenance — no source refs, no deterministic validator. The auditor model cites passages, and a threshold says "100% recall, zero false negatives tolerated." A threshold is a measurement, not a mechanism; you cannot spec an LLM to a guaranteed 100% and the brief asked for "provably truthful." Weakest of the three on promise #1.
- **The stack is the heaviest for the smallest team.** Celery + Redis + PgBouncer + Prometheus/Grafana alongside Postgres and Playwright — two additional stateful systems and a metrics stack for a one-operator deployment, where both competitors got to one stateful system. The justification for rejecting a Postgres queue (long browser tasks would "park tasks in the database" and create long-lived transactions that bloat WAL) misunderstands the SKIP LOCKED lease pattern: the claim transaction is milliseconds; nothing holds a transaction for the duration of the job. So the plan pays real complexity (Celery task state and DB state can now disagree — a split-brain neither other plan has) for a problem that doesn't exist.
- **Feature misses, several unflagged.** No data export (§3.6 requires it — absent entirely). Admin role explicitly deferred ("admin is a future feature") when the brief states "an admin role exists" at launch. No share/bookmark filtered-view URLs. No persistent notification center with unread indicator (the `events` table deletes rows on delivery — it's a transport, not a notification system). Uploaded files left unencrypted on disk, with LUKS explicitly deferred, against "protect them at rest" (this one at least is disclosed). Individually small; collectively the widest gap from the brief's feature list, and only some appear in §11.
- **Sloppiness the others don't have.** The `awaiting_otp` status used in Phase 8 isn't in the `applications.status` CHECK constraint — the plan's own DDL rejects its own OTP flow. The RAM budget ("4 + 4 + 2 + 8 + 2 + 1 = 21 GB") doesn't match its own component specs (the heavy worker is specced at 12 GB; the real sum is 29 GB). "Anthropic's voyage-2 embeddings" (Voyage is not Anthropic), "Claude 3.5 Sonnet extended thinking" (not a 3.5 capability), PyPDF2 (deprecated in favor of pypdf). None fatal alone; together they read as a plan that wasn't checked against itself, which is what the standard's final checklist exists to prevent.
- Same R10 failure as Opus on OTP: `storageState` park-and-resume without addressing DOM-state loss.

---

## Head-to-head on the decisive dimensions

*(Revised after cross-review against `analysis-gpt.md`; original scores preserved in the Addendum.)*

| Dimension (weight) | Qwen | Opus | Sonnet |
|---|---|---|---|
| Invariants & failure modes (35%) | **8/10** — schema-level barriers, intent/receipt evidence, fail-safe recovery; residual post-click retry risk, authorization grant unmodeled in schema | 6.5/10 — solid core; ledger mutation + auto-resubmit exposure | 4.5/10 — TOCTOU credits, post-click crash window, webhook double-credit race (no UNIQUE on `stripe_payment_id`) |
| Feature completeness (15%) | 6/10 — full-auto missing (undisclosed), no rendered-artifact storage model, no posting snapshot | **8.5/10** — broadest coverage, but no documents entity (generation state/audit/approval) | 6.5/10 — export, admin, share-URLs, notifications missing |
| Architecture & one-operator fit (15%) | **8/10** — two machines, clean; more surface than one box | 8/10 — simplest viable stack, but "executable DDL" claim broken (invalid `NOW()` index predicate; three prose-only schema elements) | 6.5/10 — Celery+Redis on a wrong premise; ghost `applications.deleted_at` |
| AI quality & cost strategy (15%) | **8.5/10** — enforced caps, promotion gates, deterministic ranking; unnamed models, qualitative claims can escape the audit | 7/10 — good pipeline; ranking fan-out unexamined, Layer 3 non-blocking, over-optimistic cache assumption | 6.5/10 — honest but over budget; LLM-only audit |
| Testing & release proof (10%) | **9.5/10** — chaos gate, property tests, automated restore drills | 8/10 — invariant tests + checklist | 7.5/10 — good culture; some tests assert unenforced behavior |
| Delivery phases (5%) | **9/10** — E2E at Phase 4; backups in Phase 0, justified | 8.5/10 — E2E at Phase 6, well-justified chain | 7.5/10 — E2E at Phase 6 of 11; security last |
| Security & privacy (5%) | **8.5/10** — LUKS + app-layer AES-GCM, scoped-query lint; composite-FK tenant-integrity gap | 8/10 — solid, LUKS, good headers; admin flag absent from `users` DDL | 5.5/10 — file encryption deferred, 24h JWT, no roles, unsanitized cross-user context reuse |
| **Weighted total** | **8.0 → 80/100 — A−** | **7.4 → 74/100 — B** | **5.9 → 59/100 — C+** |

---

## What I'd actually do with these plans

The strongest executable plan is **Qwen's skeleton with two Opus transplants**:

1. **Adopt Qwen wholesale** for: the data model (especially the ledger partial uniques and intent/receipt pair), the watchdog/lease design, the deterministic truthfulness audit, the deterministic scorer with its measurement regime, the chaos gate, and the cost caps.
2. **Transplant Opus's full-auto design** (the `auto_submit` queue-time flag, the confidence gate, and — critically — the sensitive-field detector that blocks auto-submit on consent/background questions regardless of confidence). This closes Qwen's one product-requirement hole with the best implementation of it in the field.
3. **Transplant Opus's feature completions**: data export, notification center semantics, share-URL feed views (Qwen has `feed_views` but Opus specifies the UX), and the deletion-audit shape.
4. **Reject from both**: Opus's ledger mutation (keep Qwen's append-only terminals) and Opus's auto-resubmit recovery (keep Qwen's dedup-guarded explicit retry). Take Sonnet's post-launch duplicate-submission monitor query as a cheap standing safety net, and Sonnet's synthetic-violation recall suite as an *additional* check layered on Qwen's deterministic audit.
5. **Force three answers before build**: (a) which pinned models, by name, sit in Qwen's cheap/strong tiers; (b) what populates `postings.requirements`; (c) whether OTP waits get their own slot budget so three simultaneous challenges can't throttle submissions to one lane.

Sonnet's plan contributes culture more than mechanism — its candor about cost and tradeoffs, and its instinct to keep adversarial checks running nightly and post-launch, are worth keeping even though its core enforcement story doesn't survive review.

---

## Addendum — cross-review of `analysis-gpt.md` and revised scores

A second review of the same three plans (`analysis-gpt.md`, scored Qwen 78 / Opus 56 / Sonnet 45) was reviewed against the source plans. Its ranking matches mine. Its material new claims were **verified line-by-line against the plans before being accepted** — none were taken on faith — and all of the following check out:

### Verified findings adopted from analysis-gpt (things I missed)

| # | Finding | Verification |
|---|---|---|
| 1 | **Opus's DDL is not executable.** The partial index `job_queue_pending_idx … WHERE status = 'pending' AND scheduled_for <= NOW()` is invalid PostgreSQL — index predicates require immutable functions. | `opus/plan.md:477-478`. Confirmed. A plan claiming schema-level rigor whose schema doesn't load is a material authoring failure. |
| 2 | **Opus has three prose-only schema elements.** `application_ai_cost` (cost-cap enforcement, §6.5), the admin boolean on `users` (§9.2), and enforced profile-version immutability (§12.1 claims rows are "never updated" while the `is_draft` flag design requires updating them) all exist in prose but not in the DDL. | `opus/plan.md:757`, `:1135`, `:237` vs the `users`/`applications` CREATE TABLEs. Confirmed. |
| 3 | **Sonnet's webhook idempotency is a race on an unconstrained column.** `stripe_payment_id` has no UNIQUE constraint; the handler is check-then-insert. Concurrent webhook deliveries can both credit the account — a third broken money path beyond the two I found. | `sonnet/plan.md:270` (`stripe_payment_id TEXT`, no UNIQUE) vs the §5.5 SELECT-then-INSERT flow. Confirmed — and Sonnet's own risk register calls it a "unique check" that its DDL never creates. |
| 4 | **Sonnet's deletion flow updates a nonexistent column.** `UPDATE applications SET deleted_at = NOW()` — `applications` has no `deleted_at`. | `sonnet/plan.md:847` vs the applications DDL. Confirmed. |
| 5 | **Sonnet reuses browser contexts across users with no sanitization protocol** — cookie/localStorage cross-user leakage risk. (Opus's pooled contexts deserve the same question; Qwen alone specifies fresh context per attempt.) | `sonnet/plan.md` §2.2. Confirmed. |
| 6 | **Qwen has no storage model for rendered artifacts.** `documents.content` is the structured model; `page.pdf({path})` renders to a path that no table references. `encrypted_bytes` exists only on uploaded `resumes`. The plan promises downloadable, encrypted PDF/DOCX with no home for the bytes. | Qwen DDL + §1.7. Confirmed. |
| 7 | **Qwen's review-authorization token is asserted, not modeled.** No token table, hash, expiry, application binding, or consumption constraint in the schema — the review gate's central artifact is prose. | Qwen §4 row 2 vs DDL. Confirmed. |
| 8 | **Qwen doesn't snapshot the posting.** Applications pin profile version and preferences, but `postings` rows mutate on change — the job-side basis of a historical document isn't frozen. | Qwen DDL. Confirmed. |
| 9 | **Sharper framing of the post-click crash window.** GPT's strongest analytical contribution: even Qwen's `dedupCheck()` + user-confirm-dialog retry does not fully restore the no-double-submit promise when the employer exposes no status page — absence of visible confirmation is not proof of non-submission, and a user guessing wrong still duplicates. The correct design is a distinct `submission_uncertain` terminal state that keeps the hold pending and forbids ordinary retry without authoritative employer-side proof. I had credited Qwen's flow as "safe-ish"; GPT is right that it is only *least bad*. One caveat GPT underplays: Qwen *disclosed* this weakening (§11.2–3), which the authoring standard treats very differently from concealment — so it costs Qwen less than the same hole costs Opus and Sonnet, who don't acknowledge theirs at all. |

Items 1–9 drove the score revisions: **Qwen 84→80, Opus 78→74, Sonnet 62→59** (ranking unchanged).

### Where I disagree with analysis-gpt

1. **Its Qwen–Opus gap (22 points) is too wide.** GPT's rubric has no explicit weight for feature completeness against §3 of the brief, so Opus's status as the only plan delivering full-auto with sensitive-field gating, data export, share URLs, a notification center, and an admin role earns almost nothing, while Sonnet's identical-magnitude feature misses are also barely priced. This is a product brief; a PM grades coverage. My gap is 6 points (80 vs 74) with the same #1.
2. **Opus 56/100 overshoots the penalty.** Its two disqualifying flaws (auto-resubmit, ledger mutation) are P0s, but they are *localized and fixable* — the surrounding architecture, phase plan, and ops story are the best in the field. A 56 implies a plan half-wrong; Opus is a strong plan with two wrong mechanisms and a sloppy DDL pass.
3. **Sonnet 45/100 undercounts real assets.** The candor (only plan to self-report a budget miss), the chaos-test culture, the post-launch duplicate-monitor query, and the synthetic-violation recall suite are worth more than 45 implies, even though its enforcement story fails. I land at 59: clearly last, not worthless.

### What my review caught that analysis-gpt missed

For the record of a merged review: Sonnet's `awaiting_otp` status is used in Phase 8 but absent from its own `applications.status` CHECK constraint; Sonnet's RAM budget arithmetic contradicts its own component specs (21 GB claimed vs 29 GB specced); Sonnet's factual errors (voyage-2 attributed to Anthropic, "extended thinking" on Claude 3.5, deprecated PyPDF2); Opus's LLM-per-pair match-scoring feed fan-out cost (GPT gestures at "model-heavy ranking" but never runs the multiplication that breaks the budget); and Qwen's unspecified mechanism for populating `postings.requirements`, the input its entire deterministic scorer depends on.

### Revised P0 list (superset, merged)

The ten P0 changes in analysis-gpt §"P0 changes required before implementation" are all endorsed — in particular #1 (`submission_attempts` state machine with `click_issued`/`uncertain` and no ordinary retry from `uncertain`), #2 (durable `application_authorizations` rows covering queue-time full-auto grants), #9 (chaos-kill *between employer commit and local receipt persistence*, asserting zero second POSTs at the employer), and #10 (uncertain attempts keep the hold pending rather than releasing it). Added from my review: name the pinned models, specify posting-requirements enrichment, give OTP waits a reserved capacity budget, and define a context-sanitization rule for any pooled browser reuse.
