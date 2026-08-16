# Comparative product and engineering review: ToolBox Poster plans

## Executive verdict

Eight identified submissions now exist in the folder: `qwen-3.8-27b`, `opus-4.5`,
`sonnet-4.5`, `sonnet-4.6`, `opus-4.6`, `opus-4.8`, `opus-5`, and `fable-5`.
This review grades all eight against the same rubric.

The ranking is:

1. **Opus 5 — 81/100 (B)**: best revision base; the only plan with committed markers guarding both publish boundaries, the strongest DST treatment, a durable publish fence through uncertain and successful outcomes, honest browser cleanup, and canonical Stripe reconciliation. It still has non-executable DDL, contradictory quota timing, incomplete grants and tenant bindings, and a broken deletion finish.
2. **Fable 5 — 77/100 (B-)**: broad, operationally concrete, and especially strong on supervised browser cleanup, kill-matrix testing, canonical billing, and failure containment. Its publish fence excludes terminal success, but exploiting that hole requires a stale or anomalous request after the application has already recorded success.
3. **Qwen 3.8 27B — 76/100 (B-)**: still an excellent revision base with strong scheduling, work isolation, evidence, and invariant-oriented thinking. It falls behind Fable 5 because retry after an inadequately specified absence check is part of its designed reconciliation path, not merely a post-success anomaly.
4. **Opus 4.8 — 70/100 (C)**: sensible current stack and useful slot, quota, rights, receipt, and restore mechanisms. Its headline active-attempt index does not cover `failed_ambiguous`, cleanup fencing expires during reconciliation, and parts of required cleanup are waived, but its overall mechanism set narrowly exceeds Sonnet 4.6's.
5. **Sonnet 4.6 — 69/100 (C)**: good side-effect markers and cleanup serialization, but it freezes content too late, relies on unsupported cleanup operations, and starts from an already unsupported runtime/auth baseline.
6. **Opus 4.6 — 62/100 (D+)**: strongest ideas are its schedule-execution ledger, real rights-acceptance relationship, cleanup fence, and process isolation; it is held back by contradictory DDL, a false DST model, unsafe workspace deletion, and incomplete external-side-effect evidence.
7. **Opus 4.5 — 60/100 (D+)**: broad product coverage and several useful tables, but too many runtime contradictions and unsafe billing/cleanup details.
8. **Sonnet 4.5 — 42/100 (F)**: readable and product-aware, but its cleanup recovery can repeat a destructive action, its live-update architecture does not work across processes, and its DDL is incomplete and partly invalid.

The identities are Qwen 3.8 27B, Opus 4.5, Sonnet 4.5, Sonnet 4.6, Opus 4.6,
Opus 4.8, Opus 5, and Fable 5. The scores are based on the submitted artifacts rather
than model reputation.

**No plan is implementation-ready.** Opus 5 should advance to a correction round, not directly into build. Fable 5 is the strongest runner-up and Qwen 3.8 27B remains a strong alternate foundation. Opus 4.8 and Sonnet 4.6 are useful supporting sources; Opus 4.5, Sonnet 4.5, and Opus 4.6 are selective checklists, not foundations.

The central PM conclusion is that feature coverage is not the differentiator. All eight can repeat the brief. The differentiator is whether the plan remains truthful at the moment Instagram, Stripe, storage, a browser, or a worker stops responding. Opus 5 does this most consistently, but it still has launch-blocking gaps.

## Grading rubric

The brief says the invariant map is the most heavily weighted section. The rubric follows that instruction and scores mechanisms, not prose volume.

| Category | Weight | What earns credit |
|---|---:|---|
| Core promises and hard invariants | 30 | Correct tenant/account/content binding; no repeat side effects after uncertainty; hold-not-delete; durable evidence |
| Data model and executability | 20 | Executable DDL; structural constraints; frozen inputs; attempts, receipts, slots, billing, cleanup, deletion, and ownership represented faithfully |
| Failure recovery | 15 | Correct pre-send/post-send distinction; numbered recovery paths; no unsafe retry; inspectable evidence |
| Product coverage and prioritization | 15 | Complete v1 experience, restricted-feature containment, operator usability, accessibility, and legally honest sourcing/cleanup treatment |
| Architecture, operations, and cost | 10 | One-founder operability, work isolation, realistic capacity/cost arithmetic, backups and restore |
| Delivery, testing, security, and privacy | 10 | Evidence-based phases, dangerous-boundary tests, real-service validation, secret/media protection, deletion/export |

## Scorecard

| Rank | Plan | Invariants /30 | Data /20 | Failure /15 | Product /15 | Ops /10 | Delivery /10 | Total |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | [Opus 5](opus-5/plan.md) | 25 | 11 | 13 | 14 | 9 | 9 | **81** |
| 2 | [Fable 5](fable-5/plan.md) | 22 | 11 | 12 | 14 | 9 | 9 | **77** |
| 3 | [Qwen 3.8 27B](qwen-3.8-27b/plan.md) | 23 | 13 | 12 | 12 | 8 | 8 | **76** |
| 4 | [Opus 4.8](opus-4.8/plan.md) | 18 | 11 | 10 | 13 | 9 | 9 | **70** |
| 5 | [Sonnet 4.6](sonnet-4.6/plan.md) | 20 | 13 | 11 | 12 | 6 | 7 | **69** |
| 6 | [Opus 4.6](opus-4.6/plan.md) | 17 | 10 | 8 | 12 | 7 | 8 | **62** |
| 7 | [Opus 4.5](opus-4.5/plan.md) | 16 | 11 | 8 | 12 | 6 | 7 | **60** |
| 8 | [Sonnet 4.5](sonnet-4.5/plan.md) | 10 | 7 | 5 | 9 | 5 | 6 | **42** |

Scores are intentionally strict. A polished document does not receive safety credit when its schema cannot enforce the stated behavior or its recovery path can repeat an external action.

## Reconciliation with `analysis-fable.md`

The updated Fable review materially improves the comparison and changes this ranking. Direct checks against the four leading plans support its central ordering argument:

- **Fable 5 moves above Qwen 3.8 27B.** Fable 5 structurally permits another attempt after terminal success, but that requires a stale or anomalous request after success has already propagated into application state. Qwen 3.8 27B's documented recovery path explicitly permits retry of the same container after reconciliation says “not published,” yet it never defines the provider contract or unique evidence that makes that absence authoritative. The latter risk sits on the normal designed failure path and therefore receives the larger penalty.
- **Opus 4.8 moves above Sonnet 4.6.** Opus 4.8's `publish_attempts_one_active` predicate really does omit `failed_ambiguous`, so its headline no-republish claim is false. Even so, its current stack, UTC slot ledger, quota reservation, rights/protection model, publish receipt, shared account lease, and restore tests form a stronger overall base than Sonnet 4.6's late content freeze, fabricated cleanup transport, and unsupported Node.js 20/Next.js 14/Lucia v3 baseline.
- **Opus 5 remains first, with corrected reasons.** The earlier claim that `account_requests.created_at` was declared twice is false in the supplied plan and has been removed. The earlier claim that container `PUBLISHED` is undocumented was also too broad and has been withdrawn. The remaining reconciliation concern is narrower: caption-plus-time matching is non-unique, and the plan does not fully specify how a `PUBLISHED` container is mapped to the canonical media ID and receipt after a lost response.

The score changes do not simply copy Fable's more generous absolute scale. This review still charges heavily for migration-stopping DDL, ungranted worker writes, contradictory quota accounting, missing analytics occurrences, incomplete tenant constraints, and deletion workflows that cannot reach their stated terminal state. It therefore lands on the same order but lower totals: **Opus 5 (81), Fable 5 (77), Qwen 3.8 27B (76), Opus 4.8 (70), Sonnet 4.6 (69), Opus 4.6 (62), Opus 4.5 (60), Sonnet 4.5 (42)**.

One factual disagreement remains. Fable 5 says Luxon selects the earlier offset during a fall-back ambiguity; Luxon's own documentation says ambiguous-time selection should not be treated as defined behavior. Opus 5 is the only plan that detects spring-forward normalization by round-tripping the local components and uses durable instant and local-slot uniqueness, so it retains the scheduling lead.

## Opus 5 — Rank 1

### What it gets right

Opus 5 is the strongest submission overall and the most useful base for a correction round.

- It is the first plan to apply composite `(workspace_id, id)` foreign-key patterns systematically. That is the right structural direction for preventing cross-tenant grafting instead of trusting every repository query to remember the workspace predicate.
- Its publication boundary is explicit: `publish_sent` and `boundary_crossed_at` are committed before `media_publish`, and the partial unique index keeps `publish_uncertain` and `publish_confirmed` inside a permanent per-item fence.
- Materialized `schedule_occurrences` carry both an absolute instant and a local wall-time key. Separate uniqueness constraints cover duplicate instants, fall-back wall times, and queue-item claims, while the preview reads the same rows the dispatcher will use.
- Its cleanup design is the best complete browser-automation proposal in the set. A live-run unique index includes `paused_reconcile`, a per-item marker is committed before the click, exact metrics are retained, protection changes invalidate the confirmation, later runs remain blocked, and evidence is redacted.
- Billing treats webhook events as pings and retrieves canonical Stripe objects. This is substantially safer than timestamp-ordering heterogeneous event payloads.
- It models versioned policy documents and attributed acceptances, content-addressed media variants, append-only analytics, durable usage, idempotency keys, source-run evidence, notifications, audit records, deletion progress, and health signals as first-class records.
- Process, database-role, credential, media, and automation isolation are unusually concrete. The web process cannot select Instagram tokens or automation sessions, and the automation worker cannot read customer identity, billing, or publication grants.
- The test plan is excellent: named invariant tests, a fault-injecting Instagram simulator, live safe-account tests, storage and Stripe service tests, crash drills, restore checks, and accessibility gates all correspond to product promises.
- Delivery phases are vertical and operator-aware. Public/legal pages, restore proof, support tooling, and restricted-feature containment arrive before their corresponding launch gates.

### What it gets wrong

- **The claimed DDL is not executable.** `ig_accounts` contains a `CHECK` with a subquery over `pg_timezone_names`, which PostgreSQL does not allow, and places `NOT VALID` inside the column definition rather than applying it through `ALTER TABLE`. `effective_entitlements` also references `subscriptions` before that table exists. These are migration-stopping errors, not stylistic issues.
- **Critical tenant and content bindings remain incomplete despite the composite-key convention.** `publish_attempts.ig_account_id` has no foreign key and is not tied to the queue item's account. `posts.queue_item_id` has no foreign key at all. A queue item's frozen variant need only belong to the workspace, not to its selected asset; its policy acceptance and source item are not bound to the same workspace/item. `schedule_occurrences.rule_id` and `claimed_queue_item_id` are likewise not composite-bound. The P1 mechanism therefore overstates what SQL rejects.
- **The reconciliation mapping is under-specified.** A `PUBLISHED` container is useful positive evidence, but the plan does not fully specify how that state is mapped to the canonical media ID and permalink after the `media_publish` response is lost. Its fallback caption-plus-time search can select the wrong post when captions repeat. The implementation must verify the exact provider response/query contract and keep the fence closed whenever that mapping is not unique.
- **An unresolved publish does not visibly hold the account.** The plan defines `held_reconcile` but the post-timeout walkthrough only holds the item. A second item can publish on the same account while reconciliation uses a ±10-minute caption/timestamp heuristic, making a mistaken match more likely and consuming capacity whose exact outcome is still unknown.
- **Quota accounting is specified at two incompatible boundaries.** The scheduler walkthrough and dispatch section put the daily usage increment in the claim transaction, while the publication walkthrough says it is not incremented until the committed `publish_sent` boundary. Implementing both descriptions double-counts; implementing either one changes whether pre-boundary failures consume quota. The schema needs one reservation/commit/release protocol.
- **The promised analytics curve cannot fit its own key.** The product section promises 27 scheduled collections per post, but `age_bucket` has only seven non-manual values and the unique `(post_id, age_bucket)` index permits only one `weekly` and one `monthly` row. Occurrence number or collection time must be part of the identity.
- **The supplied grant matrix cannot execute its own cleanup and deletion workflows.** The explicit automation-worker grant covers `automation_sessions`, not the cleanup run/item/post and receipt writes described by the worker. The deletion workflow also rewrites `audit_log` rows after `UPDATE` and `DELETE` were revoked. Either the grant set is incomplete or the stated least-privilege workflow is impossible as supplied.
- **Editable frozen content is still an in-place mutation.** A caption edit overwrites the queue row and bumps `frozen_version`; there is no immutable revision row preserving each reviewed payload. `publish_attempts` pins only a hash, not an FK to a durable revision. This is better than a single stale snapshot, but weaker than the evidence and dispute promises imply.
- **Scheduled cleanup has no occurrence ledger.** A daily/weekly rule can enqueue the same occurrence again after the first run completes because only simultaneous live runs are unique. The generic request idempotency table does not give a background occurrence a stable identity.
- **Graphile Worker recovery is overconfident.** `force_unlock_workers` requires exact dead worker IDs and its documentation warns against passing any live ID. The proposed startup wildcard-like `worker-core-1:*` is not an exact recorded worker-ID mechanism. Job keys also do not permanently deduplicate: a locked matching job causes another job to be created, so the domain constraints—not the job key—must remain the credited guarantee.
- **Deletion cannot finish as described.** `deletion_requests.requested_by` still references the user being deleted. The workflow says it replaces `receipts.subject_id UUID` with an HMAC even though an HMAC is bytes, not a UUID, and it does not model deleting or tombstoning the request row that retains the raw `subject_id`. Rewriting append-only receipts also contradicts the earlier grant-level immutability claim.
- **Operator isolation depends on an absent table/policy.** The invariant map says cross-workspace admin RLS requires a live `admin_sessions` row, but no `admin_sessions` schema is supplied. The plan gives one example RLS policy, not the promised complete policy set.
- **Several lesser enforcement claims are prose.** The owner-presence index enforces neither presence nor the promised trigger; the rights trigger is described but not defined; a source-origin queue item is not structurally required to carry rights acceptance; and `subscriptions.last_event_created_at` is unnecessary and potentially misleading when every sync already retrieves current truth.

### Strengths

- Best complete invariant map and verification program
- Best composite-tenant-key direction
- Strongest permanent publish-attempt fence
- Best browser-cleanup state machine and evidence model
- Canonical Stripe reconciliation rather than event-order guessing
- Excellent process/credential isolation, delivery, accessibility, restore, and operator coverage

### Weaknesses

- Non-executable DDL and missing critical foreign keys
- Reconciliation leaves the canonical media-ID mapping and fallback uniqueness under-specified
- Contradictory quota timing, underspecified worker grants, and an impossible 27-point analytics key
- No immutable queue-item revision history
- No stable scheduled-cleanup occurrence identity
- Deletion types, foreign keys, and immutability claims contradict the workflow
- Some Graphile Worker and RLS guarantees are overstated

### Product verdict

**First place; advance to a correction round.** Opus 5 leads because it guards both publish boundaries with committed markers, keeps the item fenced through uncertainty and success, handles DST most carefully, and closes more of cleanup, billing, operator, and testing scope with concrete mechanisms. Its inconsistent quota protocol, incomplete grants and composite bindings, non-executable DDL, and impossible deletion finish are launch blockers. It should not proceed directly to implementation.

## Qwen 3.8 27B — Rank 3

### What it gets right

Qwen 3.8 27B remains one of the plans that best understands the product's actual differentiator: trustworthy queue execution under failure.

- Its Postgres-backed work queue, conditional state transitions, leases, attempt records, receipts, and `needs_review` state form one of the strongest overall publication-control designs.
- It distinguishes a committed pre-dispatch state from a committed post-dispatch state. This is the correct shape for deciding whether work is retryable or uncertain.
- Materialized schedule slots are one of the strongest scheduling proposals in the set. This makes schedule changes and DST behavior inspectable instead of hiding them in repeated “next slot” calculations.
- `queue_item_snapshots` correctly aims to freeze destination, prepared media, caption, attribution, and preparation choices.
- Work isolation is well considered: customer web traffic, publishing/cleanup/analytics, media processing, and restricted browser work are separated by process and mostly by compute boundary.
- It supplies the best cost arithmetic, capacity alarms, restore drills, invariant tests, and operator-evidence language.
- Restricted sourcing is explicitly separated by entitlement, process, browser account pool, and failure containment. That is much closer to the brief's trust posture than treating scraping as another importer.
- The AI decision is disciplined. Deterministic behavior is sufficient for v1, and the plan does not add AI merely to look current.

### What it gets wrong

The document's biggest weakness is that several claimed mechanisms do not exist in, or are contradicted by, its own DDL.

- **The supplied DDL is not executable in order.** `workspaces.plan_id` references `plans` before `plans` is created. The example `CREATE POLICY queue_items_ws ISOLATION ON queue_items` is invalid PostgreSQL syntax.
- **The outbox is central but absent.** The architecture, invariant map, security section, and delivery plan all depend on an `outbox` table, yet no `CREATE TABLE outbox` appears.
- **Tenant ownership is not structurally consistent.** A `queue_items.workspace_id` can be paired with an `ig_account_id` or `media_object_id` belonging to a different workspace because independent foreign keys do not enforce the composite ownership relationship. The RLS text says it is repeated for every tenant table but only one invalid example is supplied.
- **Content-rights evidence is asserted, not modeled.** The invariant map says `snapshot.attribution` contains `accepted_by`, `version`, and `accepted_at`, but there is no rights-policy or acceptance table and no JSON constraint requiring those keys.
- **Cleanup is not actually deliverable.** The plan names `POST /{media-id}?archived=true` and a Graph API delete path without substantiating those operations, then admits in tradeoffs that Reel deletion may be unavailable and proposes archive-only behavior. That weakens an explicit requirement rather than solving it. It also omits a `protected_posts` model despite relying on protection checks.
- **Cleanup safety is under-modeled.** `cleanup_runs_one_active_per_account` serializes runs, but no cleanup-attempt table or fencing token proves that only one item crosses the destructive boundary. A row marked “in flight” is mentioned, but the schema has no such state or dispatch record beyond `result='pending'`.
- **Schedule deduplication is close, not complete.** Uniqueness includes `rule_fingerprint`, so an edited rule can create another row for the same account and instant under a different fingerprint. There is also no unique constraint on `claimed_by_queue_item_id`, despite the claim that one item maps to at most one slot.
- **Snapshot immutability is only a comment.** “No `updated_at`” does not prevent `UPDATE`. A trigger, permissions boundary, or append-only revision model is required.
- **Billing ordering remains prose.** “Forward-only conditional transitions” are not represented by provider event time/version columns or a named reducer. Event-ID uniqueness handles duplicates, not out-of-order distinct events.
- **Deletion is not structurally resumable to completion.** The job has a cursor, but deleting a referenced workspace/user while `deletion_jobs` and `deletion_receipts` still point to those rows needs explicit tombstone/nulling behavior. The plan does not show it.
- **Reconciliation is overconfident.** It says Instagram can authoritatively prove “not published” and then safely retry the same container. The plan does not name the exact query, match key, or proof that makes absence authoritative. When proof is unavailable, the product must remain in review rather than infer failure.

### Strengths

- Best safety-oriented architecture
- Best scheduling model
- Best attempt/receipt/evidence thinking
- Best isolation of risky and heavy workloads
- Best testing, restore, and cost discipline
- Best compliance with the requested writing structure

### Weaknesses

- Non-executable and incomplete DDL despite claiming executability
- Managed cleanup transport is unproven and partially waived
- Missing structural tenant, rights, protection, outbox, and immutability mechanisms
- Billing and deletion correctness are less concrete than publication correctness
- Some tests assert properties the schema does not enforce

### Product verdict

**Third place; retain as an alternate revision base.** Its normal reconciliation path can authorize a retry from an absence test whose authoritative query and unique proof are never specified. It is not a launch plan until that path, cleanup feasibility, executable schema, tenant ownership, schedule uniqueness, billing ordering, and deletion are repaired.

## Sonnet 4.6 — Rank 5

### What it gets right

Sonnet 4.6 is concise and often names the exact artifact behind a claim.

- `request_initiated_at` is a useful side-effect marker. Committing it before the external call causes false uncertainty after a crash before send, but that is safe; it does not cause a destructive retry.
- Cleanup uses per-item `request_sent_at`, an `uncertain` state, a `needs_review` run state, and an active-run unique index that keeps later same-account cleanup runs blocked. This is the best cleanup state-machine shape among the original five submissions.
- It includes protected posts, frozen cleanup metrics/criteria, selection hashes, auto-fill target depth, operator-owned sourcing accounts, and explicit sourcing-worker token restrictions.
- Process separation is understandable and appropriate for the expected volume.
- Public pages and the invitation gate appear early, unlike plans that onboard customers before legal/privacy pages exist.
- The delivery phases are testable and identify the first end-to-end slice correctly.
- The security section usefully separates the web process from the customer-token decryption key.

### What it gets wrong

- **It relies on unsupported cleanup endpoints as if they were settled.** The plan explicitly calls `DELETE /{ig-media-id}` for Reels and `POST /{ig-media-id}?archive=true` for photos. It provides no official API contract or tested fallback, yet managed cleanup depends on them.
- **It freezes customer intent too late.** The brief says a queued item freezes approved inputs. Sonnet 4.6 freezes caption/media/settings only when the worker transitions to `publishing`, leaving a period in which queued work can silently change.
- **The queue schema cannot execute its own media flow.** `queue_items` has no live `media_file_id` or `prepared_media_id`; it only has nullable `frozen_*` references intended to be populated later. The failure walkthrough says a retry updates fields that do not exist.
- **Scheduling has no occurrence ledger.** Rules plus `scheduled_at` and repeated `nextSlot()` computation cannot prove that a particular local-time opportunity was consumed exactly once. DST overlap, duplicate rules, deploys around a slot, and schedule edits are therefore weaker than the invariant map implies.
- **Tenant isolation is mostly application convention.** Only the token table has RLS. Queue, media, receipts, analytics, cleanup, sourcing, and background jobs can still carry cross-workspace foreign-key combinations.
- **Rights acceptance is not attributable.** `media_files` stores a boolean, timestamp, and version, but not the actor. No rights-policy table pins the accepted text.
- **Deletion cannot complete as described.** `deletion_requests.user_id` is a non-null foreign key without `ON DELETE SET NULL/CASCADE`, but the workflow deletes the user and keeps the request row. There is no step ledger, per-step evidence, or retry cursor.
- **Its DDL comments overstate enforcement.** The Instagram account unique constraint is described as deferrable although it is not. The receipt `RULE` silently ignores updates rather than raising the exception claimed in the comment. The RLS policy depends on a database role the DDL never creates.
- **Stripe ordering is only partially handled.** Comparing `current_period_start` does not order cancellation, payment, pause, or plan changes that occur within the same billing period. The handler should retrieve canonical current state or compare a real provider event/object version.
- **The auth choice is stale.** Lucia v3 is deprecated, so it is not a sound 2026 greenfield dependency.
- **One rate-limit derivation is internally inconsistent.** A 240-second minimum spacing is 15 calls/hour, not 200. Applied globally, it could leave hundreds of accounts' analytics stale.

### Strengths

- Strong uncertain-action markers
- Best cleanup data shape, apart from the missing transport
- Good protected-post and source-auto-fill coverage
- Clear delivery phases and operator controls
- Good secret separation by process

### Weaknesses

- Cleanup depends on unproven API operations
- Content freezes at publish rather than queue/review
- Queue/media relation is incomplete
- Schedule, billing, tenant, rights, and deletion mechanisms are too weak
- Uses a deprecated authentication library

### Product verdict

**Fifth place; use selectively.** Borrow its cleanup attempt fields, protected-post model, source auto-fill fields, and operator-phase detail. Its fabricated cleanup transport, late freeze point, scheduler, unsupported runtime/auth baseline, and deletion flow make it a weaker base than Opus 4.8.

## Fable 5 — Rank 2

### What it gets right

Fable 5 is broad, practical, and substantially more safety-aware than its concise failure walkthroughs first suggest.

- It commits a `publishing` marker before `media_publish` and sends expired post-boundary work to reconciliation instead of replaying it.
- The custom Postgres queue makes job, lease, reaper, and per-queue retry behavior inspectable. Media, core publication, and automation workloads run in separate processes and the automation tier has a deliberately narrow database role and network blast radius.
- Browser automation is used honestly for archive/Recently Deleted behavior rather than inventing public Graph API endpoints. Cleanup has a pre-click marker, an account-scoped uncertain-item fence, a live-run fence including uncertainty, frozen rule data, protection checks, and redacted evidence.
- Slot claims, durable account usage, content-addressed renditions, source-post uniqueness, append-only analytics, receipts, notification state, audit data, and deletion steps are all represented.
- Stripe events are treated as idempotent pings that trigger a canonical subscription refetch.
- The kill-matrix tests, contract-pinned fake Instagram surfaces, real safe-account tests, restore drills, accessibility gates, delivery exit criteria, capacity arithmetic, and operator runbooks are strong.
- It makes honest launch-scale tradeoffs and does not add AI to a deterministic safety-critical workflow.

### What it gets wrong

- **The permanent no-duplicate-publish invariant is missing.** `one_active_attempt_per_item` covers `publishing` and `uncertain` but excludes `published` and `reconciled_published`. After success, a stale request can insert another attempt for the same queue item. `receipts` is unique only by attempt, and `library_posts` is unique only by receipt, so the schema permits two posts and two receipts for one intended item. App-state checks are not an adequate substitute for the highest-priority promise.
- **The DDL is not executable as claimed.** `queue_items` and `library_posts` reference `source_posts` before it is created; required `citext`/`pgcrypto` extension setup is absent. `CREATE TABLE operator_sessions (LIKE sessions INCLUDING ALL)` does not copy the `sessions.user_id` foreign key, despite the table being presented as an isolated equivalent.
- **Tenant isolation is mostly nominal.** Independent foreign keys allow a workspace-A queue item to point at workspace-B account, asset, rendition, rights acceptance, or source rows. Renditions can label themselves with a different workspace from their asset. Publish attempts, receipts, library posts, analytics, sources, and cleanup items repeat the same pattern. RLS cannot repair inconsistent rows inserted by privileged background roles.
- **The customer cleanup-session entity is absent.** The security and cleanup sections depend on a customer-specific encrypted Playwright `storageState`, but the only session table in the DDL is `source_pool_accounts`, which represents operator-owned sourcing accounts. Cleanup cannot authenticate as the customer's account from the modeled data.
- **The scheduler's deduplication is indirect and incomplete.** `slot_key` contains rule ID and version, so duplicate rules at the same local instant produce different keys. The prose relies on a five-minute account guard and `last_attempt_started_at`, but no such field or durable occurrence constraint appears in the schema. The fall-back rule also assumes Luxon chooses the earlier offset, which Luxon does not guarantee.
- **Quota reservations have no complete release/reconciliation mechanism.** `reserved` increments at slot claim, but the schema/walkthrough never shows the atomic `reserved - 1, published + 1` transition or release after a terminal pre-boundary failure. A leaked reservation can hold an account for the rest of its local day.
- **The cleanup and access idempotency claims exceed the constraints.** A scheduled cleanup has no unique occurrence identity, and a duplicate tick can run again after the first run completes. `entitlement_grants` has no uniqueness constraint or request idempotency record. A repeated grant or completed cleanup request can therefore create another effect despite H-2 claiming replay safety.
- **Cleanup reconciliation may reauthorize a late action.** Seeing the post still live after a browser click is treated as proof the click never landed and returns the item to `pending`. Without a defined stabilization period or authoritative acknowledgement, a delayed first action plus the next click can still repeat the destructive operation.
- **Rights evidence is weakly versioned.** `policy_version` is free text with no policy-document row or body hash, and nothing structurally proves the acceptance, user, queue item, and workspace align.
- **The custom `jobs.unique_key` has problematic lifetime semantics.** Because completed rows remain `state='done'` while the column remains globally unique, a later legitimate `sync:{subscription_id}` or recurring keyed job cannot be inserted unless an unmodeled upsert/garbage-collection protocol reactivates or removes the old row.
- **Security gives the web process excessive token access.** `role_web` can read `ig_connections`, a table with no `workspace_id` and hence no stated tenant RLS policy. A compromised customer-facing process can reach every encrypted Instagram token and apparently has the decryption material needed for the connect flow.
- **Deletion does not leave only non-identifying proof.** The retained row keeps raw `workspace_id` and `user_id`; an unsalted SHA-256 email hash is dictionary-testable; the JSON step log can retain identifiers/errors; and no tombstoning statement clears those fields. This contradicts both the workflow prose and the brief.
- **Several schema claims do not enforce what their comments say.** The owner index enforces at most one owner, not exactly one. Direct-upload queue deduplication is only by client token, not content/account. Frozen caption edits have no immutable revision model, and queue order has no optimistic version or position uniqueness.

### Strengths

- Good point-of-no-return marker and conservative lease reaper
- Honest browser-based cleanup direction with strong account fencing
- Good process containment, canonical Stripe sync, testing, delivery, and cost work
- Useful custom queue and durable evidence entities

### Weaknesses

- A second publish becomes structurally legal after the first succeeds
- Non-executable DDL and pervasive cross-tenant combinations
- Missing customer automation-session table
- Weak schedule, quota-reservation, recurring-cleanup, and access-grant idempotency
- Excessive token access in the web process and incomplete deletion tombstone

### Product verdict

**Second place; strongest runner-up.** Its terminal publish fence is a serious structural hole, but reaching it requires a stale or anomalous request after success is already recorded. That is less likely on the designed recovery path than Qwen 3.8 27B's retry after an under-specified absence check. Its automation containment, cleanup attempt shape, custom-queue clarity, Stripe refetch, and kill-matrix program make it the best supporting plan for Opus 5's correction round.

## Opus 4.8 — Rank 4

### What it gets right

Opus 4.8 has a sensible launch stack and several useful invariant-bearing concepts.

- It uses committed publication phases, one active attempt per item, an account action lease, frozen queue fields, durable receipts, daily usage, resolved slot rows, protected posts, cleanup items, rights acceptances, canonical-ish error classes, and append-only analytics.
- It separates customer web traffic, media/core work, and restricted sourcing, keeps encrypted tokens out of normal API serialization, proposes RLS as a backstop, and uses short-lived private-media URLs.
- Browser sourcing is entitlement-gated and isolated. Auto-refill uses an account-row lock, and source, asset, and rendition deduplication have concrete unique indexes.
- Billing, storage, quota, revocation, DST, cleanup uncertainty, and deletion each receive an explicit failure walkthrough with inspectable evidence.
- Delivery, restore testing, accessibility, cost arithmetic, and AI deferral are appropriate for a one-founder launch.

### What it gets wrong

- **Both uncertainty fences release at the wrong time.** `publish_attempts_one_active` covers only `pending`, `creating`, and `submitting`; it excludes `submitted` and `failed_ambiguous`. A new idempotency key can therefore create a new active attempt after an uncertain outcome. Likewise, the cleanup account lease expires and there is no active-run unique index covering `paused_reconcile`, so a later cleanup run can cross the account boundary while the prior outcome is unresolved. The prose says both are blocked; the schema says otherwise.
- **The DDL is not executable in order.** `queue_items` references `source_candidates` and `rights_acks` before either table exists. `citext` extension setup is missing. The operational walkthrough writes `run_at` on an attempt even though `publish_attempts` has no `run_at`, and the Graphile Worker/job schema on which atomic scheduling depends is omitted.
- **Tenant consistency is not structural.** Workspace, account, asset, rendition, source, rights, receipt, and cleanup foreign keys are independent. The database permits cross-workspace combinations, while the plan supplies no actual RLS policies for the many tables it says are covered.
- **Queue freezing conflicts with editable captions.** The freeze trigger permits mutation during `preparing` and otherwise treats the queue row as the sole revision. There is no append-only revision per explicit caption edit and no attempt FK pinning the exact approved revision.
- **Schedule correctness is overstated.** A unique UTC instant prevents exact collisions, but there is no local occurrence key or rule/version provenance. A timezone/rule edit can create a different instant for what the customer sees as the same local opportunity. The plan also relies on Luxon's undefined choice for an ambiguous fall-back time.
- **Source and upload deduplication is incomplete.** Candidate uniqueness is per source, so the same Instagram post discovered through two sources creates two candidate IDs. Queue uniqueness on candidate ID does not stop those duplicates, and two direct uploads of the same asset can still create two queue rows for one account.
- **Cleanup is under-modeled and deliberately incomplete.** The selection hash/run schema does not pin per-item metric snapshots or protection state. The plan says a paused run blocks later work but has no constraint for that. Its explicit tradeoff simply makes unsupported media kinds ineligible rather than delivering the required photo archive and Reel Recently Deleted behavior.
- **Billing still orders event payloads instead of always fetching canonical truth.** `current_period_end` or an assumed object `updated` field cannot totally order cancellations, pauses, failures, and plan changes within one period.
- **Instagram reconciliation remains under-specified.** Caption/time matching is not unique, and the plan does not define the exact provider mapping from a `PUBLISHED` container to the canonical media ID after a lost response. After three failed reads it correctly leaves review, but its schema then allows a new attempt anyway.
- **Rights and deletion are too weak.** `rights_ack_id` is nullable despite the invariant prose; there is no versioned policy body/hash. Deletion uses an untyped subject ID and JSON checklist with no modeled step receipts, user/workspace ownership split, or decoupled tombstone table.
- **The global Instagram ownership index releases on disconnect.** A second workspace can claim the same account while the first retains queued/history rows; reconnecting safely then requires a transfer/ownership model that is absent.

### Strengths

- Sensible current stack and workload isolation
- Useful attempt, receipt, slot, rights, protection, and usage entities
- Good failure-scenario and test coverage
- Correct instinct to fail closed after a marked publication request

### Weaknesses

- Publish and cleanup uncertainty are not actually fenced
- Non-executable/incomplete schema and nonexistent runtime fields
- Weak tenant, revision, schedule, rights, and deletion enforcement
- Source/upload dedup gaps and non-canonical billing projection
- Required managed-cleanup behavior is waived

### Product verdict

**Fourth place; a useful mechanism source, not a safe base.** Its current stack, slot/quota model, rights and protection records, receipts, account lease, and restore tests narrowly outweigh Sonnet 4.6's late freeze, fabricated cleanup transport, and unsupported dependency baseline. Its two most important safety assertions—“ambiguous publish cannot be retried” and “later cleanup runs stay behind reconciliation”—are still contradicted by its own predicates and expiring lease.

## Opus 4.6 — Rank 6

### What it gets right

Opus 4.6 is comprehensive and contains several mechanisms worth carrying into the winning revision.

- `schedule_executions` gives local date and time a durable identity and a uniqueness constraint. That is materially stronger than deriving every future opportunity from a mutable rule.
- It models actual `content_rights_versions` and `content_rights_acceptances`, then links the accepted version to the queue item. This is stronger than merely storing a boolean or an unconstrained version number.
- The active cleanup-run index includes `needs_reconciliation`, so an unresolved destructive outcome continues to hold the account-level fence.
- Publish attempts, a one-per-item receipt, daily account usage, cleanup items, and append-only analytics snapshots are all first-class records.
- The web, general worker, and restricted-source worker are separated. The browser worker has its own source credential and is not given the customer Instagram-token key.
- PostgreSQL `LISTEN`/`NOTIFY` is a sensible launch-scale cross-process invalidation mechanism, and having the client refetch database state keeps the database authoritative. The channel construction and reconnect behavior still need hardening.
- Enqueuing pg-boss work inside the transaction that advances domain state is the right way to avoid an orphaned job or an unqueued committed transition.
- Its delivery plan identifies an early end-to-end slice, and its nightly crash/race/quota tests, restore drill, operator dashboard, assumptions, tradeoffs, and cost arithmetic are unusually explicit.
- The failure walkthroughs generally recognize that a post-request timeout is uncertainty rather than proof of failure.

### What it gets wrong

- **The DDL is not executable in the supplied order.** `queue_items.source_candidate_id` references `source_candidates` before that table is created. The abuse-control query also filters `publish_attempts.instagram_account_id`, a column the table does not have.
- **Tenant ownership is not structural.** Queue items independently reference workspace, account, media, source, and rights rows, so the database permits cross-workspace combinations. Receipts and cleanup rows have similar consistency gaps, and no RLS closes them.
- **Its snapshot design conflicts with editable queued captions.** There is only one supposedly immutable snapshot per queue item. The plan permits caption edits through `row_version`, but publishing remains pinned to the original snapshot. The product can therefore display one caption while publishing another. Immutability is also only an application convention, and no content hash proves the stored object stayed unchanged.
- **Its pre-container retry proof is false.** `ig_container_id IS NULL` does not prove the create-container request was never accepted; the worker may die after Instagram accepts it but before the response is stored. Both container creation and `media_publish` need committed request markers and separate attempt evidence.
- **It does not preserve visible account queue order.** In its concurrency walkthrough, two workers publish items X and Y for the same account at once and call that correct. Y can become visible before X. An account-scoped publish fence is missing.
- **The schedule ledger is incomplete and the DST behavior is wrong.** There is no unique queue-item claim, rule/version provenance, or consumed status, and the job's future execution time is not clearly represented. More seriously, the plan assumes Luxon marks a spring-forward local time invalid and deterministically chooses the first fall-back occurrence. Luxon's documented behavior instead advances many nonexistent times and makes no guarantee about which ambiguous occurrence is selected.
- **Daily quota reset contradicts the walkthrough.** The SQL uses `CURRENT_DATE`, while the product text promises reset in the Instagram account's local timezone.
- **Candidate deduplication does not guarantee queue deduplication.** A source candidate is unique, but concurrent refill can still create multiple queue items for the same account and candidate/media.
- **Cleanup is not safe or proven.** It assumes an unverified Graph API archive operation. A cleanup item has no committed dispatch marker, so a crash after Instagram accepts the action leaves `processing` with no safe recovery: resetting it risks repeating the action, while leaving it unchanged can strand the run forever. Its changed-protection walkthrough skips the newly protected item but continues with the old confirmation; the brief requires the changed selection to invalidate that confirmation. The selection hash covers IDs, not the protection state, rule, or frozen metrics that justified the selection.
- **Billing ordering is unsafe.** Distinct webhook state is ordered using event timestamps rather than reconciliation with Stripe's canonical current object. Equal-second and semantically different events can regress state. It is also unclear whether event receipt and state projection commit atomically, and expired overrides continue to occupy the partial unique key until explicitly revoked.
- **The deletion workflow is the most serious product error.** A user-deletion request deletes all media and workspace rows for each associated workspace, which can destroy collaborators' shared data. It has no per-step ledger or cursor, calls external work “all-or-nothing,” and retains a non-null FK to an anonymized—not deleted—user. Other audit and acceptance FKs make the promised deletion still less credible.
- **Security and operations are weaker than the prose suggests.** The web process holds the global customer-token encryption key; operators use an ordinary magic-link user role without stronger authentication; a signed URL is incorrectly expected to reject unauthenticated holders; and the proposed per-workspace PostgreSQL `NOTIFY` channel is fragile and non-durable.
- **Its 2026 dependency baseline is stale.** Node.js 20 reached end of life in March 2026, and Next.js 14 is unsupported. They are not appropriate greenfield defaults in August 2026.

### Strengths

- Best new schedule-occurrence concept after Qwen 3.8 27B
- First-class rights version and acceptance relationship
- Correct cleanup account fence through reconciliation
- Good workload and secret containment for restricted sourcing
- Broad failure, delivery, operator, restore, and cost coverage

### Weaknesses

- Non-executable DDL and nonexistent fields in operational queries
- Queue snapshot and edit behavior contradict each other
- Incomplete evidence at both publish and cleanup side-effect boundaries
- Incorrect DST assumptions and unclear scheduled-job timing
- Unsafe shared-workspace deletion and weak operator/token isolation
- Unsupported cleanup transport and stale runtime/framework choices

### Product verdict

**Sixth place; useful revision source, not an implementation base.** It beats Opus 4.5 and Sonnet 4.5 because it supplies a real occurrence ledger, a real rights-acceptance relationship, a stronger cleanup fence, and more coherent process isolation. Its publish/cleanup dispatch evidence, tenant boundaries, snapshot semantics, DST behavior, and deletion model contain more severe contradictions than its polished coverage initially reveals.

## Opus 4.5 — Rank 7

### What it gets right

Opus 4.5 covers more of the product surface than its score may initially suggest.

- It includes explicit publish attempts and receipts, frozen queue content, durable daily usage, notifications, feedback, source candidates, protected posts, cleanup items, rights versions, subscriptions, entitlement overrides, audit logs, browser sessions, and deletion requests.
- The uncertain-publish state correctly blocks automatic republishing.
- Queue order and optimistic row versions are visible and understandable.
- Protected-post handling and cleanup preview/selection hashing are present.
- The testing section separates commit-time, real-service, nightly, and safe-account work.
- Operator tools cover inspection, safe retry, suspension, and reconciliation.

### What it gets wrong

- **The account-revocation walkthrough cannot run.** It sets `access_token_encrypted = NULL`, but that column is `NOT NULL`.
- **Cleanup uncertainty does not hold the account.** The run becomes `paused`, while the active-run unique index covers only `pending` and `running`. A later cleanup run can therefore start behind an unresolved destructive action, directly violating the brief.
- **The generic lease table cannot be reacquired.** `UNIQUE(job_type, resource_id)` applies even after `released_at` is set, so a second legitimate lease for the same resource fails forever. The intended uniqueness needed to be partial over unreleased rows.
- **Out-of-order billing is unsafe.** Writing “full object state” from whichever distinct webhook arrives last can regress the subscription. Stripe explicitly does not guarantee event delivery order.
- **Frozen fields are not immutable.** Saying there is “no UPDATE path” is an application convention, not database enforcement.
- **The claimed rights foreign key does not exist.** `queue_items.rights_version` is an integer with no FK. A per-user terms acceptance is also not structurally tied to the media/item being queued.
- **Tenant binding is incomplete.** Independent workspace/account/media foreign keys allow cross-workspace combinations in the database.
- **The schedule edit performs a mass rewrite near the side-effect window.** There is no durable slot/occurrence identity, DST policy, or consumed-slot record.
- **Deletion is not resumable in the modeled data.** It has no step cursor or per-step records. The walkthrough assumes cascades and user ownership relationships that are not in the schema.
- **Operator authentication is referenced but absent.** The security section depends on `users.is_admin`, which the users table does not define.
- **The delivery order violates a launch condition.** Public privacy, terms, security, and deletion pages are deferred until after billing and core customer flows, even though the brief requires them before real customers are onboarded.
- **Downgrade behavior is wrong.** The phase says excess accounts are disconnected. The brief says over-plan activity is held without destroying or disconnecting account relationships.
- **Capacity and cost are optimistic.** Four concurrent FFmpeg jobs per 1 GB worker and the quoted compute total do not reconcile with the listed browser and process footprint.

### Strengths

- Broad schema and UI coverage
- Useful rights/protection entities
- Good publish-attempt and receipt foundation
- Practical operator and test checklists

### Weaknesses

- Several failure walkthroughs contradict schema constraints
- Cleanup and billing violate hard uncertainty/order rules
- Lease, deletion, tenant, and immutability mechanisms are not structurally correct
- Delivery ordering and downgrade behavior conflict with the brief

### Product verdict

**Seventh place; do not implement as written.** Reuse the protected-post and rights-policy concepts, but rework the state machines and schema from a safer base.

## Sonnet 4.5 — Rank 8

### What it gets right

Sonnet 4.5 is readable and has some good product instincts.

- It separates web, publishing, media, browser, and cron processes.
- It recognizes that managed cleanup may require browser automation rather than simply assuming a public API exists.
- Analytics snapshots are append-only, and queue/library/accessibility needs receive meaningful attention.
- It explicitly discusses operator acceptance tests, restore drills, process capacity, and manual reconciliation.
- It is candid about several tradeoffs, including coarse analytics and a single region.

### What it gets wrong

Its cleanup recovery is disqualifying.

- **The plan can repeat a destructive cleanup action.** It keeps the database transaction open around the browser/API side effect, says a crash rolls the transaction back, and then retries. If Instagram accepted the archive/delete before the crash, the local cleanup event also rolls back; the retry has no evidence that the action already happened. The alternative flow likewise retries from post 1 after a job timeout.
- **A timeout is mislabeled and processing continues.** Scenario 9 records a browser timeout as `failed`, leaves the actual outcome unknown, and continues to post 4. The brief requires the run to pause on uncertainty and later runs to remain ordered behind it.
- **Live updates do not work across the proposed topology.** The web process stores SSE clients in an in-memory map, but independent worker processes call `notifyWorkspace()` as if they shared that memory. With one to three web instances, even web-to-web fanout is missing.
- **The DDL is invalid and incomplete.** A partial index uses `WHERE expires_at > NOW()`, which PostgreSQL rejects because volatile time expressions are not allowed in index predicates. `current_user_id()` is undefined. Important modeled entities are absent: waitlist, connection requests, publish attempts, rights-policy acceptance, schedule occurrences/rules, cleanup confirmation hashes, deletion jobs, and durable events.
- **Several walkthroughs reference nonexistent columns.** For example, storage recovery updates `media_uploads.attempt_count`, which is not in the table.
- **Graphile Worker is misrepresented.** It does not automatically compute a payload hash and guarantee deduplication. Deduplication requires an explicit `jobKey`, and locked-job behavior still requires a domain-level guard.
- **Publish receipts are not unique per queue item.** `published_posts.queue_item_id` has no unique constraint, so sequential duplicate receipts remain possible.
- **The publish flow is incomplete.** It names `POST /{ig-user-id}/media` as publication, omitting the distinct container-status and `media_publish` boundary that creates the actual post.
- **The schedule has no occurrence record.** A settings JSON plus cron recomputation cannot prove which local slot was consumed, particularly during DST overlap or a deploy near a slot.
- **Rights evidence is only an audit-log convention.** There is no content-rights version table or foreign key tying an item to the exact acceptance.
- **Retention contradicts hold-not-delete.** The risk register proposes purging suspended workspaces' media after 30 days, even though suspension must hold work without destroying it.
- **The plan makes a dangerous factual assertion that Instagram publishing is idempotent.** The product must never rely on repeating a media publish request producing only one post.
- **Public legal/privacy pages arrive too late.** They appear in Phase 13, well after onboarding and publishing phases.

### Strengths

- Clear prose and reasonable top-level stack
- Good product/UI/accessibility awareness
- Correct instinct that cleanup may need browser automation
- Useful manual acceptance and restore ideas

### Weaknesses

- Catastrophic repeat-after-uncertain cleanup path
- Broken cross-process SSE design
- Invalid/incomplete DDL and invented fields
- False job-deduplication and publishing claims
- Weak schedule, rights, billing, deletion, and tenant mechanisms

### Product verdict

**Eighth place; reject as the implementation plan.** It is useful as a feature and UX checklist, but its most dangerous recovery path violates the product's third promise.

## Cross-plan comparison by hard requirement

| Requirement | Opus 5 | Fable 5 | Qwen 3.8 27B | Opus 4.8 | Sonnet 4.6 | Opus 4.6 | Opus 4.5 | Sonnet 4.5 |
|---|---|---|---|---|---|---|---|---|
| Correct frozen customer intent | In-place version+hash; no immutable revision FK | Frozen row; no revision history | Queue-time snapshot; immutability not enforced | Frozen row; edit semantics weak | Freezes at publish, too late | Singleton snapshot conflicts with edits | Frozen columns; no DB immutability | Caption/media remain mutable |
| Never publish twice | Best permanent boundary index across uncertainty and success | Terminal success leaves index, so another attempt is legal | Strong claim/attempt shape; normal reconciliation can reopen | Ambiguous state leaves index, so another attempt is legal | Strong request marker and fail-closed recovery | Attempt/receipt shape, but no account-order fence | Active guard; sequential gaps | No attempt entity or unique receipt |
| Queue/schedule truth | Best occurrence schema and DST treatment | Slot key/version plus unmodeled spacing guard | Materialized slots, but uniqueness key needs correction | UTC slots only; weak edit/local-time identity | No occurrence ledger | Local occurrence useful; DST conversion false | Rewrites `scheduled_at` | Rules in JSON; no occurrence identity |
| Ambiguous publish outcome | Both boundaries marked; media-ID fallback under-specified | Commits marker; positive mapping needs contract pinning | Designed retry relies on unspecified authoritative absence | Commits marker, but schema permits retry after review | Safest manual posture | Fails closed after publish; weak container evidence | Moves to review | Publish phases incomplete |
| Managed cleanup | Best browser state machine; no scheduled occurrence key | Good browser fence; customer session table absent | Unsupported API path; protection missing | Fence expires during uncertainty; required actions waived | Strong state schema; unsupported API path | Good live-run fence; unsafe item marker/selection | Paused uncertainty releases fence | Browser direction; catastrophic retry/continue |
| Tenant isolation | Best convention, but critical relations escape it | RLS prose over independent FKs | Invalid/incomplete RLS and missing composites | RLS prose over independent FKs | Mostly app checks | App predicates only | App predicates only | Incomplete RLS function/schema |
| Durable daily capacity | Durable data, but claim-vs-boundary timing contradicts | Reservation release unspecified | Good table and receipt-time intent | Good durable table | Good table; timing/rate inconsistencies | `CURRENT_DATE` conflicts with local reset | Good table | UTC reset/concurrency weak |
| Restricted sourcing containment | Excellent role/process/session separation | Strong automation containment; token split less safe | Strong process/account-pool separation | Good worker separation | Strong process/account-pool separation | Good worker/credential separation | Entitled/separated | Separate worker; session isolation unclear |
| Billing duplicates/order | Best canonical refetch, though guard is redundant | Canonical refetch; custom job-key lifecycle gap | Duplicate-safe; reducer unspecified | Event ordering remains approximate | Period guard insufficient | Timestamp projection unsafe | Distinct event order unsafe | Duplicate-safe only |
| Deletion | Resumable intent; type/FK/immutability contradictions | Step intent; retained identifiers are not tombstoned | Good cursor concept; tombstone/FKs unresolved | JSON checklist only | Request FK blocks finish; no steps | User deletion can destroy shared workspaces | No cursor; cascade mismatch | No deletion schema |
| Operator usability | Best health/evidence/restore/support design | Excellent tests/runbooks/operator phase | Excellent health/evidence/restore plan | Good operational coverage | Strong phases and safe actions | Broad dashboard; weak operator auth | Broad checklist | Useful acceptance ideas; some SQL dependence |
| DDL executability | Fails: subquery `CHECK`, invalid inline `NOT VALID`, forward view | Fails: forward FKs/extensions | Fails: forward FK, invalid policy, missing outbox | Fails: forward FKs; missing runtime fields/jobs | Missing roles/relations; contradictions | Fails: forward FK/nonexistent query column | Runtime constraints contradict flows | Fails: volatile index/undefined function |

## Shared problems across all eight plans

1. **Instagram post-crash receipt recovery is not fully specified.** A positive `PUBLISHED` container state can support reconciliation, but the plans do not completely define the tested query/response contract that maps it to the canonical media ID and permalink after a lost publish response. Caption/time matching or absence from a paginated media list is not unique proof. Every design must remain fail-closed whenever that mapping is unavailable or ambiguous.
2. **Managed cleanup transport and recovery remain a feasibility gate.** Qwen 3.8 27B, Sonnet 4.6, Opus 4.6, and parts of Opus 4.8 rely on unsupported or waived API behavior. Sonnet 4.5, Opus 5, and Fable 5 more honestly use browser automation, but selector stability, customer-session capture, legal/platform exposure, read-back authority, and late-action behavior still require a real-account spike.
3. **No deletion design is complete.** Every plan has a foreign-key, ownership, resumability, evidence, anonymization, retained-identifier, or shared-workspace problem. Opus 5 and Fable 5 add useful step ledgers without completing the finish.
4. **No plan completes structural tenant binding.** Opus 5 gets closest with composite keys, but even it leaves publish attempts, posts, schedule claims, revisions, and acceptances inconsistently tied. RLS filters reads; it does not make an internally cross-tenant row valid.
5. **No plan fully models editable-but-frozen queue content.** The clean answer is immutable item revisions: each explicit edit creates a revision, review approves one revision, and the attempt and receipt pin it permanently.
6. **Scheduled cleanup needs its own occurrence identity.** A live-run fence stops overlap, not a duplicate daily/weekly tick after the first run finishes. None of the plans supplies a durable `(account, rule version, local occurrence)` key for destructive scheduled work.
7. **Schema claims are not migration-tested.** All eight contain at least one execution error, missing referenced object, or walkthrough query that disagrees with the supplied schema. An empty-database migration test must precede credit for DDL enforcement.
8. **The fake-system crash tests must be paired with contract proof.** Opus 5 and Fable 5 offer excellent kill matrices, but a simulator proves only internal consistency unless its container states, media-ID mapping, pagination, and timing behavior are pinned to behavior verified on safe real accounts.

## P0 changes required before implementation

Start with Opus 5, then make these corrections before Phase 0 is considered closed:

1. **Run a cleanup feasibility spike first.** Prove, against an operator-owned professional test account, the exact supported mechanism for photo archive and Reel Recently Deleted behavior. Record required permissions, App Review implications, session acquisition, reconciliation queries, and failure behavior. If there is no permitted and reliable mechanism, managed cleanup must be removed from the launch commitment rather than represented by invented endpoints.
2. **Create explicit immutable content revisions.** Add `queue_item_revisions` containing workspace, destination account, original/prepared media IDs and hashes, caption, attribution, preparation settings, rights acceptance, and creator. Publishing pins one approved revision; editing creates another.
3. **Model the two Instagram publish boundaries separately.** Persist container-create and container-publish attempts, each with a committed dispatch marker, request hash, external container ID, response/evidence, and outcome. Never retry a post-publish unknown without authoritative proof.
4. **Fail closed in reconciliation.** “Cannot find the post” is not automatically proof of failure. Define a provider query and a unique match key that can prove absence; otherwise keep `needs_review` and require operator/customer confirmation.
5. **Make tenant consistency structural.** Add composite unique keys and composite foreign keys such as `(workspace_id, account_id)`, `(workspace_id, media_id)`, and `(workspace_id, queue_item_id)`. Add real RLS policies for web and each worker role; test migrations from an empty database.
6. **Fix scheduling with durable occurrences.** Use one unique publication occurrence per `(account_id, instant)` independent of rule fingerprint, retain rule/version and local-wall-time provenance separately, and make `claimed_by_queue_item_id` unique. Give scheduled cleanup its own unique `(account_id, rule_version, local_occurrence)` ledger. Define spring-forward skip and fall-back duplicate-time policy explicitly.
7. **Keep the cleanup attempt ledger and close its remaining gaps.** Commit `authorized`/`request_marked` before the side effect, store an account-scoped fencing token, permit only one item across the boundary, retain `needs_review` in the active-run uniqueness predicate, and require authoritative/stabilized read-back before declaring a prior click safe to repeat.
8. **Build billing as event ingestion plus canonical reconciliation.** Store event ID, provider-created time, object ID, payload, and processing state. For subscription-changing events, retrieve the latest Stripe subscription or apply a reducer with a sound monotonic version. Never let an older snapshot overwrite newer state.
9. **Make rights acceptance first-class.** Add versioned rights terms and an immutable acceptance tied to actor, workspace, media/item revision, terms version, and timestamp.
10. **Make deletion a step ledger, not a loop.** Separate user deletion from workspace deletion; account for shared workspaces; store idempotent per-step outcomes; revoke access first; delete PII/media; retain only a decoupled non-identifying tombstone that does not FK to deleted records.
11. **Replace stale or misrepresented dependencies.** Do not begin a greenfield build on deprecated Lucia v3, end-of-life Node.js 20, or unsupported Next.js 14. If Graphile Worker is used, specify `jobKey` behavior explicitly and retain domain-level constraints because job-key deduplication is not permanent or sufficient by itself.
12. **Make the schema the executable source of truth.** Add every referenced table, column, extension, composite key, policy, role, trigger, and background occurrence ledger; fix DDL order and types; then run every migration against a fresh Postgres instance in CI before accepting mechanism claims.

## Final recommendation

Choose **Opus 5** as the winner and revision base. Use **Fable 5** as the primary runner-up and source of cleanup, queue, and test mechanisms; retain Qwen 3.8 27B as the smaller alternate design surface.

Keep from Opus 5:

- the committed markers at both publish boundaries and the permanent attempt fence, after specifying and contract-testing canonical media-ID reconciliation;
- composite workspace keys, extended to every remaining relationship;
- canonical Stripe refetch, process/database-role isolation, browser cleanup fence, health signals, operator tooling, and test program;
- the schedule-occurrence model, after pinning exact rule/account relationships and the provider-independent DST policy.

Borrow from Qwen 3.8 27B:

- its simpler materialized-slot and publication-evidence framing;
- its work-isolation, capacity, restore, and invariant discipline;
- its stronger instinct to keep uncertain external outcomes unresolved rather than infer success/failure from weak evidence.

Borrow from Sonnet 4.6:

- cleanup `request_sent_at` and per-item uncertainty fields;
- the active cleanup-run fence that includes `needs_review`;
- protected-post and auto-fill target models;
- early public/legal pages and stronger operator phase.

Borrow from Fable 5:

- the explicit custom-queue lease/reaper specification and kill-matrix structure;
- browser-automation containment and real-account cleanup drills;
- point-of-no-return cleanup evidence, but only after adding the missing customer-session schema and stable read-back policy.

Borrow from Opus 4.8:

- its concise rights/protection/receipt checklist and conservative “unsupported cleanup kinds are ineligible” posture during the feasibility spike;
- none of its active-attempt or cleanup-lease predicates without correcting the uncertainty fence.

Borrow from Opus 4.6:

- a durable local-date/local-time schedule-execution record, after correcting its DST conversion and claim constraints;
- first-class versioned rights terms and acceptances tied to the exact queued revision;
- an active cleanup fence that continues through `needs_reconciliation`;
- restricted-source process/credential separation and its nightly failure/restore drill coverage.

Borrow from Opus 4.5:

- explicit rights-policy/acceptance tables, after tying them to the exact media/item revision;
- its protected-post and detailed notification entities.

Borrow from Sonnet 4.5 only at the product level:

- treat cleanup as a browser-capability feasibility problem rather than assume an API;
- retain its accessibility and non-technical operator acceptance scenarios.

Do **not** carry forward Sonnet 4.5's cleanup retry logic, in-memory cross-process SSE, automatic payload-hash claim, or external-side-effect transaction pattern.

Do **not** carry forward Opus 4.6's singleton snapshot, timestamp-only billing projection, workspace-destructive user deletion loop, assumed cleanup endpoint, or stale Node.js/Next.js baseline.

Do **not** carry forward Opus 5's non-unique caption/time fallback or any unverified expiry-to-absence inference, in-place revision overwrite, scheduled-cleanup dedup gap, startup wildcard force-unlock, or deletion type conversions.

Do **not** carry forward Fable 5's terminal-attempt index, independent tenant foreign keys, web-readable global token table, permanent custom job-key semantics, or retained deletion identifiers.

The recommended go/no-go order is: prove cleanup transport and Instagram reconciliation; make the schema executable; prove the dangerous post-accept/pre-receipt crash test; then begin the customer-facing vertical slice. Until those three proofs exist, implementation would create the exact reputation risk the product brief puts first.

## External verification notes

- Meta's official Instagram collection documents container-status inspection and the successful `media_publish` response that returns the Instagram media ID. The correction round should pin the exact post-crash `PUBLISHED`-to-media-ID reconciliation behavior with a safe-account contract test instead of inferring it from a single example response: [container status](https://www.postman.com/meta/instagram/request/munmruq/get-ig-container-status), [publish the container](https://www.postman.com/meta/instagram/request/23987686-f1c081c0-be35-4ffa-84bb-2c1726860c2b).
- The same official collection documents professional-account publishing through media creation and `media_publish`; the plans need equally authoritative proof for any claimed cleanup API calls before treating them as real: [Meta Instagram API collection](https://www.postman.com/meta/workspace/instagram/documentation/23987686-9386f468-7714-490f-9bfc-9442db5c8f00).
- Instagram's help material describes feed-post archiving as a mobile-app feature and deletion/Recently Deleted as an app recovery workflow; neither page establishes a Graph API cleanup contract: [archive a post](https://www.facebook.com/help/instagram/136706673552668), [Recently Deleted behavior](https://www.facebook.com/help/711062676142607).
- Stripe states that webhook delivery order is not guaranteed and recommends retrieving related objects when needed: [Stripe webhook event ordering](https://docs.stripe.com/webhooks#event-ordering).
- Graphile Worker deduplication requires an explicit job key, and a locked matching job can still result in a second job under normal modes: [Graphile Worker job keys](https://worker.graphile.org/docs/job-key).
- Graphile Worker's force-unlock function takes exact dead worker IDs and warns never to pass live worker IDs, so it does not support Opus 5's wildcard-like startup recovery as written: [Graphile Worker administrative functions](https://worker.graphile.org/docs/admin-functions#force-unlock-workers).
- PostgreSQL does not permit subqueries in `CHECK` expressions, which makes Opus 5's inline timezone-membership constraint invalid: [PostgreSQL `CREATE TABLE`](https://www.postgresql.org/docs/16/sql-createtable.html).
- Lucia's own documentation says v3 is deprecated and Lucia is now a learning resource rather than the proposed library dependency: [Lucia v3 migration notice](https://lucia-auth.com/lucia-v3/migrate).
- Node.js lists version 20 as end-of-life as of March 24, 2026, so Opus 4.6's proposed runtime is already outside support: [Node.js end-of-life schedule](https://nodejs.org/en/about/eol).
- Next.js lists version 14 as unsupported; the current support table puts 16 in Active LTS and 15 in Maintenance LTS: [Next.js support policy](https://nextjs.org/support-policy).
- Luxon's timezone documentation says a nonexistent spring-forward time may be advanced rather than marked invalid and that ambiguous fall-back selection is not guaranteed, contradicting scheduler assumptions in Opus 4.6, Opus 4.8, and Fable 5: [Luxon timezone and DST behavior](https://github.com/moment/luxon/blob/master/docs/zones.md).
