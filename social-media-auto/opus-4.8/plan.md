# ToolBox Poster — Engineering Plan

Founding-engineer execution plan for a queue-first Instagram operations studio. Written to
be executed as-is against the launch scale in the brief (≈1,000 registered users, ≈200 WAU,
several hundred connected accounts, hundreds–low-thousands of prepared/published items per
day, one region, ~few-hundred-dollars/month infrastructure, one technical founder + one
non-technical operator).

Conventions used throughout: all timestamps are `TIMESTAMPTZ` stored in UTC; all money is
integer minor units; all identifiers are UUIDv7 (time-ordered, generated app-side) unless
noted; "the outside side-effect boundary" means the moment we hand an irreversible request
to Instagram or Stripe.

---

## 1. Technology decisions

One choice per major part. Each names the strongest rejected alternative and what it would
cost here.

### 1.1 Language / runtime — **TypeScript on Node.js 22 LTS**

**Choice:** One language across web app, API, and workers. Strict `tsc`, no implicit `any`.

**Rejected:** Elixir/Phoenix + OTP. It is genuinely the best-fit runtime for this problem —
supervision trees, per-account processes, and back-pressure are native, and it would make
the "one publisher per account" isolation elegant. Rejected because a one-person team ships
and debugs faster in the language it already writes the React front end in; every context
switch between BEAM operational tooling and the JS front end is founder time not spent on
the invariants in §4. The concurrency guarantees Elixir gives for free, I buy explicitly in
Postgres (leases, `SKIP LOCKED`, conditional updates), which I need anyway because state
must survive process restarts — OTP process state does not.

**Why:** optimizing for founder throughput and a single mental model; durability of the
invariants lives in Postgres regardless of runtime, so the runtime should minimize human
cost.

### 1.2 Web framework — **Next.js 15 (App Router) for the customer app + admin app; a separate plain Node service for workers**

**Choice:** Next.js serves customer UI, the public marketing/legal pages, and JSON route
handlers under `/api`. The operator console is a **separate Next.js app on a separate
hostname** (`ops.` subdomain) sharing the same database and package workspace. Workers are a
standalone long-running Node process (no HTTP surface), deployed from the same monorepo.

**Rejected:** A single monolith that also runs the job workers in-process (e.g. Next.js +
`setInterval`/embedded worker). Rejected because publishing and media/browser work must not
share a process with request handling — a memory spike in ffmpeg or a hung Chromium context
must not take down the customer app (brief §3.6: "heavy media work and restricted browser
work must not starve the customer-facing app"). Separate OS processes give hard CPU/RAM
isolation the runtime alone cannot.

**Why:** process-level fault isolation between the request path and the side-effecting path
is a hard requirement, and Next gives the fastest path to accessible, responsive UI with
server components.

### 1.3 Datastore — **PostgreSQL 16 (single primary)**

**Choice:** One Postgres instance is the source of truth for all state, the job queue, and
the pub/sub bus (`LISTEN/NOTIFY`). Managed (see §1.11).

**Rejected:** Postgres for data + Redis for queue/cache/realtime. Redis is the conventional
choice and BullMQ is excellent. Rejected because a second datastore doubles the ways state
can diverge: a job enqueued in Redis but whose row didn't commit in Postgres (or vice-versa)
is exactly the "did it happen?" ambiguity the brief spends five promises trying to kill. At
this scale (peak a few thousand items/day ⇒ < 1 job/sec average, low-tens/sec in bursts),
Postgres `SELECT … FOR UPDATE SKIP LOCKED` handles the queue with room to spare, and
`LISTEN/NOTIFY` handles realtime. One datastore means one backup, one restore drill, one
transactional boundary that spans "record intent" + "enqueue work."

**Why:** transactional enqueue (intent and its job commit together or not at all) is the
backbone of exactly-once *effects*; that outranks the throughput headroom Redis would add
and that we do not need at this scale.

### 1.4 Job/queue system — **Graphile Worker (Postgres-backed) with custom lease columns for side-effecting jobs**

**Choice:** [`graphile-worker`](https://github.com/graphile/worker) for scheduling,
retries, and `SKIP LOCKED` dispatch of ordinary jobs (media prep, analytics fetch, emails).
It uses `SELECT … FOR UPDATE SKIP LOCKED` internally and supports `run_at`, `max_attempts`,
exponential backoff, and cron. For the **irreversible** jobs (publish, cleanup archive/
delete), the queue only *wakes* a handler; the actual single-flight guarantee is enforced by
my own lease + conditional state transition on the domain row (§4), not by the queue.

**Rejected:** pg-boss. Comparable and also Postgres-backed. Rejected narrowly: Graphile
Worker's job payload + `job_key` (dedupe key) and its `add_job(..., job_key, job_key_mode)`
give me idempotent enqueue and replace-in-place semantics I use for schedule materialization;
pg-boss's singleton keys are coarser. Either would work; I commit to one to avoid menus.

**Why:** the queue must share the publish transaction and never be a second source of truth
for "is this item being published"; Graphile Worker layers cleanly on the same connection
pool and I keep the authority in domain rows.

### 1.5 Object storage — **Cloudflare R2 (S3-compatible API)**

**Choice:** Originals and prepared renditions in R2. All access via short-lived presigned
URLs (`GetObject`/`PutObject`, `X-Amz-Expires`) minted per request after a tenant check.

**Rejected:** AWS S3. The reference S3-compatible product and slightly more mature. Rejected
on cost: customer media is served to browsers (previews, library) and to Instagram (which
fetches the media URL during publish). S3 egress is ~$0.09/GB; R2 egress is **$0**. With
media as the dominant cost (brief §2) and weeks of backlog per account, zero egress is the
difference between a predictable bill and a variable one. R2 keeps the same presign API
(`@aws-sdk/client-s3` works against R2).

**Why:** media egress is the single most volatile line item; R2 removes it while keeping the
S3 API, so tooling is unchanged.

### 1.6 Media processing — **ffmpeg (video) + libvips via `sharp` (images), run in the worker process with a concurrency cap**

**Choice:** `sharp` for image decode/resize/crop/metadata-strip/logo-compositing; `ffmpeg`
(invoked as a child process) for video transcode to Instagram-acceptable Reels specs.
Concurrency capped (§2 sizing) so media work cannot consume all cores.

**Rejected:** A managed transcoding service (AWS MediaConvert / Mux). Rejected on cost and
scope: at hundreds–low-thousands of items/day, per-minute managed transcode pricing and the
added external-failure surface aren't justified; ffmpeg on our own worker is a fixed cost
already paid. Named capability: `ffmpeg -i in.mp4 -c:v libx264 -profile:v high -pix_fmt
yuv420p -c:a aac -movflags +faststart out.mp4` produces the H.264/AAC/faststart MP4 the
Instagram Content Publishing API accepts.

**Why:** fixed, predictable cost and no new external dependency in the critical publish path.

### 1.7 Instagram integration — **Instagram Graph API via Facebook Login for Business (Instagram API with Instagram Login), Content Publishing API**

**Choice:** Connect professional (Business/Creator) accounts through Meta's OAuth. Publish
via the two-step Content Publishing API: `POST /{ig-user-id}/media` (create container,
returns a creation-id) then `POST /{ig-user-id}/media_publish` (publish container). Analytics
via `/{ig-media-id}/insights`. Cleanup: feed photo archive and Reel move-to-Recently-Deleted
use the Graph API media endpoints available to the app's granted permissions; where the API
cannot perform an action for a media kind, that kind is out of scope for automated cleanup
(disclosed in §11). Long-lived tokens (60-day) refreshed before expiry.

**Rejected:** Headless-browser automation of instagram.com for *publishing*. Rejected hard:
it violates platform policy, breaks constantly, and cannot give a durable creation-id/
permalink receipt. Browser automation is confined to the **restricted sourcing** capability
only (§3.5), which is operator-gated and disclosed as risk. Publishing and cleanup of our
own posts use the official API exclusively.

**Why:** the product's first promise is publishing only what was intended with inspectable
evidence; only the official API yields a durable media id + permalink receipt and a
supportable dispute trail.

### 1.8 Restricted-sourcing browser automation — **Playwright (Chromium), isolated worker pool, per-context storage state**

**Choice:** A **separate** worker deployment (`sourcing-worker`) runs Playwright Chromium
contexts to collect candidate Reels for entitled workspaces. Session persistence via
`browserContext.storageState({ path })` (persists cookies + localStorage; does **not**
persist DOM/JS heap — accounted for by re-navigating from a known URL each run). Hard caps
on concurrent contexts (§2). Sourcing sessions live only in this pool, never returned to the
browser or written to receipts (§4, §9).

**Rejected:** A third-party scraping API. Rejected on trust/isolation/legality: we cannot
place customer-facing legal exposure and account-session material inside an opaque third
party, and the brief demands we own the abuse/policy/isolation story (§3.5). Keeping it
in-house and physically separated from the publishing path is the containment.

**Why:** this capability is the highest-risk surface; it must be separable in process,
failure domain, and access — a shared or third-party design cannot guarantee that.

### 1.9 Payments — **Stripe (Checkout + Billing + Customer Portal), webhooks as the source of truth**

**Choice:** Stripe Checkout for card capture (PCI never touches us), Stripe Billing for
subscriptions, Customer Portal for self-serve changes. Our subscription state is a
projection updated **only** by verified, idempotent webhook processing (signature-verified,
event-id-deduplicated), reconciled by a nightly `GET` sweep.

**Rejected:** Paddle (merchant-of-record, handles sales tax). Genuinely attractive for a
solo founder avoiding tax registration. Rejected because Stripe's webhook/idempotency/portal
primitives are the ones the brief's billing invariants (single coherent state from
out-of-order events) map onto most directly, and single-region launch keeps tax manageable;
MoR is revisited in §11/§13 as a deliberate deferral.

**Why:** billing correctness under repeated/out-of-order events is a hard rule; Stripe's
event model + idempotency keys are the mechanism I build on.

### 1.10 Auth / sessions — **First-party email + password (Argon2id) and Sign-in-with-Google, opaque session tokens in a Postgres `sessions` table**

**Choice:** Own the identity layer. Passwords hashed with Argon2id (`argon2` lib,
`type=argon2id`, memory 64 MB, iterations 3, parallelism 1). Sessions are 256-bit random
opaque tokens stored hashed (SHA-256) in `sessions`, delivered as `Secure; HttpOnly;
SameSite=Lax` cookies. Operator console requires a separate session + mandatory TOTP 2FA and
a separate cookie domain.

**Rejected:** A hosted identity provider (Auth0/Clerk). Faster to start. Rejected because
identity is the tenancy root — every row's isolation traces to "which user, which
workspace" — and I will not put that boundary, plus the operator's elevated access model,
behind a vendor whose pricing steps up with MAU and whose data model I cannot add
workspace-scoped constraints to. Postgres sessions cost nothing extra and are already
backed up and restore-drilled.

**Why:** tenant isolation and operator privilege separation are core; owning sessions keeps
both inside the one datastore I already secure and restore.

### 1.11 Hosting / deployment — **Fly.io: separate apps (Machines) for web, worker, sourcing-worker; managed Postgres via Fly-attached provider; R2 external**

**Choice:** Each process class is its own Fly app scaled independently (web 2×shared-cpu-2×/
2 GB for HA; worker 1×dedicated-cpu-2×/4 GB; sourcing-worker 1×2 GB, scaled to zero when no
entitled workspace is active). Managed Postgres (**Supabase or Fly-managed Postgres with
automated daily backups + PITR**; committed choice: **Fly Managed Postgres** for
co-location, daily automated snapshot + WAL PITR). Region: single (`iad` or nearest to the
target region — chosen to match the customer region in §2, e.g. `fra` for EU). Deploys are
rolling; workers drain in-flight jobs on `SIGTERM`.

**Rejected:** AWS ECS/Fargate + RDS + ElastiCache. The "serious" option. Rejected on
operator count and budget: it needs an infra specialist to run safely, and its baseline
(NAT gateway, ALB, RDS Multi-AZ) alone eats the whole monthly budget before a single video
is transcoded. Fly gives per-process VMs, private networking, and rolling deploys with far
less operational surface for one founder.

**Why:** budget (§2, "a few hundred dollars/month") and one operator; Fly's per-app Machines
give the process isolation §1.2 needs without AWS's operational tax. Monthly cost derived in
§2.9.

### 1.12 Realtime updates — **Postgres `LISTEN/NOTIFY` fanned out to browsers over Server-Sent Events (SSE)**

**Choice:** Domain writes emit `NOTIFY toolbox_events, '<json>'` inside the committing
transaction (via trigger or app code); a small SSE endpoint per web instance holds one
`LISTEN` connection and pushes tenant-filtered events to subscribed browsers. Events carry
only `{workspace_id, entity, entity_id, kind}`; the browser refetches the authorized
resource (so no private payload rides the bus).

**Rejected:** A hosted realtime service (Pusher/Ably) or self-hosted WebSocket server.
Rejected on cost and isolation: SSE over HTTP/2 covers "status without refresh" (§3.4/§3.6/
§3.10) for ≤200 concurrent WAU trivially, adds no new vendor, and the notify-then-refetch
pattern means the realtime layer never carries private media links or tokens — the tenant
check happens on refetch through the normal authorized path.

**Why:** "feels live" is required; notify-then-refetch keeps tenant isolation on the one
authorized code path and adds zero new infrastructure.

### 1.13 Email — **Postmark (transactional) with a suppression-aware sender**

**Choice:** Postmark for invitations, recovery, failure alerts, billing notices. Message
stream separation (transactional vs broadcast). Rejected: self-hosted SMTP (deliverability
is a full-time job) and SES (worse deliverability defaults, more setup). Cheap at this
volume (<50k emails/mo). Recorded as minor; not a core invariant carrier.

**Why:** deliverability of recovery/failure emails matters (they are part of the "clear
recovery path" promise) and Postmark is the lowest-effort reliable option.

---

## 2. System architecture

### 2.1 Independently running parts

| Process | Responsibility | Scales on | Fault isolation guarantee |
|---|---|---|---|
| **web** (Next.js) | Customer UI, JSON API, SSE fanout, presign minting, Stripe/IG OAuth redirect handling, webhook *ingestion* (enqueue only) | concurrent users | A worker crash/OOM cannot touch it; it holds no long-running side-effect work |
| **worker** (Node) | Media prep, publishing, analytics fetch, cleanup, schedule materialization, billing-webhook *processing*, email send, reconciliation sweeps | job backlog | Runs in its own VM; ffmpeg/publish memory spikes stay here |
| **sourcing-worker** (Node + Playwright) | Restricted-source verification, collection, sample requests, media retrieval | entitled-workspace activity; scales to 0 | Chromium hangs/OOM contained to this VM; cannot starve publishing |
| **postgres** | All state, queue, pub/sub | data | Primary; daily snapshot + PITR |
| **R2** | Originals + renditions | media volume | External; presigned access only |

Communication is **only** through Postgres (job rows + domain rows + `NOTIFY`). No process
calls another over HTTP internally. This means every hand-off is transactional and
inspectable, and any process can restart without losing hand-offs.

### 2.2 How work moves — the publish path (the spine)

```
User "publish next"  ──▶  web: tx { verify tenant+entitlement+idempotency-key;
                                    insert publish_attempt(status=pending);
                                    add_job('publish', {attempt_id}, job_key=attempt_id) }  ──▶ commit
                                                    │
                                                    ▼
worker: publish handler ── acquire lease on queue_item (conditional UPDATE) ──▶
   create container (POST /media) ── record container_id + attempt=submitting ──▶
   publish container (POST /media_publish) ── record ig_media_id + permalink ──▶
   write receipt; queue_item.status=published; NOTIFY
```

The queue only *wakes* the handler. Single-flight is the conditional `UPDATE` on the domain
row (§4), so duplicate jobs, retries, and concurrent workers all collapse to one crossing of
the outside boundary.

### 2.3 Starvation control — separate queues and concurrency budgets

Jobs are tagged by **task queue name**; each worker process only pulls from queues it is
sized for. Graphile Worker supports per-task concurrency; I additionally partition by process
class so a saturated queue cannot borrow another's capacity:

| Queue | Runs on | Max concurrency | Rationale (derivation in §2.8) |
|---|---|---|---|
| `publish` | worker | 8 | I/O-bound (HTTP to IG); cheap; must never be blocked by media |
| `media_prep` | worker | 2 | CPU/RAM-bound ffmpeg; capped so it can't eat the box |
| `analytics` | worker | 4 | I/O-bound, low priority, deferrable |
| `email` | worker | 4 | I/O-bound |
| `billing_webhook` | worker | 4 | I/O-bound; must stay responsive |
| `reconcile` | worker | 2 | periodic |
| `cleanup` | worker | 1 **per account** (global 3) | destructive; serialized per account by lease |
| `sourcing_*` | sourcing-worker | 3 contexts total | separate VM entirely |

A single stuck `media_prep` job holds 1 of 2 slots; `publish` has its own 8 and is
untouched. `cleanup` is globally low and per-account serialized (§4). Sourcing is a different
VM, so even a full Chromium hang cannot consume a publish slot.

### 2.4 Media work isolation

`media_prep` transcodes to a temp path, then multipart-uploads the rendition to R2, then in
one transaction records the rendition row and (if this prep was for a queued item) flips the
item to `ready`. If the worker dies mid-transcode, the job's lease expires and it retries;
the temp file is orphaned and swept (§5.4). Because renditions are content-addressed
(`sha256` of bytes), a retry that re-produces identical output is a no-op upsert — no
duplicate rendition, no duplicate queue item.

### 2.5 Restricted sourcing isolation

`sourcing-worker` is deployed, secured, and scaled separately, reachable only by entitled
workspaces (checked at enqueue time in `web`, re-checked at execution). It writes candidates
into a `source_candidates` backlog; it **never** enqueues a `publish` job. Refill (backlog →
queue) is a distinct, deduplicated step (§4). Revoking the entitlement stops new
`sourcing_*` enqueues immediately and the running context finishes or times out; accepted
candidates and provenance remain (brief §3.5).

### 2.6 Destructive cleanup isolation

Cleanup runs on the shared `worker` but at global concurrency 3 and **one item at a time per
account**, gated by an `account_action_lease` (§4) that also gates publishing — so cleanup
and publishing on the *same* account never overlap at the destructive boundary, while
different accounts proceed independently.

### 2.7 Scheduling engine

A single `schedule_tick` cron job (every 60 s, via Graphile Worker cron) does **not**
publish. It only *materializes* due slots: for each active account, it computes the next due
run from the account's schedule rules in that account's timezone, and if a slot is due and
the account has capacity and a `ready` item, it inserts a `publish_attempt` and enqueues one
`publish` job — guarded so a given (account, slot-instant) can enqueue at most once (§4,
`publish_slot` unique index). This makes DST/duplicate-rule/timezone-edit safety a
uniqueness property, not a timing hope (§5.7).

### 2.8 Concurrency derivations

- **worker VM:** dedicated-cpu-2× = 2 vCPU, 4 GB. `media_prep` cap 2 because one `ffmpeg
  libx264` 1080p short clip uses ~1 vCPU and 300–500 MB; 2 concurrent ≈ full CPU with RAM
  headroom (4 GB − ~1 GB transcode − ~500 MB Node − ~500 MB OS ≈ 2 GB spare). `publish`
  concurrency 8 is I/O-bound (each is mostly awaiting IG HTTP), costing ~20 MB heap each ≈
  160 MB — trivial. These pools coexist on the same VM without contention because media is
  CPU-bound and publish is network-bound.
- **sourcing VM:** 2 GB ÷ ~600 MB per Chromium context ≈ 3 contexts with headroom. Cap = 3.
- **Throughput check:** peak "a few thousand items/day" with bursts. Say 3,000 published/day
  and a 3× burst around a common posting hour ⇒ ~ (3000/24)×3 ≈ 375 publishes in the peak
  hour ≈ 0.1/sec. With 8 publish slots and ~5 s median per two-call publish, capacity ≈ 1.6
  publishes/sec = ~5,700/hour. Headroom ≈ 15×. Media prep at 2 slots × (say 20 s/clip) = 6/
  min = 360/hour; if a burst uploads faster, items simply queue as `preparing` (visible,
  non-blocking) — acceptable and truthful.

### 2.9 Monthly cost (arithmetic)

| Component | Spec | Unit price | Monthly |
|---|---|---|---|
| Fly web | 2× shared-cpu-2×, 2 GB | ~$0.0000008/s ×2 | ~$62 |
| Fly worker | 1× dedicated-cpu-2×, 4 GB | dedicated | ~$62 |
| Fly sourcing-worker | 1× 2 GB, scale-to-zero (avg 4 h/day active) | prorated | ~$8 |
| Fly Managed Postgres | 2 vCPU / 4 GB / 40 GB + PITR | managed | ~$100 |
| R2 storage | see storage model below: ~1.2 TB steady | $0.015/GB-mo | ~$18 |
| R2 egress | 0 | $0 | $0 |
| R2 Class-A/B ops | ~5M ops/mo | ~$4.50/M A, $0.36/M B | ~$8 |
| Postmark | <50k emails | $15 plan | ~$15 |
| Stripe | usage-based; no fixed | 2.9%+30¢ per charge | pass-through |
| Sentry (errors) | team | $26 | ~$26 |
| Domain/misc | | | ~$5 |
| **Total** | | | **≈ $302/mo** |

Storage model: ≈300 connected accounts × ~50 items backlog × (original ~15 MB video +
rendition ~8 MB + image cases lower) ≈ 300×50×23 MB ≈ 345 GB active; plus published-library
retention growth over the year toward ~1.2 TB. At $0.015/GB-mo that is the ~$18 line, and R2
zero-egress is why serving previews/library/IG-fetch doesn't add cost. This lands inside
"a few hundred dollars/month." If storage exceeds plan (see §4 quota), uploads are held with
a clear message, not silently dropped.

---

## 3. Data model (DDL)

PostgreSQL 16. Abbreviated to the invariant-bearing tables; conventional columns
(`created_at`, `updated_at`) present throughout. `updated_at` maintained by trigger.

```sql
-- ============ Identity & tenancy ============
CREATE TABLE users (
    id            UUID PRIMARY KEY,
    email         CITEXT NOT NULL UNIQUE,
    password_hash TEXT,                        -- NULL for Google-only
    google_sub    TEXT UNIQUE,
    status        TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','suspended')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE workspaces (
    id            UUID PRIMARY KEY,
    name          TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','suspended','deleting')),
    stripe_customer_id TEXT UNIQUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE memberships (
    workspace_id  UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role          TEXT NOT NULL CHECK (role IN ('owner','admin','publisher','viewer')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, user_id)
);
-- role semantics: owner=billing+destructive admin; admin=destructive admin, no billing;
-- publisher=day-to-day publishing; viewer=read-only. (brief §3.1: billing & destructive
-- admin must be distinguishable from day-to-day publishing.)

CREATE TABLE sessions (
    id            UUID PRIMARY KEY,
    user_id       UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_sha256  BYTEA NOT NULL UNIQUE,       -- store hash, never the token
    is_operator   BOOLEAN NOT NULL DEFAULT FALSE,
    expires_at    TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Invitations (single-use, expiring; gate public signup)
CREATE TABLE invitations (
    id            UUID PRIMARY KEY,
    email         CITEXT NOT NULL,
    workspace_id  UUID REFERENCES workspaces(id) ON DELETE CASCADE, -- NULL = new workspace
    role          TEXT NOT NULL CHECK (role IN ('owner','admin','publisher','viewer')),
    token_sha256  BYTEA NOT NULL UNIQUE,
    expires_at    TIMESTAMPTZ NOT NULL,
    consumed_at   TIMESTAMPTZ,                 -- NULL until used; single-use
    created_by    UUID REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX invitations_one_live_per_email
    ON invitations (email) WHERE consumed_at IS NULL;

CREATE TABLE waitlist (
    id UUID PRIMARY KEY, email CITEXT NOT NULL UNIQUE,
    context JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============ Instagram accounts ============
CREATE TABLE ig_accounts (
    id              UUID PRIMARY KEY,
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ig_user_id      TEXT NOT NULL,             -- Instagram's stable id
    username        TEXT NOT NULL,
    profile_img_url TEXT,
    timezone        TEXT NOT NULL DEFAULT 'UTC',   -- IANA tz for schedule math
    daily_cap       INT  NOT NULL DEFAULT 25 CHECK (daily_cap BETWEEN 0 AND 50),
    status          TEXT NOT NULL DEFAULT 'connected'
                      CHECK (status IN ('connected','paused','needs_reauth','disconnected')),
    cooldown_until  TIMESTAMPTZ,               -- set when IG signals a limit
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- HARD RULE: one IG account cannot belong to two workspaces (brief §3.2).
CREATE UNIQUE INDEX ig_accounts_global_unique
    ON ig_accounts (ig_user_id)
    WHERE status <> 'disconnected';
-- (disconnected rows kept for history/reconnect; only one live claim at a time.)

-- OAuth tokens are private grants — separate table, encrypted, never joined into API reads
CREATE TABLE ig_tokens (
    ig_account_id   UUID PRIMARY KEY REFERENCES ig_accounts(id) ON DELETE CASCADE,
    ciphertext      BYTEA NOT NULL,            -- AEAD-encrypted long-lived token (§9)
    nonce           BYTEA NOT NULL,
    key_version     INT NOT NULL,
    expires_at      TIMESTAMPTZ NOT NULL,      -- refresh before this
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE account_requests (            -- operator review before connect (brief §3.2)
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    requested_handle TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','approved','declined','invited')),
    operator_note TEXT,                        -- internal, never shown to requester
    public_reason TEXT,                        -- shown to requester
    decided_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============ Media ============
CREATE TABLE media_assets (               -- ORIGINAL uploads (immutable, retrievable)
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    r2_key        TEXT NOT NULL,
    sha256        BYTEA NOT NULL,
    bytes         BIGINT NOT NULL,
    mime          TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('image','video')),
    width INT, height INT, duration_ms INT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX media_assets_dedupe
    ON media_assets (workspace_id, sha256);    -- same upload not stored twice per workspace

CREATE TABLE media_renditions (           -- PREPARED, publish-ready derivatives
    id            UUID PRIMARY KEY,
    asset_id      UUID NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
    r2_key        TEXT NOT NULL,
    sha256        BYTEA NOT NULL,
    recipe_hash   BYTEA NOT NULL,              -- hash of prep params (Reels fit, logo, etc.)
    kind          TEXT NOT NULL CHECK (kind IN ('feed_image','reel_video')),
    bytes         BIGINT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX media_renditions_idem
    ON media_renditions (asset_id, recipe_hash); -- deterministic prep = no dup rendition

-- ============ Queue ============
CREATE TABLE queue_items (
    id            UUID PRIMARY KEY,
    workspace_id  UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ig_account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    -- FROZEN action inputs (brief §3.3/§4): captured at queue time, never mutated after
    asset_id      UUID NOT NULL REFERENCES media_assets(id),
    rendition_id  UUID REFERENCES media_renditions(id),  -- set when prep completes
    frozen_caption TEXT NOT NULL,
    frozen_recipe  JSONB NOT NULL,             -- prep choices used for this publish
    frozen_attribution JSONB,                  -- source credit/link when relevant
    origin        TEXT NOT NULL CHECK (origin IN ('upload','source')),
    source_candidate_id UUID REFERENCES source_candidates(id),
    -- Queue position & lifecycle
    position      NUMERIC NOT NULL,            -- fractional ordering for cheap reorder
    status        TEXT NOT NULL DEFAULT 'preparing'
       CHECK (status IN ('preparing','ready','publishing','published',
                         'hidden','failed','needs_review')),
    rights_ack_id UUID REFERENCES rights_acks(id),  -- attributable, versioned consent
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX queue_items_account_order
    ON queue_items (ig_account_id, position)
    WHERE status IN ('ready','preparing');
-- Prevent the SAME source post being queued twice into the SAME account (brief §4):
CREATE UNIQUE INDEX queue_items_no_dup_source
    ON queue_items (ig_account_id, source_candidate_id)
    WHERE source_candidate_id IS NOT NULL AND status <> 'hidden';

CREATE TABLE rights_acks (                -- content-rights acceptance: attributable+versioned
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id),
    user_id UUID NOT NULL REFERENCES users(id),
    terms_version TEXT NOT NULL,
    acked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============ Publishing: attempts, leases, receipts ============
-- One row per intent to cross the outside boundary. THE dedup + single-flight anchor.
CREATE TABLE publish_attempts (
    id             UUID PRIMARY KEY,
    queue_item_id  UUID NOT NULL REFERENCES queue_items(id) ON DELETE CASCADE,
    ig_account_id  UUID NOT NULL REFERENCES ig_accounts(id),
    idempotency_key TEXT NOT NULL,             -- from client button OR schedule slot id
    status         TEXT NOT NULL DEFAULT 'pending'
       CHECK (status IN ('pending','creating','submitting','submitted',
                        'failed_pre','failed_ambiguous','canceled')),
    ig_creation_id TEXT,                        -- container id (pre-publish)
    ig_media_id    TEXT,                        -- final media id (post-publish)
    lease_owner    UUID,                        -- worker instance id holding the lease
    lease_expires_at TIMESTAMPTZ,
    attempt_count  INT NOT NULL DEFAULT 0,
    last_error     JSONB,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Idempotent intent: a repeated click / repeated slot enqueue = same row, not a new publish
CREATE UNIQUE INDEX publish_attempts_idem
    ON publish_attempts (queue_item_id, idempotency_key);
-- Only ONE active attempt may exist per queue item at once (brief §4):
CREATE UNIQUE INDEX publish_attempts_one_active
    ON publish_attempts (queue_item_id)
    WHERE status IN ('pending','creating','submitting');
-- Schedule slot guard: a given account+slot-instant enqueues at most one attempt (§2.7/§5.7)
CREATE TABLE publish_slots (
    ig_account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    slot_instant  TIMESTAMPTZ NOT NULL,        -- the UTC instant the local rule resolved to
    attempt_id    UUID REFERENCES publish_attempts(id),
    PRIMARY KEY (ig_account_id, slot_instant)
);

CREATE TABLE receipts (                   -- durable proof of a final claim (brief §3.6/§4)
    id            UUID PRIMARY KEY,
    publish_attempt_id UUID NOT NULL UNIQUE REFERENCES publish_attempts(id),
    ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
    workspace_id  UUID NOT NULL REFERENCES workspaces(id),
    ig_media_id   TEXT NOT NULL,
    permalink     TEXT NOT NULL,
    frozen_caption TEXT NOT NULL,
    rendition_sha256 BYTEA NOT NULL,
    published_at  TIMESTAMPTZ NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Serializes destructive actions AND publishing per account at the boundary (brief §4)
CREATE TABLE account_action_leases (
    ig_account_id UUID PRIMARY KEY REFERENCES ig_accounts(id) ON DELETE CASCADE,
    holder        UUID NOT NULL,               -- attempt_id or cleanup_run_id
    kind          TEXT NOT NULL CHECK (kind IN ('publish','cleanup')),
    expires_at    TIMESTAMPTZ NOT NULL
);

-- Tracks daily usage durably so a restart cannot forget it (brief §4)
CREATE TABLE daily_usage (
    ig_account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    usage_date    DATE NOT NULL,               -- account-local date
    published_count INT NOT NULL DEFAULT 0,
    PRIMARY KEY (ig_account_id, usage_date)
);

-- ============ Scheduling rules ============
CREATE TABLE schedule_rules (
    id            UUID PRIMARY KEY,
    ig_account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('fixed','interval')),
    days_of_week  INT[] ,                       -- fixed: e.g. {1,3,5}
    times_local   TIME[],                       -- fixed: local times
    window_start  TIME, window_end TIME, interval_minutes INT,  -- interval
    active        BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============ Restricted sourcing ============
CREATE TABLE sources (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ig_account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('account','hashtag','reels_feed')),
    query TEXT NOT NULL,
    filters JSONB NOT NULL,                     -- type/age/min-likes/exclude-words/etc.
    check_interval_minutes INT NOT NULL DEFAULT 360,
    target_depth INT,                           -- auto-refill target (NULL=manual only)
    status TEXT NOT NULL DEFAULT 'pending_verification'
      CHECK (status IN ('pending_verification','active','paused','retrying','blocked')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE source_candidates (
    id UUID PRIMARY KEY,
    source_id UUID NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id),
    external_media_id TEXT NOT NULL,            -- IG id of the discovered post
    original_link TEXT NOT NULL, author TEXT, caption TEXT,
    media_type TEXT, observed_likes INT, observed_comments INT, observed_plays INT,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    state TEXT NOT NULL DEFAULT 'held'
      CHECK (state IN ('held','eligible','queued','rejected')),
    r2_key TEXT                                 -- retrieved media, when fetched
);
-- Never import/queue the same source post repeatedly (brief §3.5/§4):
CREATE UNIQUE INDEX source_candidates_dedupe
    ON source_candidates (source_id, external_media_id);

-- ============ Analytics (append-only snapshots, never overwritten) ============
CREATE TABLE analytics_snapshots (
    id UUID PRIMARY KEY,
    receipt_id UUID NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reach INT, views INT, likes INT, comments INT, saves INT, shares INT,
    raw JSONB NOT NULL                          -- exactly what IG returned
);
CREATE INDEX analytics_by_receipt_time ON analytics_snapshots (receipt_id, captured_at DESC);

-- ============ Managed cleanup ============
CREATE TABLE cleanup_rules (
    id UUID PRIMARY KEY,
    ig_account_id UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    criteria JSONB NOT NULL,                    -- media kind, age, min metrics
    schedule TEXT,                              -- NULL=manual only; else daily/weekly local
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE cleanup_runs (
    id UUID PRIMARY KEY,
    ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
    frozen_rule JSONB NOT NULL,                 -- rule snapshot at confirm time
    selection_hash BYTEA NOT NULL,              -- hash of exact confirmed item set
    requested_by UUID NOT NULL REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'confirmed'
      CHECK (status IN ('confirmed','running','paused_reconcile','done','stopped')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE cleanup_items (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES cleanup_runs(id) ON DELETE CASCADE,
    receipt_id UUID NOT NULL REFERENCES receipts(id),
    action TEXT NOT NULL CHECK (action IN ('archive','recently_deleted')),
    status TEXT NOT NULL DEFAULT 'pending'
      CHECK (status IN ('pending','submitting','done','ambiguous','skipped')),
    result JSONB,                               -- redacted evidence, never session material
    seq INT NOT NULL                            -- one-at-a-time ordering within the run
);
CREATE TABLE protected_posts (            -- never selected by cleanup (brief §3.8)
    receipt_id UUID PRIMARY KEY REFERENCES receipts(id) ON DELETE CASCADE,
    protected_by UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============ Billing projection (webhook-sourced) ============
CREATE TABLE subscriptions (
    workspace_id UUID PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    stripe_subscription_id TEXT UNIQUE,
    plan TEXT NOT NULL,                         -- free/starter/studio
    interval TEXT CHECK (interval IN ('month','year')),
    status TEXT NOT NULL,                       -- active/past_due/canceled/...
    seats INT NOT NULL DEFAULT 1,
    account_allowance INT NOT NULL DEFAULT 1,
    current_period_end TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE stripe_events (              -- idempotent, ordered webhook ingestion
    event_id TEXT PRIMARY KEY,                  -- Stripe's evt_… id → exactly-once processing
    type TEXT NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);
-- Temporary operator-granted entitlements (beta access) beyond plan (brief §3.9)
CREATE TABLE entitlement_grants (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    feature TEXT NOT NULL CHECK (feature IN ('restricted_sourcing','managed_cleanup')),
    granted_by UUID NOT NULL REFERENCES users(id),
    expires_at TIMESTAMPTZ,                     -- NULL = until revoked
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX entitlement_one_live
    ON entitlement_grants (workspace_id, feature) WHERE revoked_at IS NULL;

-- ============ Audit, notifications, deletion, feedback ============
CREATE TABLE audit_log (                  -- privileged/attributable actions (brief §3.11/§4)
    id UUID PRIMARY KEY,
    actor_user_id UUID REFERENCES users(id),
    actor_is_operator BOOLEAN NOT NULL DEFAULT FALSE,
    workspace_id UUID,
    action TEXT NOT NULL,
    target JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE notifications (
    id UUID PRIMARY KEY,
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id),          -- NULL = whole workspace
    kind TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal'
       CHECK (priority IN ('normal','high')),
    body JSONB NOT NULL, deep_link TEXT,
    read_at TIMESTAMPTZ, dismissed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE deletion_requests (
    id UUID PRIMARY KEY,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user','workspace')),
    subject_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested'
      CHECK (status IN ('requested','revoking','purging','done')),
    checklist JSONB NOT NULL DEFAULT '{}',      -- per-step completion, resumable
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TABLE feedback (
    id UUID PRIMARY KEY, workspace_id UUID REFERENCES workspaces(id),
    user_id UUID REFERENCES users(id), kind TEXT NOT NULL, body TEXT NOT NULL,
    page_context JSONB, created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

Constraints that cannot live purely in DDL, and the function that enforces each:

- **Frozen inputs immutability:** `queue_items.frozen_*` columns are only written by
  `enqueueItem()` and never by any settings-update path; enforced by a `BEFORE UPDATE`
  trigger `queue_items_freeze_guard()` that raises if any `frozen_*` column changes while
  `status <> 'preparing'`. Test in §4.
- **Daily cap:** enforced in `acquirePublishLease()` (§4) by comparing `daily_usage` under
  the same transaction that increments it.
- **Tenant scoping on every read:** enforced by a repository layer where every query takes a
  `workspace_id` from the authenticated session; plus Postgres **Row-Level Security** as a
  backstop (see §9).

---

## 4. Invariant enforcement map

Every promise (brief §1) and hard rule (brief §4). Mechanism is structural; evidence is a
specific test I will write and run.

| # | Invariant | Mechanism | Evidence it works (named test) |
|---|---|---|---|
| I1 | Publish only what was intended (right workspace, account, media, caption) | `queue_items.frozen_*` captured at enqueue; publish handler reads *only* frozen columns + `rendition_id`; `receipts` copies frozen values | `test_publish_uses_frozen_caption`: enqueue item, change account default caption/recipe, publish, assert `receipts.frozen_caption` == original and `rendition_sha256` == frozen rendition |
| I2 | Never publish twice (double-click, retry, concurrent workers, restart, deploy, repeated callback) | `publish_attempts_idem` (unique `queue_item_id,idempotency_key`) collapses repeats to one row; `publish_attempts_one_active` partial unique blocks a second live attempt; `account_action_leases` PK gives single-flight; `media_publish` sent at most once per attempt guarded by conditional `UPDATE status pending→submitting` | `test_double_publish_one_receipt`: fire 2 concurrent publish requests same key → exactly 1 `receipts` row, 1 IG `media_publish` call (mock asserts call count == 1) |
| I3 | Queue tells the truth (order, time, progress, failure, pause, limits visible; no silent disappear/jump) | `queue_items.status` enum + `position`; `ig_account_id` FK immutable after enqueue (trigger); SSE `NOTIFY` on every status write | `test_status_transitions_are_observable`: drive an item through each status, assert an event emitted per transition and account_id never changes |
| I4 | Uncertainty ≠ second destructive action | after `media_publish` sent, a timeout sets `status='submitted'`→ reconcile, never auto-retry; `publish_attempts.status='failed_ambiguous'` blocks re-publish (`one_active` index won't allow new active attempt without reconcile) | `test_ambiguous_no_auto_retry`: mock IG timeout *after* publish call; assert 0 further publish calls and item → `needs_review` |
| I5 | Holding/leaving never destroys work | pause/disconnect/suspend/over-plan set account/workspace `status` but never delete `queue_items`; no code path deletes queue rows except explicit `DELETE` endpoint | `test_pause_preserves_queue`: pause account, assert all queue rows present & re-publishable on resume |
| I6 | Private access stays private | `ig_tokens`/sourcing sessions in separate AEAD-encrypted tables; never selected by API serializers; RLS denies cross-workspace; logs scrub via allowlist serializer | `test_token_never_in_api_or_log`: snapshot every API response + captured logs for a publish flow, assert no token/cookie substring |
| I7 | One active publication crosses the boundary per item | `account_action_leases` PK (one row per account) + `publish_attempts_one_active` | `test_lease_serializes_publish`: 2 workers race same account; assert one gets lease, other's conditional update affects 0 rows |
| I8 | One cleanup item per account crosses destructive boundary at a time | `account_action_leases(kind='cleanup')` PK; `cleanup_items.seq` processed strictly in order; cleanup and publish share the same lease table row so they mutually exclude per account | `test_cleanup_serial_and_excludes_publish`: start cleanup, attempt publish same account → publish defers; assert never concurrent |
| I9 | Known pre-action failures retry; ambiguous post-action failures don't | attempt `status` split `failed_pre` (retryable, job re-enqueued) vs `failed_ambiguous` (terminal until reconcile); the split is set by *where* the error occurred relative to the `media_publish` call | `test_pre_vs_post_failure`: inject failure before create → auto-retry succeeds; inject after publish → no retry, needs_review |
| I10 | Approved inputs frozen for the approved action | `queue_items_freeze_guard()` trigger; `cleanup_runs.selection_hash`/`frozen_rule`; `publish_attempts` bound to a specific `rendition_id` | `test_settings_change_no_effect_on_queued`: change recipe after queueing, assert queued item's rendition unchanged |
| I11 | Work held (not destroyed) when paused/disconnected/suspended/over-plan/over-quota/awaiting reauth | publish handler's first step re-checks account+workspace+entitlement+quota; on fail it sets `run_at`= later and returns without side effect | `test_over_quota_defers`: exhaust `daily_usage`, enqueue publish, assert attempt deferred not failed, item stays `ready` |
| I12 | Daily use survives restart/cache loss | `daily_usage` is a durable table incremented in the *same tx* that records the receipt | `test_usage_survives_restart`: publish, kill worker, restart, assert count intact and cap still enforced |
| I13 | Source & upload content not duplicated into same account queue | `source_candidates_dedupe` (unique source_id+external_media_id); `queue_items_no_dup_source`; `media_assets_dedupe` (workspace+sha256) | `test_concurrent_refill_no_dup`: 2 refills race same candidate → 1 queue_item (unique index rejects 2nd) |
| I14 | Restricted features invisible & unreachable without entitlement | `entitlement_grants` checked at enqueue *and* execution; sourcing-worker checks before every context; UI hides but server authorizes | `test_no_entitlement_403`: call sourcing/cleanup API without grant → 403 and 0 jobs enqueued (not just hidden button) |
| I15 | Customer media private by default; outside access narrow & expiring | R2 objects private; access only via presigned URL, `X-Amz-Expires=300` (5 min), minted after tenant check | `test_presign_scoped_and_expiring`: presign for wsA media, assert wsB session can't mint it; assert URL 403s after 5 min |
| I16 | Private grants/sessions never returned to browser, in receipts, or ordinary logs | serializer allowlist; `receipts` has no token column; log middleware redacts `Authorization`, `Set-Cookie`, `storageState` | `test_receipt_and_log_scrub` (shared with I6) asserts absence |
| I17 | Content-rights acceptance attributable & versioned | `rights_acks` (user_id + terms_version); `queue_items.rights_ack_id` NOT NULL required before `ready` | `test_no_publish_without_rights_ack`: enqueue without ack → blocked at `preparing`; with ack → proceeds |
| I18 | External callbacks/billing/deletion idempotent & order-independent | `stripe_events.event_id` PK (process once); state derived from `current_period_end`/`status` fields not event order; deletion `checklist` resumable | `test_stripe_out_of_order`: deliver `updated` then `created` then duplicate `updated`; assert one coherent subscription row |
| I19 | Every final claim has inspectable evidence | `receipts` (publish), `cleanup_items.result` (cleanup), `analytics_snapshots.raw`, `audit_log` (charged/deleted), `deletion_requests.checklist` | `test_every_claim_has_evidence`: for each terminal action assert a corresponding evidence row exists with IG id/permalink/timestamp |
| I20 | Tenant isolation on every surface incl. media links, realtime, notifications, analytics, operator recovery | RLS policies keyed on `workspace_id`; SSE events carry only ids + refetch through authorized path; operator actions logged & scoped | `test_rls_blocks_cross_tenant`: with wsB session, attempt to read/notify/presign every wsA entity type → all denied |
| I21 | Cancel only while safely cancelable | cancel allowed while attempt `status IN ('pending','creating')`; once `submitting`, cancel endpoint returns "point of no return" | `test_cancel_boundary`: cancel at `creating` works; cancel at `submitting` refused with correct message |
| I22 | Schedule edits don't cause surprise double posts (DST, dup rules, skipped local times, tz edit) | `publish_slots` PK (account, slot_instant-in-UTC); materializer resolves local rule→UTC instant once; duplicate/edited rules resolving to same instant hit the PK and enqueue once | `test_dst_no_double`: configure rule spanning DST fall-back; assert each wall-clock slot → one UTC instant → one attempt |

---

## 5. Failure-mode walkthrough

Format: Scenario → numbered What happens (each step names a mechanism) → Evidence.

### 5.1 Crash **before** publish

**Scenario:** Worker dies after acquiring the lease and creating the IG container, before
calling `media_publish`.
1. TCP to Postgres drops; the in-flight transaction (if any open) rolls back. The
   `publish_attempts` row is at `status='creating'` with `ig_creation_id` set (committed
   earlier in a short tx).
2. `account_action_leases` row's `expires_at` (set to now+120 s at acquisition) passes.
3. `reconcile` cron (every 60 s) finds attempts in `creating` past lease expiry and
   re-enqueues one `publish` job.
4. The retried handler sees `status='creating'` + existing `ig_creation_id`, so it does **not**
   create a new container; it resumes at `media_publish` (the create step is idempotent to
   us because we reuse the stored creation-id).
5. Publish succeeds; `receipts` written; `daily_usage` incremented; `status='submitted'`;
   item `published`.
**Evidence:** `publish_attempts` shows one row, `attempt_count=2`, single `ig_creation_id`,
single `ig_media_id`; `receipts` has exactly one row; `audit_log`/job history shows the retry.

### 5.2 Crash **after** publish may have been accepted

**Scenario:** Worker sends `media_publish`, IG receives it, but the worker dies before
reading the response.
1. Attempt is at `status='submitting'` (flipped `creating→submitting` in the tx *before* the
   HTTP call). No `ig_media_id` recorded.
2. Lease expires; `reconcile` picks it up but sees `status='submitting'` — the ambiguous
   zone. It does **not** re-call `media_publish` (I4/I9).
3. Reconciler calls the **read-only** recovery: `GET /{ig-user-id}/media?since=…` (or the
   container status endpoint) to find whether a media with our creation-id / matching
   caption+timestamp exists.
4a. If found: record `ig_media_id`+permalink, write receipt, `status='submitted'`, item
   `published`. No second publish occurred.
4b. If not found after N=3 read attempts over 10 min: `status='failed_ambiguous'`, item →
   `needs_review`; user notified with a "we're confirming this post" message. It cannot be
   republished until an operator/user reconciles (the `one_active` index blocks a new active
   attempt).
**Evidence:** `publish_attempts.last_error` records the timeout and the read-recovery result;
`notifications` shows the needs-review item; if 4a, `receipts` proves the single publish.

### 5.3 Duplicate publish work (two jobs, double click, redeploy replays queue)

**Scenario:** A double-click plus a stuck-job retry produce three `publish` jobs for one item.
1. All three carry the same `idempotency_key`; the first `INSERT … ON CONFLICT` on
   `publish_attempts_idem` creates one attempt; the others select the existing row.
2. Only one worker wins `account_action_leases` (PK insert); the others' lease insert
   conflicts and they exit.
3. The winner flips `pending→submitting` via `UPDATE … WHERE status='pending'`; the losers'
   same update matches 0 rows.
4. One `media_publish` call. One receipt.
**Evidence:** IG mock/records show `media_publish` called once; `receipts` count = 1;
`publish_attempts` count = 1 with `attempt_count` reflecting retries.

### 5.4 Media preparation failure

**Scenario:** ffmpeg exits non-zero on a corrupt upload; or worker OOMs mid-transcode.
1. `media_prep` job throws; Graphile Worker records the failure and retries with backoff up
   to `max_attempts=4` (delays 15 s, 1 m, 5 m, 20 m).
2. Temp file is unlinked in a `finally`; any partial R2 multipart is aborted
   (`AbortMultipartUpload`).
3. On terminal failure, `queue_items.status='failed'` with a machine reason
   (`unsupported_codec`, `oversized`, `corrupt`), surfaced to the user.
4. User "retry prep" creates a **new** prep job against the **same** `asset_id`; because
   `media_renditions_idem (asset_id, recipe_hash)` is unique and deterministic, a successful
   retry produces exactly one rendition and does not create a second queue item (I13).
**Evidence:** job history shows attempts + final error; `queue_items` shows one row moving
`preparing→failed→ready`; `media_renditions` has ≤1 row per (asset, recipe).

### 5.5 Account revocation

**Scenario:** User removes our app in Instagram; token now invalid.
1. Next publish handler call to IG returns an OAuth error; handler classifies it as
   `needs_reauth` (not a content failure).
2. In one tx: `ig_accounts.status='needs_reauth'`, the current attempt set to
   `failed_pre` (pre-boundary, retryable *after* reauth), item returns to `ready`.
3. Queue is untouched (I5/I11). User notified with a reconnect deep link; scheduler skips
   this account (materializer checks `status='connected'`).
4. On reconnect, ownership verified by matching `ig_user_id`; queue and history reattach
   because they key on `ig_account_id`, which is preserved.
**Evidence:** `ig_accounts` status history; `notifications` reconnect entry; queue rows
unchanged; after reauth, publish resumes and receipt appears.

### 5.6 Quota exhaustion / cooldown

**Scenario:** Account hits its `daily_cap` or IG returns a rate/limit signal.
1. Publish handler's pre-check reads `daily_usage` for the account-local date; if
   `published_count >= daily_cap`, it sets the attempt to defer: `run_at` = next local
   midnight, returns with no side effect; item stays `ready` (I11).
2. If IG itself returns a limit error mid-flight (pre-publish), handler sets
   `ig_accounts.cooldown_until` = now + backoff (e.g. 1 h) and defers; not a permanent
   failure.
3. Scheduler and manual publish both honor `cooldown_until` and `daily_cap`.
**Evidence:** `daily_usage` row; `ig_accounts.cooldown_until`; job `run_at` in the future;
UI shows "daily limit reached, resumes <time>".

### 5.7 Schedule edit near a slot (DST / dup rule / tz change)

**Scenario:** At 01:30 local during a DST fall-back, a rule "post at 01:30 daily" would fire
twice; separately the user edits the timezone minutes before a due slot.
1. The materializer resolves each rule occurrence to a single **UTC instant** using the IANA
   tz database (via `luxon` `DateTime.fromObject({...}, {zone})`), which maps the ambiguous
   local time to one instant deterministically.
2. It attempts `INSERT INTO publish_slots (ig_account_id, slot_instant)`; the PK makes a
   second rule (or a re-run of the tick) resolving to the same instant a no-op.
3. A timezone edit changes future resolutions but cannot retroactively double-book a past
   `slot_instant` already recorded.
**Evidence:** `publish_slots` has one row per (account, instant); at most one
`publish_attempt` per slot; test `test_dst_no_double` asserts single attempt across the
repeated wall-clock hour.

### 5.8 Duplicate source collection

**Scenario:** Two overlapping sourcing runs discover the same Reel; auto-refill and a manual
refill fire together.
1. Each collected candidate is `INSERT … ON CONFLICT (source_id, external_media_id) DO
   NOTHING`; the duplicate discovery is dropped.
2. Refill picks candidates in `eligible` state and inserts `queue_items`; the
   `queue_items_no_dup_source` unique index rejects a second insert of the same
   `source_candidate_id` into the same account.
3. Auto-refill stops at `target_depth` by counting `ready`+`preparing` items before
   inserting (checked in the same tx that inserts, under `SELECT … FOR UPDATE` on the account
   row to prevent two refills exceeding depth).
**Evidence:** `source_candidates` has one row per external id; queue has one item per
candidate; depth never exceeds `target_depth` in `test_concurrent_refill_no_dup`.

### 5.9 Browser hang during cleanup

**Scenario:** Cleanup is archiving item 4 of 10; the API call hangs past timeout, worker is
killed.
1. `cleanup_items[4].status='submitting'` was committed before the IG call. Lease
   (`account_action_leases kind='cleanup'`) expires.
2. Reconciler sees a `submitting` cleanup item: it does **not** re-issue the archive/delete
   (destructive, ambiguous). `cleanup_runs.status='paused_reconcile'`.
3. It reads IG to determine whether item 4 was archived/moved; if confirmed, marks `done`
   and continues to item 5; if unknown, leaves `ambiguous` and stops the run for operator
   review. Later cleanup runs for this account remain ordered behind it (they can't acquire
   the lease while this run holds the account, and a paused run blocks new runs by the
   `entitlement`/run-active check).
**Evidence:** `cleanup_items` shows item-by-item status incl. the `ambiguous` item;
`cleanup_runs.status='paused_reconcile'`; `result` holds redacted evidence (no session data).

### 5.10 Changed cleanup selection before execution

**Scenario:** User confirms a 12-item selection; before the run starts, one post's analytics
refresh makes it no longer qualify (or the user protects it).
1. Confirmation stored `cleanup_runs.selection_hash` = hash of the exact confirmed
   `receipt_id` set + `frozen_rule`.
2. At run start, the handler recomputes the current qualifying set (fresh analytics,
   `protected_posts` excluded) and hashes it; if it differs from `selection_hash`, the run is
   **invalidated** (`status='stopped'`, reason `selection_changed`) and the user must
   re-confirm (brief §3.8).
**Evidence:** `cleanup_runs` shows `stopped`/`selection_changed`; no `cleanup_items`
executed; UI prompts re-confirmation.

### 5.11 Repeated / out-of-order billing events

**Scenario:** Stripe delivers `customer.subscription.updated` twice and before the
corresponding `created`, plus a late `deleted`.
1. Webhook endpoint verifies signature, then `INSERT INTO stripe_events (event_id) ON
   CONFLICT DO NOTHING`; a duplicate `event_id` is ignored (I18).
2. The `billing_webhook` job reads the event and **upserts** `subscriptions` from the
   event's object fields (`status`, `current_period_end`, plan from price id) — state is a
   projection of the latest known object, not a function of arrival order. An older event
   with an earlier `current_period_end` does not overwrite a newer one (guard: only apply if
   event's object `updated`/period is ≥ stored).
3. Downgrade/past_due sets entitlements to hold activity above new limits (I11) without
   deleting anything.
**Evidence:** `stripe_events` shows each event once with `processed_at`; `subscriptions` has
one coherent row; `test_stripe_out_of_order` asserts final state matches the newest object.

### 5.12 Storage failure

**Scenario:** R2 is unreachable during upload finalize or during a publish that needs to
serve media to IG.
1. Upload finalize: multipart `CompleteMultipartUpload` fails → client shown a retryable
   error; no `media_assets` row is committed (row is written only after successful upload),
   so no dangling queue item (I13 dedupe still holds on retry via sha256).
2. Publish: IG fetches media from a presigned R2 URL; if R2 is down, IG container creation
   fails **pre-boundary** → attempt `failed_pre`, retried with backoff; item stays `ready`.
3. Sustained R2 outage raises the `media_prep`/`publish` failure rate; alarm (see §7) fires;
   publishing defers rather than failing permanently.
**Evidence:** job history shows pre-boundary retries; no receipts written for the failed
attempts; `media_assets` count consistent with successful uploads only.

### 5.13 Deletion interrupted halfway

**Scenario:** A workspace deletion crashes after revoking IG access and deleting some R2
objects but before removing DB rows.
1. `deletion_requests.checklist` tracks steps: `revoke_ig`, `cancel_billing`,
   `purge_media`, `purge_db`, `write_tombstone` — each idempotent and marked complete only
   after success.
2. On restart, the deletion job resumes from the first incomplete step; `purge_media` lists
   remaining objects by `workspace_id` prefix and deletes (re-deleting an already-gone object
   is a no-op).
3. On completion, only a minimal non-identifying `tombstone` remains (request id, timestamps,
   counts) — enough to prove completion, no customer data in disguise (brief §3.10).
**Evidence:** `deletion_requests.checklist` shows each step's completion time;
`tombstone`/`audit_log` proves the request finished; a re-list of R2 by prefix returns empty.

---

## 6. AI strategy

**Decision: no AI in v1.** The brief permits AI only with a compelling, budgeted,
safety-conscious case, and explicitly states AI is not required (brief §5, §6).

Every capability here is deterministic and better served without a model:

- **Scheduling / queueing:** pure date math (luxon + `publish_slots` uniqueness). A model
  would add nondeterminism to the exact place the brief demands predictability.
- **Media prep:** ffmpeg/sharp with fixed recipes. Deterministic, cheap, reproducible
  (recipe_hash), which AI transcoding would undermine.
- **Cleanup selection:** explicit numeric criteria the user confirms. Introducing a model
  "recommendation" would blur the frozen-selection guarantee (I10) that makes destructive
  actions safe.
- **Analytics:** raw IG numbers stored append-only. Any AI "insight" would risk implying
  measures IG didn't provide; honesty about missing/stale data (brief §3.7) is a
  correctness property, not a generation task.

Where AI *could* plausibly help later — caption suggestions — it is a non-goal (brief §5)
unless justified, and it would introduce cost per item and a content-safety review burden
that a launch with one operator cannot staff. **Cost of the chosen path: $0/mo in model
spend.** The section is intentionally deterministic; if caption assistance is later
requested, it returns here with a per-call arithmetic (tokens×price×items/day) and an opt-in,
human-reviewed, never-auto-published design.

---

## 7. Testing and release confidence

### 7.1 On every change (CI, < 5 min, no external services)

- `tsc --strict`, ESLint, `prettier --check`.
- **Unit tests** for date/schedule resolution (DST cases in 7.5), caption assembly, filter
  evaluation, recipe hashing, serializer allowlist (asserts no token field escapes).
- **Invariant tests** from §4 run against **ephemeral Postgres** (Testcontainers), with IG
  and Stripe **mocked** at the HTTP boundary (call-count assertions). This includes I2/I7/I8
  single-flight races run with real concurrent transactions (spawn N async workers against
  one DB).
- **Migration check:** apply all migrations to an empty DB; assert every `CHECK`/unique/FK
  present (schema snapshot diff).

### 7.2 Requires real supporting services (pre-merge on protected branches, nightly)

- **Stripe test mode:** drive Checkout → webhook → subscription projection, including
  replayed and out-of-order events via the Stripe CLI `stripe events resend`.
- **R2:** real presign + expiry test (assert 403 after 300 s), multipart abort on failure.

### 7.3 Nightly

- **Backup restore drill (automated):** restore last night's Postgres snapshot into a scratch
  instance, run a smoke query set, assert row counts within tolerance, tear down. A green
  health screen is explicitly *not* accepted as proof (brief §3.11); the restore itself is the
  test. Alarm if restore fails or exceeds RTO 30 min.
- **Orphan sweep dry-run:** report R2 objects with no `media_assets`/`renditions` row and DB
  media rows with no R2 object (should be ~0).
- **Token expiry scan:** assert every `connected` account has a token refreshed within its
  60-day window; refresh those inside 7 days of expiry.

### 7.4 Must be proven against **safe test Instagram accounts** (staging, gated)

Dedicated Meta app + throwaway professional test accounts. These run on demand before a
release touching the publish/cleanup path:

- **Full spine:** connect → upload → prepare → queue → publish → receipt → analytics; assert
  a real `permalink` and `ig_media_id`.
- **Ambiguous-outcome drill:** use a network fault injector (toggisort/`toxiproxy`) to drop
  the response *after* `media_publish`; assert the read-recovery path (5.2) finds the post
  and writes exactly one receipt — and separately, when it truly didn't post, that it lands
  in `needs_review` with no second attempt.
- **Cleanup drill:** publish a test post, run cleanup, assert feed photo archived / Reel in
  Recently Deleted via a follow-up `GET`; kill the worker mid-item and assert no repeated
  destructive action (5.9).

### 7.5 Named drills (each a written test)

- **Crash:** `test_crash_before_publish`, `test_crash_after_publish` (SIGKILL the worker at a
  fault point, restart, assert single effect).
- **Retry:** `test_pre_vs_post_failure`.
- **Race:** `test_double_publish_one_receipt`, `test_concurrent_refill_no_dup`,
  `test_lease_serializes_publish`.
- **Quota:** `test_over_quota_defers`, `test_cooldown_honored`.
- **Timezone:** `test_dst_fallback_no_double`, `test_tz_edit_no_double`,
  `test_skipped_spring_forward_time`.
- **Restore:** nightly restore drill above, plus a quarterly *full* restore-into-prod-shape
  rehearsal.
- **Ambiguous outcome:** `test_ambiguous_no_auto_retry` (mock) + the staging IG drill.

### 7.6 Release gating

Merge to `main` requires 7.1 green. Deploy to prod requires 7.1+7.2 green and, for changes
touching `publish`/`cleanup`/`billing`/migration paths, a green 7.4 run within the last 24 h.
Rolling deploy drains workers on `SIGTERM` (finish in-flight job or release lease); a job
mid-side-effect that can't finish in the 30 s drain window leaves its attempt in a
reconcilable state (never a lost or double effect), which the deploy smoke test asserts.

---

## 8. Delivery phases

Vertical slices. Exit criteria are verifiable statements, no dates.

**Phase 0 — Skeleton & tenancy.** Monorepo, migrations, `users/workspaces/memberships/
sessions`, RLS policies, auth (password + Google), invitation gate, session cookies.
*Exit:* accept an invitation creates a workspace; a second workspace's session cannot read
the first's rows (RLS test green); public signup without a live invitation is rejected.

**Phase 1 — Media in/out.** Upload (multipart to R2), `media_assets` with sha256 dedupe,
presigned read with 300 s expiry, `media_prep` (sharp/ffmpeg) → `media_renditions` with
recipe_hash idempotency, progress via SSE, retry-prep without duplication.
*Exit:* upload an image and a video, see prepared renditions; re-upload identical bytes →
one asset row; kill worker mid-prep → retry yields exactly one rendition.

**Phase 2 — The spine (FIRST end-to-end run).** Connect a professional account via Meta
OAuth, `ig_accounts`+encrypted `ig_tokens`, single-account queue with frozen inputs,
"publish next now", `publish_attempts`/leases/`receipts`, `daily_usage`, analytics fetch.
*This is the earliest phase that completes connect → upload → prepare → queue → publish →
receipt → analytics against a safe test account.*
*Justification for ordering:* it must come after Phase 0 (a publish is meaningless without
the workspace/account it belongs to — I1/I20) and Phase 1 (you cannot publish media you
cannot prepare or store). It comes before scheduling, restricted sourcing, cleanup, and
billing because every one of those depends on a correct, single-flight publish with a
receipt; building them first would mean building on an unproven boundary.
*Exit:* against a test IG account, one click yields exactly one live post with a real
permalink in `receipts`; a double-click yields one receipt; `test_crash_after_publish`
(staging) yields one post; analytics snapshot appears and never overwrites a prior snapshot.

**Phase 3 — Queue & schedule.** Drag-reorder (`position`), hide/restore/remove, bulk
actions, `schedule_rules`, `schedule_tick` materializer, `publish_slots` uniqueness, pause/
resume, cooldown/cap deferral, live status.
*Exit:* a scheduled rule publishes the next item at the local time; the DST/tz drills pass;
pausing holds the queue and resume states the next slot; reaching the cap defers (not fails).

**Phase 4 — Library, analytics depth, notifications.** Per-account library, snapshot history,
totals/trends/breakdowns with honest missing-data handling, manual refresh with rate cap,
in-app notifications with read/dismiss + deep links, feedback.
*Exit:* a published post appears in the library with permalink; trends render across ≥2
snapshots; a manual refresh is rate-limited; a publish failure raises a high-priority
notification that deep-links to the item.

**Phase 5 — Billing & entitlements.** Stripe Checkout/Portal, `stripe_events` idempotent
ingestion, `subscriptions` projection, plan-based `account_allowance`/seats enforced
server-side, over-plan holds (not deletes), `entitlement_grants`.
*Exit:* checkout produces an active subscription from webhooks only; replayed/out-of-order
events yield one coherent state; exceeding account allowance holds activity without data
loss; hiding a button is proven insufficient by a direct-API test that is still refused.

**Phase 6 — Operator console.** Separate app + TOTP, waitlist/invite/connection-request/
user/workspace management, suspensions, temporary entitlements, per-item "where is it stuck /
is retry safe" inspector, safe operator actions (retry-known-safe, pause, reconcile, revoke),
health screen, audit log, **backup restore drill wired to nightly**.
*Exit:* operator resolves a `needs_review` item via the inspector without touching the DB;
every privileged action appears in `audit_log`; the nightly restore drill runs and reports.

**Phase 7 — Restricted sourcing (entitled).** `sourcing-worker` (Playwright), sources with
verification, filtered candidates → backlog, manual + auto-refill with dedupe/depth caps,
one-time sample, per-source states, entitlement gating + isolation, provenance retention.
*Justification for last-among-features:* highest legal/trust/policy/abuse/isolation risk
(brief §3.5); it must sit on a proven, contained platform and behind a hard entitlement so
it is invisible/unreachable by default (I14). *Exit:* only an entitled workspace can create a
source; duplicate discovery/refill yields no duplicate queue items; revoking the entitlement
stops acquisition but preserves accepted work and provenance; sourcing session material never
appears in any API response, receipt, or log.

**Phase 8 — Managed cleanup (entitled).** `cleanup_rules`, preview with fresh analytics +
protection, exact-selection confirm with `selection_hash`, one-at-a-time archive/Reel-to-
Recently-Deleted, stop-before-next, scheduled runs with re-checks, crash→reconcile pause,
redacted history.
*Exit:* a run archives/moves exactly the confirmed items one at a time; changing the
selection before run invalidates the confirmation; a mid-item crash pauses for reconcile with
no repeated destructive action; history contains redacted evidence and no session material.

**Phase 9 — Deletion, export, legal pages, launch hardening.** Data export, workspace/account
deletion with resumable `checklist` + tombstone, public privacy/terms/copyright/security-
contact/data-deletion pages, load-check against §2 numbers.
*Exit:* a deletion revokes IG access, cancels billing, purges media+DB, leaves only a
tombstone, and is resumable after an injected crash; all legal pages are live before real
customers onboard.

---

## 9. Security and privacy

- **Identity & authz.** First-party sessions (§1.10); every request resolves a
  `(user, workspace, role)` tuple. Authorization is enforced in a single middleware that
  loads membership + effective entitlements; route handlers receive an authorized context and
  cannot query without a `workspace_id`.
- **Workspace isolation.** Postgres **Row-Level Security** on every tenant table: `CREATE
  POLICY tenant_isolation USING (workspace_id = current_setting('app.workspace_id')::uuid)`.
  The app sets `app.workspace_id` per transaction via `SET LOCAL` after authenticating. This
  is a backstop beneath the repository layer so a missed `WHERE` cannot leak rows (I20). The
  operator app connects as a role that bypasses RLS but only through logged, scoped queries.
- **Secrets.** IG long-lived tokens and sourcing `storageState` are encrypted at rest with
  **AEAD (XChaCha20-Poly1305 via libsodium `crypto_aead`)**; the data key is wrapped by a KMS
  master key (`key_version` column supports rotation). Application env secrets live in Fly
  secrets, never in the repo. DB connection uses TLS.
- **Private media.** R2 objects are private; the only access is a presigned URL minted after
  a tenant check, `X-Amz-Expires=300`. IG fetches media within that window during container
  creation. No public bucket, no long-lived URL (I15).
- **Restricted sessions.** Confined to `sourcing-worker`; `storageState` never leaves that
  VM's encrypted store, is never returned to a browser, written to a receipt, or logged
  (I16). Log middleware redacts `Authorization`, `Cookie`, `Set-Cookie`, and any field named
  in a denylist; the serializer uses an **allowlist**, so a new sensitive column can't leak
  by default.
- **Billing callbacks.** Stripe webhook signature verified (`stripe.webhooks.
  constructEvent`) before any processing; `event_id` PK dedupes; the endpoint only enqueues,
  so a slow processor can't drop events (I18).
- **Abuse controls.** Per-IP and per-session rate limits on auth, upload, presign, and manual
  refresh (token bucket in Postgres). Manual analytics refresh capped (e.g. 1/post/hour) so
  it can't exhaust IG limits (brief §3.7). Invitation tokens single-use + expiring.
- **Audit & attribution.** Every privileged/operator/billing/destructive action writes
  `audit_log` with the actor; operator actions require the elevated session (I19, brief §4).
- **Retention & deletion.** Originals retained while the workspace is active or until
  explicit deletion; deletion purges media + DB and leaves only a non-identifying tombstone
  (§5.13). Export produces the workspace's media + structured data on request.
- **Support access.** The operator never edits production data directly or uses a DB console
  for normal support (brief §2/§3.11); all support actions go through the audited operator
  console with the same invariant checks as customer actions (e.g. operator "retry" refuses
  on `failed_ambiguous` just like the system does).

---

## 10. Risk register

| # | Risk | Early warning signal | Mitigation already in this plan |
|---|---|---|---|
| R1 | Duplicate live posts (worst reputational failure) | `receipts` count > 1 per queue_item; IG mock call-count > 1 in CI | `publish_attempts_idem` + `one_active` + `account_action_leases` PK + conditional `submitting` flip (I2/I7); staging ambiguous drill |
| R2 | Ambiguous outcome mishandled → double post or silent loss | attempts stuck in `submitting`; needs_review backlog rising | read-recovery reconciler (5.2); `failed_ambiguous` blocks republish (I4) |
| R3 | Meta app review / permission scope denied or changed | OAuth error rate; Meta dev dashboard notices | official Content Publishing API only; token refresh scan (§7.3); `needs_reauth` flow (5.5) preserves queue |
| R4 | Restricted sourcing triggers legal/platform action | Meta warnings; source `blocked` states; takedown notices | entitlement-gated, isolated VM, provenance retained, public product upload-only (§1.8/§2.5); disclosed risk; can be revoked instantly (I14) |
| R5 | Media storage cost/volume runs away | R2 storage line vs §2.9 model; per-workspace bytes over plan | sha256 dedupe (I13), R2 zero-egress, plan storage allowance holds uploads over quota (I11) |
| R6 | Worker starvation (media/browser starves publish) | publish queue depth alarm (> 100 or oldest > 10 min) | separate queues + concurrency budgets + separate sourcing VM (§2.3) |
| R7 | Billing state divergence | `subscriptions` mismatch vs nightly Stripe `GET` sweep | webhook idempotency + projection + nightly reconcile (5.11/I18) |
| R8 | Data loss / unrecoverable backup | nightly restore drill fails or exceeds RTO | automated restore drill is a *test*, not a checkbox (§7.3); daily snapshot + PITR |
| R9 | Token/secret leak across tenants | serializer allowlist test fails; log scan hits | encrypted separate tables, allowlist serializer, RLS, log redaction (I6/I16) |
| R10 | Solo-founder operational overload | rising manual operator interventions; on-call fatigue | one datastore, one language, managed Postgres/R2/Stripe; operator console handles normal support without DB access (§9) |

---

## 11. Explicit tradeoffs (weaker than the brief, deliberately)

1. **Single region, single Postgres primary (no HA replica at launch).** The brief allows
   single-region (§5) but expects durability. A primary failure means downtime until
   restore/failover (RTO target 30 min via PITR). Accepted because Multi-AZ RDS-class HA
   would consume the budget; mitigated by daily snapshot + PITR + rehearsed restore.
2. **Reconciliation adds latency to ambiguous publishes.** An ambiguous outcome can sit in
   `needs_review` for up to ~10 min (read-recovery window) before resolving. The brief wants
   truthful status, which this delivers, but the item is "stuck" meanwhile. Accepted:
   correctness (no double post) outranks immediacy.
3. **Cleanup of media kinds the Graph API can't mutate is out of scope.** If Instagram's API
   cannot archive/delete a given media kind for our permissions, that kind is simply
   ineligible for automated cleanup rather than driven via browser automation. Weaker than "a
   cleanup rule over all library posts," but browser-driving destructive actions on our own
   posts would reintroduce the exact policy/ambiguity risk the API avoids.
4. **Analytics freshness is best-effort, capped.** Manual refresh is rate-limited (1/post/hr)
   and background refresh lags. The brief accepts lag; the tradeoff is a user occasionally
   sees stale numbers, disclosed in the UI as "as of <time>."
5. **No merchant-of-record; we handle only single-region tax.** Stripe (not Paddle) means we
   own tax registration if we expand regions. Accepted for a single-region launch (§13).
6. **Auto-refill depth control uses a per-account row lock**, which briefly serializes
   refills for one account. Slightly weaker throughput than lock-free, but it's the mechanism
   that guarantees depth is never exceeded (5.8).
7. **Roles are four fixed levels, not custom permissions.** Simpler than fine-grained ACLs;
   distinguishes billing/destructive from day-to-day as required, but a studio can't craft a
   bespoke role. Accepted for launch scale.

## 12. Where this is stronger than required

1. **Postgres RLS as a hard backstop beneath the app layer.** The brief requires tenant
   isolation; RLS makes a forgotten `WHERE` fail closed rather than leak. Cost: per-tx `SET
   LOCAL` and policy maintenance — worth it because isolation is the highest-consequence
   invariant.
2. **`publish_slots` uniqueness on the resolved UTC instant.** Beyond "predictable schedule
   edits," it makes double-posting from DST/duplicate rules a *structural impossibility*, not
   a carefully-timed avoidance. Cheap (one small table), high safety value.
3. **Automated nightly restore drill as a gating test.** The brief asks for a rehearsed
   restore; automating it nightly (not quarterly) catches backup rot early. Cost: a scratch
   instance for ~30 min/night (~pennies).
4. **Content-addressed media (sha256/recipe_hash) everywhere.** Beyond dedupe, it makes prep
   retries provably idempotent and receipts verifiable against exact bytes. Small hashing
   cost.
5. **Allowlist serializer (deny-by-default field exposure).** Stronger than redacting known
   secrets: a newly added sensitive column cannot leak unless explicitly allowlisted.
6. **Scale-to-zero, physically separate sourcing VM.** Beyond "separable," the restricted
   capability literally cannot consume publish capacity because it's a different machine that
   is off when unused. Saves ~$50/mo and hard-contains the riskiest code.

## 13. Assumptions (brief was silent)

1. **Target region** is a single geography matching the customers in §2 (e.g. EU→`fra`);
   chosen at deploy, drives default account timezones and Fly region.
2. **Default `daily_cap` = 25** posts/account/day (below typical IG automation ceilings),
   editable per account 0–50.
3. **Plans:** Free (1 account, 1 seat, no cleanup/sourcing), Starter (3 accounts, 3 seats),
   Studio (10 accounts, extra seats purchasable, cleanup available); sourcing is
   operator-entitlement only, never a public plan feature.
4. **Session lifetime** 30 days sliding; operator session 12 h absolute + TOTP.
5. **Lease durations:** publish lease 120 s (renewed by heartbeat for long publishes);
   cleanup lease 300 s; reconcile cron 60 s.
6. **Retry budgets:** `media_prep` 4 attempts (15 s→1 m→5 m→20 m); `publish` pre-boundary 5
   attempts with backoff; post-boundary 0 (reconcile only); `analytics` 3.
7. **Presigned URL TTL** 300 s; **long-lived IG token** refreshed within 7 days of the 60-day
   expiry.
8. **Analytics refresh cadence:** background per receipt at 1 h, 24 h, 7 d, then weekly for 90
   days, then monthly; manual refresh 1/post/hour.
9. **Media limits:** original upload ≤ 500 MB, video ≤ 90 s (Reels), images ≤ 30 MB;
   oversized rejected with reason.
10. **Data-retention default:** originals retained for the life of the workspace; deleted
    workspace media purged within 24 h of a deletion request; tombstone retained
    indefinitely (non-identifying).
11. **"Region/tax":** single-region billing; no VAT/GST handling beyond Stripe Tax if the
    single region requires it; multi-region tax deferred (§11.5).
12. **Meta app:** a single Meta app in Live mode with Instagram Content Publishing + Insights
    permissions granted through App Review; safe test accounts under a separate dev app for
    §7.4.
13. **Timezone source:** IANA tz database via `luxon`; account timezone chosen at connect,
    editable, and all schedule math resolves to UTC instants.
14. **Reorder representation:** fractional `NUMERIC` positions, rebalanced when gaps shrink
    below a threshold (background job), so drag-reorder is O(1) writes.
```
