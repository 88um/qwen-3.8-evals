# Comparative product and engineering review: Pilot plans

## Executive verdict

**Ranking:**

1. **Qwen — 77/100**
2. **Opus — 63/100**
3. **Sonnet — 47/100**

Qwen is the clear winner. It is the only plan that consistently turns requirements into named schema constraints, transactions, worker leases, tests, cost arithmetic, and measurable exit criteria. It is also the only one that is close to the requested authoring standard throughout.

However, **none of the three plans is safe to implement unchanged**. All three fail the hardest adversarial case: the browser clicks the employer's submit control, the employer accepts the application, and Pilot crashes before recording the receipt. Because Pilot and the employer do not share a transaction or employer-supported idempotency key, internal locks and unique indexes cannot prove whether that external side effect happened. Each plan eventually allows a path that can submit again without authoritative proof. That violates the highest-severity promise: never double-submit.

The product recommendation is to use Qwen as the base plan, but block implementation until its submission protocol, durable authorization model, generated-artifact storage, and board-discovery design are rewritten. These are comparative plan-quality scores, not estimates of percent launch-ready. They were recalibrated after two rounds of cross-review with `analysis-fable.md`: the ordering is unchanged, but feature completeness now receives an explicit 15% weight.

## Grading rubric

The plans were graded against both `product.md` and `engineering-plan-standard.md`.

| Category | Weight | What earned credit |
|---|---:|---|
| Core promises and hard invariants | 35 | Truthfulness, per-application authorization, no-double-submit, form fidelity, credit safety, EEO quarantine, version pinning, crash recovery, OTP |
| Feature completeness | 15 | Coverage of every §3 workflow, including full-auto, export, notifications, admin, account deletion, feed URLs, discovery, and artifact approval |
| Architecture and data model | 15 | Executable and complete DDL, process isolation, async mechanics, state ownership, auditability |
| AI, ranking, and cost strategy | 12 | Deterministic constraints, evaluation corpora, regression gates, calibrated confidence, multiplied-out costs |
| Failure handling and testing | 10 | Worst-window walkthroughs, adversarial evidence, chaos and concurrency tests |
| Security, privacy, and operations | 8 | Tenant isolation, encryption, deletion, secrets, backups, restore, operator tooling |
| Delivery quality and authoring-standard compliance | 5 | One committed choice, strongest rejected option, numbers, named APIs, evidence-based phases, disclosed tradeoffs |

Any plan that does not close the post-click/pre-receipt ambiguity cannot receive an A, regardless of how polished the rest of the document is.

## Scorecard

| Plan | Invariants /35 | Features /15 | Architecture + data /15 | AI + cost /12 | Failure + tests /10 | Security + ops /8 | Delivery + standard /5 | Total |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Qwen | 23 | 11 | 13 | 10 | 9 | 7 | 4 | **77** |
| Opus | 16 | 13 | 10 | 7 | 7 | 6 | 4 | **63** |
| Sonnet | 10 | 8 | 7 | 7 | 7 | 4 | 4 | **47** |

## Cross-review update after `analysis-fable.md`

Fable's review confirms the same ordering and adds several valid findings that are incorporated below:

- Qwen never identifies the producer of `postings.requirements`, even though those structured requirements drive matching and document generation. Its `MAX(seq) + 1` event numbering can also fail under concurrent writers and needs an advisory lock, counter row, or retry protocol.
- Opus's LLM match-scoring cost is counted once per eventual application rather than once per scored user–job pair. Without a deterministic candidate prefilter, its feed economics can exceed the stated application budget by orders of magnitude.
- Sonnet uses `awaiting_otp` even though that value is absent from the application's status constraint, understates its own memory allocation by 8 GB, omits data export and a launch admin role, and explicitly leaves uploaded/generated files unencrypted at rest.

I do not adopt Fable's A− grade for Qwen. Fable treats “explicit and dedup-guarded retry” as sufficient because Qwen never *automatically* resubmits. That distinction does not satisfy the product promise. If confirmation detection returns a false negative, Pilot still enables a second employer-side POST after the first may have succeeded. Human confirmation changes who initiated the duplicate; it does not make the duplicate impossible. Qwen remains the clear winner, but this is a launch-blocking invariant failure rather than a minor UX tradeoff.

Fable's second-round criticism of my original 22-point Qwen–Opus gap is valid as a scoring-calibration point. Opus earns substantial credit for full-auto gating, persistent notifications, export, admin tooling, and share/bookmark UX. The revised gap is 14 points. I do not narrow it to six: Opus is not literally feature-complete (automatic board discovery and a proper generated-document entity remain absent), and its invalid DDL, unsafe automatic post-crash resubmission, mutable ledger, non-working generic OTP restoration, and unbounded feed-scoring economics span multiple core mechanisms rather than two isolated fixes. Sonnet also receives more credit for candor and testing culture, while its three catastrophic invariant holes still cap the result.

## 1. Qwen

### What it gets right

Qwen best understands the assignment's central demand: show mechanisms, not aspirations.

- Its technology decisions consistently use the required **Choice / Rejected / Why** structure. PostgreSQL plus a Postgres job queue is a strong one-operator choice because application state, leases, and credit mutations remain inspectable in one datastore.
- The two-machine deployment is the best topology of the three. Keeping Chromium on Machine B means a browser OOM does not also kill PostgreSQL. This is a meaningful product-safety choice, not architecture for architecture's sake.
- The data model is the broadest and most reviewable. It includes immutable profile versions, drafts, quarantined demographics, application events, submission intents and receipts, OTP waits, credit accounts and ledger entries, purchases, webhook events, jobs, notifications, alarms, backup reports, deletion records, and AI costs.
- Credit handling is substantially stronger than the other plans. The conditional account update, nonnegative check, one-hold and one-terminal-entry partial unique indexes, and randomized concurrency test form a coherent safety story.
- EEO quarantine is handled well: physically separate storage, prompt allowlisting, scorer input that cannot accept demographics, exact option matching, and explicit tests for prompt leakage and ranking equality.
- Its form-drift fingerprint includes control type, label, requiredness, and option sets. This correctly treats changed dropdown options as form drift; Sonnet explicitly defers that requirement.
- The AI quality strategy is the best of the three. It defines golden sets, promotion thresholds, a deterministic ranking function, ranking regression metrics, strong-model escalation, per-call validation, and per-application cost caps. The cost section shows arithmetic for median, P95, worst-realistic, and monthly volume.
- Delivery is evidence-led. The first safe-employer end-to-end slice arrives in Phase 4, and the prerequisites are justified. The backup/restore drill is introduced before real user data accumulates.
- Operationally, the watchdog, worker heartbeats, queue-depth/error/backup alarms, weekly restore drills, and admin-facing state are concrete enough for a solo operator.

### What it gets wrong

The largest flaw is its no-double-submit recovery story.

Qwen inserts a unique `submission_intents` row before clicking and a unique `submission_receipts` row after observing success. Those constraints prevent two local workers from racing, but they do not make the employer's external submission atomic with Pilot's database. If the employer accepts the form and Machine B dies before the receipt insert, Pilot has an intent and no receipt. Qwen marks the application failed, releases the credit, and later allows an explicit retry after `dedupCheck()`. When the employer exposes no authoritative status page, absence of visible confirmation is not proof of non-submission. Retrying can submit twice. A user confirmation dialog does not restore the product promise.

The correct safe terminal state is something like `submission_uncertain`, not ordinary `failed`. Once the click has been issued, Pilot must never issue another click unless it obtains authoritative employer-side proof that the first attempt did not succeed. If the ATS offers neither an idempotency key nor a reliable status API, the application remains blocked for operator/user reconciliation. That is less convenient, but it preserves the stated ordering of promises.

Other important gaps:

- **Full-auto authorization is not modeled.** There is no `auto_submit`/authorization-grant field or table in the DDL. The review-gate row mentions a token minted by the explicit submit click, but an asynchronous worker needs durable evidence of who authorized what application, in which mode, at what time, against which reviewed form/answer hash, and whether the grant was revoked. Queue-time authorization is absent.
- **The authorization token mechanism is asserted more than shown.** No token table, hash, expiry, application binding, actor, or consumption constraint appears in the schema.
- **Generated artifacts have no storage model.** `documents` stores structured JSON, but no PDF/DOCX file reference, encrypted bytes, checksum, render version, or separate artifact rows. The plan promises async rendering and downloads without showing where both formats live.
- **Submission proof is too thin.** The receipt stores a success URL and hashes, while the exact approved answers sit elsewhere. It does not define an immutable proof bundle containing the submitted field/value manifest, uploaded artifact checksums, form fingerprint, timestamps, and browser evidence.
- **Truthfulness validation is better than the other plans but not sufficient for “every claim.”** Checking cited paths plus named skills, credentials, dates, and numbers can still admit unsupported qualitative or causal claims such as “led,” “improved,” “expert,” or “increased efficiency.” The plan needs a typed, user-approved atomic-claim model or a deterministic entailment policy that rejects any generated predicate not grounded in an approved claim.
- **OTP waits consume the scarce pool.** Holding one of four browser contexts for five minutes means four simultaneous OTP challenges exhaust all submission capacity. A separate capped waiting pool or a deliberately reserved OTP capacity budget is needed. Persisting cookies alone is not enough to reconstruct DOM state, so releasing all contexts is also not automatically safe.
- **Automatic career-board discovery is missing.** The schema records `discovered_by`, but no discovery process, seed expansion rule, validation mechanism, queue kind, schedule, or exit test exists. All three plans miss this requirement; Qwen merely leaves the clearest placeholder.
- **Posting enrichment has storage but no producer.** `postings.requirements` is the central input to the scorer, yet the AI task map has no job-description parsing/enrichment task and the deterministic scorer cannot derive the field itself. The plan therefore measures a ranking pipeline whose key input-generation mechanism is unspecified.
- **Job and preference bases are not fully snapshotted.** Applications pin the profile and preferences, but the referenced posting remains mutable. To answer what job and requirements drove a historical document/application, the application or document needs an immutable posting snapshot/version.
- **Cross-tenant relational integrity is incomplete.** Separate foreign keys allow, at the database level, an application for user A to reference user B's profile version or document. Application code scopes reads, but composite ownership constraints would make the invariant structural.
- **The AI tiers are not actual model decisions.** “Cheap tier” and “strong tier” plus a config file preserve flexibility, but the standard asked for committed model choices. Pinning exact model identifiers is required at implementation time.
- **Event sequence allocation needs a concurrency mechanism.** `MAX(seq) + 1` plus a unique index detects a race but does not resolve it. The event insert needs a per-application advisory lock/counter row or a specified retry on the unique violation.

### Strengths

- Most implementable plan
- Best schema and invariant map
- Best cost discipline and ranking measurement
- Best worker isolation and restore practice
- Best compliance with the supplied authoring standard

### Weaknesses

- Still unsafe in the ambiguous post-click crash window
- Missing durable full-auto authorization
- Incomplete generated-file and submission-proof model
- Truthfulness checker does not cover all semantic claims
- OTP capacity and automatic board discovery are unresolved

### Product verdict

**Advance this plan to revision.** It is a credible foundation, but the launch gate remains closed until the P0 invariant defects are corrected.

## 2. Opus

### What it gets right

Opus is generally decisive and concrete. Its TypeScript/Node/Postgres/Playwright stack fits the browser-heavy product and the one-operator constraint. The Postgres queue is simpler to audit than a Redis-backed workflow, and process/container caps at least separate web, application automation, and scraping workloads.

Its stronger elements are:

- A useful form snapshot table containing the full structure plus a canonical hash.
- A durable notifications table and unread indicator support.
- Explicit auto-submit gating at queue time, including a high confidence threshold and a hard block for sensitive/consent questions.
- A serializable credit-hold transaction that creates the application and reserves the credit together.
- A real form-drift return-to-review flow rather than simply discarding the application.
- Good coverage of profiles, user preferences, EEO data, files, boards, postings, applications, form snapshots, events, OTP, credits, queue jobs, notifications, admin actions, and deletion audit.
- A phased delivery plan with verifiable exits and a clearly identified first end-to-end phase.
- Better document provenance than Sonnet conceptually: generated fields carry source paths and a validator checks them.

### What it gets wrong

Its no-double-submit mechanism fails directly. The plan says that after a crash, if it cannot find a confirmation and sees a blank form, it re-fills and submits. A blank form is not proof that the previous POST was rejected; many ATS sites show a fresh form on revisit even after a successful application. “Reviewed answers are immutable” proves fidelity, not idempotency. This path can send a duplicate application.

The plan compounds this with retries after browser hangs and OOMs without separating pre-click failures from post-click uncertainty. A job-level retry policy cannot be applied uniformly to an external side effect.

Other major issues:

- **OTP resumption relies on a false capability assumption.** `browserContext.storageState()` preserves cookies and local storage, not the DOM, in-memory JavaScript state, a filled multi-step form, or a parked network request. Closing the context and later reopening it cannot generally “continue” the same OTP flow. The plan would need an ATS-specific restart-and-refill protocol whose safety is demonstrated, or it must keep the context alive in a separately capped waiting pool.
- **The DDL is not executable as written.** The partial index predicate `scheduled_for <= NOW()` is invalid in PostgreSQL because index predicates cannot use volatile/non-immutable time functions. Claiming executable DDL while including this statement is a material authoring failure.
- **There is no `documents` entity.** Generated resumes and cover letters are reduced to generic `files`, leaving no generation state, kind/version uniqueness, audit report, approval timestamp, profile/job basis, or relationship between PDF and DOCX variants.
- **Published profile immutability is not enforced.** Draft and published versions share one table with mutable `is_draft` and `data` columns. The plan says versions are immutable but supplies no trigger, privilege boundary, or update prohibition.
- **Preferences are mutable and not snapshotted on the application.** Historical answers can lose their original basis.
- **The credit ledger is mutated in place from `hold` to `charge` or `release`.** That destroys the append-only audit trail the product asks users to see. There are also no unique per-application constraints preventing multiple hold/terminal records if another code path inserts them.
- **Truthfulness validation is underdefined for prose.** Source paths prove where the model pointed, not that a rewritten bullet is entailed by the source. The optional “Claude embedding model or local sentence-transformers” is both an option menu and a weak safety check: semantic similarity is not factual entailment, and the plan does not name an actual Claude embedding API or model.
- **The cost math assumes a 90% prompt-cache discount too broadly.** It treats nearly every Sonnet input as cached even though cache hits depend on exact eligible prefixes, minimum cacheable sizes, ordering, and timing. It also names an `application_ai_cost` column that is absent from the DDL.
- **Match-scoring economics use the wrong unit of work.** The table charges one Sonnet score call to an application, but a ranked feed needs scores for many jobs the user never applies to. No deterministic candidate prefilter is defined. Even 200 users × 2,000 candidates × $0.0036 is about $1,440 per refresh cycle, before document or form costs.
- **One physical machine remains one crash domain.** Containers protect resource allocation, but power loss, kernel failure, and disk failure take down PostgreSQL and all workers together.
- **The security section references an admin boolean that is absent from `users`.** Several other mechanisms similarly appear in prose without schema support.
- **Automatic board discovery is absent.** The delivery plan supports manual board seeding only.

### Strengths

- Decisive stack and reasonable one-operator shape
- Good form snapshot and notification concepts
- Better auto-submit treatment than Qwen
- Useful phased build plan
- Credit reservation transaction starts from the right concurrency model

### Weaknesses

- Unsafe automatic resubmission after an ambiguous external side effect
- Non-working OTP session restoration model
- Invalid PostgreSQL DDL
- Missing document/audit/approval data model
- Mutable ledger and profile versions contradict audit claims
- Cost and truthfulness mechanisms are less rigorous than they appear

### Product verdict

**Do not implement without a substantial redesign.** It contains reusable ideas, especially around form snapshots and explicit auto-submit gating, but too many core mechanisms are unsafe or non-executable.

## 3. Sonnet

### What it gets right

Sonnet is readable, comprehensive at a feature-list level, and unusually candid about tradeoffs. It correctly identifies PostgreSQL, Playwright, immutable profile snapshots, an application event trail, a review state, form snapshots, credit holds, SSE, backup/restore drills, chaos tests, and a safe fake employer as important building blocks.

Specific positives:

- It clearly separates profile drafts from published versions and pins matches, documents, and applications to a version.
- It includes an `auto_submit` flag and explains the missing-required-field block.
- Its failure walkthroughs are easy to follow and usually end with inspectable evidence.
- It calls out its own LLM budget miss instead of hiding it.
- It provides explicit quality metrics for extraction, ranking, truthfulness recall, and form-edit rates.
- It includes the requested 13 sections and uses the required decision format.

### What it gets wrong

Sonnet fails all three top promises at the mechanism level.

**No-double-submit:** the stale-submission cleanup resets `submitting` to `ready_for_review`, after which the user re-approves and the browser submits again. Its chaos test kills the worker only before the HTTP POST, carefully avoiding the dangerous case after employer acceptance and before Pilot persistence. The claim that this “recovers stuck work without risking double-submit” is false.

**Credit safety:** the hold flow reads `SUM(amount)` and then inserts a hold without locking a balance row or using serializable isolation. Ten concurrent queues can all observe the same five-credit balance and all insert holds. The plan's own concurrency test would expose this. Stripe idempotency is also a check-then-insert against a column with no `UNIQUE` constraint, so concurrent webhook deliveries can both credit the account.

**Truthfulness:** the final authority is a second LLM asked to cite profile passages. A model-generated citation is evidence for a reviewer, not a deterministic enforcement boundary. Nothing prevents the auditor from approving an unsupported paraphrase or citing a vaguely related passage. The plan tests only a few synthetic missing-skill examples and then asserts the promise.

Additional problems:

- `storageState` is again treated as if it persisted the active form and OTP flow. It does not persist DOM or JavaScript state.
- Form drift intentionally omits dropdown option changes, even though the brief explicitly requires that the reviewed form be what is submitted. This is a disclosed but unacceptable weakening of a hard rule.
- The event delivery design deletes events after sending them and deletes all events older than one hour. It has no durable notifications table, so unread indicators and disconnected-user notifications are not correctly supported.
- Persistent browser contexts are reused across user tasks without a specified teardown/sanitization protocol, creating cookie, storage, and cross-user leakage risk.
- The one-machine topology puts the database, Redis, web process, and browser workers in the same host failure domain.
- The OTP flow transitions to `awaiting_otp`, but that value is absent from the `applications.status` CHECK constraint, so the supplied DDL rejects the planned transition.
- The stated memory total is internally inconsistent: `Worker-Heavy` is allocated 12 GB, but the arithmetic counts 4 GB, understating base allocation by 8 GB.
- The Celery/Redis rejection of a Postgres queue is based on long-lived database transactions, but a correct lease-based Postgres queue does not hold a transaction for the duration of browser work.
- The generated-document table has one `file_path`, no PDF/DOCX artifact pair, no generation number/uniqueness, and no approval field despite the required review-and-approve flow.
- The deletion procedure updates an `applications.deleted_at` column that does not exist in its DDL. Its stated cascades are also not declared with `ON DELETE CASCADE`.
- Data export and a launch admin role are absent, shareable filtered views have no durable model, and the plan explicitly defers filesystem encryption for uploaded resumes and generated documents despite the at-rest protection requirement.
- The AI plan knowingly prices the full median application at $0.1371 and sets the hard cost cap at $0.50, five times the requested target. Making cover letters optional does not satisfy a brief that includes cover-letter generation in the prepared-application experience.
- Several technology claims are overstated or inaccurate: Playwright storage state does not enable form continuation, and model self-citation/“extended thinking” is not a product-level truth guarantee.
- Automatic board discovery is absent; a delivery phase begins with only 10 seeded boards despite the launch target of roughly 500.

### Strengths

- Clear prose and transparent tradeoffs
- Good high-level feature coverage
- Useful profile-versioning foundation
- Concrete test names and evidence format

### Weaknesses

- Broken concurrent credit reservation and webhook idempotency
- Catastrophic duplicate-submission recovery path
- LLM-as-auditor is not a truthfulness enforcement mechanism
- Durable notifications, document approval/versioning, and deletion are incomplete
- Cost target is knowingly missed
- Several core API assumptions are incorrect

### Product verdict

**Reject as the implementation plan.** It is useful as a requirements checklist, but the most important guarantees are asserted rather than structurally enforced.

## Cross-plan comparison by hard requirement

| Requirement | Qwen | Opus | Sonnet |
|---|---|---|---|
| Truthfulness | Best: source refs plus deterministic checks, but semantic claim coverage is incomplete | Source-path validator plus unsafe similarity heuristic | Second LLM audits first LLM; weakest enforcement |
| Explicit authorization | Manual gate described, but durable/full-auto grant missing from schema | Auto-submit flag and confidence/sensitive gate; audit detail still thin | Auto-submit flag and review state exist; authorization evidence not durable enough |
| Never double-submit | Best local concurrency controls, but unsafe explicit retry after ambiguous click | Automatically re-submits if no confirmation; unsafe | Requeues for user reapproval and resubmission; unsafe |
| Form fidelity | Strong canonical fingerprint including options | Full form snapshot and hash | Intentionally omits option-set changes |
| Credit safety | Strongest constraints and concurrency tests | Serializable hold is good; mutable ledger and missing uniqueness are weak | Broken check-then-insert hold and webhook flows |
| EEO quarantine | Strongest allowlist and property tests | Good separate DAO, but JSON exact-match query is dubious | Separate table and tests; grep-based enforcement is brittle |
| Profile pinning | Good immutable snapshots; posting basis still mutable | App pins profile, but draft/published rows remain mutable | Good snapshot split; publication version allocation can race |
| Crash recovery | Leases and watchdog are strong before click; post-click ambiguity unresolved | Reaper exists; unsafe post-click replay | Reaper exists; unsafe reset-and-resubmit |
| OTP | Resource-safe only by holding scarce context | Releases context but cannot restore DOM state | Same invalid storage-state assumption |
| Ranking quality | Best: deterministic scoring plus offline and online measures | Golden set exists, but model-heavy ranking and weak metrics | nDCG plan is good but quarterly cadence is too slow |
| Cost control | Best arithmetic and enforced $0.08/$0.10 behavior | Attractive estimate rests on optimistic caching; cap is $0.15 | Full path exceeds target; cap is $0.50 |
| Ops and restore | Best: two machines, daily backup, weekly automated restore | One host, quarterly restore in risk section | One host, monthly restore; decent runbook concept |
| Authoring-standard compliance | Best by a wide margin | Moderate; concrete but contains invalid and inconsistent mechanisms | Moderate presentation, weak mechanisms behind claims |

## P0 changes required before implementation

The best combined plan would start from Qwen and incorporate selected Opus ideas, but it needs these launch-blocking changes:

1. **Define an at-most-once external submission protocol.** Add `submission_attempts` with states such as `authorized`, `click_issued`, `confirmed`, and `uncertain`. Commit `click_issued` before the browser action. After that boundary, never click again without authoritative ATS proof of non-submission. No ordinary retry is available from `uncertain`.
2. **Persist authorization evidence.** Add an immutable `application_authorizations` row with application, actor, mode (`review_now` or `queue_time_auto`), answer/form/profile/job hashes, timestamp, and revocation/consumption state. The submit transition must require and consume the matching grant transactionally.
3. **Use immutable job snapshots.** Pin the exact posting text, structured requirements, apply URL, and form version used for matching, documents, and application preparation.
4. **Create a real artifact model.** Store each PDF/DOCX and proof bundle as a checksummed, encrypted artifact tied to document generation, renderer version, profile version, and job snapshot. Record user approval separately.
5. **Strengthen truthfulness to typed claims.** Generate documents from user-approved atomic facts and transformations with explicit allowed predicates. Reject unsupported qualitative, causal, numerical, credential, date, and scope changes; do not rely on source paths or a second model alone.
6. **Redesign OTP capacity.** Keep only a separately capped number of live OTP contexts, with reserved capacity for normal submissions, or implement and prove an ATS-specific restart/refill flow. Do not claim `storageState` restores page state.
7. **Make relational ownership structural.** Add composite foreign keys or triggers so a user's application cannot reference another user's profile, document, artifact, authorization, or credit entry.
8. **Implement automatic board discovery.** Define the discovery sources, URL normalization/deduplication, validation, crawl budget, scheduling, and false-positive tests. Seed-only ingestion does not meet the brief.
9. **Test the exact dangerous boundary.** The fake employer must record acceptance independently, while the chaos runner kills Pilot immediately after employer commit but before local receipt persistence. The assertion is zero second POSTs, not merely one local `submitted` event.
10. **Resolve uncertain billing honestly.** A confirmed submission charges; a proven pre-click failure releases. A post-click uncertain attempt keeps the hold pending until reconciliation rather than releasing and inviting another submission.

## Final recommendation

Choose **Qwen** as the winner and revision base. Borrow Opus's explicit queue-time auto-submit flag and full form-snapshot entity, but not its crash recovery or OTP design. Do not carry forward Sonnet's LLM-audits-LLM truth model, credit implementation, or stale-submit reset.

The key product-management conclusion is that the plans differ less in feature coverage than in whether they respect uncertainty. Qwen comes closest: it is willing to fail closed in several places and makes most behavior inspectable. Its remaining mistake is allowing convenience to reopen an application after the one moment where certainty has been lost. Fix that, make authorization durable, and the plan becomes a credible route to a trustworthy v1.
