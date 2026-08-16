# ToolBox Poster Engineering Plans — PM Evaluation

> Graded evaluation of eight engineering plans submitted against the ToolBox Poster product brief.
> Brief: queue-first Instagram operations studio. Section §4 (invariant enforcement map) most heavily weighted.
>
> _Rankings revised following cross-review with an independent analysis. The primary correction: Graph API endpoints for post archiving and Reel Recently Deleted are not documented operations — credit given to three plans for "using the official API" for cleanup was unearned. Sonnet 4.5's instinct to treat cleanup as a browser automation problem is the more honest starting point. Its crash-recovery pattern, invalid DDL, and broken cross-process SSE are the actual disqualifiers._
>
> _Second round (August 16, 2026): three additional submissions evaluated — Opus 4.8, Opus 5, and Fable 5. Two of them displace Qwen 3.8 27B from the top. All three treat cleanup more honestly than the original field: Opus 5 and Fable 5 design browser automation with real fences; Opus 4.8 hedges. All three ship fresh stacks (Node 22, Next.js 15) where Opus 4.5, Sonnet 4.6, and Opus 4.6 shipped EOL runtimes._
>
> _Second cross-review: findings were reconciled against the independent eight-model analysis. Verified additions from that review are incorporated below — most materially, Fable 5's active-attempt fence releases after a confirmed success (a second publish becomes structurally legal), its customer cleanup-session entity is absent from the DDL, and its deletion tombstone retains raw identifiers; Opus 4.8's account-ownership index releases on disconnect; Opus 5 lacks a scheduled-cleanup occurrence ledger and an immutable revision history, and its reconciliation does not hold the account. One of that review's claims was checked and rejected (Opus 5's `account_requests` does not declare `created_at` twice). Scores adjusted: Opus 5 88→87, Fable 5 86→85, Qwen 3.8 27B 85→84, Opus 4.8 75→73. See the cross-review notes at the end._

---

## Overall Rankings

| Rank | Model | Score | One-line verdict |
|------|-------|-------|-----------------|
| 1 | Opus 5 | 87/100 | Only plan to commit markers before both external boundaries, the only correct DST treatment, honest cleanup, canonical billing sync; docked for a data model and grant matrix that contradict its own walkthroughs. |
| 2 | Fable 5 | 85/100 | Honest browser-automation cleanup with fences that hold through uncertainty and fetch-canonical billing; docked because the fence releases after success, the customer session entity is unmodeled, and a global job-key collision can starve billing syncs. |
| 3 | Qwen 3.8 27B | 84/100 | Strongest safety architecture of the original field; DDL fails to execute, the central outbox table is absent, cleanup rests on unverified API endpoints, and rights evidence is asserted rather than modeled. |
| 4 | Opus 4.8 | 73/100 | Excellent mechanism instincts — slot ledger, shared publish/cleanup lease, restore-drill-as-test — but its centerpiece republish block is false against its own index predicate and cleanup rejects browser automation while resting on an unverified API. |
| 5 | Sonnet 4.6 | 71/100 | Best uncertainty markers of the original field and a strong cleanup state-machine; content freeze is too late, Lucia v3 is deprecated, and cleanup endpoints remain unverified. |
| 6 | Opus 4.6 | 68/100 | Strongest occurrence ledger, rights model, and failure walkthrough coverage of the original field; undone by a deletion design that destroys collaborator data, false Luxon DST assumptions, stale runtime, and missing pre-dispatch marker at container creation. |
| 7 | Opus 4.5 | 64/100 | Broad coverage undercut by schema contradictions that break stated recovery paths and a direct cleanup fence invariant violation. |
| 8 | Sonnet 4.5 | 51/100 | Readable and product-aware; crash recovery can repeat a destructive action, cross-process SSE is architecturally broken, and the DDL is invalid. |

---

## Section-by-Section Scores

| Section | Weight | Opus 5 | Fable 5 | Qwen 3.8 27B | Opus 4.8 | Sonnet 4.6 | Opus 4.6 | Opus 4.5 | Sonnet 4.5 |
|---------|--------|----|----|----|----|----|----|----|-----|
| §1 Technology decisions | derivations, fit to scale | 92 | 90 | 96 | 88 | 74 | 74 | 74 | 78 |
| §2 Architecture | isolation, named processes | 92 | 84 | 95 | 80 | 74 | 80 | 80 | 76 |
| §3 Data model | DDL, constraints, schema invariants | 78 | 70 | 74 | 64 | 68 | 64 | 62 | 42 |
| §4 Invariant enforcement map | **highest weight** | 92 | 84 | 88 | 70 | 72 | 80 | 68 | 56 |
| §5 Failure walkthroughs | 13+ scenarios, evidence | 88 | 88 | 85 | 78 | 74 | 80 | 61 | 38 |
| §6 AI strategy | rationale, scoped | 95 | 90 | 92 | 88 | 83 | 92 | 82 | 88 |
| §7 Testing confidence | named tests, real-account drills | 93 | 94 | 90 | 88 | 78 | 82 | 80 | 74 |
| §8 Delivery phases | vertical slices, first E2E | 90 | 92 | 88 | 78 | 82 | 84 | 60 | 65 |
| §9 Security & privacy | isolation, secrets, audit, deletion | 88 | 78 | 94 | 72 | 84 | 70 | 68 | 74 |
| §10 Risk register | 10 risks, early warning | 88 | 85 | 88 | 84 | 78 | 80 | 78 | 76 |
| §§11–13 Tradeoffs, improvements, assumptions | honesty about gaps | 92 | 90 | 90 | 80 | 74 | 72 | 77 | 72 |

---

## #1 — Opus 5 — 87/100

**Stack:** TypeScript/Node.js 22 LTS, Next.js 15 App Router (server actions disabled), Postgres 17 + PgBouncer, Graphile Worker 0.16, Hetzner Cloud (6 VMs, Terraform, Docker Compose + systemd), R2 with a Hetzner Object Storage replica, pgBackRest (60 s RPO), first-party auth (argon2id, WebAuthn for admin), Stripe Billing + Tax, official Instagram Login API v23.0, Playwright + residential proxies on an isolated VM, SSE over LISTEN/NOTIFY, ~$167/mo fixed (~$237 with automation).

This is the plan that comes closest to the brief's demand for mechanisms rather than assurances, and it is the first submission to guard **both** external publish boundaries. Before the container-create call, one transaction commits `queue_items.status='publishing'` with `phase='container_creating'`. Immediately before `media_publish`, another commits `phase='publish_sent'` with `boundary_crossed_at` — and a CHECK constraint (`phase NOT IN ('publish_sent', …) OR boundary_crossed_at IS NOT NULL`) makes the marker's presence structural. The record of "we may have acted" cannot be lost by the same event that loses the response. Every other plan in the field guards at most one boundary precisely.

It is also the only plan whose DST treatment is factually correct: it states that Luxon *advances* nonexistent spring-forward times (the trap Opus 4.6 fell into), detects the shift by asserting `dt.hour === requestedHour`, and records the occurrence as `skipped_dst`. Its materialized occurrence ledger carries three unique indexes — `(account, instant)`, `(account, local_slot_key)`, and a claim uniqueness per queue item — with no rule identity in any key, so a rule edit cannot mint a duplicate slot. That dual instant-plus-local-wall-clock key is the only complete answer in the field to both DST fall-back and timezone edits.

### Strengths

- **Committed pre-dispatch markers at both boundaries**, one CHECK-enforced — see above
- **Structural at-most-once fence, walked adversarially:** partial unique index `publish_attempts_one_past_boundary ON (queue_item_id) WHERE phase IN ('publish_sent','publish_uncertain','publish_confirmed')`, layered under `posts UNIQUE(queue_item_id)`, a conditional-UPDATE lease claim, and per-account serial job queues. §5.3 deliberately walks the stack assuming each layer fails in turn.
- **Honest managed cleanup with a held fence.** States outright that the official API offers no archive/delete and builds Playwright automation. `cleanup_runs_one_live` partial unique includes `paused_reconcile`, so an uncertain destructive outcome blocks all later runs — scheduled ones queue behind reconciliation with a unique-violation, not a policy. `phase='sent'` + `boundary_crossed_at` commit before the click; recovery observes and never re-clicks; a protect-after-confirm recomputes `selection_sha256` and aborts with zero browser actions.
- **Correct crash-after-send posture with a defined match key:** `publish_uncertain` + `needs_review`, converging timeout/SIGKILL paths; non-publish is concluded only from container `EXPIRED` after the 24 h window plus no matching media — silence is never proof
- **Grant-level secret isolation:** `app_web` has zero privileges on `ig_account_credentials` — a fully compromised web process cannot read token ciphertext even via SQL injection; writes go through a `SECURITY DEFINER` function. The 5-roles-×-tables privilege matrix is itself a checked-in test artifact.
- **Canonical-object billing sync:** the webhook handler ignores event payloads entirely, re-fetches the live subscription, and an hourly reconcile converges even if every webhook is lost. Tested with 30 shuffled, triplicated event replays asserting identical final state. This is the correct answer to Stripe ordering that Opus 4.5, Sonnet 4.6, Opus 4.6, and Opus 4.8 all approximate with flawed guards.
- **Composite tenant FKs** (`(workspace_id, child) → parent(workspace_id, id)`) prevent cross-workspace pairings in SQL — the gap flagged in Qwen 3.8 27B, Opus 4.5, and Fable 5 — plus RLS with `FORCE` and `NOBYPASSRLS` roles as backstop
- **Restart-safe daily quota** as a per-account, per-*local*-date row with a conditional in-transaction increment; a drill kills all workers mid-day and asserts the count survives
- **`ig-sim` with `timeout_after_accept`** — a first-party simulator that accepts a `media_publish`, records it, then hangs the socket, making the central ambiguity a routine CI case; 15 named nightly drills including restore with `pg_amcheck`
- **CDP-screencast credential capture** for restricted sessions: the customer types their Instagram password into a streamed browser and the product's servers never receive the password field's value
- Versioned, attributable rights acceptance (`policy_documents` + `policy_acceptances`, pinned per queue item via `frozen_policy_acceptance_id` with a trigger); content-addressed dedup at the queue boundary itself, not just the candidate table
- Legal/privacy pages ship in Phase 7 with an explicit "precedes any real customer" note, ahead of Phase 10 invites; the restore drill is a Phase 0 exit criterion
- Model §6 answer on AI: two candidates seriously costed and rejected on arithmetic that checks out, with a falsifiable revisit trigger

### Weaknesses

- **The daily quota increment — the single most restart-critical mechanism — is specified in two mutually exclusive places.** §2.2 increments it at scheduler dispatch, in the same transaction that claims the occurrence; §3.8, §5.1, and §5.2 increment it at the `publish_sent` boundary. Implemented as written, scheduler-dispatched publishes double-count; as specified, the plan has no single answer.
- **The analytics DDL makes the plan's own collection curve impossible.** §2.7 promises 27 collections per post; §2.4 sizes the table at 53 rows per post; the unique index `(post_id, age_bucket)` over an 8-value bucket enum caps non-manual rows at 7. Three mutually inconsistent numbers for the same table.
- **Invalid PostgreSQL:** `CHECK (timezone IN (SELECT … FROM pg_timezone_names)) NOT VALID` inside `CREATE TABLE` fails twice over — subqueries are not allowed in CHECK constraints and `NOT VALID` is ALTER-only.
- **Forward reference:** the `effective_entitlements` view selects from `subscriptions`, defined nine sections later; the migrations "applied in document order" fail there.
- **The declared grant matrix makes the plan's own walkthroughs impossible.** `app_automation` has no grant on `receipts` and no UPDATE on anything — yet the cleanup walkthrough writes trash receipts and updates run/item/post state. `REVOKE UPDATE ON audit_log` covers every application role — yet deletion step 4 "rewrites" audit rows. No role named in the plan can perform these writes.
- **`admin_sessions` is load-bearing and undefined.** Operator tenant isolation rests on "RLS policies that permit cross-workspace reads only while an `admin_sessions` row is live"; no such table exists in the DDL.
- **User-level deletion is advertised by the schema (`subject_kind IN ('user','workspace')`) but never walked and structurally blocked**: NO ACTION FKs from `created_by`/`uploaded_by`/`actor_user_id`, and a `policy_acceptances ON DELETE CASCADE` that collides with `frozen_policy_acceptance_id` still referencing the rows. The restrictive FKs at least mean collaborators' shared data cannot be silently destroyed — the failure mode is an error, not data loss.
- **PgBouncer transaction pooling vs LISTEN/NOTIFY is unaddressed.** LISTEN does not work through transaction-mode pooling and the plan never carves out direct connections for its SSE listener and worker wake-ups.
- **The SSE "Last-Event-ID for free" claim is hollow** — the transport is transient `pg_notify` with no outbox or replay source, so nothing can serve missed events on reconnect.
- **An unresolved publish does not visibly hold the account.** The plan defines a `held_reconcile` state, but the post-timeout walkthrough holds only the *item* — a second item can publish on the same account while reconciliation is still using a ±10-minute caption/timestamp heuristic, consuming capacity whose prior outcome is unknown. (Cross-review finding, verified.)
- **Scheduled cleanup has no occurrence ledger.** Only *simultaneously live* runs are unique; a daily/weekly rule can enqueue the same occurrence again after the first run completes. The generic request-idempotency table gives a background occurrence no stable identity. (Cross-review finding, verified.)
- **Editable frozen content is an in-place mutation.** A caption edit overwrites the queue row and bumps `frozen_version`; no immutable revision row preserves each reviewed payload, and `publish_attempts` pins only a hash, not an FK to a durable revision — weaker than the dispute-evidence promise implies. (Cross-review finding, verified.)
- **Graphile Worker recovery is overstated:** `force_unlock_workers` takes exact dead worker IDs; the proposed `worker-core-1:*` wildcard is not documented behavior, so crashed-worker jobs likely wait the 4-hour default (confirmed against Graphile Worker's admin-function docs). The deletion walkthrough also replaces `receipts.subject_id UUID` with an HMAC — bytes into a UUID column.
- Analytics call-volume arithmetic understated ~4× against its own curve; 240 s stop-grace justified as "longer than" its own 300 s container-poll ceiling; three different workspace-table counts quoted for the same schema
- Minor: the crash window between container-create *send* and id-commit is the one window of its class not walked (duplicate container possible, duplicate post not); a published item's `dedup_key` permanently blocks intentional re-queueing of the same asset (undisclosed); a handful of identity-bearing columns lack FKs

### Both boundaries, one index

The reason this plan wins §4: its at-most-once story survives an adversarial reading at every layer. The lease can be lost, the runner's serial queue can misfire, the process can be SIGKILLed between the marker commit and the HTTP call, or after the call and before the response — and in every walked case the partial unique index over past-boundary phases means a second attempt is a `23505`, not a second post. Opus 4.8 claims exactly this property and its index predicate fails to deliver it; Opus 5's predicate includes the uncertain and confirmed phases, so occupation is structural. The plan then treats its own defense-in-depth honestly: §5.3 asks what happens if each layer is the only one left, which is precisely the "show mechanisms" posture the brief demands.

---

## #2 — Fable 5 — 85/100

**Stack:** TypeScript/Node.js 22 LTS, Next.js 15/React 19 self-hosted, PostgreSQL 16 on DigitalOcean Managed DB (7-day PITR — accurately claimed, unlike the Fly Postgres trap), custom `jobs` table with `FOR UPDATE SKIP LOCKED` + 5-min leases + reaper, R2, official Instagram Login API, Playwright + 10 ISP proxies on a dedicated droplet, Stripe + Stripe Tax, self-managed Argon2id auth with separate TOTP operator sessions, ffmpeg 7 + sharp, SSE via NOTIFY/LISTEN, 3 droplets, SOPS/age secrets, $274/mo.

Fable 5's distinctive quality is that uncertainty *occupies* the safety fences instead of merely being described as blocking them: `one_active_attempt_per_item` is a partial unique index whose predicate includes `'uncertain'`, and `one_destructive_item_per_account` covers `('executing','uncertain')`. An unreconciled outcome is not an application state someone must remember to check — it is a row that makes the next insert fail. This is exactly the property Opus 4.8 claims and does not have. The cross-review qualified the picture, however: the same predicate *excludes* the terminal success states, so the fence that holds perfectly through uncertainty releases the moment a publish is confirmed — see the first weakness below.

### Strengths

- **Committed point-of-no-return marker:** the commit that writes `publishing` (with `publishing_marked_at` in the DDL) happens strictly before the `media_publish` call leaves the process; the reaper routes expired `publishing` leases to `uncertain` plus a reconcile job, never a re-publish. Reconciliation has a defined match key: container `status_code` first, then a media-list scan by `timestamp ≥ publishing_marked_at` plus exact frozen-caption match.
- **`uncertain` structurally holds both fences** — see above
- **Best-in-class honest cleanup.** No invented Graph API endpoints anywhere; cleanup is Playwright acting as the customer's account because "no official API" exists. Committed `executing` state before navigation, a `click_sent_at` marker committed just before `page.click`, read-only reconciliation against the Archived list, retry only when the post is provably still live.
- **Supervised customer login for automation sessions:** the customer types their own password into a streamed browser; the product keeps only encrypted `storageState`, decryptable solely on the automation droplet. Promise 5 preserved without pretending an official API exists.
- **Kill-matrix on every merge:** SIGKILL at named compiled-in failpoints, asserting the fake Instagram received ≤1 `media_publish` per item. Plus contract-pinned fakes with no-response modes, Stripe CLI shuffled/duplicated replay, a weekly **automated** restore drill, monthly manual PITR, and a DST suite pinned to real 2026 dates.
- **Correct Luxon claims** — spring-forward advances (02:30 → 03:30), fall-back takes the earlier offset — stated accurately and pinned by tests
- **Restart-safe local-day quota:** `account_daily_usage` PK `(ig_account_id, usage_date)` with `usage_date` in the account's timezone, conditional `SET reserved=reserved+1 WHERE published+reserved < daily_limit` inside the claiming transaction; no cron reset exists at all
- **Dedup at the effect, not the candidate:** `one_source_post_per_account` partial unique on `queue_items` itself, beneath `UNIQUE (ig_account_id, shortcode)` on `source_posts` and an advisory lock in refill — fully closes the concurrent-refill hole Opus 4.6 left open
- **Stripe fetch-canonical sync:** webhooks are pings; the handler re-fetches `GET /v1/subscriptions/{id}`; downgrades and `past_due` hold over-limit activity and delete nothing
- **Append-only evidence at the GRANT layer:** UPDATE/DELETE revoked on `receipts`, `audit_log`, `analytics_snapshots`, `stripe_events` — tampering is a permission error, not code discipline
- Entitlement gating enforced at the worker lease (dropped jobs, 404-not-403 invisibility); schema-enforced versioned rights (`queue_items.rights_acceptance_id NOT NULL` FK); a `slot_claims` ledger that records *skipped* slots so no-catch-up is evidenced, not silent
- Legal/privacy pages in Phase 1, before any onboarding; Meta App Review submitted at Phase 2 exit to absorb its lead time; Phase 4 explicitly the first end-to-end slice with phases 0–3 justified beneath it

### Weaknesses

- **The permanent no-duplicate-publish invariant is missing: a second publish becomes structurally legal after the first succeeds.** `one_active_attempt_per_item` covers `('created','media_upload','container_wait','publishing','uncertain')` — it excludes `published` and `reconciled_published`. Once an attempt reaches a terminal success state it occupies nothing; a stale "publish now" request or replayed job can insert a fresh attempt for an already-published item. `receipts` is unique only per *attempt* (`publish_attempt_id UNIQUE`), not per queue item, so two receipts for one intended item are structurally possible. The only guard is `queue_items.state='published'` in application code — an app-state check standing in for the brief's highest-priority promise. Verified in cross-review; the fix is one predicate edit plus one unique index, but as written the plan's H-3 claim overstates its mechanism the same way Opus 4.8's I4 does — just on the other side of the outcome.
- **The customer cleanup-session entity is absent from the DDL.** The security and cleanup sections depend on a customer-specific encrypted Playwright `storageState` (captured via supervised login, decryptable only on the automation droplet) — but the only session-ciphertext table in the schema is `source_pool_accounts`, explicitly "operator-owned; NOT tenant data." Cleanup cannot authenticate as the customer's account from the modeled data. A load-bearing entity described everywhere and defined nowhere — the same defect class as Qwen 3.8 27B's outbox. (Cross-review finding, verified.)
- **`jobs.unique_key TEXT UNIQUE` is global across all job states, silently starving recurring work.** Job rows persist in `done`; billing enqueues `sync:{subscription_id}` on every Stripe event; after the first sync completes, subsequent events collide with the completed row and new billing events stop triggering syncs. The nightly full re-sync turns a minutes-level guarantee into a 24-hour one. The correct shape is a partial unique index over `('pending','leased')`.
- **The 5-minute anti-double-fire guard references a column that does not exist.** `claimDueSlots()` checks `last_attempt_started_at < now() - interval '5 minutes'`, but no table defines that column — and this guard is the *only* thing standing between a schedule edit and two publishes minutes apart.
- **Rule version is embedded in `slot_key`** — the same class of key flaw as Qwen 3.8 27B's rule fingerprint. An edit invalidates old-key dedup; §5.7 openly shows both the old 19:00 and new 19:30 slots firing. Disclosed, but the sole mitigation is the undefined guard above.
- **The web droplet can decrypt customer Instagram tokens.** The master key lives on droplets A and B (A is web) and `role_web` reads `ig_connections`. The plan proves it knows the better pattern — automation sessions decrypt solely on droplet C — and doesn't apply it to publish tokens.
- **No user-level deletion design.** §5.13 covers workspaces only; NOT NULL user FKs (`created_by`, `requested_by`, …) make user deletion impossible without a reassignment/anonymization strategy the plan never states. `deletion_requests.user_id` implies it was contemplated.
- **DDL not executable as ordered:** two forward FKs to `source_posts`, missing `CREATE EXTENSION citext`; and the load-bearing `automation_flags` kill switch plus the maintenance rollup table (used as §5.12 evidence and a risk-register signal) are never defined
- **Quota `reserved` is never released on terminal failure** — each permanently failed attempt burns one unit of the day's capacity, and the invariant test bakes the leak in
- No composite FKs — cross-workspace pairing is not schema-prevented (FK checks bypass RLS); the "every tenant table carries `workspace_id`" RLS claim is untrue of three tables
- **The deletion tombstone retains identifiers while claiming "non-identifying proof."** `deletion_requests` keeps raw `workspace_id` and `user_id` ("no FK: rows outlive their subjects") and an *unsalted* SHA-256 email hash — dictionary-testable — plus a JSON step log that can retain errors. No tombstoning statement clears these fields. (Cross-review finding, verified.)
- Scheduled cleanup runs and `entitlement_grants` have no unique occurrence identity or idempotency record — a duplicate tick after a completed run, or a repeated grant, can produce a second effect despite the H-2 replay-safety claim; cleanup reconciliation also treats "post still live" as proof the click never landed, which can reauthorize a delayed first action (cross-review findings)
- Minor: duplicate *containers* possible on crash and unacknowledged; media duty-cycle stated 33% vs computed 16.7% and worst-month storage "2.3×" vs computed 3.0× (both errors in the conservative direction); workspace-scoped `notifications.read_at` hides a notification for all collaborators when one reads it

### The jobs.unique_key trap

This is the plan's most instructive defect because everything around it is right: event-ID dedup, fetch-canonical sync, shuffled-replay tests. The failure is one column constraint interacting with a retention decision — completed jobs are kept, and uniqueness doesn't care about state. It would pass every test in the plan's own §7 suite (each replay corpus starts from a clean jobs table) and fail in week two of production, quietly, with entitlements drifting up to 24 hours behind reality. It is the strongest argument in the field for the brief's insistence on adversarial review: the mechanism is named precisely, and precisely named mechanisms can be checked.

---

## #3 — Qwen 3.8 27B — 84/100

**Stack:** TypeScript/Node.js 22, Next.js App Router, Postgres 16 (Neon managed), R2, Hetzner VPS, Playwright, libsodium, Stripe, custom `work_items` queue.

This was the strongest of the original five submissions, and the only one of that field that takes the brief's ground rules seriously as a constraint rather than a format. The requirement to _show mechanisms, not assurances_ is honored in every section. Claims are tied to specific Postgres constructs: a named index, a conditional update, a transaction boundary. Where the brief demands numbers and derivations, numbers and derivations appear. The score falls from its initial assessment because several praised mechanisms — including the outbox table — are described throughout the document but absent from the DDL, and it now sits behind Opus 5 and Fable 5, which deliver comparable mechanism discipline with an honest cleanup design and (in Opus 5's case) both publish boundaries guarded. The second cross-review shaved one further point for two defects this evaluation had not priced: rights evidence that is asserted rather than modeled, and a cleanup dispatch record that does not exist.

### Strengths

- All 15 §4 invariants are named, each with a specific test name (`test_p1a_cross_tenant`, `test_h3_single_cross`) and a concrete mechanism
- Materialized schedule slots are the right structural concept: a local time that does not exist (DST spring-forward) produces no row — though the uniqueness key has a gap (see Weaknesses)
- libsodium secretbox over pgcrypto: key lives outside the database, so a DB compromise alone does not yield plaintext tokens
- Explicit cost arithmetic: 500-item queue alarm = 1 hour of burst-rate work; 4 concurrent × 30s each drains 8/min at a burst of 50/min
- `deletion_jobs.step_cursor` makes deletion resumable and each step idempotent — the strongest deletion design of the original five
- RLS as a backstop layer is the right intent: a forgotten `workspace_id` filter returns zero rows rather than leaking data
- Per-account token bucket for IG API calls prevents any single worker type from exhausting a shared limit
- The outbox design is the correct concept — durable SSE transport that doubles as an audit artifact — but it is described, not defined (see Weaknesses)

### Weaknesses

- **DDL fails to execute.** `workspaces.plan_id` references `plans` before that table is created. `CREATE POLICY queue_items_ws ISOLATION ON queue_items` is not valid PostgreSQL syntax.
- **The outbox table is absent from the DDL.** The architecture, invariant map, security section, and delivery plan all depend on an `outbox` table. No `CREATE TABLE outbox` appears in the schema. Mechanisms built on top of it cannot be credited as implemented.
- **Slot uniqueness key allows duplicates after a rule edit.** `UNIQUE(account_id, instant, rule_fingerprint)` means an edited rule produces a second row for the same account and instant under a different fingerprint — exactly the double-post scenario the constraint is meant to prevent. The correct key is `UNIQUE(account_id, instant)` with rule provenance stored separately (Opus 4.8 and Opus 5 both get this right).
- **`claimed_by_queue_item_id` has no unique constraint** despite the claim that one slot maps to at most one item.
- **Composite tenant ownership is not enforced.** Independent foreign keys on `queue_items` permit a row with one workspace's `account_id` paired with a different workspace's `media_object_id`. (Opus 5 shows the composite-FK convention that closes this.)
- **`protected_posts` table is absent** despite protection checks being referenced in the invariant map.
- **Snapshot immutability is a comment, not a constraint.** "No `updated_at`" does not prevent `UPDATE`. No trigger, permission boundary, or append-only revision model exists in the DDL.
- **Cleanup API endpoints are unverified.** The plan names `POST /{media-id}?archived=true` and a Graph API delete path. Meta's official API collection documents container creation and `media_publish`; no equivalent official contract exists for archiving feed posts or moving Reels to Recently Deleted. Opus 5 and Fable 5 demonstrate what the honest version of this feature looks like.
- **Reconciliation is overconfident.** The plan treats Instagram silence as proof of non-publish and proceeds to retry. No authoritative query or unique match key is defined to make absence of evidence equivalent to evidence of absence. This is an active duplicate-post path in the *designed* recovery flow — the receipts unique constraint would detect the second receipt but cannot un-publish the second Instagram post.
- **Content-rights evidence is asserted, not modeled.** The invariant map says `snapshot.attribution` contains `accepted_by`, `version`, and `accepted_at`, but no rights-policy or acceptance table exists and no JSON constraint requires those keys — the same defect this evaluation dinged Sonnet 4.6 for. (Cross-review finding.)
- **Cleanup's destructive boundary has no dispatch record.** `cleanup_runs_one_active_per_account` serializes runs, but no cleanup-attempt table or fencing token proves only one item crosses the destructive boundary; the "in flight" row the prose mentions has no schema state beyond `result='pending'`. (Cross-review finding.)
- Worker-1 consolidates publish, cleanup, analytics, reaper, and scheduler in one VM — heavy outage surface for a one-person team
- libsodium KMS integration path is underspecified
- Operator admin section (Phase 8) is very late — operator cannot manage the product at all until near the end of delivery

### What makes this plan stand apart

The invariant enforcement map covers all fifteen identifiable §4 rules and every §3 "hard product rule." Each entry answers the same two questions: what exactly enforces this, and how do you prove it works. The test names are specific enough that an engineer could write them from the description alone. Of the original five, no other plan achieved this consistently — Opus 5's 33-row map is the only submission that now surpasses it.

```sql
-- At-most-once publish: conditional update
UPDATE queue_items
  SET status='publishing', lease_owner=me, lease_expires=NOW()+interval '30s'
  WHERE id=… AND status='ready'
-- Second worker: status is now 'publishing', WHERE matches 0 rows, worker exits.
-- receipts UNIQUE(queue_item_id) bounds receipts to one even under restart/replay.
```

---

## #4 — Opus 4.8 — 73/100

**Stack:** TypeScript/Node.js 22 LTS, Next.js 15 customer app + separate ops-subdomain admin, PostgreSQL 16 (Fly Managed Postgres), Graphile Worker, R2, ffmpeg + sharp in-worker, Instagram Graph API two-step publish, Playwright on a scale-to-zero sourcing VM, Stripe, first-party auth (Argon2id + Google, opaque sessions), SSE over LISTEN/NOTIFY, Postmark, ≈$302/mo.

Opus 4.8 has some of the best individual mechanisms in the entire field: a materialized slot ledger keyed on resolved UTC instants with no rule fingerprint, a restart-safe daily quota in account-local dates incremented in the receipt's transaction, committed pre-dispatch markers before `media_publish` and before each destructive cleanup call, a single per-account lease table that makes publish and cleanup mutually exclusive at the boundary, and a nightly restore drill that *is* the test, with an RTO alarm. Its §1 rejections (Elixir, Redis) are the best-argued in the field. What sinks it to fourth is that its single most load-bearing safety claim is false against its own DDL, its schema does not execute, its deletion flow is impossible against its own FK graph, and its cleanup feature rests on an unverified API contract while explicitly rejecting the browser-automation alternative.

### Strengths

- **Materialized slot ledger with the correct key:** `publish_slots (ig_account_id, slot_instant)` primary key, resolved local→UTC once at materialization — a duplicated or edited rule resolving to the same instant collapses into one row. The design Qwen 3.8 27B aimed at and missed.
- **Committed marker before `media_publish`:** status flipped `→ submitting` in a transaction *before* the HTTP call; the reconciler explicitly never re-calls `media_publish`
- **Committed marker before each destructive cleanup call**, with reconcile-not-repeat recovery
- **Publish and cleanup share one per-account lease table** (`account_action_leases`, PK on `ig_account_id`), so the two side-effect classes mutually exclude — no other plan does this
- **Restart-proof daily quota:** `daily_usage (ig_account_id, usage_date)` with `usage_date` explicitly the account-local date, incremented in the same transaction as the receipt; no cron reset, no UTC-date bug
- **Nightly automated restore drill as a gating test** — "the restore itself is the test," with an alarm if it fails or exceeds RTO 30 min
- **Selection-hash re-verification at cleanup run start**, recomputed with fresh analytics and `protected_posts` — protecting an item after confirmation does invalidate the run
- Content-addressed media + recipe hash makes prep retries provably idempotent; webhook ingestion/processing split so a slow processor can't drop events
- Versioned, attributable rights acceptance as a table (`rights_acks`) with an FK from queue items
- Exactly the required 13 walkthroughs, all ending in Evidence blocks; 22 invariants whose mechanisms are mostly named DDL objects; fresh stack throughout

### Weaknesses

- **The republish-block for ambiguous outcomes is structurally broken by the plan's own index predicate.** The plan claims `failed_ambiguous` "cannot be republished until reconciled (the `one_active` index blocks a new active attempt)" — but `publish_attempts_one_active` covers only `WHERE status IN ('pending','creating','submitting')`. A `failed_ambiguous` row occupies nothing; a fresh `pending` attempt inserts cleanly. The guarantee rests entirely on an unstated application check — an assurance, not a mechanism, on the highest-weighted invariant. Receipts are unique per *attempt*, not per queue item, so two receipts per item are structurally possible. Compare Fable 5, whose predicate includes `'uncertain'` and therefore actually delivers this property.
- **DDL does not execute top-to-bottom:** `queue_items` references `source_candidates` and `rights_acks` before either is created; `CITEXT` used with no `CREATE EXTENSION`.
- **The deletion flow is impossible against the FK graph.** `receipts`, `publish_attempts.ig_account_id`, and `cleanup_runs.ig_account_id` carry no `ON DELETE` clause in a schema otherwise built on cascades, so the `purge_db` step raises FK violations on any workspace with history. User deletion has no walkthrough at all, and NOT NULL NO-ACTION FKs to `users` (rights acks, cleanup requesters, protection actors, grant issuers) make it either fail or destroy other collaborators' workspace evidence.
- **Cleanup rests on an unverified Graph API contract, with browser automation explicitly rejected.** "Where the API cannot perform an action for a media kind, that kind is out of scope" is a hedge that — applied to reality, where Meta documents no archive or Recently-Deleted operations — silently zeroes out the entire Phase 8 feature the Studio plan sells. The plan never flags endpoint existence as a risk.
- **No structural one-active-cleanup-run fence once the lease expires.** After a crash the run is `paused_reconcile` and the 300 s lease lapses; blocking of later runs is an unnamed app check. `cleanup_runs` has no unique index at all — the partial unique over active states that Opus 4.6, Opus 5, and Fable 5 all have is simply missing.
- **Timezone-edit double-post window.** The instant-keyed PK cannot prevent the same local slot re-materializing at a different instant after a timezone edit; the named test `test_tz_edit_no_double` asserts a property the mechanism does not provide.
- **Container boundary is only softly guarded and the state machine is internally inconsistent:** `ig_creation_id` is recorded after the container POST (crash between → duplicate container, with no recovery branch); §4/§5.3 say the guarded flip is `pending→submitting` while §5.2 says `creating→submitting`; container status polling before `media_publish` is omitted from the publish flow.
- **The RLS backstop cannot work as stated:** the single policy template requires `workspace_id`, which ~10 tables lack; zero RLS statements appear in the DDL. No composite FKs either, so cross-workspace pairings have neither constraint nor net.
- **The freeze trigger overshoots the brief:** `queue_items_freeze_guard()` raises on any `frozen_*` change outside `preparing` — making the brief-required caption editing of queued items impossible. Undisclosed in §11.
- **Billing ordering guard is built on a field that doesn't exist** (Stripe subscriptions carry no `updated` timestamp) and passes on equality; no canonical-object fetch; the nightly sweep leaves up-to-24 h windows of wrong entitlement state
- **The global Instagram-ownership index releases on disconnect.** `ig_accounts_global_unique ON (ig_user_id) WHERE status <> 'disconnected'` — a second workspace can claim the same Instagram account while the first workspace retains its queue and history rows. Safe reconnection then requires a transfer/ownership model the plan does not have. (Cross-review finding, verified.)
- **Same-post duplication through two sources.** Candidate uniqueness is per *source*, so the same Instagram post discovered via two sources creates two candidate IDs, and queue uniqueness on candidate ID cannot stop the duplicate. (Cross-review finding.)
- Legal/privacy pages are the final phase, after billing goes live in Phase 5 — the exit criterion asserts compliance the sequencing contradicts
- Minor: cost-table unit price off ~15× from its own monthly figure; no unique `(account, asset)` so concurrent double-submits of the same upload duplicate queue items; the rights gate is claimed NOT NULL but the column is nullable with no CHECK; a walkthrough writes `run_at` on an attempt row that has no such column; assorted referenced-but-undefined artifacts (tombstone table, cleanup stop-reason column, account-immutability trigger, TOTP storage)

### The index that doesn't hold

Opus 4.8 is the field's clearest lesson in the distance between naming a mechanism and having one. The plan does everything the brief asks rhetorically — it names `publish_attempts_one_active`, quotes its predicate, and cites it in the invariant map as the thing that makes an ambiguous outcome un-republishable. But the predicate is `('pending','creating','submitting')`, and the state it must block is `failed_ambiguous`. The index is real, executable, and irrelevant to the claim. One line — adding the ambiguous state to the predicate, as Fable 5 does — would have made the claim true. The evaluation weights this heavily because the brief does: a precisely named mechanism that does not enforce its invariant is worse than an admitted gap, since it survives review until an adversarial reader executes the predicate in their head.

---

## #5 — Sonnet 4.6 — 71/100

**Stack:** TypeScript 5/Node.js 20, Next.js 14, Postgres 16 (Railway managed), R2, Railway (6 services), pg-boss, Lucia Auth, Playwright, ffmpeg, Stripe, Resend.

Sonnet 4.6 contributed two design insights no other plan in the original field matched and has the strongest cleanup state-machine shape of that field. It also contains gaps that surface under failure conditions and carries a deprecated authentication dependency and an EOL runtime — defects the second-round submissions (all on Node 22/Next 15) throw into sharper relief.

### Strengths

- **Best security insight of the original field:** The web process does not have `TOKEN_ENCRYPTION_KEY` — only the publishing and sourcing workers do, enforced by separate Railway service environment variable sets. A compromised web process cannot decrypt customer tokens even with full DB access. (Opus 5 later achieves the same property one layer deeper, at the database-grant level; Fable 5, notably, fails it.)
- `publish_attempts.request_initiated_at` distinguishes crash-before-send (safe to re-queue) from crash-after-send (requires reconciliation). Among the original five, no other plan drew this line so precisely; Opus 4.8, Opus 5, and Fable 5 all independently arrived at equivalent or stronger committed-marker designs.
- Best cleanup state-machine of the original field: per-item `request_sent_at`, an `uncertain` state, a `needs_review` run state, and an active-run unique index that keeps later same-account cleanup runs blocked — including those behind an unresolved uncertain outcome
- Cleanup selection hash verified per item during execution, not just at run start — catches mid-run protection changes
- Daily publish count reconciliation via `GET /{ig-user-id}/content_publishing_limit` on account connect
- 9 clean delivery phases; Phase 3 is the first end-to-end slice, well-justified

### Weaknesses

- **Cleanup endpoints are unverified.** `DELETE /{ig-media-id}` and `POST /{ig-media-id}?archive=true` are treated as settled Graph API operations. No official API contract exists for these calls. Same shared gap as Qwen 3.8 27B.
- **Content is frozen too late.** Caption/media/settings freeze only at the `publishing` worker transition. Between queue/review and that transition, approved inputs can silently change.
- **Queue-to-media relation is incomplete.** `queue_items` carries only nullable `frozen_*` references with no live `media_file_id`. The failure walkthrough retries updating fields that do not exist in the schema.
- **`publish_receipts` RULE silently drops updates** rather than raising the exception the comment claims. The behavior contradicts the stated enforcement.
- **RLS policy references a database role the DDL never creates.**
- **The Instagram account unique constraint is described as deferrable** in a DDL comment but is not defined as such.
- **Deletion cannot complete as described.** `deletion_requests.user_id` is a non-null foreign key with no `ON DELETE` clause, yet the workflow deletes the user and retains the request row. No step ledger or retry cursor exists.
- **Rights acceptance is not attributable.** `media_files` stores a boolean and timestamp but not the accepting actor. No rights-policy table ties the acceptance to a specific terms version.
- **Lucia Auth v3 is deprecated.** Lucia's own documentation states v3 is a learning resource rather than an active library. Not a sound dependency for a 2026 greenfield project.
- **Stripe ordering is only partially handled.** Comparing `current_period_start` does not distinguish cancellation, pause, or plan-change events that occur within the same billing period.
- **Rate-limit arithmetic is internally inconsistent.** A 240-second minimum spacing yields 15 calls/hour, not the stated 200. Applied globally this could leave hundreds of accounts' analytics stale.
- No Row-Level Security backstop — workspace isolation depends entirely on application-level predicates
- No materialized schedule slots — `nextSlotAfter()` is recomputed each tick; DST spring-forward is noted as a test case but not structurally prevented
- `pg_notify` payloads are transient: if no SSE listener is connected when the notification fires, the event is lost permanently
- No user-facing audit log — only an `operator_audit_log`

### The `request_initiated_at` insight

This was the most distinctive contribution of the original field. The publish attempt records a timestamp the moment the HTTP request body is sent, before awaiting the response. A crash between that write and receiving the response leaves an attempt row with `request_initiated_at IS NOT NULL` and `outcome IS NULL`. The lease recovery job can then make a safe automatic triage: if `request_initiated_at` is null, the request definitely never left — safe to re-queue. If it is set, the outcome is unknown — move to `needs_review`. The second-round plans validate the idea by converging on it independently: Opus 5's `boundary_crossed_at` is this exact marker, CHECK-enforced and applied at both external boundaries.

### The pg_notify gap

Sonnet 4.6's SSE mechanism is elegant and transactional — notifications only fire when the committing transaction commits. But `LISTEN`/`NOTIFY` is a push channel, not a durable log. If the web process restarts and no listener is connected when a worker fires a notify, the event evaporates. Qwen 3.8 27B's outbox table design survives restarts: the SSE reader catches up from the last delivered event ID. At launch scale this matters less — but the brief requires status changes to "feel live," not "feel live when the connection is steady."

---

## #6 — Opus 4.6 — 68/100

**Stack:** TypeScript 5/Node.js 20 (EOL March 2026), Next.js 14 (unsupported), Postgres 16 (self-hosted), R2, Hetzner CPX41 VPS, pg-boss 10, NextAuth.js 5 (magic links), Playwright, Sharp + FFmpeg, Stripe, Resend, Luxon, Caddy, AES-256-GCM (application-level, dual keys).

Opus 4.6 has the broadest failure walkthrough coverage of the original field (13 scenarios, all with operator-inspectable evidence), a durable local-occurrence ledger for scheduling, and first-class rights-acceptance modeling. On structural read it appears to be one of the strongest submissions. Under adversarial examination it contains a deletion design that would destroy collaborators' shared data, a false assumption about how Luxon handles DST spring-forward, no pre-dispatch marker at the container-creation boundary, and a stale runtime baseline that was already end-of-life before the evaluation date.

### Strengths

- **`schedule_executions` as a durable occurrence ledger.** `UNIQUE (instagram_account_id, scheduled_date, scheduled_local_time)` is rule-independent — an edited rule cannot produce a second row for the same account, date, and local time. This is the correct structural answer to the DST fall-back problem: the second occurrence of 1:30 AM computes the same `(date, '01:30')` and hits `ON CONFLICT DO NOTHING`.
- **First-class rights-acceptance relationship.** `content_rights_versions`, `content_rights_acceptances`, and the `NOT NULL FK` on `queue_items.content_rights_acceptance_id` make content rights versioned, attributable, and enforced at the schema level.
- **Cleanup account fence includes reconciliation.** `cleanup_runs_one_active` covers `(confirmed, running, needs_reconciliation)`. An unresolved destructive outcome keeps the fence closed; later runs for the same account remain blocked.
- **LISTEN/NOTIFY for cross-process SSE.** The worker issues `NOTIFY workspace_{id}` in the same transaction as the status update. Any Next.js process holding `LISTEN workspace_{id}` receives it via PostgreSQL, not via process memory — inherently cross-process without Redis. The client uses `queryClient.invalidateQueries()`, so the database is the source of truth and a reconnecting client refetches current state.
- **pg-boss transactional job creation.** Enqueuing a publish job and updating `queue_items.status` commit in the same transaction. Phantom jobs and orphaned state transitions are structurally impossible.
- **Content freeze at queue time.** `queue_item_snapshots` is created in the same transaction as `queue_items` when the user queues. The publish worker reads exclusively from the snapshot.
- **Cleanup selection hash.** `SHA-256(sorted(item_ids).join(','))` is stored at confirmation and recomputed at execution start. A structural change to the item set stops the run without processing any items.
- **Broadly complete §5.** All 13 required failure scenarios covered, each ending with specific table-column evidence an operator can query.
- **Honest tradeoffs and assumptions.** Seven numbered tradeoffs, twenty numbered assumptions.

### Weaknesses

- **Deletion destroys collaborator data.** The F13 walkthrough issues `DELETE FROM {table} WHERE workspace_id = $1` and `DELETE objects keyed by media/{workspace_id}/*` for a *user* deletion request. If Alice shares a workspace with Bob and Alice requests account deletion, the deletion loop destroys the entire workspace — Bob's queue items, media, publish history, and schedule are gone. The brief requires "holding or leaving never destroys the customer's work." This violates it for every workspace member except the one who requested deletion. User deletion should revoke the user's access, cancel their personal billing, and anonymize their row — not delete shared workspace data. (Notably, Opus 4.8, Opus 5, and Fable 5 all *block* on this same problem rather than destroying data — none of the eight plans actually solves user-level deletion.)
- **Luxon DST assumption is technically incorrect.** §1.17 and F7 state: "spring-forward gaps (nonexistent times) produce an invalid DateTime (`dt.isValid === false`), which the scheduler detects and skips." Luxon's documented behavior for nonexistent spring-forward times is to advance the time into the next valid instant, not mark `isValid = false`. The scheduler's DST logic is built on a false premise and would produce incorrect behavior in production. (Opus 5 states Luxon's real behavior and counters it with an hour-assertion check — the correct fix for exactly this ledger design.)
- **No pre-dispatch marker for container creation.** The plan uses `ig_container_id IS NULL` to decide whether retrying is safe after a crash. This guards the `media_publish` call correctly. It does not guard container *creation* — a worker can crash after Instagram accepts the `POST /{ig-user-id}/media` call but before storing the returned `ig_container_id`. The plan would then retry container creation, potentially creating a duplicate container. A committed marker before the container-creation call is required at both boundaries.
- **No account-level publish ordering fence.** F3 describes two workers picking up items X and Y for the same account simultaneously and calls this "correct behavior — each job publishes the next available item." X and Y can then be published out of queue order. The queue is ordered by `position`; concurrent publishes to the same account violate the product's queue-ordering guarantee.
- **`CURRENT_DATE` contradicts the account-local quota reset promise.** The quota check uses `WHERE usage_date = CURRENT_DATE`. `CURRENT_DATE` evaluates to the server's UTC date. An account set to `America/Los_Angeles` would see its daily count reset at 4 PM local time (midnight UTC) rather than midnight local time as the product promises.
- **DDL has two execution errors.** The forward FK (`queue_items.source_candidate_id` references `source_candidates` before that table is defined) would fail on execution. The abuse-control rate-limit query references `publish_attempts.instagram_account_id`, a column that does not exist in the `publish_attempts` DDL — it has `queue_item_id`, not `instagram_account_id`.
- **Stale runtime as of evaluation date (August 2026).** Node.js 20 reached end-of-life in March 2026; Next.js 14 is unsupported as of the current Next.js support policy. A greenfield plan committed to building on an EOL runtime is not implementation-ready.
- **Candidate deduplication does not prevent queue-item duplication.** `UNIQUE (instagram_account_id, external_post_id)` on `source_candidates` prevents duplicate *candidates*. There is no equivalent constraint on `queue_items` preventing multiple queue items for the same source candidate. Concurrent auto-refill runs can create multiple queue items from the same candidate. (Fable 5's `one_source_post_per_account` partial unique on `queue_items` itself is the closing move.)
- **Cleanup protection hash does not cover protection-status changes.** The hash is `SHA-256(sorted(item_ids))`. If a user protects item B after confirmation (but before execution), the item IDs are unchanged — the hash matches — and the run proceeds. The confirmed selection that originally included B is not invalidated, so the user's confirmation was for a set that included work they later chose to exclude.
- **Billing timestamp ordering has an equal-second gap.** `WHERE updated_at < $event_timestamp` prevents stale events from overwriting newer state, but two semantically different events with the same `created` timestamp can still regress the subscription. The reliable fix is to retrieve the canonical Stripe subscription object, as Opus 5 and Fable 5 do.
- **Snapshot immutability is application-level only.** No trigger or column-level permission enforces it at the database layer.
- Web process holds the global customer-token encryption key — a compromised web process yields all decrypted tokens, unlike Sonnet 4.6's containment to worker processes only
- No row-level security backstop — all tenant isolation relies on application predicates

### The schedule-execution ledger vs the DST implementation gap

The `schedule_executions` table structure — `UNIQUE (instagram_account_id, scheduled_date, scheduled_local_time)` — is the correct concept and better than any other original-field plan's approach. Its dedup operates on *local* time, which is exactly the right domain for the DST fall-back problem. The implementation gap is in the conversion layer: the plan relies on Luxon marking nonexistent spring-forward times as `isValid = false`, but Luxon advances them instead. The ledger survives a correct DST conversion; this one would need an explicit gap detection — which is precisely what Opus 5 implements on top of an equivalent ledger.

---

## #7 — Opus 4.5 — 64/100

**Stack:** TypeScript/Node.js 20, Next.js 14, Postgres 16 (Fly Postgres), R2, Fly.io, Playwright, pg-boss, magic link auth, Stripe.

This plan shows the most coverage effort of the original field. Thirty-four invariants in the enforcement map is more than double any original-field plan, and the failure walkthroughs follow numbered sequences with clear evidence statements. It understands the product well and its schema design is thoughtful. Schema contradictions in the walkthroughs and a direct violation of the cleanup fence invariant reduce its trustworthiness as an implementation base.

### Strengths

- 34 invariants with decent mechanism precision — the most numerous coverage of §4 rules in the original field
- Clean architecture diagram; clear process separation between media, publish, browser, and scheduler
- `publish_attempts_one_active_idx` partial unique on `(queue_item_id) WHERE status IN ('pending', 'sent')` is a clever single-constraint at-most-once approach
- Correlation ID for webhook matching to publish attempt is a practical reconciliation tool
- Billing event handling: `stripe_events UNIQUE(stripe_event_id)` with forward-only subscription transitions is correct for duplicate prevention
- 10 delivery phases with clear exit criteria; Phase 4 is first end-to-end and justified

### Weaknesses

- **Fly Postgres is not managed.** The plan says "managed (Fly Postgres)" — but Fly's community Postgres is a self-hosted Fly app. Automated PITR, failover, and backup restoration are the operator's responsibility. This contradicts the brief's requirement for proven backup/restore. (Fable 5, evaluating the same option, rejected Fly for exactly this reason and chose DigitalOcean's genuinely managed offering.)
- **Account-revocation walkthrough cannot execute.** The flow sets `access_token_encrypted = NULL` on a column defined `NOT NULL`. The described recovery path fails with a constraint violation.
- **Cleanup uncertainty does not hold the account fence.** When a cleanup run reaches `paused` state it exits the active-run uniqueness predicate (which covers only `pending` and `running`). A subsequent cleanup run on the same account can start while a prior destructive outcome is unresolved — a direct violation of the hold-on-uncertainty requirement.
- **Lease table blocks legitimate reacquisition permanently.** `UNIQUE(job_type, resource_id)` is not a partial index over unreleased rows. After a lease is released, a second legitimate lease for the same resource fails forever.
- **Out-of-order billing events can regress subscription state.** Writing "full object state" from whichever distinct Stripe event arrives last can overwrite newer data. Stripe explicitly states delivery order is not guaranteed; the duplicate guard on event ID does not solve ordering for distinct events.
- **`queue_items.rights_version` has no foreign key** — a bare integer with no referential integrity.
- **Token encryption key is accessible to all services.** Fly.io secrets are environment variables injected at runtime across all services. A compromised web process can decrypt customer tokens — the opposite of Sonnet 4.6's explicit containment.
- **`users.is_admin` does not exist in the schema.** The security section depends on this column for operator authentication; the users table does not define it.
- **Delivery order violates a launch condition.** Public privacy, terms, security, and deletion pages are deferred until after billing and core customer flows, even though the brief requires them before real customers are onboarded.
- **Downgrade behavior is wrong.** The plan disconnects excess accounts. The brief requires over-plan activity to be held without destroying account relationships.
- Lazy next-slot computation without materialized slots — does not structurally prevent DST double-posts
- Some of the 34 invariants read more as QA assertions than structural mechanisms

### The platform decision

The plan calls out managed Postgres (in parentheses, "Fly Postgres") as a benefit alongside "automated PITR/daily backups." Fly.io's Postgres is a community project — it runs inside a Fly app that the operator manages themselves. There is no automated PITR, no managed failover, and no one-click restore. The brief lists "daily backups and a rehearsed restoration process" as a launch requirement. An engineer executing this plan would discover this gap only when they needed it.

### The access-token security gap

Fly.io secrets are environment variables injected at runtime. All services in the Fly app share the secret namespace by default. Opus 4.5's plan does not restrict token decryption to the worker layer. Sonnet 4.6 makes the opposite choice — explicitly noting the web process has no `TOKEN_ENCRYPTION_KEY` — and that distinction matters: a web process vulnerability can expose user data in one model and cannot in the other.

### On the invariant count

Thirty-four invariants looks impressive but some lose the mechanism requirement. Invariant #8: "Queue tells truth: order visible — `queue_items.queue_position` integer, UI queries `ORDER BY queue_position`." The mechanism is the SELECT clause. That's a test observation. The mechanism would be: `UNIQUE(workspace_id, account_id, queue_position)` preventing two items from claiming the same position, combined with a transactional renumber. The breadth is real and useful; the depth is variable.

---

## #8 — Sonnet 4.5 — 51/100

**Stack:** TypeScript/Node.js 22, Next.js 15, Postgres 16 (Render managed), R2, Render.com, Playwright, Graphile Worker, bcrypt, Stripe, Postmark.

Sonnet 4.5 is the most readable plan in the set. It is well-organized, covers all 13 required sections with competent writing, and demonstrates a solid grasp of the product's purpose. It also correctly recognizes that managed cleanup may require browser automation rather than assuming an undocumented Graph API operation exists — this instinct is accurate, and the second-round submissions vindicated it: Opus 5 and Fable 5, the two top-ranked plans, both built cleanup on browser automation. What disqualifies Sonnet 4.5 is the crash-recovery pattern that can repeat a destructive cleanup action, an architecturally broken cross-process SSE design, and DDL that fails to parse.

### Strengths

- Best AI strategy section of the original field: includes competitive context, specific cost math ($0.00012/suggestion), and a clear rationale for deferral
- Graphile Worker is an excellent choice — better fit than generic pg-boss for a one-person team; built-in priority, cron, and exclusive-task support (Opus 4.8 and Opus 5 later made the same choice)
- Render.com with managed Postgres and one-click restore is an honest platform choice for the stated operator constraint
- Manual acceptance scenarios in §7 (non-technical operator completing flows without developer help) is a useful addition no other plan includes
- WCAG 2.1 AA commitment — the brief makes accessibility a launch requirement and this plan takes it seriously
- 11 risks in the register (more than required), including "solo founder unavailable"
- Correctly recognizes that cleanup may require browser automation when no official Graph API endpoint for post archiving or Reel Recently Deleted is publicly documented

### Weaknesses

- **Cleanup crash recovery can repeat the destructive action.** The plan keeps the database transaction open around the browser/API side effect and relies on a crash to roll it back, then retries. If Instagram accepted the archive or delete before the crash, the local event rolls back with no record that the action already succeeded. The retry re-issues the action with no evidence the first one completed. This is a catastrophic safety failure — the brief's third promise is broken for cleanup. Contrast Fable 5's `click_sent_at` committed immediately before `page.click`: same transport, opposite safety property.
- **Scenario 9 continues after an uncertain outcome.** A browser timeout is recorded as `failed` and the job continues to post 4. The brief requires the run to pause on uncertainty and later runs to remain ordered behind it. Both requirements are violated.
- **Cross-process SSE is architecturally broken.** SSE clients are stored in an in-memory map in the web process. Worker processes call `notifyWorkspace()` as if they share that map. Across independent processes they do not — events silently drop.
- **The DDL is invalid.** A partial index uses `WHERE expires_at > NOW()`, which PostgreSQL rejects because volatile functions are not permitted in index predicates. `current_user_id()` is called in a policy but never defined. Several walkthroughs reference columns that do not exist in the schema (`media_uploads.attempt_count`).
- **Graphile Worker deduplication is misrepresented.** The plan implies automatic payload-hash deduplication. Graphile Worker requires an explicit `jobKey`; without one, no deduplication occurs. The deduplication guarantee the plan makes is false.
- **`published_posts.queue_item_id` has no unique constraint**, allowing sequential duplicate receipts.
- **Publish flow omits the container-status and `media_publish` boundary.** The plan names `POST /{ig-user-id}/media` as the publish step, skipping the required container polling and `media_publish` call that actually creates the post.
- **Instagram publishing is falsely asserted to be idempotent.** Retrying a publish request cannot be assumed to produce only one post. This is a dangerous factual claim the product must never rely on.
- **Suspended-workspace media purge contradicts hold-not-delete.** The risk register proposes purging media after 30 days of suspension. The brief requires suspension to hold work without destroying it.
- **No separate `publish_attempts` table.** Distinguishing customer-fixable failures, transient failures, permission expirations, account limits, and uncertain outcomes requires per-attempt records with typed error classes.
- **`accounts.posts_today` reset by cron is fragile.** A cron failure leaves accounts blocked forever with no record of yesterday's count versus today's.
- **Schedule rules stored in `accounts.settings` JSONB** — no constraints, no FK relationships, no queryable occurrence ledger.
- **Public legal/privacy pages appear in Phase 13**, well after onboarding and publishing phases — violates the brief's launch condition.
- 15 delivery phases is excessive; first end-to-end slice is Phase 5 — four phases before the core product is provably working

### The crash-recovery failure

The brief's third promise — a published post is never duplicated — depends on the cleanup fence never repeating a destructive action when the prior outcome is uncertain. Sonnet 4.5's recovery pattern violates this directly. Keeping the database transaction open around the external side effect means a crash produces a rolled-back local state with no record that the side effect may have already occurred. The retry fires again with no way to detect the prior attempt.

This is independent of the browser vs. Graph API question. Using browser automation for cleanup is the correct instinct when the Graph API endpoints for archiving posts are not publicly documented — Opus 5 and Fable 5 prove the instinct can be built safely. The failure is in the retry-on-crash pattern, which is unsafe regardless of which transport executes the action.

### The quota counter problem

```sql
-- Sonnet 4.5 approach (fragile):
accounts.posts_today INTEGER NOT NULL DEFAULT 0
-- Reset by: UPDATE accounts SET posts_today=0 WHERE posts_reset_at < CURRENT_DATE

-- Qwen 3.8 27B approach (restart-safe):
CREATE TABLE quota_usage (
  ig_account_id UUID NOT NULL,
  day           DATE NOT NULL,
  published_count INT NOT NULL DEFAULT 0,
  UNIQUE (ig_account_id, day)
  -- incremented in-transaction with the receipt write
);
```

If the Sonnet 4.5 cron that resets `posts_today` fails or is late, accounts remain blocked. There's no record of yesterday's count versus today's. The `UNIQUE(account_id, day)` approach is self-healing: a new day's first publish creates a new row starting at 1. A worker restart doesn't reset anything because the count is in a table row, not a mutable column. Opus 4.8, Opus 5, and Fable 5 all adopt this shape — and Opus 4.8 and Opus 5 further key the date to the *account's* timezone, closing the UTC-reset bug that Opus 4.6 left open.

---

## Cross-Model Observations

### On managed cleanup feasibility

The field split three ways. Qwen 3.8 27B, Opus 4.5, Sonnet 4.6, and Opus 4.6 named specific Graph API endpoints for archiving and deletion that Meta does not document — unearned credit, corrected in the first revision. Opus 4.8 avoided naming fake endpoints but hedged ("where the API cannot perform an action, that kind is out of scope") while explicitly rejecting browser automation — a hedge that, applied to reality, silently zeroes out a sold feature. Sonnet 4.5, Opus 5, and Fable 5 took the honest position that cleanup is a browser automation problem; Opus 5 and Fable 5 then actually engineered it — committed markers before each click, evidence capture, read-only reconciliation, and fences whose predicates include the uncertain state. The lesson: the two top-ranked plans are the two that accepted an unglamorous truth and built safety machinery for it, and the plan that got the instinct right first (Sonnet 4.5) still finished last because instinct without a safe retry pattern is worthless.

### On the publish boundaries

The two-step Instagram publish (create container → poll → `media_publish`) has two external boundaries, and the field's ranking tracks how many are guarded with a committed marker. Opus 5 guards both, with a CHECK constraint making the marker structural. Opus 4.8 and Fable 5 guard `media_publish` strictly but leave container creation soft (duplicate containers possible — harmless for double-posting, but an unguarded external call). Sonnet 4.6's `request_initiated_at` guards the send precisely but only at one boundary. Opus 4.6 guards only `media_publish` via `ig_container_id IS NULL`. Sonnet 4.5 doesn't model the boundary at all. Notably, Sonnet 4.6, Opus 4.8, and Opus 5 — plus Fable 5 with `publishing_marked_at` — independently converged on the same committed-marker idea, which is strong evidence it is the correct primitive.

### On fences that hold vs fences that are named

The sharpest quality separator in eight plans is whether the "cannot happen again until reconciled" claim is a predicate or a promise. Fable 5's `one_active_attempt_per_item` includes `'uncertain'` in its partial-index predicate; Opus 5's `publish_attempts_one_past_boundary` includes `'publish_uncertain'` *and* `'publish_confirmed'`; Opus 4.6's cleanup fence includes `needs_reconciliation`. Those hold. Opus 4.8 names an index whose predicate excludes the ambiguous state it claims to block; Opus 4.5's cleanup fence excludes `paused`; and Fable 5's own publish fence, for all its correctness through uncertainty, excludes the terminal success states — making Opus 5's the only publish fence in the field that is permanent. The brief's "show mechanisms, not assurances" rule turns out to have a precise operational test: execute the predicate in your head against *every* state the claim is about, including the ones after the happy ending.

### On slots, ledgers, and rule identity

Five plans materialize scheduling state; the differences are all in the uniqueness key. Qwen 3.8 27B: `(account, instant, rule_fingerprint)` — rule edits mint duplicates. Fable 5: rule *version* inside `slot_key` — same trap, disclosed, mitigated only by a guard column its own DDL never defines. Opus 4.6: `(account, date, local_time)` — correct domain for fall-back, but built on a false Luxon premise. Opus 4.8: `(account, instant)` — correct for rule edits, but an instant-keyed ledger cannot see a timezone edit re-mapping the same local slot. Opus 5: both `(account, instant)` *and* `(account, local_slot_key)` — the only complete answer, and the only plan that states Luxon's real spring-forward behavior (advance, not invalidate) and detects it explicitly. Opus 4.5, Sonnet 4.5, and Sonnet 4.6 compute lazily and structurally prevent nothing.

### On billing ordering

Only the second round produced the robust pattern. Opus 5 and Fable 5 both treat webhooks as pings, discard the payload, and re-fetch the canonical Stripe subscription — making event ordering irrelevant by construction, with reconciliation sweeps behind it. Every original-field plan plus Opus 4.8 tried to order events instead: `current_period_start` comparisons (Sonnet 4.6), `updated_at <` guards with equal-second gaps (Opus 4.6), last-writer-wins full-object writes (Opus 4.5), or a guard on a field Stripe subscription objects don't carry (Opus 4.8). Fable 5 then demonstrates that even the right pattern can be starved by an adjacent defect: its global job-key uniqueness collides recurring sync jobs against completed rows.

### On user deletion — a field-wide unsolved problem

No plan in eight can correctly delete a user who collaborates in a shared workspace. Opus 4.6 destroys the entire shared workspace — the worst outcome, violating promise 4 outright. Opus 4.8, Opus 5, and Fable 5 all have FK graphs that *block* the deletion instead (NOT NULL NO-ACTION references from queue items, audit rows, rights acceptances), which at least fails safe, but none of the three walks a user-deletion flow and Opus 5's schema advertises one (`subject_kind IN ('user','workspace')`) it cannot execute. Sonnet 4.6's flow deletes the user while retaining a NOT NULL FK to them. The correct design — revoke access, cancel personal billing, anonymize the user row, preserve shared workspace data and attributable evidence under a tombstone — appears in no submission. Workspace-level deletion, by contrast, was solved well three times (Qwen 3.8 27B's `step_cursor`, Opus 5's committed step ledger, Fable 5's `steps` JSONB with a public status page).

### On DDL that survives its own plan

Across eight submissions, not one delivered a schema that both executes top-to-bottom and supports every claim its own walkthroughs make. Forward FK references appear in five plans (Qwen 3.8 27B, Opus 4.6, Opus 4.8, Opus 5, Fable 5); invalid PostgreSQL in three (Qwen 3.8 27B's `CREATE POLICY … ISOLATION`, Sonnet 4.5's volatile index predicate, Opus 5's subquery CHECK); and every plan references at least one table or column its DDL never defines — Qwen 3.8 27B's outbox and `protected_posts`, Sonnet 4.6's retried fields, Opus 4.6's `publish_attempts.instagram_account_id`, Opus 4.8's tombstone and account-immutability trigger, Opus 5's `admin_sessions`, Fable 5's `automation_flags` and `last_attempt_started_at`. The recurring failure mode is not ignorance but non-verification: these schemas were written, not run. The gap matters most when the phantom object is load-bearing — Fable 5's undefined guard column is the sole defense behind a disclosed double-fire; Opus 5's undefined `admin_sessions` is the operator isolation mechanism.

### On stack freshness as a signal

The three second-round plans all shipped Node 22 and Next.js 15 with no deprecated dependencies; Opus 4.5, Sonnet 4.6, and Opus 4.6 shipped Node 20 (EOL March 2026), Next.js 14 (unsupported), and in Sonnet 4.6's case a deprecated auth library. The correlation with overall quality is not coincidental — the same verification habit that checks a runtime's support window also checks whether an API endpoint exists or an index predicate covers its claim. Platform honesty tracked the same way: Opus 4.5 called community Fly Postgres "managed"; Fable 5 rejected Fly for that exact reason and chose a genuinely managed database; Opus 5 assumed nothing was managed and budgeted pgBackRest with rehearsed restores.

### Reconciliation with the independent cross-review

The independent eight-model analysis and this evaluation agree on the winner (Opus 5), on the original-field order, and on nearly every mechanism-level finding. Verified findings from that review are incorporated above and moved four scores: Opus 5 88→87, Fable 5 86→85, Qwen 3.8 27B 85→84, Opus 4.8 75→73. Two ranking disagreements remain deliberate:

- **Fable 5's position.** The other review drops Fable 5 below Qwen 3.8 27B and Sonnet 4.6 on the strength of the terminal-fence gap ("a plan for this product cannot make a second post structurally legal immediately after the first one succeeds"). The finding is verified and serious, but this evaluation weighs *probability-weighted* duplicate risk: Fable 5's gap requires an anomalous stale request after a confirmed success, against an app-state guard, and its fix is one predicate edit plus one unique index. Qwen 3.8 27B's duplicate path — retry-on-silence in its *designed* reconciliation flow — fires in the routine ambiguous-timeout case, and Sonnet 4.6's cleanup transport does not exist at all. Fable 5 also delivers the honest cleanup design, canonical billing, and correct crash-ambiguity posture that Qwen 3.8 27B and Sonnet 4.6 lack. It stays second, at a reduced margin.
- **Opus 4.8's position.** The other review places Opus 4.8 below Sonnet 4.6 and Fable 5. This evaluation keeps it above Sonnet 4.6: both plans' cleanup transports are unverified, but Opus 4.8's quota, slot ledger, freeze timing, rights modeling, restore drill, and stack freshness all beat Sonnet 4.6's, and Sonnet 4.6 carries an EOL runtime and deprecated auth library. Opus 4.8's broken fences are graded harshly in §4 either way.

One cross-review claim was checked and rejected: Opus 5's `account_requests` does not declare `created_at` twice — the table has exactly one. And its assertion that the container `status_code=PUBLISHED` value is undocumented was not adopted; the shared, defensible criticism is narrower — caption-plus-timestamp matching is not a unique key, and container expiry plus absence from a media list is not authoritative proof of non-publication.

### What every plan got right

All eight plans chose PostgreSQL as the system of record, rejected Redis for the queue, chose Cloudflare R2 for media, used Stripe with idempotent webhook handling, chose Playwright for browser automation, and said no to AI in v1 (with real cost arithmetic in the better plans). These are the table-stakes decisions and the field converged on them without disagreement. The differentiation is entirely in the depth of the safety mechanisms, the schema's fidelity to its own claims, and the failure walkthrough accuracy — and the spread on those dimensions, 51 to 88, is the whole story of this evaluation.

---

*Evaluated against the product brief's required sections and ground rules. Section §4 (invariant enforcement map) weighted highest per brief instructions. Eight models evaluated: Qwen 3.8 27B, Opus 4.5, Sonnet 4.5, Sonnet 4.6, and Opus 4.6 in the first round; Opus 4.8, Opus 5, and Fable 5 added August 16, 2026. First-round rankings were revised following cross-review with an independent analysis (Graph API cleanup endpoints are not documented operations; credit for "using the official API" was unearned). Second-round evaluations applied the same adversarial checks — DDL execution tracing, index-predicate-vs-claim verification, both publish boundaries, cleanup fence predicates, DST library behavior, deletion FK graphs, and billing ordering — and were then reconciled against the independent review's eight-model update; verified findings from that reconciliation are marked "cross-review" in the model sections and adjusted four scores (Opus 5 87, Fable 5 85, Qwen 3.8 27B 84, Opus 4.8 73). Both evaluations agree Opus 5 is the winner and that no plan is implementation-ready without a correction round.*
