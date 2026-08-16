# ToolBox Poster — Engineering Plan (Qwen 3.8 27B)

Self-contained plan for a queue-first Instagram operations studio, built from scratch,
operated by one technical founder + one non-technical operator, at launch scale
(~1,000 registered users, ~200 WAU, hundreds of connected accounts in one region, hundreds to
thousands of prepared/published items/day). Every mechanism below is named as a concrete
artifact (constraint, transaction, ordering, lease, retained evidence). Where the brief was
silent, a decision is made inline and collected in §13.

---

## 1. Technology decisions

### 1.1 Language and runtime

**Choice:** TypeScript on Node.js 22 LTS, one language across the web tier, all workers, and
browser automation. Shared typed domain packages (`@tbx/domain`, `@tbx/instagram`,
`@tbx/media`) imported by every process.

**Rejected:** Python 3.12. The strongest competing option: first-class Playwright bindings,
mature media tooling (Pillow), and a large ecosystem. Rejected because the customer web app
is React/TypeScript and the browser-automation layer (Playwright) is Node-first; running the
workers in Python would force a second language, a second dependency graph, and a second
review surface for the same domain invariants, which a one-person team cannot carry. The
media work is ffmpeg subprocess either way, so Python's media edge buys nothing here.

**Why:** one language lets the same typed state-machine and invariant code run in the web
tier and in workers, so the §4 invariants are enforced by one reviewed code path, not two.
That property outranks Python's ecosystem edge for this product and headcount.

### 1.2 Web framework

**Choice:** Next.js (App Router) for the customer app and the operator admin area, deployed
as a self-hosted Node process.

**Rejected:** a headless API (Fastify) + a separate SPA (Vite/React). Stronger on
inspectability of the API. Rejected because it splits routing, auth middleware, and rendering
across two deployables that must stay in lockstep for invitation gating, entitlement
hiding, and deep links; at this scale the SSR/ISR convenience and one deployable outweigh
the marginal API-inspectability gain.

**Why:** one deployable keeps the invitation gate (§3.1), entitlement enforcement (§3.9),
and notification deep links (§3.10) in a single reviewed surface.

### 1.3 Database

**Choice:** PostgreSQL 16, managed (Neon), single primary.

**Rejected:** SQLite. Genuinely simpler and zero-ops, and it would serve the read path.
Rejected because concurrent writers (web tier + N worker pools) need row-level locking and
`SELECT … FOR UPDATE SKIP LOCKED` for work-queue claims, partial indexes for active-only
uniqueness, and advisory locks for per-account serialization; SQLite is single-writer and
has no equivalent of `SKIP LOCKED`.

**Why:** the §4 invariants (single active publish claim, single cleanup item per account,
dedup) are enforced by conditional updates and partial unique indexes under concurrency;
that requirement outranks the operational simplicity SQLite would buy. Managed hosting
buys automated PITR/daily backups of the crown-jewel state (queue, receipts, billing,
entitlements) without the founder hand-rolling backup correctness.

### 1.4 Object storage

**Choice:** Cloudflare R2 (S3-compatible API), two buckets: `tbx-media` (originals +
prepared versions) and `tbx-backup` (pgBackRest + export archives).

**Rejected:** AWS S3. Same feature set. Rejected because media delivery to customers
(previews, exports, and the short-lived URLs Instagram fetches) is egress-heavy, and S3
charges ~$0.09/GB egress while R2 charges $0 egress; at the brief's media volume the egress
delta dominates the storage-price delta.

**Why:** zero egress removes the largest variable cost in the media path and keeps the
monthly budget flat as media volume grows.

### 1.5 Compute

**Choice:** Hetzner VPS. One web VM (CX32, 3 vCPU/8 GB) and two worker VMs (CX52, 6 vCPU/16 GB
each): worker-1 = publish + cleanup + analytics; worker-2 = media + restricted-sourcing
browser. systemd-supervised processes with restart=on-failure.

**Rejected:** pure serverless (Vercel/Fly.io functions) for workers. Stronger on
auto-scaling. Rejected because media jobs (ffmpeg) and browser jobs (Chromium) are
long-running, CPU/RAM-bound, and need persistent deps, core dumps, and cgroup caps that fit a
VM, not a function; and the brief's isolation rule (§3.6) wants explicit, operator-visible
concurrency budgets, which a VM gives directly.

**Why:** explicit concurrency budgets + cgroup caps on named VMs satisfy "heavy media work
and restricted browser work must not starve normal publishing or the customer-facing app"
with a mechanism the operator can see, at a flat monthly cost inside budget.

### 1.6 Browser automation

**Choice:** Playwright driving headless Chromium, used **only** for restricted sourcing
(operator's pooled accounts). `browserContext.storageState({path})` persists cookies/localStorage
between collection jobs; DOM state is not persisted, so every job re-navigates from stored
auth.

**Rejected:** Selenium. Mature, but slower API, more brittle waiting, and no first-class
storage-state persistence; Playwright's auto-waiting and `storageState` fit the
collect-verify-retry loop better.

**Why:** sourcing is the only capability that needs a browser; confining the browser to one
worker pool and one capability keeps its failure blast radius contained (§3.5, §4).

### 1.7 Media processing

**Choice:** ffmpeg 6.x as a subprocess (transcode, aspect-fit, metadata strip, probe) + sharp
for image ops (logo/banner overlay, format). Jobs run in the media worker pool.

**Rejected:** an in-process image library only (no ffmpeg). Cannot do short-form video, the
dominant cost in the brief. ffmpeg is the only option that covers both media kinds.

**Why:** the brief states images and short-form video are the dominant storage/processing
cost; ffmpeg covers both from one subprocess interface with per-job timeouts.

### 1.8 Identity and sessions

**Choice:** our own identity on Postgres. Short-lived JWT (12 h) carrying `sub`,
`workspace_id`, and `user.version`; per-request validation checks signature + expiry + one
indexed read of `users.version` (suspension/role change bumps the version → immediate
invalidation). Invitations and workspace membership are application tables, not a hosted auth
product.

**Rejected:** Supabase Auth / Clerk. Stronger on turnkey auth. Rejected because the brief's
identity model — expiring single-use invitations, workspace membership, role separation of
billing/destructive admin from publishing, temporary operator entitlement grants — is custom
logic; a hosted auth product would couple us to its claims/RLS semantics and fight the
invitation gate and entitlement enforcement.

**Why:** keeping identity in our own tables lets the invitation gate (§3.1) and entitlement
enforcement (§3.9) be the same reviewed Postgres state, with revocation by version bump.

### 1.9 Payments

**Choice:** Stripe: Checkout Session for purchase/upgrade, Customer Portal for self-serve,
and signed webhooks for lifecycle events. Card details never touch our product.

**Rejected:** Paddle / Lemon Squeezy (merchant-of-record). Stronger on tax/invoice
administration. Rejected because the brief's billing model (workspace-owned subscription,
operator-visible state, upgrades effective promptly, downgrades hold-not-delete) maps
directly onto Stripe subscription + webhook semantics, and Stripe's webhook idempotency
story is the one we must build anyway.

**Why:** Stripe's subscription + webhook model is the shape of §3.9; the idempotent webhook
handler (§4) is required regardless of provider.

### 1.10 Instagram integration

**Choice:** Instagram Graph API over the official `graph.instagram.com` endpoints using the
customer's OAuth grant: media container create (`POST /{ig-user-id}/media`), container
publish (`POST /{ig-user-id}/media_publish`), insights (`GET /{media-id}/insights`), and
archive (`POST /{media-id}?archived=true`). Long-lived token refreshed proactively; expiry
pauses publishing and surfaces recovery.

**Rejected:** browser-automation publishing against the customer's account. Would require
the customer's session/cookies (violates §5, "private access stays private") and is brittle.
The Graph API publishes with the OAuth token only — no password, no customer cookies.

**Why:** publishing, analytics, and cleanup all run on the customer's OAuth token via the
Graph API; the browser is reserved for restricted sourcing (operator pool), which is the only
capability that legitimately needs a logged-in browsing session. Exact field names follow
Meta's current Graph API reference at build time; the two-phase container→publish split is
the load-bearing structure and is stable.

### 1.11 Live updates

**Choice:** server-sent events (SSE) fanned out from a Postgres `outbox` table; workers and
web append outbox rows in-transaction with the state change; per-workspace SSE readers
poll/`LISTEN` and stream to that workspace's connected clients.

**Rejected:** Redis pub/sub or a managed realtime product. Stronger on fanout throughput.
Rejected because it introduces a second datastore for state that must stay consistent with
Postgres, and at the brief's event rate (status flips, not a firehose) an outbox + SSE is
simpler and keeps the event a durable, replayable audit artifact.

**Why:** the outbox row is both the live-update transport and a durable record of what
changed, so "status changes feel live" and "every final claim has inspectable evidence" share
one mechanism.

### 1.12 Work-queue transport

**Choice:** Postgres-backed work queues: a `work_items` table claimed via
`SELECT … FOR UPDATE SKIP LOCKED` + conditional status update, with leases and a reaper. No
separate message broker.

**Rejected:** RabbitMQ / SQS. Stronger on throughput and native DLQ. Rejected because claims
must be transactional with the state transition (the claim and the `ready→publishing` flip in
one transaction) and must be inspectable/auditable as rows; a broker hides the claim from the
same transaction and from the operator's inspection surface.

**Why:** at-most-once external effects require the claim, the state flip, and the attempt
record to commit atomically; only an in-DB queue gives that atomicity.

### 1.13 Scheduling

**Choice:** materialized immutable slots. Schedule rules generate concrete UTC instants
(slots) 7 days ahead; slots are upserted idempotently keyed by `(account_id, instant,
rule_fingerprint)`; a slot is claimed by at most one queue item via a conditional update.

**Rejected:** lazy next-slot computation on every evaluation. Rejected because re-deriving
instants on the fly is exactly where DST/timezone double-post bugs live, and it makes the
effect of a schedule edit un-inspectable.

**Why:** immutable materialized instants make timezone edits, DST spring-forward (no instant
maps to the skipped local time → no slot), and duplicated rules (same key → one slot) safe and
testable; editing a rule only regenerates *unclaimed future* slots.

### 1.14 Secrets at rest

**Choice:** IG tokens and other private grants encrypted at rest with libsodium
`secretbox` (XSalsa20-Poly1305) under a data key held in env/KMS; ciphertext + nonce in
`ig_tokens`. Tokens are never logged and never returned to the browser.

**Rejected:** Postgres `pgcrypto` symmetric encryption. In-DB and convenient, but key material
lives in the DB and every read decrypts server-side with the key in the same store; libsodium
keeps the data key out of the crown-jewel database.

**Why:** §5 requires private grants to stay private across logs, receipts, and support tools;
keeping the key out of Postgres and encrypting with a named library is the stronger posture.

### 1.15 Backups and restoration

**Choice:** managed Postgres PITR + daily base backups (Neon) for relational state; pgBackRest
for any self-hosted state to `tbx-backup`; media is inherently durable in R2 (versioned). A
rehearsed restore runbook restores a named workspace to a staging branch and diffs.

**Rejected:** nightly `pg_dump` to a single file only. Weaker: no PITR, coarse granularity,
harder point-in-time restore of one workspace.

**Why:** §3.11 makes daily backups *and* a rehearsed restoration process launch requirements;
PITR + versioned media + a runbook is the mechanism that lets us prove restore, not just show a
green health screen.

---

## 2. System architecture

Independently running parts, their responsibilities, and how work moves between them.

### 2.1 Processes

- **web** (Next.js, web VM): customer pages, admin area, authN/authZ, invitation gate,
  upload orchestration, SSE fanout readers, Stripe redirect/portal. No long-running work.
- **worker-publish** (worker-1): claims `ready` queue items, runs the two-phase publish
  (container create → publish) against the Graph API, writes attempts/receipts, drives
  reconciliation, enforces quota and daily limits.
- **worker-cleanup** (worker-1): runs cleanup runs one item at a time per account via the
  Graph API archive/delete, serialized by a per-account run lock.
- **worker-analytics** (worker-1): polls insights on schedule, appends snapshots, never
  overwrites.
- **worker-media** (worker-2): ffmpeg/sharp preparation jobs; writes prepared versions;
  retries prep failures on the same queue item.
- **worker-sourcing** (worker-2): restricted sourcing — verifies sources, runs collection
  jobs in Playwright contexts, filters, and lands eligible results in the backlog.
- **reaper** (worker-1, low priority): reclaims expired leases, expires invitations,
  finalizes deletion jobs past their retention, alarms on queue depth.
- **scheduler** (worker-1): materializes/refreshes schedule slots within the horizon;
  generates deferred-work re-queue for quota/cooldown.

### 2.2 How work moves

All cross-process work is a row in Postgres. There is no out-of-DB message path for state.

1. A web request that enqueues work writes the domain row (e.g., `queue_items` in
   `preparing`) **and** a `work_items` claim row in the same transaction. Commit makes it
   visible to workers.
2. A worker claims with `SELECT id FROM work_items WHERE kind='media' AND status='queued'
   ORDER BY priority, enqueued_at FOR UPDATE SKIP LOCKED LIMIT <pool cap>` then flips
   `status='leased', lease_owner=me, lease_expires=now()+<lease>` in the same transaction.
   A crashed worker's lease expires; the reaper returns it to `queued`.
3. State transitions are conditional updates guarded by the expected current status, so two
   workers cannot both advance the same item: the loser's `WHERE status='ready'` matches zero
   rows and it exits.
4. Durable side-effect evidence (attempts, receipts, snapshots) is written in the same
   transaction as the transition it justifies.
5. Live updates: any committed transition also appends an `outbox` row (same transaction);
   web SSE readers for that workspace stream it. The outbox row is the audit artifact too.

### 2.3 Starvation and endangerment isolation

- **Pool separation:** media/browser work (worker-2) never shares a process or a claim pool
  with publishing (worker-1). A media backlog cannot consume a publish claim.
- **cgroup caps:** worker-2 media jobs capped at 4 of 6 vCPU (leaving 2 for the sourcing
  browser + OS); worker-1 publish concurrency capped at 4 claims in flight.
- **Priority + aging:** `work_items.priority` (publish=100, cleanup=90, analytics=50,
   media=40, sourcing=30) with an aging term so no class starves another indefinitely; the
   reaper alarms if the publish pool is idle while `ready` items exist (starvation alarm).
   Numeric alarms: publish-queue depth > 500 `ready` items sustained 15 min; publish-latency
   p95 > 90 s over 15 min; media-queue depth > 1,000. Derivation: launch scale ≈1,000
   items/day ≈ 0.012 item/s average with posting-window bursts ≈50/min; a healthy publish pool
   (4 concurrent × ~30 s each) drains ≈8 items/min, so a 500-item backlog is ≈1 h of work at
   burst rate — 15 min sustained means genuine saturation, not a transient burst.
- **External politeness:** per-account token bucket (50 calls/5 min, refill 1/6 s) gates all
  Graph API calls from every pool, so analytics/cleanup/publish share one budget per account
  and none can exhaust Instagram's limits.
- **Browser isolation:** sourcing contexts run only in worker-2, capped at 3 concurrent
  contexts (16 GB ÷ ~800 MB/context ≈ 20 theoretical; capped at 3 by external-site rate
  limits and RAM headroom). A hung context hits its 30 s operation timeout and is killed; it
  cannot hold a publish or cleanup slot because those live in worker-1.

### 2.4 The two experiences stay separable

Public upload-only experience and restricted-automation experience are separable in access,
operations, testing, and failure containment:
- **Access:** restricted capabilities are invisible and unreachable without an explicit
  workspace entitlement (§4); the public UI never renders them.
- **Operations:** sourcing runs only in worker-2; a sourcing outage cannot block publishing.
- **Testing:** sourcing has its own test lane (operator pool sandbox accounts) independent of
  the publish lane (safe test accounts).
- **Failure containment:** a sourcing failure lands in the backlog as `retrying`/`blocked`
  and never touches a queue item's publish state.

### 2.5 Monthly cost (arithmetic)

Representative list prices (confirm at procurement; see §13). Normal case:

| Component | Unit price | Quantity | Monthly |
|---|---|---|---|
| Web VM (Hetzner CX32, 3 vCPU/8 GB) | ~$9/mo | 1 | $9 |
| Worker-1 VM (CX52, 6 vCPU/16 GB) | ~$13/mo | 1 | $13 |
| Worker-2 VM (CX52, 6 vCPU/16 GB) | ~$13/mo | 1 | $13 |
| Managed Postgres (Neon entry always-on) | ~$19/mo | 1 | $19 |
| R2 media storage | $0.015/GB-mo | 500 GB | $7.50 |
| Domains + email + monitoring | ~$10/mo flat | 1 | $10 |
| **Sum (normal)** | | | **≈ $71.50/mo** |

R2 egress is $0, so media delivery (previews, exports, the URLs Instagram fetches) adds no
egress line. Stripe charges per successful payment (~2.9% + $0.30) as revenue, not infra.

Worst realistic case: media volume ×5 (2.5 TB) → R2 2,500 GB × $0.015 = $37.50; sustained load
doubles Postgres compute-hours → ~$38; VMs unchanged ($35); misc $10. **Sum ≈ $120/mo** — inside
the stated budget envelope (≈$300–$900/month).

Over-budget behavior (if monthly exceeds the $300 floor of the envelope): apply, in order,
(a) an R2 lifecycle rule moving cold originals (>90 d untouched) to a cheaper archive class,
(b) right-sizing the worker VMs down (compute is stateless; state lives in Postgres/R2),
(c) reducing sourcing concurrency and check frequency. None require a schema or invariant change.

---

## 3. Data model

Executable PostgreSQL 16 DDL. Column types, `NOT NULL`, `CHECK`, `UNIQUE`, foreign keys, and
the indexes that enforce something are all present. Where a constraint cannot be expressed in
DDL, the enforcing code path is named. Tenant isolation backstop: every tenant table also gets
a Postgres Row-Level-Security policy keyed to a per-operation GUC (`SET app.workspace_id`), so
a query that forgets the application-level scope still returns zero rows. RLS is the backstop;
application scoping is the primary path.

```sql
-- ============ identity, workspaces, access ============
CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT NOT NULL,
    full_name   TEXT,
    version     INT  NOT NULL DEFAULT 1,          -- bumped on suspend/role change -> JWT invalidation
    suspended   BOOL NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX users_email_uq ON users (email);

CREATE TABLE workspaces (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT NOT NULL,
    owner_user_id UUID NOT NULL REFERENCES users(id),
    plan_id       UUID REFERENCES plans(id),
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active','suspended')),
    niche         TEXT, goal TEXT, cadence TEXT,    -- onboarding answers
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE workspace_members (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id      UUID NOT NULL REFERENCES users(id)     ON DELETE CASCADE,
    role         TEXT NOT NULL CHECK (role IN ('owner','admin','editor','viewer')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, user_id)
);
-- role separation: billing/destructive admin vs day-to-day publishing is enforced in code by
-- require_role(capability) consulting this table server-side on every gated route/worker.

CREATE TABLE invitations (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id),        -- NULL => creates a new workspace
    email        TEXT NOT NULL,
    role         TEXT NOT NULL DEFAULT 'editor' CHECK (role IN ('owner','admin','editor','viewer')),
    redeemed_by  UUID REFERENCES users(id),
    redeemed_at  TIMESTAMPTZ,
    expires_at   TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (redeemed_at IS NULL OR redeemed_by IS NOT NULL)
);
-- single-use is enforced by atomic redeem() : UPDATE invitations SET redeemed_by=…, redeemed_at=NOW()
-- WHERE id=… AND redeemed_at IS NULL AND expires_at>NOW()  -- second caller matches 0 rows.

CREATE TABLE waitlist_entries (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT NOT NULL,
    invited_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX waitlist_email_uq ON waitlist_entries (email);

-- ============ plans, billing, entitlements ============
CREATE TABLE plans (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code        TEXT NOT NULL,
    name        TEXT NOT NULL,
    interval    TEXT NOT NULL CHECK (interval IN ('monthly','annual')),
    price_cents INT  NOT NULL,
    limits      JSONB NOT NULL,   -- {accounts, collaborators, posts_per_month, storage_gb, features[]}
    UNIQUE (code)
);

CREATE TABLE subscriptions (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id           UUID NOT NULL REFERENCES workspaces(id),
    plan_id                UUID NOT NULL REFERENCES plans(id),
    stripe_subscription_id TEXT,
    status                 TEXT NOT NULL DEFAULT 'incomplete'
                           CHECK (status IN ('active','trialing','past_due','canceled','incomplete')),
    current_period_end     TIMESTAMPTZ,
    cancel_at_period_end   BOOL NOT NULL DEFAULT FALSE,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- one active subscription per workspace:
CREATE UNIQUE INDEX subscriptions_one_active_per_ws
    ON subscriptions (workspace_id)
    WHERE status IN ('active','trialing','past_due');

CREATE TABLE stripe_events (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_event_id  TEXT NOT NULL,
    type             TEXT NOT NULL,
    payload          JSONB NOT NULL,
    received_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at       TIMESTAMPTZ,
    UNIQUE (stripe_event_id)          -- webhook replay => INSERT fails => no-op (idempotent)
);

CREATE TABLE entitlement_grants (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    capability   TEXT NOT NULL,        -- e.g. 'restricted_sourcing','managed_cleanup','beta'
    granted_by   UUID NOT NULL REFERENCES users(id),   -- operator
    reason       TEXT,
    expires_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- effective entitlement = plan.limits.features UNION live entitlement_grants (unexpired).
-- enforced server-side by evaluate_entitlement(workspace_id, capability) on every gated path.

-- ============ Instagram accounts, grants, connection review ============
CREATE TABLE ig_accounts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ig_user_id      TEXT NOT NULL,
    display_name    TEXT,
    profile_image_url TEXT,
    timezone        TEXT NOT NULL DEFAULT 'UTC',
    daily_allowance INT  NOT NULL DEFAULT 1 CHECK (daily_allowance >= 0),
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','paused','reauth_required','disconnected')),
    connected_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disconnected_at TIMESTAMPTZ,
    UNIQUE (ig_user_id)   -- same IG account cannot belong to two workspaces (global)
);

CREATE TABLE ig_tokens (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ig_account_id    UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    access_token     BYTEA NOT NULL,     -- libsodium secretbox ciphertext
    nonce            BYTEA NOT NULL,
    expires_at       TIMESTAMPTZ NOT NULL,
    refreshed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ig_account_id)   -- one private grant per account; never logged/returned to browser
);

CREATE TABLE connection_requests (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id   UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email          TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending','approved','declined','invited')),
    operator_note  TEXT,                 -- internal only, never exposed to requester
    public_reason  TEXT,                 -- shown to requester
    decided_by     UUID REFERENCES users(id),
    decided_at     TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============ media (content-addressed, immutable) ============
CREATE TABLE media_objects (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id   UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    content_hash   TEXT NOT NULL,        -- sha256 of original bytes; immutable
    kind           TEXT NOT NULL CHECK (kind IN ('image','video')),
    size_bytes     BIGINT NOT NULL CHECK (size_bytes >= 0),
    duration_ms    INT,
    width          INT, height INT,
    original_key   TEXT NOT NULL,        -- R2 object key
    uploaded_by    UUID NOT NULL REFERENCES users(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, content_hash)  -- dedupe identical uploads within a workspace
);

CREATE TABLE prep_preferences (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ig_account_id    UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    version          INT NOT NULL DEFAULT 1,
    settings         JSONB NOT NULL,     -- {reels_format, aspect_ratio, strip_metadata, logo_key, banner_key, caption_assembly}
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ig_account_id, version)      -- versioned; queued items freeze a version
);

CREATE TABLE media_prepared (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    media_object_id    UUID NOT NULL REFERENCES media_objects(id) ON DELETE CASCADE,
    prep_preference_id UUID REFERENCES prep_preferences(id),
    prepared_key       TEXT NOT NULL,    -- R2 key of publish-ready version
    kind               TEXT NOT NULL CHECK (kind IN ('image','video')),
    aspect_ratio       TEXT,
    size_bytes         BIGINT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (media_object_id, prep_preference_id)
);

-- ============ the queue and its frozen inputs ============
CREATE TABLE queue_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ig_account_id   UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    media_object_id UUID NOT NULL REFERENCES media_objects(id) ON DELETE CASCADE,
    caption         TEXT,
    status          TEXT NOT NULL DEFAULT 'preparing'
                    CHECK (status IN ('preparing','ready','publishing','published',
                                      'hidden','failed','needs_review','deferred')),
    position        NUMERIC NOT NULL DEFAULT 0,   -- queue order (what is next)
    snapshot_id     UUID,                          -- frozen inputs for THIS publish
    scheduled_slot_id UUID,                        -- claimed slot (when it may go)
    created_by      UUID NOT NULL REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    lease_owner     TEXT,
    lease_expires   TIMESTAMPTZ
);
CREATE INDEX queue_items_account_status_pos ON queue_items (ig_account_id, status, position);
CREATE INDEX queue_items_workspace_status   ON queue_items (workspace_id, status);
-- at-most-once publish is enforced by claim_publish() : conditional update
--   UPDATE queue_items SET status='publishing', lease_owner=me, lease_expires=NOW()+interval '30s'
--   WHERE id=… AND status='ready'   -- second worker matches 0 rows and exits.

CREATE TABLE queue_item_snapshots (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id           UUID NOT NULL REFERENCES queue_items(id) ON DELETE CASCADE,
    media_object_id         UUID NOT NULL REFERENCES media_objects(id),
    prepared_key            TEXT NOT NULL,
    caption                 TEXT NOT NULL,
    attribution             JSONB,                 -- source credit + original link where relevant
    destination_ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
    prep_preference_id      UUID REFERENCES prep_preferences(id),
    prep_settings_version   INT,
    frozen_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (queue_item_id)  -- exactly one frozen input set per item; immutable (no updated_at)
);
```

```sql
-- ============ scheduling (materialized immutable slots) ============
CREATE TABLE schedule_rules (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ig_account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('fixed_days','interval_window')),
    spec        JSONB NOT NULL,   -- fixed_days:{days[],times_local[]} | interval_window:{interval_minutes,window_start_local,window_end_local}
    timezone    TEXT NOT NULL,
    enabled     BOOL NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE schedule_slots (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ig_account_id           UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    instant                 TIMESTAMPTZ NOT NULL,   -- concrete UTC instant; immutable once materialized
    rule_id                 UUID NOT NULL REFERENCES schedule_rules(id),
    rule_fingerprint        TEXT NOT NULL,          -- hash(kind,spec,timezone); changes when rule edited
    claimed_by_queue_item_id UUID REFERENCES queue_items(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (ig_account_id, instant, rule_fingerprint)  -- duplicated rules / tz edits => one slot
);
-- slot claim is atomic: UPDATE schedule_slots SET claimed_by_queue_item_id=…
--   WHERE id=… AND claimed_by_queue_item_id IS NULL  -- second claimant matches 0 rows.
-- DST spring-forward: no instant maps to the skipped local time => no slot generated.

-- ============ durable daily quota (restart-safe) ============
CREATE TABLE quota_usage (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ig_account_id   UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    day             DATE NOT NULL,
    published_count INT NOT NULL DEFAULT 0 CHECK (published_count >= 0),
    UNIQUE (ig_account_id, day)   -- incremented in-transaction with the receipt write
);

-- ============ publish attempts and receipts (evidence) ============
CREATE TABLE publish_attempts (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id UUID NOT NULL REFERENCES queue_items(id) ON DELETE CASCADE,
    phase         TEXT NOT NULL CHECK (phase IN ('container_create','container_publish','reconcile')),
    attempt_no    INT NOT NULL DEFAULT 1 CHECK (attempt_no >= 1),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at   TIMESTAMPTZ,
    outcome       TEXT CHECK (outcome IN ('sent','ok','error','timeout','unknown')),
    external_id   TEXT,                 -- container_id or ig media_id when known
    error_class   TEXT CHECK (error_class IN ('pre_send_network','rate_limited','invalid_media',
                                              'permission_expired','account_limit','transient','unknown')),
    http_status   INT,
    evidence      JSONB,                -- request summary (no tokens); redacted
    UNIQUE (queue_item_id, phase, attempt_no)
);

CREATE TABLE receipts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id   UUID NOT NULL REFERENCES queue_items(id) ON DELETE CASCADE,
    ig_account_id   UUID NOT NULL REFERENCES ig_accounts(id),
    media_object_id UUID NOT NULL REFERENCES media_objects(id),
    frozen_caption  TEXT NOT NULL,
    ig_media_id     TEXT NOT NULL,
    ig_permanurl    TEXT NOT NULL,
    container_id    TEXT,
    published_at    TIMESTAMPTZ NOT NULL,
    published_by    TEXT NOT NULL,      -- worker identity (attribution)
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (queue_item_id)   -- one receipt per item; the durable publish claim
);

-- ============ managed cleanup (entitled, high-risk) ============
CREATE TABLE cleanup_rules (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ig_account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    media_kind   TEXT NOT NULL CHECK (media_kind IN ('photo','reel','any')),
    min_age_days INT NOT NULL DEFAULT 0 CHECK (min_age_days >= 0),
    min_metrics  JSONB NOT NULL,        -- {reach?, likes?, comments?, plays?} minimums
    version      INT NOT NULL DEFAULT 1,
    created_by   UUID NOT NULL REFERENCES users(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE cleanup_runs (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ig_account_id       UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    cleanup_rule_id     UUID NOT NULL REFERENCES cleanup_rules(id),
    frozen_rule         JSONB NOT NULL,   -- rule as confirmed
    frozen_selection    JSONB NOT NULL,   -- exact item ids + metrics used at confirmation
    selection_fingerprint TEXT NOT NULL,  -- hash(frozen_selection); stale if items change
    status              TEXT NOT NULL DEFAULT 'preview'
                        CHECK (status IN ('preview','confirmed','running','paused_reconcile','completed','stopped')),
    confirmed_by        UUID REFERENCES users(id),
    confirmed_at        TIMESTAMPTZ,
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);
-- one destructive run per account at a time:
CREATE UNIQUE INDEX cleanup_runs_one_active_per_account
    ON cleanup_runs (ig_account_id)
    WHERE status IN ('confirmed','running','paused_reconcile');

CREATE TABLE cleanup_run_items (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cleanup_run_id UUID NOT NULL REFERENCES cleanup_runs(id) ON DELETE CASCADE,
    queue_item_id  UUID NOT NULL REFERENCES queue_items(id),   -- the library post
    media_kind     TEXT NOT NULL CHECK (media_kind IN ('photo','reel')),
    action         TEXT NOT NULL CHECK (action IN ('archive','recently_deleted')),
    result         TEXT NOT NULL DEFAULT 'pending' CHECK (result IN ('pending','ok','error','unknown')),
    result_detail  JSONB,                 -- redacted evidence; no session material
    processed_at   TIMESTAMPTZ,
    UNIQUE (cleanup_run_id, queue_item_id)
);

-- ============ analytics (append-only history) ============
CREATE TABLE analytics_snapshots (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ig_account_id  UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    ig_media_id    TEXT NOT NULL,
    queue_item_id  UUID REFERENCES queue_items(id),
    taken_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metrics        JSONB NOT NULL,   -- {reach, plays, likes, comments, saves, shares, impressions}
    source         TEXT NOT NULL CHECK (source IN ('scheduled','manual'))
);
CREATE INDEX analytics_by_media_time ON analytics_snapshots (ig_media_id, taken_at);  -- append-only, never overwritten

-- ============ notifications, feedback ============
CREATE TABLE notifications (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id     UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    recipient_user_id UUID REFERENCES users(id),     -- NULL => workspace-wide
    kind             TEXT NOT NULL,
    priority         TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('normal','high')),
    title            TEXT NOT NULL,
    body             TEXT,
    deep_link        TEXT,
    read_at          TIMESTAMPTZ,
    dismissed_at     TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX notifications_ws_time ON notifications (workspace_id, created_at);

CREATE TABLE feedback (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id   UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id        UUID NOT NULL REFERENCES users(id),
    kind           TEXT NOT NULL CHECK (kind IN ('bug','confusing','idea','praise','other')),
    body           TEXT NOT NULL,
    page_context   JSONB,                 -- enough for the operator to investigate
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============ deletion (durable, resumable) ============
CREATE TABLE deletion_jobs (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id   UUID NOT NULL REFERENCES workspaces(id),
    requested_by   UUID NOT NULL REFERENCES users(id),
    status         TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed')),
    step_cursor    INT NOT NULL DEFAULT 0,
    steps          JSONB NOT NULL,   -- ordered: revoke_ig, cancel_billing, null_personal, delete_media, write_receipt
    started_at     TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ,
    receipt_id     UUID
);

CREATE TABLE deletion_receipts (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    deletion_job_id  UUID NOT NULL UNIQUE REFERENCES deletion_jobs(id),
    completed_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    removed_summary  JSONB NOT NULL,   -- non-identifying
    retained_evidence JSONB NOT NULL   -- minimum to prove completion
);

-- ============ audit ============
CREATE TABLE audit_log (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id  UUID REFERENCES workspaces(id),
    actor_user_id UUID REFERENCES users(id),
    actor_kind    TEXT NOT NULL CHECK (actor_kind IN ('user','operator','worker','system')),
    action        TEXT NOT NULL,
    target_kind   TEXT,
    target_id     TEXT,
    detail        JSONB,
    at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX audit_ws_time ON audit_log (workspace_id, at);

-- ============ restricted sourcing (entitled) ============
CREATE TABLE sourcing_sources (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id   UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ig_account_id  UUID REFERENCES ig_accounts(id),
    kind           TEXT NOT NULL CHECK (kind IN ('account','hashtag','reels_feed')),
    spec           JSONB NOT NULL,   -- {media_type, max_age_days, min_likes, min_comments, min_plays, exclude_words[], candidate_count, check_interval_minutes}
    status         TEXT NOT NULL DEFAULT 'pending_verification'
                   CHECK (status IN ('pending_verification','active','paused','retrying','blocked')),
    verified_at    TIMESTAMPTZ,
    created_by     UUID NOT NULL REFERENCES users(id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE sourcing_backlog (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ig_account_id     UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    source_id         UUID REFERENCES sourcing_sources(id),
    external_post_id  TEXT NOT NULL,
    original_url      TEXT NOT NULL,
    author            TEXT,
    caption           TEXT,
    media_kind        TEXT NOT NULL CHECK (media_kind IN ('image','video')),
    observed_engagement JSONB,
    discovered_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status            TEXT NOT NULL DEFAULT 'eligible'
                      CHECK (status IN ('eligible','held_filtered','queued','rejected')),
    queue_item_id     UUID REFERENCES queue_items(id)
);
-- never import/queue the same source post twice (concurrent collect/refill safe):
CREATE UNIQUE INDEX sourcing_backlog_one_live_per_post
    ON sourcing_backlog (ig_account_id, external_post_id)
    WHERE status IN ('eligible','held_filtered','queued');

CREATE TABLE sourcing_pool_accounts (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label              TEXT NOT NULL,
    browser_profile_key TEXT NOT NULL,   -- R2 key of Playwright storageState
    status             TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','disabled')),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============ generic background work (leases) ============
CREATE TABLE work_items (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind          TEXT NOT NULL CHECK (kind IN ('media_prep','publish','reconcile','cleanup_item',
                                                'analytics','sourcing_verify','sourcing_collect',
                                                'deletion_step','slot_refresh')),
    ref_id        UUID NOT NULL,          -- points at the domain row
    priority      INT NOT NULL DEFAULT 50 CHECK (priority BETWEEN 0 AND 100),
    status        TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','leased','done','failed','dead')),
    lease_owner   TEXT,
    lease_expires TIMESTAMPTZ,
    attempts      INT NOT NULL DEFAULT 0,
    enqueued_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX work_items_claim ON work_items (kind, status, priority, enqueued_at);
-- claim() : SELECT … FOR UPDATE SKIP LOCKED LIMIT <pool cap> then flip status='leased' in-txn;
-- reaper returns expired leases (lease_expires < NOW()) to 'queued'.
```

**RLS backstop (applied to every tenant table above):**

```sql
ALTER TABLE queue_items FORCE ROW LEVEL SECURITY;
CREATE POLICY queue_items_ws ISOLATION ON queue_items
    USING (workspace_id = NULLIF(current_setting('app.workspace_id'), '')::uuid);
-- repeated per tenant table; workers/web SET app.workspace_id per operation.
```

---

## 4. Invariant enforcement map

Three columns: the invariant, the structural mechanism that enforces it, and a specific test that
proves it. Mechanisms are constraints, transactions, orderings, leases, or retained evidence —
not intentions. Tests are named so they can be written and run.

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| P1a — publish belongs to the intended workspace+account | `queue_items.workspace_id`+`ig_account_id` FKs; publish executes only against `snapshot.destination_ig_account_id`; RLS backstop scopes every read to the requester's `app.workspace_id` | `test_p1a_cross_tenant`: two workspaces, each with account+item; as workspace-A actor, GET/publish workspace-B's item via API and via SQL with `app.workspace_id=A`; assert 404 / zero rows and that only A's account resolves |
| P1b — reviewed media+caption only | `queue_item_snapshots` (UNIQUE per item, immutable) freezes `prepared_key`+`caption` at queue time; publish reads the snapshot, never live caption/media | `test_p1b_snapshot_freeze`: queue, then edit caption, change prep preference, re-upload media; publish; assert `receipt.frozen_caption` and `prepared_key` equal the queue-time values, not the new ones |
| P1c — never publish twice | `claim_publish()` conditional update `WHERE status='ready'`; `receipts UNIQUE(queue_item_id)`; `publish_attempts UNIQUE(item,phase,attempt_no)`; no retry on post-send unknown | `test_p1c_double_click`: two concurrent publish clicks + a worker restart mid-publish; assert exactly one `receipts` row and one `container_publish` attempt with outcome ok/unknown; status is published or needs_review, never two receipts |
| P2 — queue tells the truth | status enum + `updated_at`; `outbox` fanout appended in-transaction; `position NUMERIC` ordering; `needs_review` distinct from `published` | `test_p2_no_false_published`: induce a post-send timeout; assert status=needs_review (not published), an outbox needs_review event was emitted, and the item remains present with its position intact (no silent disappear) |
| P3 — uncertainty never becomes a second destructive action | `publish_attempts.outcome` includes `unknown`; `reconcile()` queries IG before any retry; cleanup `result='unknown'` sets run `paused_reconcile` and `cleanup_runs_one_active_per_account` orders later runs behind it | `test_p3_no_blind_retry`: simulate unknown on `container_publish`; assert no second `container_publish` attempt until a `reconcile` attempt with outcome ok/error exists; for cleanup assert the run paused and a second same-account run cannot start |
| P4 — holding/leaving never destroys work | transitions to `deferred`/`hidden`/account `paused`/workspace `suspended`/account `disconnected` never DELETE; suspension gates activity, not data | `test_p4_hold_not_destroy`: pause account, suspend workspace, disconnect account, flush caches; assert `queue_items` rows+positions unchanged and fully restorable on resume/reactivate/reconnect |
| P5 — private access stays private | no password column exists in the schema; `ig_tokens` sealed (libsodium) + UNIQUE per account; RLS isolation; media presigned URLs narrow+expiring; `audit_log`/outbox written via `redact()` | `test_p5_no_password_no_leak`: assert schema has zero password columns; connect an account; assert `ig_tokens.access_token` != plaintext and absent from every `audit_log.detail`/outbox payload; assert a presigned media URL 403s after expiry |
| H1 — tenant isolation on every action incl. media links, live updates, notifications, analytics, operator recovery | RLS backstop on every tenant table; `workspace_id` on media-link resolution, outbox, notifications, analytics; operator recovery paths `SET app.workspace_id` before reading | `test_h1_isolation_sweep`: for each tenant table run a representative read as workspace A against workspace B rows with `app.workspace_id=A`; assert zero rows across media-link, notification fanout, analytics read, and operator work-item inspection |
| H2 — publish/charge/archive/delete/invite/access safe against repeats | UNIQUE + conditional updates: invite `redeem()`; `stripe_events UNIQUE(stripe_event_id)`; `receipts UNIQUE(queue_item_id)`; `cleanup_runs_one_active_per_account`; `deletion_jobs.step_cursor` | `test_h2_replay`: replay the same invite redeem, the same Stripe webhook (same `stripe_event_id`), the same publish click, the same cleanup confirm; assert each is a no-op the second time and state is unchanged |
| H3 — one active publication crosses the boundary | `claim_publish()` conditional update + `lease_expires`; `receipts UNIQUE(queue_item_id)` | `test_h3_single_cross`: N workers race one ready item; assert exactly one transitions to `publishing` and at most one `container_publish` attempt; losers' updates match zero rows and exit |
| H4 — one cleanup item per account at a time | `cleanup_runs_one_active_per_account` partial unique; run loop advances one `cleanup_run_items` row per claim | `test_h4_serial_cleanup`: start two same-account runs concurrently; assert second blocked; assert items processed strictly one-at-a-time (`processed_at` strictly increasing, no overlap) |
| H5 — pre-action retryable, post-action not until reconciled | `publish_attempts.error_class='pre_send_network'` retryable; outcome `timeout`/`unknown` post-send not retryable until a `reconcile` attempt resolves | `test_h5_retry_policy`: induce a pre-send network error then a post-send timeout; assert the first yields attempt_no+1 and return to ready, the second yields no retry until reconcile resolves |
| H6 — approved media/caption/destination/settings/rule/metrics frozen | `queue_item_snapshots` (publish); `cleanup_runs.frozen_rule`+`frozen_selection`+`selection_fingerprint` (cleanup); execution reads frozen rows only | `test_h6_cleanup_freeze`: confirm a cleanup selection, then change underlying metrics/protection before execution; assert `selection_fingerprint` mismatch invalidates the old confirmation and execution uses `frozen_selection` only |
| H7 — held not destroyed under pause/disconnect/suspend/over-plan/over-quota/reauth | status `deferred` for quota/cooldown; `workspaces.status='suspended'` gates; plan limits evaluated server-side; no DELETE on limit breach | `test_h7_quota_defer`: drive an account to its daily allowance; assert next item becomes `deferred` (not failed/deleted), `quota_usage` incremented, item re-eligible next day |
| H8 — daily use survives restart/cache loss | `quota_usage` (UNIQUE account+day) incremented in-transaction with the receipt; read from DB, never cache | `test_h8_restart_quota`: publish k items, restart the worker process; assert `quota_usage` count persists and allowance-k remain; restart does not reset the counter |
| H9 — no duplicate source/upload into the same queue | `sourcing_backlog_one_live_per_post` partial unique; `media_objects UNIQUE(workspace_id,content_hash)`; enqueue is an atomic backlog status flip | `test_h9_concurrent_refill`: concurrent collection + manual refill for the same account/source; assert no two `queue_items` reference the same `external_post_id` (second enqueue matches zero rows) |
| H10 — restricted source/cleanup invisible+unreachable without entitlement | `evaluate_entitlement(workspace_id,capability)` consulted server-side on every restricted route/worker; UI hiding is cosmetic | `test_h10_entitlement_gate`: without the capability call the restricted sourcing+cleanup APIs directly; assert 403/404 and no rows; grant capability then assert reachable; revoke then assert unreachable with existing work preserved |
| H11 — media private by default, narrow expiring outside access | private R2 buckets; presigned GET URLs scoped to one object key with 15-min expiry; originals never served publicly | `test_h11_narrow_expire`: generate a presigned URL; assert it fetches exactly that object, 403s after expiry, and cannot be rewritten to another key (signature binds the key) |
| H12 — grants/sessions never to browser, receipts, or logs | `ig_tokens` sealed and never serialized to API responses; `receipts` schema has no token column; `audit_log`/outbox written via `redact()` stripping token/session fields | `test_h12_no_secret_leak`: byte-scan all API responses, `receipts` rows, `audit_log.detail`, and outbox payloads for a publish+cleanup; assert no access-token or storageState bytes appear |
| H13 — content-rights acceptance attributable+versioned | enqueue requires a rights-acceptance record in `snapshot.attribution` (`accepted_by`, `version`, `accepted_at`); versioned per rights-policy revision | `test_h13_rights_versioned`: queue an item; assert `snapshot.attribution` carries `accepted_by`+`version`; bump the rights-policy version; assert new queues carry the new version and old snapshots retain the old |
| H14 — callbacks/billing/deletion repeat+out-of-order safe | `stripe_events UNIQUE(stripe_event_id)`; forward-only conditional subscription state transitions; `deletion_jobs.step_cursor` idempotent steps | `test_h14_out_of_order_billing`: deliver webhook events shuffled+duplicated (invoice.paid before checkout.completed; duplicate subscription.updated); assert one correct final subscription state and no duplicate transitions |
| H15 — every final claim has inspectable evidence | `receipts` (publish); `cleanup_run_items.result_detail` (archive/delete); `stripe_events`+`subscriptions` (charge); `deletion_receipts` (deletion) | `test_h15_evidence_present`: for each final-claim type assert a non-empty evidence row exists (receipt / result_detail / stripe_event / deletion_receipt) |

---

## 5. Failure-mode walkthrough

Numbered sequences. Each step names the mechanism that does the work. Each scenario ends with the
evidence an operator can inspect. Note on scenario 9: cleanup executes via the Graph API on the
customer's OAuth token (no customer browser session exists without their password), so the brief's
"browser hang during cleanup" is realized here as a hung destructive API call at the destructive
boundary; the genuine browser hang is covered separately as scenario 14 (restricted sourcing).

**Scenario 1 — crash before publish.**
*Scenario:* the publish worker dies after claiming an item but before/after dispatching the
container-create request, with no response recorded.
*What happens:*
1. `claim_publish()` committed `status='publishing'` + `lease_expires=now()+30s`; the attempt-start
   row (`publish_attempts` phase=container_create) committed with `outcome` NULL if pre-dispatch, or
   `outcome='sent'` if the worker committed the dispatch marker before awaiting the response.
2. The lease expires with no resolved attempt. The **reaper** (sweep every 10 s) reads the attempt's
   committed outcome to choose the safe path.
3. Pre-dispatch (`outcome` NULL): reaper sets `status='ready'` and clears the lease — no side-effect
   crossed, so retry is safe. Post-dispatch (`outcome='sent'`): reaper sets `status='needs_review'`
   and enqueues a `reconcile` work item — the request may have been accepted.
4. `reconcile()` queries IG for actual state; published → write `receipts` + `status='published'`;
   not published → `status='ready'` (retry from the last confirmed phase); IG unreachable → remain
   `needs_review`, retry reconcile with backoff, never blind-retry.
*Evidence:* `publish_attempts` shows the attempt with outcome NULL or 'sent' and no matching receipt;
`queue_items.status` is `ready` (pre-send) or `needs_review` (post-send); `audit_log` shows the reaper
reclaim (`actor_kind='system'`); a `reconcile` attempt row exists in the post-send case.

**Scenario 2 — crash after a publish may have been accepted.**
*Scenario:* the worker dispatches `container_publish`, commits the dispatch marker, then dies after IG
accepted but before the media_id was recorded.
*What happens:*
1. The committed attempt row is phase=container_publish, `outcome='sent'`, `external_id` NULL; item is
   `publishing` with an expiring lease.
2. Reaper sees post-send unknown → `status='needs_review'` + enqueues `reconcile`.
3. `reconcile()` uses the known `container_id` (from phase 1) to look up whether the media was published.
4. Published → write the single `receipts` row (`UNIQUE(queue_item_id)`) with `ig_media_id`+permalink,
   `status='published'`. Not published → `status='ready'` to retry `container_publish` on the SAME
   container (safe only because reconcile authoritatively found no post). IG unreachable during reconcile
   → remain `needs_review`; the publish is never blind-retried.
*Evidence:* `publish_attempts` shows container_publish `outcome='sent'` followed by a `reconcile` attempt
with outcome ok/error; a `receipts` row exists iff reconcile confirmed published; the item never has two
receipts (`UNIQUE`).

**Scenario 3 — duplicate publish work.**
*Scenario:* two workers (or a worker plus a retry) target the same ready item concurrently.
*What happens:*
1. Worker A runs `claim_publish()`: `UPDATE … SET status='publishing' WHERE id=X AND status='ready'` →
   1 row, commits.
2. Worker B runs the identical update → 0 rows (status now `publishing`); B exits without acting.
3. Even if B held an earlier `work_items` lease, the domain-level conditional update is the authority and
   voids B's claim.
4. A proceeds; `receipts UNIQUE(queue_item_id)` bounds receipts to one even under restart/replay.
*Evidence:* exactly one `container_publish` attempt_no=1 with outcome ok/unknown; exactly one `receipts`
row; `audit_log` records B's no-op (0-row) claim.

**Scenario 4 — media preparation failure.**
*Scenario:* a queued item in `preparing` fails ffmpeg (corrupt/oversized/incompatible) or times out.
*What happens:*
1. worker-media claims the `media_prep` work item (`FOR UPDATE SKIP LOCKED` + lease) and runs the ffmpeg
   subprocess with a 5-min per-job timeout.
2. Non-zero exit/timeout increments the work item's `attempts`. Transient and `attempts<2` → re-queue the
   SAME item (`status` stays `preparing`; no new queue item). `invalid_media` → `queue_items.status='failed'`
   with a useful reason; the user fixes and retries prep on the SAME item.
3. Because the queue item predates prep, a prep retry can never create a second queue item (dedup by
   construction).
*Evidence:* `work_items` shows the attempts count and final status; `queue_items.status='failed'` with
reason for invalid media, or `ready` after a successful retry; `media_prepared` absent until success.

**Scenario 5 — account revocation.**
*Scenario:* the IG token expires or the user revokes permission.
*What happens:*
1. Proactive refresh (before `expires_at`) fails, or a publish returns `error_class='permission_expired'`.
2. `ig_accounts.status` → `reauth_required`; the scheduler/worker checks account status before any claim, so
   publishing pauses while queued work stays intact (statuses and positions unchanged).
3. A high-priority notification deep-links to the recovery path (reconnect via IG OAuth).
4. On reconnect with verified ownership, `ig_accounts.status='active'` + a new `ig_tokens` row; the queue
   resumes from the clearly stated next slot.
*Evidence:* `ig_accounts.status` transitions in `audit_log`; `queue_items` untouched (positions intact); the
recovery notification present; the reconnect audit entry carries an actor.

**Scenario 6 — quota exhaustion.**
*Scenario:* an account reaches its daily allowance or an IG limit/cooldown.
*What happens:*
1. Before publishing, the worker checks `quota_usage(ig_account_id, today)` against
   `ig_accounts.daily_allowance` and the per-account token bucket (50 calls/5 min, refill 1/6 s).
2. At/over limit → the item is NOT published; `status='deferred'` (not failed) and a deferred re-queue is
   generated for the next eligible window (next day / post-cooldown).
3. `quota_usage` increments only in-transaction with a successful receipt, so deferred items never consume
   quota. An IG `rate_limited` response likewise defers with backoff, not permanent failure.
*Evidence:* `quota_usage` count == allowance; the deferred item present with `status='deferred'`; no receipt;
a next-window re-queue work item exists.

**Scenario 7 — schedule edits near a slot.**
*Scenario:* a user edits a schedule rule or timezone while slots are materialized and some are claimed.
*What happens:*
1. The scheduler regenerates the horizon idempotently: upsert keyed by `(ig_account_id, instant,
   rule_fingerprint)`; an edited rule yields a new fingerprint and new future slot rows.
2. Already-claimed slots (`claimed_by_queue_item_id` NOT NULL) are never deleted or moved; only unclaimed
   future slots regenerate, so a claimed slot's instant is immutable.
3. DST spring-forward: local times with no mapping instant produce no slot (no surprise). A timezone edit
   shifts future unclaimed instants only. Duplicated rules collapse via the `UNIQUE` to one slot.
4. A slot is claimed by at most one item (atomic claim) and an item maps to at most one slot → no double post.
*Evidence:* `schedule_slots` has no two rows sharing `(account, instant, fingerprint)`; claimed slots' instants
are unchanged across an edit; `audit_log` shows the regeneration with an actor; no item carries two slots.

---

**Scenario 8 — duplicate source collection.**
*Scenario:* concurrent collection jobs plus a manual refill target the same source posts for the same
account.
*What happens:*
1. Each candidate lands in `sourcing_backlog` via INSERT guarded by the partial
   `UNIQUE(ig_account_id, external_post_id)` over live statuses; concurrent inserts of the same post → one
   wins, the rest hit duplicate-key and are dropped.
2. Backlog→queue enqueue is an atomic status flip (`eligible`→`queued`) that sets `queue_item_id`; a second
   enqueue matches zero rows (status no longer `eligible`).
3. The same source post therefore yields at most one queue item under concurrent collect+refill.
*Evidence:* `sourcing_backlog` holds one live row per `(account, external_post_id)`; `queue_items` holds at
most one item per `external_post_id`; duplicate-key rejections are logged (`actor_kind='system'`).

**Scenario 9 — hung/timeout destructive call during cleanup.**
*Scenario:* a cleanup run's destructive archive/delete API call hangs at the destructive boundary.
*What happens:*
1. worker-cleanup advances `cleanup_run_items` one at a time; before the destructive call it commits the item
   as in-flight; the call carries a 30-s per-operation timeout.
2. Timeout with no response → `result='unknown'` (never auto-retried); the run flips to `paused_reconcile`.
3. `cleanup_runs_one_active_per_account` holds while status IN (confirmed, running, paused_reconcile), so no
   second same-account run starts; later runs order behind it.
4. Reconciliation queries IG for that item's actual state; confirmed → `result='ok'` + `result_detail`
   evidence; not confirmed → stays unknown until reconciled. The run resumes only after reconciliation and
   continues with the NEXT item; the uncertain item is never auto-repeated.
*Evidence:* `cleanup_run_items` shows `result='unknown'` then a reconcile-determined final result with
`result_detail`; `cleanup_runs.status` shows `paused_reconcile`; no second same-account run row is active;
`audit_log` shows the pause+resume with an actor.

**Scenario 10 — a changed cleanup selection.**
*Scenario:* between confirmation and execution the selection's underlying data changes (metrics refresh, a
post gets protected, an item crosses the age threshold).
*What happens:*
1. At confirmation, `cleanup_runs` stores `frozen_selection` + `selection_fingerprint` (hash of the exact item
   ids + metrics used).
2. Before execution the run recomputes the current fingerprint from fresh analytics + current protection +
   rule; if it differs from `selection_fingerprint`, the old confirmation is invalid → the run returns to
   `preview` requiring re-confirmation.
3. Execution always uses `frozen_selection` (the approved set), never a live re-query, so what ran is what was
   approved.
*Evidence:* a recomputed-fingerprint mismatch forces re-preview; `frozen_selection` equals the executed item
set; `audit_log` shows the invalidation with an actor.

**Scenario 11 — repeated or out-of-order billing events.**
*Scenario:* Stripe delivers webhooks duplicated and/or out of order.
*What happens:*
1. Each webhook INSERTs into `stripe_events` keyed by `UNIQUE(stripe_event_id)`; a duplicate → duplicate-key
   → no-op (idempotent).
2. The handler applies a forward-only conditional transition to `subscriptions` (e.g., incomplete→active only
   if current status is incomplete and the event's period is not older than current). Out-of-order older events
   fail the conditional (0 rows) and are ignored.
3. The result is one understandable subscription state regardless of delivery order or repeats.
*Evidence:* `stripe_events` holds one row per `stripe_event_id`; `subscriptions` shows a single coherent
status; `audit_log` records ignored duplicate/out-of-order events with no state change.

**Scenario 12 — storage failure.**
*Scenario:* object storage (R2) becomes unavailable or an object is missing.
*What happens:*
1. Media writes are idempotent by `content_hash`: an upload writes to a temp key then verifies by hash before
   the `media_objects` row commits; a failed mid-way upload leaves no partial object, and retry re-uploads the
   same hash (`UNIQUE(workspace_id, content_hash)` prevents duplicates).
2. A missing/failed original blocks prep (item stays `preparing`/`failed` with a reason) and never corrupts
   other items; publishing requires a present `prepared_key`, so no publish proceeds on absent media.
3. R2 versioning + `tbx-backup` allow restore; the durable relational state (queue, receipts) is independent of
   object availability, so a storage outage holds work rather than destroying it.
*Evidence:* a `media_objects` row exists iff bytes were hash-verified; the queue item is held
(`preparing`/`failed`) during the outage; no `receipts` row references an absent `prepared_key`; the restore
runbook recovers objects from versioning/backup.

**Scenario 13 — deletion interrupted halfway.**
*Scenario:* a deletion job crashes mid-sequence.
*What happens:*
1. `deletion_jobs.step_cursor` marks how far the ordered steps completed; each step is idempotent
   (revoke_ig no-op if already revoked; cancel_billing no-op if already canceled; delete_media idempotent by
   key).
2. On restart the job resumes from `step_cursor`, not zero, so completed steps are not repeated destructively
   and incomplete steps finish.
3. Only after all steps does it write `deletion_receipts` (`UNIQUE` per job) with non-identifying
   `retained_evidence`; personal data/media removed; a status reference remains.
*Evidence:* `deletion_jobs.step_cursor` advanced; `deletion_receipts` present exactly once; the IG token
revoked, the subscription canceled, the media keys removed; `retained_evidence` minimal and non-identifying.

**Scenario 14 — browser hang during restricted sourcing (additional).**
*Scenario:* a Playwright collection context hangs because an external site stalls.
*What happens:*
1. worker-sourcing runs collection in a capped context (3 concurrent) with a 30-s per-operation timeout and a
   10-min whole-job timeout.
2. A hung operation hits the 30-s timeout → Playwright aborts the action; the context is killed and its
   `storageState` (cookies/localStorage) re-saved so auth survives the kill.
3. The job's work item increments `attempts`; transient → re-queue with backoff (source `status='retrying'`);
   repeated failure → `status='blocked'`. No queue item is touched; publishing (worker-1, separate VM) is
   unaffected.
*Evidence:* `sourcing_sources.status` transitions active→retrying→blocked; `work_items.attempts` incremented;
no `queue_items` created from the hung job; publish-pool latency unaffected (separate VM/pool).

---

## 6. AI strategy

**Decision: no AI in v1.** Deterministic product behavior meets every launch-critical
requirement, and adding AI would inject nondeterminism exactly where the brief demands
determinism.

Why deterministic behavior meets the brief:
- Every launch-critical behavior is a finite state machine with frozen inputs and at-most-once
  external effects: queue lifecycle, two-phase publish + reconciliation, cleanup serialization,
  billing transitions, deletion steps. These are specified by constraints, transactions, and
  orderings (§3/§4). A stochastic model in this path would undermine the frozen-input and
  ambiguity-resolution rules, which are the product's reputation.
- Caption and media generation are explicit non-goals unless a compelling, budgeted,
  safety-conscious case is made. That case is not made for v1: the customer supplies the media
  and caption; the product prepares, brands, queues, publishes, and reports. Nothing in the five
  promises requires generation.
- The one place a model could help (drafting a caption from user-supplied notes) is a
  convenience, not a promise, and shipping it would add a privacy surface (context leaves the
  tenant boundary) and a quality-measurement burden the one-person team cannot carry at launch.

Scoped role if added later (out of v1, stated so the decision is complete): a user-initiated,
non-authoritative caption-drafting assist. Validation: output is a draft only; the user edits and
confirms, and the confirmed text is what freezes into `queue_item_snapshots`. Privacy: inference
receives only the requesting tenant's notes+context (no cross-tenant context), under a per-workspace
token budget. Fallback: deterministic — when the budget is reached or the call fails, the assist
returns no suggestion and the manual caption path is unchanged. Cost (illustrative, non-v1): ~1K in /
300 out at $0.15/$0.60 per 1M ≈ $0.0003/call; ≤5 drafts/item → ≤$0.0015/item; at a 1,000-item/day
worst case ≤$1.5/day ≈ $45/mo, capped by a per-workspace monthly token meter (e.g., $20) that disables
the assist when reached. This cost and surface are why it is deferred, not why it is impossible.

---

## 7. Testing and release confidence

**On every change (CI gate):** lint + typecheck; schema-migration check against a disposable Neon
branch; the full §4 invariant suite (`test_p1a`…`test_h15`) against that branch; property-based tests
over the status state machines (every transition guarded, no illegal edge) and over schedule-slot
generation against a timezone/DST corpus (IANA tzdata across spring/autumn boundaries). No merge
without a green invariant suite.

**Requires real supporting services (integration lane, not per-change):** real ffmpeg preparation
against fixture images/short-form video; real Playwright against a sandboxed sourcing target;
Stripe test-mode webhooks; an R2 test bucket. These run on a schedule and before release, not on
every commit.

**Nightly:** the full §4 invariant suite + every §5 failure drill against a Neon staging branch with
seeded multi-tenant data; a restore drill (restore one named workspace to a branch and diff against
source); a backup-integrity check (restore a random media object from versioning/`tbx-backup`); and a
telemetry sanity check (queue-depth alarms, publish-latency p95, cost-per-day within budget).

**Proven against safe test Instagram accounts (release gate), each a named drill:**
- **End-to-end:** connect→upload→prepare→queue→publish→receipt→analytics on a dedicated professional
  test account; assert a durable receipt with IG id+permalink.
- **Ambiguous-outcome:** force a post-send timeout; assert `needs_review`, then reconcile resolves to
  exactly one receipt (never two).
- **Race/duplicate:** N concurrent publish clicks + a mid-publish worker restart; assert one receipt.
- **Quota:** exhaust the daily allowance; assert deferral (not failure), `quota_usage` persistence
  across a worker restart, and next-day re-eligibility.
- **Timezone/DST:** edit the account timezone across a DST boundary and duplicate a rule; assert no
  double slot/claim and no surprise double post.
- **Cleanup:** archive one item; assert `result_detail` evidence, per-account serialization, and that a
  hung call pauses (`paused_reconcile`) with later runs ordering behind.
- **Revocation:** revoke the token; assert publishing pauses, queued work intact, and the recovery path
  restores publishing after verified reconnect.
- **Restore:** execute the rehearsed restoration runbook end-to-end and diff clean.

---

## 8. Delivery phases

Vertical slices, each closed on verifiable exit evidence (no dates, no durations). Phase 2 is the
first end-to-end run; every earlier phase is justified below.

**Phase 0 — Foundations.** Postgres schema+migrations, RLS isolation backstop, identity
(users/workspaces/members), invitation+waitlist gate, JWT+version revocation, `audit_log`, R2
idempotent-by-hash uploads, `work_items`+leases+reaper, outbox+SSE.
*Justified before end-to-end:* every later phase is tenant work and depends on isolation, the
invitation gate, and lease-safe work transport existing first.
*Exit:* invitation-gated signup creates a workspace and public signup cannot bypass the gate; a
suspended user's JWT is immediately invalidated; an upload is idempotent by hash; a work item
leases and is reclaimed after a simulated crash.

**Phase 1 — Connect + media + preparation.** IG OAuth connect (token sealed), connection_requests
review, `ig_accounts` lifecycle (pause/disconnect/reauth), `media_objects`+`prep_preferences`+
`media_prepared`, worker-media prep with retry-without-duplicate.
*Justified before end-to-end:* publishing consumes prepared media bound to a connected account, so
connect+prep must exist first.
*Exit:* a safe test account connects (token sealed, absent from logs); an image+video upload shows
prep progress and the original is retrievable; a prep failure retries on the SAME item without a
duplicate; disconnect preserves the queue.

**Phase 2 — Queue + schedule + publish + receipt (FIRST END-TO-END).** `queue_items`+snapshots,
`schedule_rules`+materialized slots, two-phase `claim_publish`, `publish_attempts`+`receipts`,
`quota_usage`, reconciliation, worker-publish.
*Exit:* connect→upload→prepare→queue→publish→receipt against a safe test account; double-click/restart
yields exactly one receipt; a post-send timeout → `needs_review` → reconcile → one receipt; quota
exhaustion defers and survives a restart.

**Phase 3 — Analytics.** `analytics_snapshots` append-only, worker-analytics polling, honest
comparisons.
*Exit:* scheduled+manual refresh appends snapshots (never overwrites); best/worst and trend derive
from history; stale/missing/incomparable measures are labeled, not fabricated.

**Phase 4 — Billing + entitlements.** `plans`, `subscriptions`, idempotent `stripe_events`
handler, `entitlement_grants`, `evaluate_entitlement` enforcement, billing pages.
*Exit:* purchase/upgrade/downgrade/cancel/payment-failure each yield one coherent subscription
state; repeated/out-of-order webhooks no-op; entitlements enforce server-side (hiding a button does
not grant access).

**Phase 5 — Restricted sourcing (entitled).** `sourcing_sources`+`sourcing_backlog`+pool,
worker-sourcing browser, verification, filters, dedup, refill/auto-depth, one-time sample.
*Exit:* an entitled workspace verifies+collects into the backlog with concurrency dedup; refill yields
≤1 queue item per post; revocation stops acquisition and preserves accepted work; the capability is
invisible and unreachable without the entitlement.

**Phase 6 — Managed cleanup (entitled).** `cleanup_rules`+`cleanup_runs`+`cleanup_run_items`,
per-account serialization, frozen selection, reconciliation, scheduled runs.
*Exit:* preview→confirm→run one-at-a-time with per-item evidence; a changed selection invalidates the
confirmation; a hung call pauses (`paused_reconcile`) with later runs ordering behind; a scheduled run
rechecks permissions/health/fresh analytics/protection/active-run before acting.

**Phase 7 — Notifications, feedback, account control, deletion.** notifications+fanout, feedback,
settings/leave/export/disconnect, resumable `deletion_jobs`+`deletion_receipts`.
*Exit:* notifications are live, deep-linked, with read/dismiss; deletion completes resumably with
minimal non-identifying retained evidence; export returns the customer's originals.

**Phase 8 — Operator/admin + backups/restore.** Admin area (stronger auth), waitlist/invites/
connections/users/suspensions/temporary grants/feedback/sourcing-pool management, health, work-item
inspection, attributable safe/privileged actions, daily backups + rehearsed restore.
*Exit:* the operator performs each safe action with attribution; the restore runbook restores a named
workspace and diffs clean; the health screen is backed by a proven restore, not a green light alone.

---

## 9. Security and privacy

- **Identity:** short-lived JWT (12 h) carrying `sub`/`workspace_id`/`user.version`; per-request
  validation checks signature+expiry+one indexed `users.version` read, so suspension/role change
  (version bump) invalidates existing tokens immediately. Public signup cannot bypass the
  invitation gate (single-use, expiring invitations; atomic `redeem()`).
- **Authorization:** roles (owner/admin/editor/viewer) plus capability checks enforced
  server-side on every gated route/worker; billing and destructive administration are distinct
  capabilities from day-to-day publishing. The operator admin area uses a separate admin JWT scope
  with TOTP MFA, stronger than an ordinary customer session.
- **Workspace isolation:** application scoping (primary) + Postgres Row-Level-Security backstop on
  every tenant table keyed to `app.workspace_id`; `workspace_id` carried on media-link resolution,
  outbox fanout, notifications, analytics, and operator-assisted recovery paths.
- **Secrets:** IG tokens sealed with libsodium `secretbox` under a data key held in env/KMS (out of
  the database); never logged, never returned to the browser; `audit_log`/outbox written via
  `redact()` which strips token/session fields.
- **Private media:** private R2 buckets; originals immutable and content-addressed; any outside
  access is a presigned GET URL scoped to one object key with a 15-min expiry (narrow + expiring).
- **Restricted sessions:** Playwright `storageState` persisted to a private R2 key readable only by
  worker-sourcing; confined to worker-2; never returned to the browser or written to receipts/logs.
- **Billing callbacks:** Stripe signature verified (raw body + signature header) before INSERT into
  `stripe_events` (`UNIQUE(stripe_event_id)` idempotency) and before any forward-only subscription
  transition.
- **Abuse controls:** per-account IG token bucket; per-user API rate limit (60 req/min); upload
  size/type/dimension validation; invitation gating; entitlement gating; content-hash dedup;
  sourcing candidate caps and check-interval floors.
- **Audit:** `audit_log` records every privileged, destructive, or state-changing action with
  `actor_kind`+`actor_user_id`; privileged actions are attributable to an actor.
- **Retention:** sessions 12 h; invitations default 14 d; analytics append-only and retained as
  customer history (removed on deletion); audit/feedback 24 mo then archived; deletion per below.
- **Export:** workspace export produces a durable archive (originals+queue+history+receipts) to R2
  with a scoped, expiring download link.
- **Deletion:** resumable `deletion_jobs` revokes external access, cancels billing, removes personal
  data+media, and writes `deletion_receipts` retaining only minimum non-identifying evidence.
- **Support access:** operator admin is separate and read-mostly; normal support uses work-item
  inspection + attributable safe actions, not direct production-data editing.

---

## 10. Risk register

The ten risks most likely to sink the product, an early warning signal, and the mitigation already
present in this plan.

| # | Risk | Early warning signal | Mitigation already in the plan |
|---|---|---|---|
| 1 | IG Graph API publishing capability/limits differ from assumed (endpoints, rate limits, token lifetimes) | connect/publish failures on the safe test account during Phase 2 | two-phase publish + reconciliation; per-account token bucket; exact field names confirmed at build time; ambiguous outcomes never blind-retried |
| 2 | Ambiguous publish outcomes mis-resolved into duplicate posts | reconcile false-negatives in the ambiguous-outcome drill | reconcile is authoritative before any retry; `receipts UNIQUE(queue_item_id)`; at-most-once via conditional update |
| 3 | Timezone/DST schedule edits cause surprise double posts | slot-generation property tests fail on the tz corpus | materialized immutable slots; claimed slots never moved; dedup by `(account,instant,fingerprint)` |
| 4 | One-person operation is a single point of failure | any VM/DB outage or missed backup | managed Postgres PITR+daily backups; systemd restart; rehearsed restore runbook; stateless compute (state in Postgres/R2) |
| 5 | Media storage/egress cost blowout | cost-per-day telemetry exceeds budget envelope | R2 zero-egress; content-hash dedup; prep only on demand; storage+cost alarms |
| 6 | Restricted sourcing attracts IG policy/abuse exposure | sourcing blocks/rate limits; IG ToC review | entitled-only; operator pool; candidate caps; verification; isolation in worker-2; reversible revocation; disclosed as high-risk |
| 7 | Cleanup destructive error (wrong item / wrong action) | cleanup drills show selection drift or serialization overlap | frozen selection+fingerprint; per-account serialization; per-item evidence; reconciliation; protect flags |
| 8 | Billing state corruption from webhook anomalies | subscription-state divergence in out-of-order drills | `stripe_events UNIQUE` idempotency + forward-only conditional transitions |
| 9 | Tenant isolation leak (cross-workspace data) | `test_h1_isolation_sweep` failures on any change | RLS backstop + application scoping; sweep runs on every change |
| 10 | Secret/session leakage (tokens, browser sessions) | byte-scan leak-test failures | libsodium sealing; `redact()` on logs/outbox; sessions confined to worker-2, never returned to browser |

---

## 11. Explicit tradeoffs

Every place this plan knowingly delivers less than the brief asks, and why the trade is acceptable
for v1.

- **Reel hard-delete (Recently Deleted) may be unavailable via the Graph API.** If Meta does not
  expose Reel deletion, v1 scopes Reel cleanup to archiving (reversible) and adjusts the UI's
  restore-window language accordingly. Acceptable because archiving still meets the
  underperformance-cleanup goal and is safer/reversible; the hard-delete path is added when/if the
  API supports it. Relatedly, cleanup executes via the Graph API (no customer browser session
  exists without the password), so the brief's "browser hang during cleanup" is realized as a hung
  destructive API call (§5 scenario 9); the genuine browser hang is covered for restricted sourcing
  (scenario 14).
- **Analytics freshness is capped by the poll schedule.** Between polls, change-over-time and
  best/worst figures are stale. v1 labels staleness/missing/incomparable measures rather than
  guaranteeing freshness. Acceptable because the brief permits analytics lag without blocking
  publishing and requires honesty about stale/missing measures, which is provided.
- **Live-update latency is capped by the SSE outbox poll interval** (sub-second to seconds), not
  zero-latency push. Acceptable because the brief requires "live without a page refresh," which is
  satisfied at this latency for status-flip event rates at launch scale.
- **Single-region launch.** No multi-region operation. Acceptable because the brief lists global
  multi-region as a v1 non-goal.
- **Scheduled cleanup has a check-then-act gap (TOCTOU).** A scheduled run rechecks preconditions but
  cannot guarantee IG-side state is unchanged at the instant of each destructive call. Acceptable
  because destructive actions remain serialized, attributable, evidence-retained, and
  reconciliation-contained, so the blast radius of a stale check is one item.

---

## 12. Where this is stronger than required

Deliberate improvements beyond the brief, with product value and cost.

- **Postgres Row-Level-Security backstop on every tenant table.** The brief requires isolation; RLS
  makes a forgotten application-scope return zero rows instead of leaking. Cost: migrations + a
  per-operation `SET app.workspace_id`. Value: isolation enforced structurally and proven by
  `test_h1_isolation_sweep` on every change.
- **Retained evidence for every final claim** (`receipts`, `cleanup_run_items.result_detail`,
  `stripe_events`, `deletion_receipts`) exceeds a minimal audit by making customer disputes and
  operator inspection provable. Cost: schema+storage. Value: directly serves promises 1–2 and hard
  rule H15.
- **Materialized immutable schedule slots** exceed a lazy scheduler by making timezone/DST edits
  inspectable and property-testable. Cost: slot table + horizon regeneration. Value: removes a whole
  class of surprise double-post bugs.
- **Content-addressed immutable media** (hash-deduped originals) exceeds a plain upload store by
  making originals tamper-evident and prep retries idempotent. Cost: hash compute on upload. Value:
  integrity + dedup + safe retry without duplicate queue items.
- **Resumable deletion with retained minimal evidence** exceeds a simple delete by making deletion
  interrupt-safe and provably complete. Cost: job+receipt schema. Value: §3.10 deletion integrity.

---

## 13. Assumptions

Every decision made where the product brief was silent.

- **Vendor choices** where the brief named only the capability: managed Postgres = Neon; object
  storage = Cloudflare R2; compute = Hetzner VPS; browser = Playwright/Chromium; media = ffmpeg+sharp;
  payments = Stripe; Instagram = Graph API.
- **Cleanup transport:** cleanup executes via the Graph API on the customer's OAuth token because no
  customer browser session exists without their password; consequently the brief's "browser hang
  during cleanup" maps to a hung destructive API call, and the browser is confined to restricted
  sourcing.
- **Plan limit values** (accounts/collaborators/posts-per-month/storage/features per plan, and the
  free-plan shape) are product-tunable configuration, not fixed here; representative values appear
  only inside cost derivations.
- **Representative unit prices** (Hetzner/R2/Neon/Stripe) are list-price estimates to confirm at
  procurement; the arithmetic structure and the budget envelope are the load-bearing content.
- **Default periods** chosen where the brief gave no number: invitation expiry 14 d; JWT lifetime
  12 h; audit/feedback retention 24 mo. All operator-tunable.
- **Starting rate limits** to tune against Meta's current published limits: per-account IG token
  bucket 50 calls/5 min (refill 1/6 s); per-user API limit 60 req/min.
- **Launch region** = the customers' single geographic region (brief: "one geographic region"),
  chosen at onboarding.
- **Rights-acceptance** is enforced at queue time as a versioned record (`attribution.version`,
  `accepted_by`); the rights-policy revision cadence is operator-managed.
