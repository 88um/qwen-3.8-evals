# ToolBox Poster — Engineering Plan

Scale basis used throughout this document (from the brief, with mid-range values chosen
where the brief gives a range): 1,000 registered users, 200 weekly active users, 500
connected Instagram accounts, 800 prepared-or-published items per day on an average day,
2,000 on the worst day, with 30% of a day's publishes landing inside the single busiest
hour. Every capacity, cost, and alarm number below derives from these values.

---

## 1. Technology decisions

### 1.1 Language and runtime

**Choice:** TypeScript on Node.js 22 LTS, one repository, strict mode, for the web app and
every worker.
**Rejected:** Go. Stronger concurrency primitives, single static binary, lower memory per
process — genuinely better for the worker fleet in isolation.
**Why:** one founding engineer maintains the browser frontend, the API, three workers, and
the Playwright automation. TypeScript is the only option where the frontend, the API DTOs,
the job payloads, and the Playwright code share one type system and one toolchain.
Playwright, sharp, and Stripe's SDK are first-party TypeScript. The concurrency Go wins on
is not the bottleneck here: at 2,000 publishes/day the workers are I/O-bound on Instagram,
and correctness comes from Postgres row locks (§4), not from in-process concurrency.

### 1.2 Application framework

**Choice:** Next.js 15 (App Router, React 19) self-hosted in a Node process; API
implemented as route handlers under `/api`; server-sent events from a route handler.
**Rejected:** separate SPA (Vite/React) + Fastify API. Cleaner process separation and no
framework coupling between UI and API.
**Why:** one deployable for all customer-facing HTTP halves the deploy, TLS, session, and
CORS surface a single operator must run. Nothing long-running lives in the web process —
every publish, transcode, or crawl is a row in `jobs` executed by a separate worker
process (§2) — so the main argument for a separate API server (long requests starving the
UI) does not apply.

### 1.3 Database

**Choice:** PostgreSQL 16, DigitalOcean Managed Database (2 vCPU / 4 GB, primary only),
7-day point-in-time recovery enabled. The sole datastore: relational data, job queue,
sessions, rate-limit counters, notifications.
**Rejected:** PostgreSQL + Redis (BullMQ for jobs, Redis for sessions/rate limits). Redis
is the strongest job-queue option and its latency is better.
**Why:** every invariant in §4 is enforced by uniqueness, row locks, and transactions that
span *both* business state and job state — "flip queue item to `publishing` and enqueue
the job" must be one atomic commit, which a Redis queue cannot join. A second datastore is
a second thing to back up, restore-rehearse, and page one person about. At 800 items/day
the job table peaks below 10 writes/second; Postgres `FOR UPDATE SKIP LOCKED` covers that
with three orders of magnitude of headroom.

### 1.4 Job queue

**Choice:** a `jobs` table in Postgres (DDL in §3) worked by `leaseJobs()` using
`SELECT … FOR UPDATE SKIP LOCKED`, 5-minute leases, 60-second heartbeats, a reaper every
60 seconds, and a `unique_key` column for enqueue idempotency.
**Rejected:** pg-boss. It is a maintained implementation of exactly this pattern.
**Why:** the publish, cleanup, and deletion jobs need custom lease-expiry semantics — an
expired publish lease must consult `publish_attempts.state` to decide retry-vs-reconcile
(§5.2) instead of blindly re-running, and pg-boss's retry policy is per-queue, not
per-row-state. Owning ~300 lines of queue code is cheaper than working around a
library's retry model in the one place where a wrong retry double-posts.

### 1.5 Object storage

**Choice:** Cloudflare R2, one private bucket, all keys prefixed `ws/{workspace_id}/…`,
S3-compatible API (`CreateMultipartUpload` / `UploadPart` / `CompleteMultipartUpload` for
uploads, presigned GET for reads).
**Rejected:** DigitalOcean Spaces (keeps everything on one provider bill).
**Why:** R2 charges zero egress. Instagram fetches every published video from us by URL
(the Graph API takes a `video_url`, not an upload), users re-download originals, and
exports ship media out — at 22 GB/day of video (§2.6) Spaces' egress allowance is
consumed within its first month and overage is $0.01/GB, ≈ $6–20/month growing; R2 makes
the dominant cost (storage) the only cost. The workspace-id key prefix is load-bearing:
deletion (§5.13) and tenant isolation of media (§9.4) operate on the prefix.

### 1.6 Instagram integration

**Choice:** the official **Instagram API with Instagram Login** (`graph.instagram.com`)
for everything customer-facing: OAuth connect (scopes `instagram_business_basic`,
`instagram_business_content_publish`, `instagram_business_manage_insights`), publishing
(`POST /{ig-user-id}/media` → container, `GET /{container-id}?fields=status_code` →
poll, `POST /{ig-user-id}/media_publish` → publish), quota
(`GET /{ig-user-id}/content_publishing_limit`), insights
(`GET /{ig-media-id}/insights`), long-lived 60-day tokens refreshed via
`GET /refresh_access_token?grant_type=ig_refresh_token`.
**Rejected:** the Facebook-Login variant of the Graph API (`graph.facebook.com` with
`pages_show_list` + linked Page). It is the older, more battle-tested path.
**Why:** it requires every customer to have a Facebook Page linked to their Instagram
account, which theme-page operators and local businesses frequently do not. The
Instagram-Login variant removes the single largest onboarding drop-off while offering the
same publish/insights endpoints. Restricted sourcing and managed cleanup are *not*
available in any official API and are built on Playwright automation, fully separated
from this integration (§1.7, §2.3).

### 1.7 Browser automation (restricted sourcing + managed cleanup)

**Choice:** Playwright 1.x driving Chromium, 3 concurrent browser contexts on a dedicated
droplet, session persistence via `browserContext.storageState({path})` (cookies +
localStorage; it does not persist DOM or in-memory state, so every task starts from a
fresh navigation), egress through 10 static ISP proxy IPs, storage-state blobs encrypted
at rest (§9.3).
**Rejected:** a third-party scraping API (e.g. a hosted Instagram data provider) for
sourcing. It removes ban risk from our infrastructure and is operationally simpler.
**Why:** cleanup (archive / move-to-Recently-Deleted) has no third-party provider at all —
it must act *as the customer's account* — so Playwright capability is mandatory anyway;
running sourcing on the same isolated automation tier reuses that investment, keeps
tester data out of a third party, and keeps the per-check cost at proxy bandwidth
(≈ $0.02) instead of per-request API pricing. The ban risk is contained by the pool
design and kill switch in §2.3 and priced in the risk register (§10).

### 1.8 Payments

**Choice:** Stripe: Checkout for purchase, Billing portal for self-service changes,
Stripe Tax for tax, webhooks (signature-verified, 5-minute tolerance) treated as *pings*
that trigger `syncSubscriptionFromStripe()` — the handler always refetches the
subscription object and never trusts event payload ordering.
**Rejected:** Paddle. As merchant of record it absorbs tax filing entirely, which is a
real burden for a one-person company.
**Why:** the fetch-latest-on-ping pattern (§5.11) plus the `stripe_events` idempotency
table is the mechanism that satisfies the brief's out-of-order-callback rule, and
Stripe's event model, test clocks, and CLI webhook replayer are what make that pattern
*testable* (§7). Paddle's webhook simulator cannot replay out-of-order sequences. Stripe
Tax at 0.5% of revenue covers the tax gap.

### 1.9 Authentication

**Choice:** self-managed email + password (Argon2id: 64 MB memory, 3 iterations,
parallelism 1), opaque 256-bit session tokens stored SHA-256-hashed in a `sessions`
table, cookie `HttpOnly; Secure; SameSite=Lax`, 30-day absolute expiry. Operator access
is a separate `operator_sessions` table with mandatory TOTP and its own hostname (§9.6).
**Rejected:** Clerk. Fastest to integrate and its invitation feature overlaps our beta
gate.
**Why:** the invitation gate, workspace membership, and operator separation all live in
our schema regardless; Clerk would add a second user store that must be kept consistent
with `workspace_members` and would put customer identity in a third party for $25+/month.
A password table with Argon2id and hashed session tokens is ~200 lines and sits inside
the same transactions as everything else (e.g. invite acceptance, §3).

### 1.10 Media processing

**Choice:** ffmpeg 7 (via `fluent-ffmpeg` invoking the system binary) for video probe +
transcode, sharp for images, running only on the media worker, 2 concurrent transcodes.
Output contract for Reels: MP4 container, H.264 high profile, yuv420p, ≤1080×1920, 30 fps,
AAC 128 kbps 48 kHz — validated by a post-transcode `ffprobe` re-probe.
**Rejected:** a hosted transcoder (Transloadit). Zero CPU management and format edge
cases become someone else's problem.
**Why:** at 320 videos/day, Transloadit's ~$0.05–0.10/video is $480–960/month — more
than the entire infrastructure budget. ffmpeg on a $48 droplet does the same work for a
fixed cost, and the rendition-by-settings-hash uniqueness (§3, `media_renditions`) makes
its failures retryable without duplicates.

### 1.11 Realtime updates

**Choice:** server-sent events. One `GET /api/events` stream per open tab; Postgres
`NOTIFY` on channel `ws_events` fired by the same transactions that change state; the web
process `LISTEN`s once and fans out to streams filtered by the session's workspace.
Client reconnect via native `EventSource` retry with `Last-Event-ID`; on reconnect the
client refetches current state, so a dropped event costs staleness of one reconnect
(≤ 5 s), never wrong data.
**Rejected:** WebSockets (Socket.IO). Bidirectional and better under corporate proxies.
**Why:** every client→server action is already an idempotent HTTP POST; the only realtime
need is server→client status. SSE needs no extra server, no sticky-session concern, and
degrades to plain HTTP. NOTIFY payloads carry only `(workspace_id, entity, id)` — data is
refetched through the authorized API, so the realtime path cannot leak across tenants
(§4 row I-1).

### 1.12 Hosting and delivery

**Choice:** DigitalOcean, one region (NYC3): web droplet (2 vCPU/4 GB, $24), core-worker
droplet (4 vCPU/8 GB, $48, runs core + media workers as two processes), automation
droplet (4 vCPU/8 GB, $48), managed Postgres ($60), Cloudflare in front for DNS/TLS/CDN
of static assets. Deploys: GitHub Actions builds a Docker image per process, pushes to
DO registry, SSH-triggered `docker compose up -d` per droplet; secrets live in a
SOPS-encrypted file in the repo (age key held by the founder) rendered to
root-only `/etc/toolbox/env` (mode 0600) at deploy.
**Rejected:** Fly.io. Better deploy ergonomics, built-in private networking.
**Why:** the automation droplet needs long-lived Chromium processes, custom egress
routes through ISP proxies, and predictable memory — plain VMs are the boring fit, and
one provider for compute + managed Postgres keeps the operational surface at "three
droplets and a database" for a solo operator. Monthly cost arithmetic in §2.6.

### 1.13 Email

**Choice:** Postmark (transactional only: invites, password reset, uncertain-outcome and
connection-lost alerts, deletion confirmations).
**Rejected:** Amazon SES at ~1/10th the price.
**Why:** invite and recovery mail that lands in spam is a support burden the two-person
team cannot absorb; Postmark's deliverability and 45-day searchable activity log replace
a support tool. Volume ≈ 60 emails/day (invites 10, alerts 40, auth 10) = 1,800/month,
inside the $15/month 10k tier.

---

## 2. System architecture

### 2.1 Processes

Five independently running parts, three droplets:

```
                 ┌────────────────────────────────────────────────┐
 Browser ── SSE ─┤  web (Next.js)      droplet A  2vCPU/4GB       │
          HTTPS  │  UI, API, SSE fan-out, Stripe webhooks         │
                 └───────────────┬────────────────────────────────┘
                                 │ SQL (all coordination via Postgres)
      ┌──────────────────────────┼───────────────────────────────────┐
      │  PostgreSQL 16 (managed) │  tables incl. jobs, PITR 7 days   │
      └──────┬───────────────────┴──────────────┬────────────────────┘
             │ leaseJobs()                      │ leaseJobs()
 ┌───────────┴───────────────┐      ┌───────────┴──────────────────────┐
 │ droplet B 4vCPU/8GB       │      │ droplet C 4vCPU/8GB (automation) │
 │  core-worker: publish(8), │      │  auto-worker: source(3),         │
 │   analytics(4),billing(2),│      │   cleanup(1)                     │
 │   email(2), deletion(1),  │      │  Playwright/Chromium,            │
 │   scheduler tick, reaper  │      │  egress via 10 ISP proxy IPs     │
 │  media-worker: media(2)   │      └──────────────────────────────────┘
 └───────────────────────────┘
        │                │                          │
   Cloudflare R2    graph.instagram.com        instagram.com (web)
   (media, private) (publish, insights)        (sourcing, cleanup)
```

External systems — Instagram API, instagram.com, Stripe, Postmark, R2 — are each treated
as three-state: success, failure, unknown. Every call site that can have a side effect
records intent *before* the call and outcome *after* (§4).

### 2.2 How work moves

All cross-process work is a row in `jobs` with a `queue` label. The queues, their
worker-pool sizes, and their per-attempt budgets:

| queue | pool | ran by | per-job timeout | retries (pre-side-effect) |
|---|---|---|---|---|
| publish | 8 | core-worker | 60 s/API call, 10 min container wait | 3 (1 m / 5 m / 15 m) |
| media | 2 | media-worker | 20 min | 2 (1 m / 10 m) |
| analytics | 4 | core-worker | 30 s/call | 0 (next cycle catches up) |
| billing | 2 | core-worker | 30 s | 5 (Stripe refetch is safe) |
| email | 2 | core-worker | 15 s | 3 |
| deletion | 1 | core-worker | 10 min/step | unlimited, 15 m apart |
| source | 3 | auto-worker | 4 min/check | 2 (10 m / 60 m) |
| cleanup | 1 | auto-worker | 3 min/item | 0 (uncertainty rules, §5.9) |
| maintenance | 1 | core-worker | 5 min | 1 |

Derivations. *publish pool = 8*: worst hour is 30% of a 2,000-item day = 600 publishes;
container creation + publish calls occupy a slot ≈ 10 s (the multi-minute video container
wait is a separate delayed poll job, not a held slot), so 600 × 10 s / 8 slots = 12.5
minutes of drain inside the hour — a publish starts at most ~13 minutes after its slot
on the worst day, disclosed in §11. *media pool = 2*: 2 vCPU per 1080p transcode on a
4-vCPU droplet shared with nothing else CPU-bound; 320 videos/day × ~90 s = 8 CPU-hours,
i.e. 33% duty cycle at pool 2. *source pool = 3*: 3 Chromium contexts × ~800 MB + worker
overhead fits 8 GB with 4 GB headroom; 50 sources × 4 checks/day = 200 checks/day × ~3
min = 10 context-hours/day, 14% duty cycle. *cleanup pool = 1*: the brief's one-item-at-
a-time rule makes parallelism worthless; one context is the enforcement-friendly shape.

Starvation containment, concretely: publishing never waits on media (a queue item is only
`ready` after its rendition exists), media never runs on the web or publish droplet (CPU
isolation by placement), automation runs on its own droplet and proxy IPs (an Instagram
ban of pool IPs cannot touch API publishing, which egresses from droplet B's IP), and the
web process runs zero jobs (a queue backlog cannot slow a page load). The `jobs` table is
indexed `(queue, state, run_at)` so one queue's backlog does not slow another's
`leaseJobs()` scan.

### 2.3 The automation tier is a containment cell

Restricted sourcing and cleanup are the two capabilities that can damage trust, so the
architecture treats droplet C as disposable and quarantined:

- **Identity separation.** Sourcing uses *operator-owned pool accounts*
  (`source_pool_accounts`), never customer identities. Cleanup uses the customer's own
  session established through the entitled-onboarding flow (§9.3) and touches only posts
  in that customer's library.
- **Network separation.** Droplet C egresses to instagram.com only through the 10 ISP
  proxy IPs; droplets A/B never use those IPs. A pool ban burns proxies, not the product.
- **Kill switch.** `automation_flags` row (`sourcing_enabled`, `cleanup_enabled`) checked
  by `leaseJobs()` on droplet C before every lease; the operator console flips it; effect
  within one lease cycle (60 s). Flipping it holds jobs (`pending`, untouched), never
  fails them.
- **Blast-radius rule in code.** The auto-worker binary has no import path to Stripe,
  Postmark, or token-decryption for API publishing; its DB role `role_automation` has no
  GRANT on `ig_connections`, `subscriptions`, or `stripe_events`. A compromised Chromium
  process cannot reach publish tokens or billing (§9.3).
- **Policy risks addressed head-on** (brief §3.5): legal/rights — every sourced item
  carries provenance (`source_posts` author, original URL, caption) that survives into
  the queue and library, DMCA contact page is public before launch, and takedown means
  deleting the media asset + tombstoning the source post (mechanism in §9.7); platform
  policy — sourcing violates Instagram ToS, so it is entitlement-gated to tester
  workspaces the operator selects, invisible without the entitlement (§4 row H-10), and
  its removal path (revoke entitlement → acquisition stops, accepted work stays) is a
  product feature, not an emergency; abuse — per-source frequency floor (minimum check
  interval 60 min), per-workspace source cap (10), and pool-account rotation with
  per-account daily action budget (200 navigations) are enforced in `runSourceCheck()`
  before any navigation.

### 2.4 Publishing pipeline (the spine)

1. **Scheduler tick** (core-worker, every 30 s): `claimDueSlots()` — per §4 row H-3's
   transaction — turns a due schedule slot + the head `ready` queue item into a
   `publish_attempts` row and a `publish` job, reserving daily quota in the same commit.
2. **Publish job** advances the attempt state machine
   `created → media_upload → container_wait → publishing → published`, committing each
   transition. `media_upload` mints a 60-minute presigned R2 GET URL and calls
   `POST /{ig-user-id}/media`; `container_wait` is a self-rescheduling poll job (15 s
   interval, 10-minute budget, then 4 rechecks 15 minutes apart) on
   `GET /{container-id}?fields=status_code`; the commit that writes `publishing` is the
   **point-of-no-return marker** — it happens strictly before the `media_publish` HTTP
   call leaves the process.
3. **Receipt**: the same transaction that records `published` inserts the `receipts` row
   (Instagram media id, permalink from `GET /{ig-media-id}?fields=permalink`, frozen
   caption, rendition hash, timestamps, raw API response) and the `library_posts` row,
   increments `account_daily_usage.published`, and NOTIFYs the workspace channel.
4. **Analytics**: `scheduleInsightRefreshes()` enqueues per-post `analytics` jobs at
   1 h, 6 h, 24 h, 3 d, 7 d, 14 d, 30 d after publish (7 calls per post; 800 posts/day →
   5,600 insight calls/day ≈ 11/day per connected account — far under the per-token
   platform rate ceiling). Each run *appends* an `analytics_snapshots` row; nothing
   overwrites.

### 2.5 Timezones and slots

`computeNextSlots(account)` expands `schedule_rules` in the account's IANA timezone with
Luxon; Luxon resolves nonexistent local times (spring-forward gap) by shifting forward to
the next valid instant and resolves ambiguous times (fall-back) to the earlier offset —
both behaviors are pinned by tests (§7.4). A slot's identity is
`slot_key = rule_id:local_date:local_time`; `slot_claims` (PK `(ig_account_id,
slot_key)`) makes each slot fire at most once ever, across restarts, edits, and DST
transitions. A schedule or timezone edit bumps `schedule_rules.version`, which changes
future `slot_key`s; the guard against an edit creating a *second* slot at nearly the same
instant is `claimDueSlots()`'s account-level check `last_attempt_started_at < now() -
interval '5 minutes'` inside the claiming transaction.

### 2.6 Monthly cost

| Component | Arithmetic | $/mo |
|---|---|---|
| Droplet A (web) | 2 vCPU/4 GB | 24 |
| Droplet B (core+media) | 4 vCPU/8 GB | 48 |
| Droplet C (automation) | 4 vCPU/8 GB | 48 |
| Managed Postgres | 2 vCPU/4 GB + PITR | 60 |
| R2 storage | growth: 480 img/day×6 MB + 320 vid/day×70 MB = 25 GB/day ≈ 760 GB/mo; renditions (≈35% of bytes) deleted 30 days after publish; month-6 steady ≈ 3.0 TB × $0.015 | 45 |
| R2 operations | 2M class-A ($4.50/M) ≈ 0.3M actual | 2 |
| ISP proxies | 10 static IPs × $3 | 30 |
| Postmark | 10k tier | 15 |
| Stripe | 2.9% + 30¢ + 0.5% tax — revenue-proportional, not infra | — |
| Sentry, Cloudflare, domain | free tiers + $2 | 2 |
| **Total** | | **274** |

Worst realistic month (2,000 items/day sustained, video-heavy at 50%): storage growth
2.3× → R2 ≈ $105, total ≈ $335. When a workspace exceeds its plan storage allowance
(§3 `workspace_storage`), `POST /api/media/uploads` returns 402 with the quota detail and
the upload UI shows the blocked state — growth is capped by paid quota, not by hope.

---

## 3. Data model

Executable PostgreSQL 16 DDL. Conventions: every tenant-owned table carries
`workspace_id` and is covered by the RLS policy in §9.1; `updated_at` triggers and
non-enforcing indexes are elided for space, enforcing indexes are not.

```sql
-- ============ identity, workspaces, access ============
CREATE TABLE users (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  email         CITEXT NOT NULL UNIQUE,
  password_hash TEXT   NOT NULL,               -- argon2id
  state         TEXT   NOT NULL DEFAULT 'active'
                CHECK (state IN ('active','suspended','deleted')),
  is_operator   BOOLEAN NOT NULL DEFAULT FALSE,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id      UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  token_hash   BYTEA NOT NULL UNIQUE,          -- sha256(opaque 256-bit token)
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  expires_at   TIMESTAMPTZ NOT NULL            -- created_at + 30 days
);

CREATE TABLE operator_sessions (LIKE sessions INCLUDING ALL);  -- separate table: a
-- customer session token can never authenticate an operator route (§9.6)

CREATE TABLE workspaces (
  id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name       TEXT NOT NULL,
  state      TEXT NOT NULL DEFAULT 'active'
             CHECK (state IN ('active','suspended','deleting','deleted')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workspace_members (
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  user_id      UUID NOT NULL REFERENCES users(id),
  role         TEXT NOT NULL CHECK (role IN ('owner','admin','publisher')),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);
-- exactly one owner per workspace
CREATE UNIQUE INDEX one_owner_per_workspace
  ON workspace_members (workspace_id) WHERE role = 'owner';

CREATE TABLE waitlist_entries (
  id CITEXT PRIMARY KEY,                        -- the email itself: dedupe by design
  note TEXT, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  state TEXT NOT NULL DEFAULT 'waiting' CHECK (state IN ('waiting','invited','removed'))
);

CREATE TABLE invitations (
  id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID REFERENCES workspaces(id),  -- NULL = beta invite (creates workspace)
  email        CITEXT NOT NULL,
  role         TEXT NOT NULL DEFAULT 'publisher'
               CHECK (role IN ('admin','publisher')),
  token_hash   BYTEA NOT NULL UNIQUE,
  expires_at   TIMESTAMPTZ NOT NULL,            -- issue + 7 days
  used_at      TIMESTAMPTZ,                     -- single-use flag, set by conditional
  used_by      UUID REFERENCES users(id),       --   UPDATE (§4 row H-2)
  created_by   UUID NOT NULL REFERENCES users(id),
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE account_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  ig_username TEXT NOT NULL,
  requester_note TEXT,
  state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','approved','declined','invited')),
  public_reason  TEXT,                          -- shown to requester
  operator_notes TEXT,                          -- never serialized to customer DTOs;
  decided_by UUID REFERENCES users(id),         --   enforced by DTO allowlist (§4 I-5)
  decided_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE onboarding_profiles (
  workspace_id UUID PRIMARY KEY REFERENCES workspaces(id),
  operates TEXT, niche TEXT, main_goal TEXT, desired_cadence TEXT,
  completed_at TIMESTAMPTZ
);

CREATE TABLE rights_acceptances (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  user_id UUID NOT NULL REFERENCES users(id),
  policy_version TEXT NOT NULL,                 -- e.g. 'rights-2026-08'
  accepted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============ instagram accounts & tokens ============
CREATE TABLE ig_accounts (
  id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  UUID NOT NULL REFERENCES workspaces(id),
  ig_user_id    TEXT NOT NULL,                  -- Instagram's stable id
  username      TEXT NOT NULL,
  profile_pic_url TEXT,
  timezone      TEXT NOT NULL DEFAULT 'America/New_York',  -- IANA name
  state         TEXT NOT NULL DEFAULT 'connected'
                CHECK (state IN ('connected','expired','paused','disconnected','archived')),
  daily_limit   INT  NOT NULL DEFAULT 25 CHECK (daily_limit BETWEEN 1 AND 50),
  refill_target INT  CHECK (refill_target BETWEEN 1 AND 100), -- NULL = manual refill only
  paused_at     TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- one workspace may hold an IG account; reconnect reuses the same row (H-1 in §4)
CREATE UNIQUE INDEX ig_account_single_tenancy
  ON ig_accounts (ig_user_id) WHERE state <> 'archived';

CREATE TABLE ig_connections (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  token_ciphertext BYTEA NOT NULL,              -- AES-256-GCM, envelope (§9.2)
  token_key_id  TEXT  NOT NULL,
  scopes        TEXT[] NOT NULL,
  ig_expires_at TIMESTAMPTZ NOT NULL,           -- 60-day token
  refreshed_at  TIMESTAMPTZ,
  revoked_at    TIMESTAMPTZ,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_live_connection
  ON ig_connections (ig_account_id) WHERE revoked_at IS NULL;

CREATE TABLE account_daily_usage (
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  usage_date    DATE NOT NULL,                  -- in the account's timezone
  reserved      INT  NOT NULL DEFAULT 0 CHECK (reserved >= 0),
  published     INT  NOT NULL DEFAULT 0 CHECK (published >= 0),
  PRIMARY KEY (ig_account_id, usage_date)       -- durable: survives restart/cache loss
);

-- ============ media ============
CREATE TABLE media_assets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  uploader_id  UUID NOT NULL REFERENCES users(id),
  r2_key       TEXT NOT NULL UNIQUE,            -- ws/{workspace_id}/orig/{asset_id}
  content_sha256 BYTEA,
  mime  TEXT NOT NULL, bytes BIGINT NOT NULL CHECK (bytes <= 1073741824), -- 1 GB cap
  width INT, height INT, duration_s NUMERIC(8,2),
  state TEXT NOT NULL DEFAULT 'uploading'
        CHECK (state IN ('uploading','stored','rejected','deleted')),
  reject_reason TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE media_renditions (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  asset_id UUID NOT NULL REFERENCES media_assets(id),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  settings_hash BYTEA NOT NULL,                 -- sha256(canonical prep settings JSON)
  r2_key TEXT UNIQUE, mime TEXT, bytes BIGINT,
  state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','processing','ready','failed')),
  error_code TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (asset_id, settings_hash)              -- retry never duplicates a rendition
);

CREATE TABLE workspace_storage (
  workspace_id UUID PRIMARY KEY REFERENCES workspaces(id),
  bytes_used BIGINT NOT NULL DEFAULT 0 CHECK (bytes_used >= 0)
  -- maintained in the same tx as media_assets/media_renditions writes by
  -- adjustStorage(); reconciled nightly against R2 ListObjectsV2 by maintenance job
);

-- ============ queue & scheduling ============
CREATE TABLE queue_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  UUID NOT NULL REFERENCES workspaces(id),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  client_token  UUID NOT NULL,                  -- minted by the browser form
  position      NUMERIC(30,15) NOT NULL,        -- fractional ordering; weekly rebalance
  state TEXT NOT NULL DEFAULT 'preparing'
        CHECK (state IN ('preparing','ready','publishing','published',
                         'hidden','failed','needs_review','canceled')),
  -- frozen at queue time (H-6): later settings edits cannot alter these
  caption_frozen  TEXT NOT NULL DEFAULT '',
  asset_id        UUID NOT NULL REFERENCES media_assets(id),
  rendition_id    UUID REFERENCES media_renditions(id),   -- set when 'ready'
  prep_settings   JSONB NOT NULL,               -- snapshot of account prep prefs
  media_kind      TEXT NOT NULL CHECK (media_kind IN ('image','reel')),
  rights_acceptance_id UUID NOT NULL REFERENCES rights_acceptances(id),
  source_post_id  UUID REFERENCES source_posts(id),
  fail_class      TEXT,                         -- customer_fixable | outside_temporary |
  fail_detail     TEXT,                         --  permission | account_limit |
  created_by UUID NOT NULL REFERENCES users(id),--  invalid_media | uncertain
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, client_token)           -- double-submit safe (H-2)
);
CREATE INDEX queue_order ON queue_items (ig_account_id, state, position);
-- concurrent refill/collection cannot queue one source post twice per account (H-9)
CREATE UNIQUE INDEX one_source_post_per_account
  ON queue_items (ig_account_id, source_post_id) WHERE source_post_id IS NOT NULL;

CREATE TABLE schedule_rules (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  workspace_id  UUID NOT NULL REFERENCES workspaces(id),
  kind TEXT NOT NULL CHECK (kind IN ('fixed_times','interval')),
  days_mask INT  NOT NULL DEFAULT 127,          -- bit 0 = Monday
  times_local TIME[],                           -- kind = fixed_times
  interval_minutes INT CHECK (interval_minutes >= 30),
  window_start_local TIME, window_end_local TIME,
  version INT NOT NULL DEFAULT 1,               -- bumped on edit → new slot_keys (§2.5)
  active BOOLEAN NOT NULL DEFAULT TRUE,
  CHECK ((kind='fixed_times') = (times_local IS NOT NULL))
);

CREATE TABLE slot_claims (
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  slot_key      TEXT NOT NULL,                  -- rule_id:v{version}:local_date:local_time
  claimed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  publish_attempt_id UUID,
  PRIMARY KEY (ig_account_id, slot_key)         -- a slot fires at most once, ever (§2.5)
);

-- ============ publishing ============
CREATE TABLE publish_attempts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  queue_item_id UUID NOT NULL REFERENCES queue_items(id),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  workspace_id  UUID NOT NULL REFERENCES workspaces(id),
  state TEXT NOT NULL DEFAULT 'created'
        CHECK (state IN ('created','media_upload','container_wait','publishing',
                         'published','failed','uncertain',
                         'reconciled_published','reconciled_failed')),
  container_id TEXT,                            -- Instagram creation container id
  error_class TEXT, error_detail JSONB,
  started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  publishing_marked_at TIMESTAMPTZ,             -- point-of-no-return commit time
  finished_at TIMESTAMPTZ
);
-- only one active publication of a queue item exists (H-3)
CREATE UNIQUE INDEX one_active_attempt_per_item
  ON publish_attempts (queue_item_id)
  WHERE state IN ('created','media_upload','container_wait','publishing','uncertain');

CREATE TABLE receipts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  publish_attempt_id UUID NOT NULL UNIQUE REFERENCES publish_attempts(id),
  queue_item_id UUID NOT NULL REFERENCES queue_items(id),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  workspace_id  UUID NOT NULL REFERENCES workspaces(id),
  ig_media_id   TEXT NOT NULL,
  permalink     TEXT,
  caption_frozen TEXT NOT NULL,
  rendition_sha256 BYTEA,
  published_at  TIMESTAMPTZ NOT NULL,
  evidence      JSONB NOT NULL,                 -- redacted raw API responses + timings
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE library_posts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  receipt_id UUID NOT NULL UNIQUE REFERENCES receipts(id),
  ig_media_id TEXT NOT NULL UNIQUE,
  media_kind TEXT NOT NULL CHECK (media_kind IN ('image','reel')),
  state TEXT NOT NULL DEFAULT 'live'
        CHECK (state IN ('live','archived','recently_deleted','gone','unknown')),
  protected BOOLEAN NOT NULL DEFAULT FALSE,     -- cleanup can never select (§3.8 brief)
  source_post_id UUID REFERENCES source_posts(id),
  published_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE analytics_snapshots (               -- append-only; no UPDATE granted (§9.1)
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  library_post_id UUID NOT NULL REFERENCES library_posts(id),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  captured_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  reach BIGINT, views BIGINT, likes BIGINT, comments BIGINT,
  saves BIGINT, shares BIGINT,
  raw JSONB NOT NULL                            -- exact insights payload
);
CREATE INDEX snapshots_by_post ON analytics_snapshots (library_post_id, captured_at);

-- ============ restricted sourcing ============
CREATE TABLE source_pool_accounts (               -- operator-owned; NOT tenant data
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  label TEXT NOT NULL,
  session_ciphertext BYTEA,                      -- Playwright storageState, encrypted
  session_key_id TEXT,
  state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active','checkpoint','banned','retired')),
  actions_today INT NOT NULL DEFAULT 0,          -- reset by maintenance at 00:00 UTC
  last_used_at TIMESTAMPTZ
);

CREATE TABLE sources (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),  -- destination account
  kind TEXT NOT NULL CHECK (kind IN ('account','hashtag','reels_feed')),
  query TEXT NOT NULL,
  filters JSONB NOT NULL DEFAULT '{}',           -- media type/age, min likes/comments/
  check_interval_minutes INT NOT NULL DEFAULT 360 CHECK (check_interval_minutes >= 60),
  max_candidates INT NOT NULL DEFAULT 10 CHECK (max_candidates BETWEEN 1 AND 25),
  state TEXT NOT NULL DEFAULT 'pending_verification'
        CHECK (state IN ('pending_verification','active','paused','retrying','blocked')),
  last_checked_at TIMESTAMPTZ,
  created_by UUID NOT NULL REFERENCES users(id),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE source_posts (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id UUID REFERENCES sources(id),         -- NULL for one-time samples
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  shortcode TEXT NOT NULL,                       -- instagram.com/p/{shortcode}
  author_username TEXT NOT NULL,
  original_url TEXT NOT NULL,
  caption TEXT, media_type TEXT,
  like_count BIGINT, comment_count BIGINT, play_count BIGINT,
  discovered_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  media_asset_id UUID REFERENCES media_assets(id),
  state TEXT NOT NULL DEFAULT 'discovered'
        CHECK (state IN ('discovered','fetching','ready','filtered_out','failed','removed')),
  filter_reason TEXT,
  UNIQUE (ig_account_id, shortcode)              -- same post never imported twice per
);                                               --   destination account (H-9)

-- ============ managed cleanup ============
CREATE TABLE cleanup_rules (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  media_kind TEXT NOT NULL CHECK (media_kind IN ('image','reel','any')),
  min_age_days INT NOT NULL CHECK (min_age_days >= 3),
  thresholds JSONB NOT NULL,                     -- {"views_lt":500,"likes_lt":20,...}
  schedule_kind TEXT NOT NULL DEFAULT 'manual'
        CHECK (schedule_kind IN ('manual','daily','weekly')),
  run_at_local TIME, run_day INT CHECK (run_day BETWEEN 0 AND 6),
  version INT NOT NULL DEFAULT 1,
  active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE cleanup_runs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  rule_snapshot JSONB NOT NULL,                  -- frozen rule (H-6)
  analytics_as_of TIMESTAMPTZ NOT NULL,          -- staleness shown at preview
  selection_hash BYTEA,                          -- sha256(sorted selected post ids +
  state TEXT NOT NULL DEFAULT 'draft'            --        rule version) (§5.10)
        CHECK (state IN ('draft','confirmed','running','paused_uncertain',
                         'stopped','completed','invalidated')),
  requested_by UUID NOT NULL REFERENCES users(id),
  confirmed_by UUID REFERENCES users(id),
  confirmed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- later runs for the account queue behind an unfinished one (brief §3.8)
CREATE UNIQUE INDEX one_unfinished_run_per_account
  ON cleanup_runs (ig_account_id)
  WHERE state IN ('confirmed','running','paused_uncertain');

CREATE TABLE cleanup_run_items (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  run_id UUID NOT NULL REFERENCES cleanup_runs(id),
  ig_account_id UUID NOT NULL REFERENCES ig_accounts(id),
  library_post_id UUID NOT NULL REFERENCES library_posts(id),
  action TEXT NOT NULL CHECK (action IN ('archive_photo','delete_reel')),
  qualify_reason JSONB NOT NULL,                 -- metrics + thresholds that matched
  state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','executing','done','failed','uncertain','skipped')),
  executed_at TIMESTAMPTZ,
  evidence JSONB,                                -- screenshots' R2 keys, redacted DOM
  UNIQUE (run_id, library_post_id)
);
-- only one item per account may cross the destructive boundary at a time (H-4)
CREATE UNIQUE INDEX one_destructive_item_per_account
  ON cleanup_run_items (ig_account_id) WHERE state IN ('executing','uncertain');

-- ============ billing & entitlements ============
CREATE TABLE plans (
  id TEXT PRIMARY KEY,                           -- 'free' | 'creator' | 'studio'
  name TEXT NOT NULL,
  max_accounts INT NOT NULL, included_seats INT NOT NULL,
  storage_gb INT NOT NULL, max_queued_per_account INT NOT NULL,
  features TEXT[] NOT NULL                       -- e.g. '{cleanup}'
);

CREATE TABLE subscriptions (
  workspace_id UUID PRIMARY KEY REFERENCES workspaces(id),
  stripe_customer_id TEXT NOT NULL UNIQUE,
  stripe_subscription_id TEXT UNIQUE,
  plan_id TEXT NOT NULL REFERENCES plans(id) DEFAULT 'free',
  status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','trialing','past_due','canceled','incomplete')),
  extra_seats INT NOT NULL DEFAULT 0,
  current_period_end TIMESTAMPTZ,
  cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
  last_synced_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE stripe_events (
  id TEXT PRIMARY KEY,                           -- Stripe event id: replay-proof (H-13)
  type TEXT NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  processed_at TIMESTAMPTZ,
  payload JSONB NOT NULL
);

CREATE TABLE entitlement_grants (                 -- operator-granted temporary access
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  feature TEXT NOT NULL CHECK (feature IN ('sourcing','cleanup','beta_accounts')),
  granted_by UUID NOT NULL REFERENCES users(id),
  expires_at TIMESTAMPTZ NOT NULL,
  revoked_at TIMESTAMPTZ
);

-- ============ notifications, feedback, audit, jobs, deletion ============
CREATE TABLE notifications (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  kind TEXT NOT NULL, priority TEXT NOT NULL DEFAULT 'normal'
       CHECK (priority IN ('normal','high')),
  title TEXT NOT NULL, body TEXT, link_path TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  read_at TIMESTAMPTZ, dismissed_at TIMESTAMPTZ
);

CREATE TABLE feedback (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id UUID NOT NULL REFERENCES workspaces(id),
  user_id UUID NOT NULL REFERENCES users(id),
  kind TEXT NOT NULL CHECK (kind IN ('bug','confusing','idea','praise','other')),
  body TEXT NOT NULL,
  page_context JSONB NOT NULL,                   -- route, entity ids, app version
  state TEXT NOT NULL DEFAULT 'new' CHECK (state IN ('new','seen','done')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (                          -- append-only (§9.1 revokes UPDATE/DELETE)
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  actor_type TEXT NOT NULL CHECK (actor_type IN ('user','operator','system')),
  actor_id UUID,
  workspace_id UUID,
  action TEXT NOT NULL, subject_type TEXT NOT NULL, subject_id TEXT NOT NULL,
  detail JSONB NOT NULL DEFAULT '{}',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
  id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  queue TEXT NOT NULL,
  unique_key TEXT UNIQUE,                        -- e.g. 'publish:{attempt_id}':
  payload JSONB NOT NULL,                        --   double-enqueue collapses (H-2)
  state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','leased','done','failed','dead')),
  run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  attempts INT NOT NULL DEFAULT 0, max_attempts INT NOT NULL DEFAULT 3,
  lease_expires_at TIMESTAMPTZ, leased_by TEXT, last_error TEXT
);
CREATE INDEX jobs_lease_scan ON jobs (queue, state, run_at);

CREATE TABLE rate_limit_counters (
  key TEXT NOT NULL, window_start TIMESTAMPTZ NOT NULL,
  count INT NOT NULL DEFAULT 0,
  PRIMARY KEY (key, window_start)                -- fixed 1-minute windows (§9.5)
);

CREATE TABLE deletion_requests (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  reference_code TEXT NOT NULL UNIQUE,           -- shown to the user; survives deletion
  workspace_id UUID, user_id UUID,               -- no FK: rows outlive their subjects
  requested_by_email_hash BYTEA NOT NULL,        -- sha256; minimum non-identifying proof
  steps JSONB NOT NULL DEFAULT '{}',             -- {"revoke_ig":"done","stripe":"done",
  state TEXT NOT NULL DEFAULT 'pending'          --  "r2_purge":"in_progress",...}
        CHECK (state IN ('pending','in_progress','completed')),
  requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  completed_at TIMESTAMPTZ
);
```

Constraints that live in code, with the function named: fractional `position` rebalance —
`rebalanceQueuePositions()` (maintenance queue, weekly, and on gap < 10⁻⁶); storage
accounting — `adjustStorage()` called inside every media asset/rendition state
transaction; plan-limit checks (accounts, seats, queued items, storage) —
`assertWithinEntitlements()` called in the same transaction as the INSERT it guards,
reading `plans` + `entitlement_grants` + current counts under the workspace row lock
(`SELECT … FROM workspaces WHERE id=$1 FOR UPDATE`) so two concurrent "connect account"
requests cannot both pass a limit of one remaining slot.

---

## 4. Invariant enforcement map

Promises P1–P5 (brief §1) and hard rules H-1…H-15 (brief §4). Every mechanism is a named
schema object, transaction, or function from §2/§3; every evidence entry is a test that
exists in the suite named in §7.

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| P1a Publishes only intended content to intended account | `queue_items` frozen columns (`caption_frozen`, `rendition_id`, `prep_settings`, `ig_account_id`); `runPublishAttempt()` reads only these, never live account settings | `test/publish/frozen.int.ts`: queue an item, change account prep settings + caption template, publish against fake-IG, assert the container call carried the frozen caption and the rendition's presigned key |
| P1b Never publishes twice on double-click | `UNIQUE (workspace_id, client_token)` on `queue_items`; `one_active_attempt_per_item` partial unique index; `jobs.unique_key` | `test/publish/doubleclick.int.ts`: fire 20 concurrent POSTs with one client_token → exactly 1 queue item; fire 20 concurrent "publish now" → exactly 1 attempt row |
| P1c Never publishes twice across workers/restarts/deploys | Attempt state machine: reaper re-runs only states before `publishing`; `publishing_marked_at` committed before the `media_publish` call (§2.4) | `test/publish/killmatrix.int.ts`: SIGKILL the worker at each of 6 injected fault points (env `FAULT_POINT`), restart, drain; assert fake-IG received ≤ 1 `media_publish` per item in every case |
| P2a Queue tells the truth (visible states) | `queue_items.state` CHECK covers preparing/ready/publishing/published/hidden/failed/needs_review; every transition function (`markReady()`, `markPublished()`, …) commits state + NOTIFY in one transaction | `test/queue/state-visibility.int.ts`: walk an item through its lifecycle; after each transition assert the API list response and an open SSE stream both reflect it |
| P2b No silent disappearance or account jump | `queue_items.ig_account_id` has no UPDATE path in any code route (verified by grep-rule lint `no-update-igaccount` in CI); removal is `state='canceled'`, never DELETE (DELETE revoked from app role, §9.1) | `test/queue/immutable-destination.int.ts`: attempt account move via every mutation endpoint → 422; SQL DELETE as app role → permission denied |
| P3 Uncertainty never repeats a destructive action | Reaper rule in `reapExpiredLeases()`: publish lease expired AND attempt.state='publishing' → attempt:='uncertain', enqueue `reconcile` (not `publish`); `one_active_attempt_per_item` includes 'uncertain' so no new attempt can start | `test/publish/uncertain-no-retry.int.ts`: kill after fault point FAULT_AFTER_PUBLISH_SENT with fake-IG returning nothing; assert no second `media_publish` arrives within a full reaper+retry cycle and item lands in needs_review |
| P4 Holding/leaving never destroys work | Pause/suspend/disconnect/downgrade code paths touch only `state` columns (`pauseAccount()`, `suspendWorkspace()`, `applyDowngrade()`); FKs are RESTRICT (no cascade from accounts/workspaces to queue/history); app DB role has no DELETE on content tables | `test/holds/no-destruction.int.ts`: seed queue+history, then pause, disconnect, suspend, downgrade, expire token; assert identical row counts and payloads after each |
| P5 Private access stays private | Token/session ciphertext columns readable only by roles that need them (§9.1 grants); DTO allowlist serializers (`toAccountDto()` etc.) — raw rows never JSON-serialized; pino `redact` on `authorization`, `token`, `cookie` paths; receipts schema has no token column | `test/privacy/dto-snapshot.test.ts`: snapshot every API response schema, CI fails on new fields; `test/privacy/log-canary.int.ts`: run a full publish with a canary token value, grep captured logs/receipts/notifications for it → zero hits |
| H-1 Tenant isolation everywhere | Postgres RLS policy `workspace_id = current_setting('app.workspace_id')::uuid` on all tenant tables, set per request/job (§9.1); R2 keys prefixed `ws/{workspace_id}/` and presigner `signMediaUrl()` asserts prefix matches session workspace; SSE fan-out filters on workspace_id | `test/security/cross-tenant-fuzz.int.ts`: two seeded workspaces; session A replays every GET/POST route with B's ids → all 404; A requests presign for B's key → 403; A's SSE stream receives zero B events during B activity |
| H-2 Repeat-safe publish/charge/archive/delete/invite/access | Publish: P1b/P1c; charge: `stripe_events.id` PK + `syncSubscriptionFromStripe()`; archive/delete: `one_destructive_item_per_account` + item state; invite: `UPDATE invitations SET used_at=now(), used_by=$u WHERE id=$i AND used_at IS NULL AND expires_at>now() RETURNING id` — zero rows = reject; access grants: `entitlement_grants` insert with audit row | `test/idempotency/replay-suite.int.ts`: replay each action 10× concurrently, assert single effect (1 invite acceptance, 1 subscription state, 1 archive) |
| H-3 One active publication crosses the side-effect boundary | `claimDueSlots()` transaction: `SELECT … FROM ig_accounts … FOR UPDATE SKIP LOCKED` → `INSERT slot_claims ON CONFLICT DO NOTHING` (abort if conflict) → pick head item `FOR UPDATE SKIP LOCKED` → `INSERT publish_attempts` (partial unique) → `INSERT jobs` — one commit | `test/publish/race.int.ts`: run 4 scheduler processes against one due account for 1,000 simulated slots → attempts == slots, no unique-violation leaks to callers |
| H-4 One cleanup item per account crosses the destructive boundary | `one_destructive_item_per_account` partial unique index; `executeCleanupItem()` sets `state='executing'` in its own committed transaction before any Playwright navigation | `test/cleanup/serialize.int.ts`: start two cleanup workers on one account; second `executing` INSERT gets unique violation and requeues; fake-instagram-web records strictly serial actions |
| H-5 Pre-action failures retry; ambiguous post-action failures reconcile first | Attempt states before `publishing` → retryable (jobs.attempts < max); `publishing`/`uncertain` → only `reconcileAttempt()` may transition them; same split for cleanup items (`pending` retryable, `executing`→`uncertain` not) | `killmatrix.int.ts` (above) asserts the retry/reconcile split per fault point |
| H-6 Approved inputs are frozen for the approved action | Queue: frozen columns (P1a); cleanup: `rule_snapshot` + `selection_hash` verified by `startCleanupRun()` recomputing the hash over current selection before first item (§5.10); billing: Checkout session carries price id, portal changes resync from Stripe | `test/cleanup/selection-hash.int.ts`: confirm a run, protect one selected post, start run → state='invalidated', zero items executed |
| H-7 Held, not destroyed, on pause/disconnect/suspend/over-plan/over-quota/reauth | `claimDueSlots()` WHERE clause: `a.state='connected' AND w.state='active' AND withinEntitlements(a)`; excluded work stays `ready` untouched | `no-destruction.int.ts` (P4) plus assertion that after recovery (resume/reconnect/upgrade) the same items publish in order |
| H-8 Daily usage survives restart/cache loss | `account_daily_usage` row incremented (`reserved` at claim, `published` at receipt) inside the claiming/receipt transactions — no in-memory counter exists | `test/quota/restart.int.ts`: publish 3, SIGKILL all workers, restart, assert claims stop at daily_limit and `reserved+published` matches attempts |
| H-9 No duplicate queueing from concurrent collection/refill | `UNIQUE (ig_account_id, shortcode)` on `source_posts`; `one_source_post_per_account` on `queue_items`; `refillQueue()` wraps in `pg_advisory_xact_lock(hashtextextended(ig_account_id::text,0))` | `test/sourcing/concurrent-refill.int.ts`: two collectors discover the same 20 shortcodes, two refills race to target depth → no duplicate source_posts, no duplicate queue items, depth == target |
| H-10 Restricted features unreachable without entitlement | `requireEntitlement(feature)` middleware on all sourcing/cleanup routes returns 404 (not 403 — invisible); worker guard: `leaseJobs()` join drops source/cleanup jobs whose workspace lacks a live grant (they stay pending) | `test/entitlements/invisible.int.ts`: non-entitled session calls every restricted route → 404 and no `sources` row; revoke grant mid-run → acquisition jobs hold, accepted `source_posts` rows remain |
| H-11 Customer media private; temporary access narrow and expiring | R2 bucket has no public access; user reads via 10-min presigned GET; Instagram fetch via 60-min presigned GET minted per attempt in `media_upload` and scoped to one rendition key | `test/media/presign.int.ts`: unsigned GET → 403; expired signature (clock-skewed signer) → 403; presign request for another workspace's key → 403 |
| H-12 Grants/sessions never reach browser, receipts, or logs | P5 mechanisms; additionally `cleanup_run_items.evidence` written by `redactEvidence()` (allowlist of fields; screenshots taken with Playwright `page.screenshot` after `addStyleTag` masking the session UI chrome) | `log-canary.int.ts` extended with a canary cookie value through a full cleanup run: zero hits in evidence JSONB, notifications, and logs |
| H-13 Rights acceptance attributable and versioned | `queue_items.rights_acceptance_id NOT NULL` FK → `rights_acceptances(user_id, policy_version, accepted_at)` | `test/rights/required.int.ts`: queueing without acceptance id → 422; receipt joins back to acceptance row with the policy version active at queue time |
| H-14 Repeated/out-of-order external callbacks are safe | Stripe: `stripe_events.id` PK (replay no-ops) + refetch-latest pattern (order-independent); deletion requests: `deletion_requests.state` machine, `advanceDeletion()` steps idempotent (§5.13); IG has no callbacks in this design (polling only) | `test/billing/reorder.int.ts`: deliver a real recorded event sequence shuffled and duplicated ×3 → final `subscriptions` row equals Stripe test-clock truth |
| H-15 Every final claim has inspectable evidence | `markPublished()`, `finishCleanupItem()`, `applyStripeEvent()`, `advanceDeletion()` each insert their evidence row (receipt / run-item evidence / stripe_events / deletion steps) in the same transaction as the final state; a terminal state cannot commit without it | `test/evidence/coverage.int.ts`: after the full E2E suite, SQL asserts zero terminal-state rows lacking their evidence row |

---

## 5. Failure-mode walkthrough

Fault points are compiled-in named hooks (`failpoint('name')`) active only when
`FAULT_POINT` is set — the kill-matrix tests in §7 use exactly these names.

### 5.1 Crash before publish
**Scenario:** core-worker dies (OOM) after leasing a publish job, before any Instagram call.
1. The job row is `leased`, `lease_expires_at = lease_time + 5 min`; the attempt row is `created`.
2. Worker heartbeats stop; nothing else changes — the open transaction, if any, rolled back with the connection.
3. `reapExpiredLeases()` (every 60 s) finds the expired lease: attempt state `created` is before `publishing`, so it sets the job `pending`, `attempts+1`.
4. Any worker leases it again; `runPublishAttempt()` proceeds from `created`; the daily-quota reservation from `claimDueSlots()` is still held, so no re-reservation occurs.
5. Publish completes; receipt written.
**Evidence:** `jobs.attempts = 2` with `last_error` recording the lease expiry; `publish_attempts` shows one row, `started_at` → `finished_at` spanning the crash; exactly one receipt.

### 5.2 Crash after a publish may have been accepted
**Scenario:** worker sends `media_publish`, the process is SIGKILLed before reading the response.
1. Before the HTTP call, the transaction committing `state='publishing'`, `publishing_marked_at=now()` completed (§2.4 point-of-no-return marker).
2. Lease expires; reaper sees attempt state `publishing` → sets attempt `uncertain`, queue item `needs_review` is *not* yet set (reconciliation may resolve silently first), enqueues `reconcile:{attempt_id}` (unique_key).
3. `reconcileAttempt()` calls `GET /{container_id}?fields=status_code`. `PUBLISHED` → walks `GET /{ig-user-id}/media?fields=id,timestamp,caption` for a media whose timestamp ≥ `publishing_marked_at` and caption equals `caption_frozen`; found → attempt `reconciled_published`, receipt + library row written with `evidence.reconciled=true`. `ERROR`/`EXPIRED` → the publish did not happen → attempt `reconciled_failed`, item back to `ready` (retry is now safe: container consumed nothing).
4. If the status call itself fails (network, 5xx), the reconcile job reschedules with backoff 1/2/4/8/16/32 min (6 tries).
5. After the 6th inconclusive try, item → `needs_review`, high-priority notification with deep link, operator sees the attempt timeline. The `one_active_attempt_per_item` index (uncertain counts as active) blocks any republish until an operator or a later successful reconcile resolves it.
**Evidence:** `publish_attempts` row with `publishing_marked_at` set and state `reconciled_published|reconciled_failed|uncertain`; the reconcile job's attempt trail in `jobs`; the receipt's `evidence.reconciled` flag; the notification row.

### 5.3 Duplicate publish work
**Scenario:** a deploy overlap leaves two core-workers polling; both want the same due slot.
1. Both run `claimDueSlots()`; `FOR UPDATE SKIP LOCKED` on the account row means the second process skips the locked account entirely.
2. If the tick straddles processes in time instead, both compute `slot_key = rule:v3:2026-08-16:19:00`; the second `INSERT INTO slot_claims … ON CONFLICT DO NOTHING` inserts zero rows and that code path aborts before creating an attempt.
3. Had both somehow enqueued, `jobs.unique_key='publish:{attempt_id}'` and the `one_active_attempt_per_item` index each independently collapse the duplicate.
**Evidence:** one `slot_claims` row per slot; attempt count == claimed-slot count in `test/publish/race.int.ts` output; fake-IG call log shows one container per item.

### 5.4 Media preparation failure
**Scenario:** ffmpeg exits 1 on a corrupt MP4 during rendition of a queued item.
1. The media job catches the non-zero exit; `media_renditions` row → `failed`, `error_code='transcode_failed'`, stderr tail stored.
2. Retry policy (2 retries, 1 m / 10 m) re-runs; same failure → job `dead`.
3. `queue_items` row → `failed`, `fail_class='customer_fixable'`, `fail_detail` carries the probe summary ("video stream unreadable at 00:12"); notification (normal priority) links to the item.
4. User replaces the media on the item; `retryPreparation()` inserts a *new* rendition row keyed `(new_asset_id, settings_hash)` — the `UNIQUE (asset_id, settings_hash)` plus the unchanged queue item id mean no duplicate queue entry can result.
5. Rendition `ready` → item `ready`; it rejoins the queue at its existing `position`.
**Evidence:** the failed and ready rendition rows side by side; `jobs` trail with 3 attempts then `dead`; item history shows failed → ready without a second item id.

### 5.5 Account revocation
**Scenario:** the customer changes their Instagram password; the token dies mid-day with 4 items queued.
1. The next API call (publish or insights) returns OAuth error 190; the caller runs `markConnectionExpired()`: `ig_connections.revoked_at=now()`, `ig_accounts.state='expired'`, one transaction.
2. `claimDueSlots()`'s `a.state='connected'` predicate now excludes the account; queued items stay `ready`, untouched (H-7).
3. An in-flight attempt at `media_upload`/`container_wait` fails its next call with 190 → attempt `failed`, `error_class='permission'`, item back to `ready` (no side effect occurred; a 190 on `media_publish` itself cannot have published).
4. High-priority notification + email: "Reconnect @handle"; the account card shows the expired state and a reconnect button.
5. Reconnect runs OAuth; callback matches `ig_user_id` to the existing row (`ig_account_single_tenancy` index): same workspace → new `ig_connections` row, state `connected`, queue and history intact; different workspace → blocked with "this account is already managed in another workspace — contact support".
6. Publishing resumes at the next slot; missed slots are already-claimed or unclaimed keys in the past, which `claimDueSlots()` never fires retroactively (slots older than 15 min are skipped, claim recorded with `publish_attempt_id NULL`).
**Evidence:** `ig_connections` rows old (revoked_at set) and new; audit_log `connection.expired` / `connection.reconnected`; untouched `queue_items.created_at`/`position`; skipped-slot claims with null attempt.

### 5.6 Quota exhaustion
**Scenario:** an account with `daily_limit=25` hits its 25th publish at 14:00; 3 more items are ready; Instagram's own rolling limit also matters.
1. `claimDueSlots()`'s quota UPDATE — `SET reserved=reserved+1 WHERE published+reserved < daily_limit` — matches zero rows for slot 26; the transaction aborts the claim *before* inserting `slot_claims`, so the slot re-evaluates tomorrow's key naturally.
2. The account card shows "daily limit reached — next post {tomorrow 09:00 local}", computed from the same data the scheduler reads.
3. Before every container creation, `runPublishAttempt()` also calls `GET /{ig-user-id}/content_publishing_limit`; if `quota_usage >= quota_total` (Instagram's rolling 24 h count of 50, which includes posts made outside our product), attempt → `failed` with `error_class='account_limit'`, item → `ready`, and a `defer` job re-checks in 60 min — deferral, not permanent failure.
4. No item state was destroyed; order is preserved because items were never dequeued.
**Evidence:** `account_daily_usage` row `(reserved+published)=25`; zero slot_claims past the limit; attempt rows with `error_class='account_limit'` and the raw `content_publishing_limit` response in `error_detail`.

### 5.7 Schedule edit near a slot
**Scenario:** at 18:59:40 the user edits the 19:00 daily rule to 19:30, while the scheduler tick runs at 19:00:00.
1. The edit transaction bumps `schedule_rules.version` 3→4 and commits at 18:59:41.
2. The 19:00 tick reads version 4; the only computable key is `rule:v4:…:19:30` — the 19:00 v3 slot no longer exists, so nothing fires at 19:00.
3. Race variant (tick read v3 just before commit): the tick claims `rule:v3:…:19:00` — a slot the user's pre-edit configuration legitimately owned; the edit cannot retract a claim already inserted. The 19:30 v4 slot then also becomes due; the account-level guard in `claimDueSlots()` (`last_attempt_started_at < now() - 5 min`) is satisfied (30 min apart) so both fire — which matches the user's visible preview showing 19:30 as the next run after the in-flight 19:00. What can never happen is two fires *within* 5 minutes: the guard predicate is inside the claiming transaction under the account row lock.
4. DST variants: spring-forward 02:30 rule → Luxon returns 03:30 local, key `…:02:30` (key uses configured local time, so no duplicate when the clock normalizes); fall-back 01:30 → earlier offset only, one key, one claim.
**Evidence:** `slot_claims` keys carry rule version and local time; `test/schedule/edit-race.int.ts` and `test/schedule/dst.test.ts` pin all four sequences; preview endpoint output equals scheduler behavior (same `computeNextSlots()`).

### 5.8 Duplicate source collection
**Scenario:** two source checks for overlapping hashtags discover the same Reel; simultaneously auto-refill and a manual refill race.
1. Both checks `INSERT INTO source_posts … ON CONFLICT (ig_account_id, shortcode) DO NOTHING`; one row survives with the first discoverer's `source_id`.
2. Media fetch for that row is a single job (`unique_key='srcmedia:{source_post_id}'`) — no double download.
3. Both refills call `refillQueue()`; `pg_advisory_xact_lock` on the account id serializes them; the second sees depth already at target and inserts nothing.
4. Even if the lock were bypassed by a future code path, `one_source_post_per_account` on `queue_items` makes the second insert of the same source post a caught conflict, skipped.
**Evidence:** one `source_posts` row, one media asset, queue depth == `refill_target`; `concurrent-refill.int.ts` asserts these counts under 2-way races.

### 5.9 Browser hang during cleanup
**Scenario:** Chromium freezes after clicking "Archive" on item 4 of 9; the click may have landed.
1. `executeCleanupItem()` had committed item 4 `state='executing'` before navigation (H-4 index now holds the account).
2. The Playwright action wraps in a 3-min timeout; on expiry the worker force-closes the context (`browserContext.close()`), and because the destructive click had been issued (`failpoint`-adjacent marker `click_sent_at` committed just before `page.click`), the item → `uncertain`, run → `paused_uncertain`.
3. No automatic retry: the `one_destructive_item_per_account` index treats `uncertain` as occupying the account; items 5–9 stay `pending`; later scheduled runs queue behind via `one_unfinished_run_per_account`.
4. Reconciliation is *read-only*: a fresh context loads the post's permalink and the account's Archived list; post absent from feed and present in Archive → item `done` with `evidence.reconciled=true`; still live → item `pending` again (the click never landed; pre-action failures may retry), run resumes.
5. Ambiguous read (page unreadable) → item stays `uncertain`, high-priority notification; the operator console shows the pre-click screenshot and lets the operator mark the true outcome, which is an audited action.
**Evidence:** run timeline `running → paused_uncertain (item 4)`; `cleanup_run_items.evidence` with `click_sent_at`, pre-click screenshot key, reconcile result; audit_log if an operator resolved it.

### 5.10 Changed cleanup selection
**Scenario:** user confirms a 9-item selection; before the run starts, a teammate protects 2 of the posts and fresh analytics move a 3rd above threshold.
1. Confirmation stored `selection_hash = sha256(sorted library_post_ids + rule version)` at `confirmed_at`.
2. `startCleanupRun()` — first thing after leasing — recomputes the selection from the *frozen* `rule_snapshot` against current `protected` flags and the same `analytics_as_of` snapshot boundary, hashes it, and compares.
3. Hashes differ (the protections changed membership) → run `state='invalidated'`, zero items executed, notification "selection changed since you confirmed — review again" deep-linking to a fresh preview.
4. The fresh preview re-derives qualification and shows per-item reasons; a new confirm writes a new run row; the invalidated row remains as history.
**Evidence:** invalidated run row with both hashes in `rule_snapshot`/detail; zero `cleanup_run_items` in state `done`; the successor run's new `selection_hash`; audit_log `cleanup.invalidated`.

### 5.11 Repeated or out-of-order billing events
**Scenario:** Stripe delivers `invoice.payment_failed`, then `customer.subscription.updated` (older), then the same `payment_failed` again after a webhook-endpoint restart.
1. Each delivery: verify `Stripe-Signature` (5-min tolerance) → `INSERT INTO stripe_events ON CONFLICT (id) DO NOTHING`; the third delivery inserts zero rows and returns 200 immediately.
2. For each *new* event, the handler enqueues `billing` job `sync:{subscription_id}` (unique_key) — the event's payload is never applied directly.
3. `syncSubscriptionFromStripe()` calls `GET /v1/subscriptions/{id}` and upserts `subscriptions` from that authoritative snapshot; ordering of pings is irrelevant because every sync converges on current truth.
4. `status='past_due'` flows into `assertWithinEntitlements()`/`claimDueSlots()` exactly like a downgrade: activity above free-plan limits is held (`ready`, unclaimed), nothing deleted (H-7); the billing page shows past-due with the consequence text.
5. Payment retry succeeds → next ping → sync → `active` → held work resumes at the next slot.
**Evidence:** `stripe_events` rows with one `processed_at` per unique id; `subscriptions.last_synced_at` monotone; `reorder.int.ts` equality against Stripe test-clock state; held items' unchanged rows.

### 5.12 Storage failure
**Scenario:** R2 returns 503s for 40 minutes during the evening publish burst.
1. Uploads: browser multipart parts fail; the client retries parts (3× per part); on exhaustion the asset stays `uploading` with a visible stalled indicator and a resume button (uploadId persisted client-side; server row untouched — no duplicate item exists because no queue item is created until the asset is `stored`).
2. Media prep: ffmpeg's R2 GET fails → normal retry ladder (1 m / 10 m); renditions stay `pending`.
3. Publishing: Instagram's fetch of the presigned URL fails → container `status_code=ERROR` → attempt `failed` with `error_class='outside_temporary'`, item → `ready`, re-claimed at the next slot (pre-side-effect, safe).
4. Nothing enters `needs_review` from a storage outage alone: every R2-dependent step precedes the point of no return.
5. Backlog drains through the normal pools when R2 recovers; the publish burst finishes late (up to outage + 13-min drain), which the queue displays as attempt timestamps, not as silence.
**Evidence:** attempt rows with container ERROR payloads in `error_detail`; `jobs.attempts` counts; zero uncertain attempts in the window; R2 5xx counts in the maintenance job's daily rollup row.

### 5.13 Deletion interrupted halfway
**Scenario:** workspace deletion crashes after revoking Instagram access and canceling Stripe, before purging R2.
1. `requestDeletion()` (owner-only, password re-entry) writes `deletion_requests` (`reference_code` returned to the user), sets workspace `state='deleting'` — all activity gates (H-7 predicates) now exclude it.
2. `advanceDeletion()` executes ordered steps, each recording `steps.{name}='done'` in its own transaction: `revoke_ig` (best-effort token revoke call, then delete `ig_connections` rows) → `stripe_cancel` (`DELETE /v1/subscriptions/{id}`, idempotent on already-canceled) → `r2_purge` (ListObjectsV2 on `ws/{id}/`, batched DeleteObjects, loop until empty) → `db_purge` (deletes in FK order) → `tombstone`.
3. Crash during `r2_purge`: the deletion job's lease expires; reaper requeues (`deletion` queue allows unlimited attempts, 15 min apart); `advanceDeletion()` resumes at the first step not marked `done` — every step is a converging operation (revoking revoked tokens, canceling canceled subscriptions, deleting deleted keys all no-op).
4. `db_purge` removes personal data and content rows; `deletion_requests` itself keeps only `reference_code`, `email_hash`, timestamps, step log — the minimum non-identifying proof.
5. Status page `/deletion/{reference_code}` (unauthenticated, code is a 128-bit secret) shows the step log until `completed`, then a completion statement.
**Evidence:** the tombstoned `deletion_requests` row; R2 `ListObjectsV2` on the prefix returns empty; `SELECT count(*)` by workspace id across tenant tables returns zero; Stripe dashboard shows the canceled subscription.

---

## 6. AI strategy

**AI is not in v1.** Every decision the product makes — what publishes next, when, whether
a source post qualifies, whether a cleanup item is selected — is a deterministic function
of user-entered rules over recorded data, and §4 depends on that determinism: frozen
inputs and replayable selections cannot contain a nondeterministic model call, and an
uncertain publish outcome must be resolved by evidence, not inference. The brief's only
tempting insertion points are caption suggestions and source-post ranking; both are
deferred because each adds a per-item marginal cost (at 800 items/day, even $0.002/item
is $48/month — 18% of the infrastructure budget) and a privacy review (customer captions
and sourced content leaving our boundary) while removing nothing from the critical path.
Caption assembly (`prep_settings.caption_template`: prefix + body + credit line + hashtag
block) is string templating. This section is intentionally one paragraph: deterministic
behavior is the product promise, so the AI budget for v1 is $0.

---

## 7. Testing and release confidence

### 7.1 On every change (CI, blocks merge, target < 10 min)
- `tsc --noEmit`, ESLint (including the custom rules named in §4: `no-update-igaccount`,
  DTO-serializer allowlist rule), Prettier check.
- Unit suites (`*.test.ts`): slot computation, caption assembly, filter evaluation,
  selection hashing, redaction — pure functions, no I/O.
- Integration suites (`*.int.ts`) against **real Postgres 16 in Docker (testcontainers)**
  — every §4 evidence test runs here, including the concurrency races (multiple worker
  processes forked inside the test) and the kill-matrix (SIGKILL at each `failpoint`).
- **fake-IG**: an in-repo HTTP server implementing the exact endpoints of §1.6 (container
  lifecycle with configurable IN_PROGRESS delays, error and *no-response* modes, quota
  endpoint) plus **fake-instagram-web**: static DOM fixtures for the automation flows,
  served to Playwright. Both are contract-pinned: recorded real responses (redacted) are
  replayed as fixtures, and a fixture drift test fails if code reads a field no fixture has.
- Accessibility gate: axe-core against the 12 core pages' Storybook stories (queue,
  composer, connect, billing, cleanup preview, empty/error variants); keyboard-only
  Playwright walk of add→reorder→publish-now.

### 7.2 Requiring real supporting services (CI nightly + on release branch)
- Stripe test mode: `stripe` CLI replays the recorded event corpus in-order, shuffled,
  and duplicated (`reorder.int.ts` corpus) against a live webhook endpoint; test clocks
  drive renewal, payment failure, downgrade.
- R2: multipart upload/resume/presign-expiry suite against a dedicated test bucket.
- Postmark sandbox: template render + delivery API contract.

### 7.3 Nightly drills (staging environment, scripted, results posted to the ops channel)
- Crash drill: chaos runner SIGKILLs random workers every 2 min for 30 min under a
  600-item synthetic burst against fake-IG; asserts: zero duplicate `media_publish`
  calls, zero lost items, all uncertain attempts reconciled or flagged.
- Quota drill: drives an account to 25 and to a fake-IG rolling-limit rejection.
- Restore drill (**automated, weekly**): restores the newest production `pg_dump` into a
  scratch database, runs row-count + checksum assertions per table and a sample media
  HEAD against R2; failure pages the founder. Monthly, the founder additionally executes
  the written PITR runbook end-to-end (restore-to-timestamp on a fork) and files the
  completion note — a green health screen never substitutes for this file existing.
- DST suite runs with `TZ` pinned dates 2026-03-08 and 2026-11-01 for
  America/New_York, America/Chicago, and Europe/London accounts.

### 7.4 Against safe real Instagram accounts (weekly, and before any release touching publish/automation)
- A staging Meta app + 2 company-owned professional test accounts: full
  connect → upload → prepare → queue → publish → receipt → insights pass for one image
  and one Reel; assertions run against the real permalink and insights payload.
- Ambiguous-outcome drill: publish with the response dropped at the network layer
  (proxy-injected), verify reconciliation finds the real post.
- Cleanup drills (archive photo, delete Reel, stop mid-run) run **only** against a
  company-owned throwaway account, never a customer account; the Recently-Deleted
  recovery-window behavior is re-verified here quarterly since Instagram can change it.
- Sourcing smoke: one hashtag check through the real pool against a company-owned target,
  validating selector health; selector breakage flips `automation_flags.sourcing_enabled`
  off automatically when the smoke fails twice consecutively.

---

## 8. Delivery phases

Vertical slices; each closes on verifiable evidence, never on a date. **Phase 4 is the
first end-to-end run** (connect → upload → prepare → queue → publish → receipt →
analytics against a safe test account). Phases 0–3 are justified beneath it.

### Phase 0 — Steel thread of operations
Repo, CI (7.1 skeleton), three droplets + managed Postgres provisioned by a checked-in
script, deploy pipeline, SOPS secrets, error tracking, nightly `pg_dump` to R2, the
restore drill script, RLS scaffolding and the `jobs` table with `leaseJobs()`/reaper.
*Justification before E2E:* every later phase's exit evidence is "deployed and drilled",
which is unprovable without deploy + backup + queue machinery existing first.
**Exit criteria:** a commit auto-deploys all three processes · a job enqueued in
production executes on droplet B · the weekly restore drill has passed once against a
real dump · SIGKILLing the worker mid-job produces a reaped, re-run job.

### Phase 1 — Identity, workspaces, invites
Waitlist + public pages (privacy/terms/copyright/security-contact/deletion — required
public before onboarding), signup via single-use expiring invite, sessions, workspace
create/join, roles, onboarding profile, suspension gates, audit log, notification
plumbing + SSE.
*Justification:* tenancy (RLS, roles, invite gate) is the substrate every table in §3
references; retrofitting `workspace_id` and RLS after content exists is the highest-risk
migration this product could attempt.
**Exit criteria:** public signup without an invite is impossible (test H-2 invite row
passes) · cross-tenant fuzz suite passes · a suspended workspace's sessions can read but
every mutation returns the suspended state · axe-core gate green on auth/onboarding pages.

### Phase 2 — Connect Instagram
OAuth connect (Instagram Login API), account requests + operator review queue, token
encryption + refresh job, connection health card, pause/disconnect/reconnect,
`ig_account_single_tenancy`, Meta App Review submission for the three scopes.
*Justification:* App Review is the longest external lead time in the plan (risk R-1);
it must start as early as a demoable connect flow exists.
**Exit criteria:** a real test account connects, shows identity/health, disconnects, and
reconnects preserving its row · the same account cannot join a second workspace ·
token-expiry simulation flips state to `expired` and holds (not fails) queued work ·
App Review submitted (approval itself gates Phase 4's beta exit, not this phase).

### Phase 3 — Media and queue
Multipart upload to R2 with resume, validation/rejection reasons, prep pipeline
(renditions, settings hash), rights acceptance, queue CRUD with fractional ordering,
hide/restore/bulk actions, schedule rules + preview, storage accounting + quota block.
*Justification:* publishing (Phase 4) consumes `ready` items with frozen renditions;
building publish first would mean publishing unprepared media with no order to draw from.
**Exit criteria:** a 500 MB upload survives a page close and resumes · a corrupt file
shows its reject reason · rendition retry produces no duplicate rows (§5.4 test) ·
reorder under two concurrent sessions converges with no lost items · schedule preview
equals `computeNextSlots()` output for the DST fixture dates.

### Phase 4 — Publish, receipts, analytics **(first E2E)**
Scheduler tick, slot claims, quota reservation, publish state machine, reconciliation,
receipts, library, insight refresh ladder, needs-review flow, publish-now, live SSE
status, the §4 kill-matrix and race suites, weekly real-account run (§7.4).
**Exit criteria:** the full §7.4 pass against both test accounts, image + Reel · the
kill-matrix shows ≤ 1 `media_publish` per item at every fault point · the dropped-response
drill lands in `reconciled_published` with evidence · daily-limit and rolling-limit
drills defer rather than fail · 600-item synthetic burst drains inside 15 min.

### Phase 5 — Billing and entitlements
Stripe Checkout/portal/Tax, webhook sync, plans table, `assertWithinEntitlements()` on
every guarded insert, downgrade/past-due holds, billing page with consequences,
operator temporary grants.
**Exit criteria:** the shuffled/duplicated event corpus converges (`reorder.int.ts`) ·
test-clock renewal, failure, downgrade each land in one correct `subscriptions` row ·
a downgrade below current usage holds excess accounts' publishing and deletes nothing ·
free-plan workspace hits every limit with a visible blocked state, not an error.

### Phase 6 — Operator console, feedback, export, deletion
Operator area (separate hostname, TOTP), waitlist/invite/request/suspension management,
per-item stuck-work inspector (attempt timeline + safe-retry button that refuses
post-side-effect states), health screen, feedback intake, data export (zip job, 24 h
link), full deletion flow (§5.13).
*Beta opens at the end of Phase 6* — the brief's launch requirements (operator without a
dev console, deletion, public policy pages, backups+rehearsal) are all green here.
**Exit criteria:** the operator resolves a seeded uncertain publish and a stuck media job
entirely from the console · safe-retry on a `publishing`-state attempt is refused by the
UI *and* the API · export zip round-trips media checksums · deletion drill (crash at
each step) completes with an empty R2 prefix and a tombstone only.

### Phase 7 — Restricted sourcing (entitled testers)
Pool-account management, source CRUD + verification, checks via Playwright, filters,
backlog, refill (manual + target-depth), one-time samples, provenance display, kill
switch, entitlement gating (invisible without grant), abuse floors.
**Exit criteria:** H-9/H-10 suites pass · revoking the entitlement mid-day stops
acquisition within one lease cycle and strands nothing · pool-account checkpoint state
quarantines the account and reroutes checks · a takedown request path removes media and
tombstones the source post.

### Phase 8 — Managed cleanup (entitled)
Session onboarding for entitled customers (§9.3), protection flags, rules, preview with
reasons + analytics freshness, confirm-with-hash, serialized execution, stop, scheduled
runs with pre-flight rechecks, uncertainty pause + read-only reconcile, evidence
redaction.
**Exit criteria:** §5.9 and §5.10 walkthrough tests pass against fake-instagram-web ·
the real-account drill archives a photo and deletes a Reel with correct
Recently-Deleted messaging · canary-cookie test shows zero session material in evidence,
logs, or notifications · a scheduled run aborts cleanly when analytics are older than
the rule's freshness bound.

### Phase 9 — Launch hardening
Load test at 3× worst day (6,000 items) · alarm wiring (publish backlog > 100 jobs late
by 10 min, uncertain attempts > 5/hour, R2 5xx > 1%, disk > 80%, automation smoke red) ·
runbooks for the top 10 risks · invite the first 50 waitlist cohort.
**Exit criteria:** the 3× load run keeps p95 publish latency-after-slot < 15 min with
zero invariant-test regressions on the same database · every §10 risk has a written
runbook the operator has executed once in staging.

---

## 9. Security and privacy

### 9.1 Database-level isolation and least privilege
Four DB roles: `role_web`, `role_core`, `role_media`, `role_automation`. RLS
(`FORCE ROW LEVEL SECURITY`) on all tenant tables with policy
`USING (workspace_id = current_setting('app.workspace_id')::uuid)`; the web request
middleware and `leaseJobs()` each run `SET LOCAL app.workspace_id` from the session /
job row before touching tenant data. Cross-tenant work (scheduler scan, reaper,
operator console) uses `role_core`'s `app.bypass_rls` policy clause, and every
operator-initiated read/write inserts an `audit_log` row in the same transaction
(`withOperatorAudit()` wrapper — the console has no raw-query surface). Grants:
`audit_log`, `analytics_snapshots`, `receipts`, `stripe_events` have UPDATE/DELETE
revoked from all app roles (append-only at the grant layer); `ig_connections` is
readable only by `role_web` (connect flow) and `role_core` (publish/refresh);
`role_automation` has no grant on it, on `subscriptions`, or on `stripe_events` (§2.3).

### 9.2 Secrets and tokens
Instagram tokens and Playwright storage-state blobs: AES-256-GCM with a per-row random
data key, data key wrapped by a master key (32 bytes, in `/etc/toolbox/env`, present
only on droplets A/B for tokens and only on C for automation sessions; `token_key_id`
names the master for rotation — rotate by re-wrapping data keys, a maintenance job).
Passwords: Argon2id (§1.9). Session tokens stored hashed. Stripe/Postmark/R2 keys:
scoped (R2 token is bucket-scoped; Stripe restricted key without payout scope). SOPS
file is the single secret source; the age private key lives in the founder's password
manager and one printed copy in physical storage (bus-factor mitigation, §10 R-8).

### 9.3 Restricted sessions
The cleanup session is established in a supervised flow: the entitled customer opens a
short-lived (10-min token) page streaming a Playwright-controlled browser (VNC-over-
websocket to droplet C via `page.screencast`-driven frames) and logs in *themselves*,
handling their own 2FA/checkpoint; the product never sees or asks for the password —
consistent with promise 5's "public product never requests Instagram passwords", and
recorded as consent with a `rights_acceptances`-style row (`policy_version
'automation-2026-08'`). Only the resulting `storageState` is kept, encrypted (§9.2),
decryptable solely on droplet C. It is never serialized into DTOs, receipts, evidence
(enforced by `redactEvidence()` allowlist + the canary tests in §4 P5/H-12), and is
deleted at: entitlement revocation, account disconnect, workspace deletion, or 30 days
of non-use.

### 9.4 Media privacy
Private R2 bucket; all reads via presigned GET (10 min user / 60 min Instagram-fetch,
§4 H-11); presigner refuses keys outside the session's `ws/{workspace_id}/` prefix. No
public bucket listing; upload presigns are content-length-capped (1 GB) and key-pinned.

### 9.5 Abuse and rate limits
Fixed 1-minute windows in `rate_limit_counters` (`INSERT … ON CONFLICT DO UPDATE SET
count=count+1 RETURNING count`, reject over limit): login 10/min/IP,
signup 5/min/IP, upload initiation 60/hour/workspace, manual analytics refresh
1/10 min/account, manual source sample 2/hour/workspace, feedback 10/hour/user,
presign 120/min/workspace. Invitation issuance is operator- or owner-only and audited.
Sourcing floors from §2.3 cap automation pressure independently of any UI.

### 9.6 Operator surface
Separate hostname (`ops.` subdomain), Cloudflare Access allowlisted to the two staff
identities, `operator_sessions` (8-hour expiry) + TOTP on every login, `is_operator`
checked server-side per request. The console's power set is exactly the safe-action list
in the brief (§3.11) plus the inspectors in Phase 6; it contains no free-form SQL and no
token/secret display — support access to customer data flows through the same DTO
serializers as the product (operator_notes and internal fields added, secrets still
absent).

### 9.7 Retention, export, deletion, takedown
Retention: originals kept while the owning workspace exists; published-item renditions
deleted 30 days after publish (rebuildable from original + frozen settings); logs 30
days; `pg_dump` archives 30 days; audit_log and receipts retained for the workspace
lifetime; Playwright evidence screenshots 90 days. Export: owner/admin-triggered zip
(JSON of all tenant rows + original media), built by a `deletion`-queue-class job,
presigned link 24 h, audited. Deletion: §5.13. Takedown/DMCA: public security-contact
and copyright pages route to the operator console's takedown action, which sets the
`source_posts` row to `removed`, deletes the media asset (R2 + row state), and cancels
dependent queue items with `fail_class='customer_fixable'` and an explanatory
notification — logged in audit_log with the notice reference.

### 9.8 Transport and application hardening
TLS via Cloudflare (full-strict to origin), HSTS, session cookie flags (§1.9), CSRF
double-submit token on all mutations, CSP `default-src 'self'` + R2 presigned hosts for
media, dependency updates monthly via Renovate with CI as the gate, Docker images run as
non-root with read-only filesystems except scratch dirs, droplet SSH is key-only via
Tailscale (no public SSH port).

---

## 10. Risk register

| # | Risk | Early warning signal | Mitigation already in the plan |
|---|---|---|---|
| R-1 | Meta App Review rejects or stalls the publish scopes | Review > 21 days in status, or a rejection note | Submission at Phase 2 exit, months before beta (§8); fake-IG keeps all development unblocked; beta gate, not build gate |
| R-2 | Automation pool accounts banned en masse; sourcing selectors break | Pool `checkpoint`/`banned` states rising; nightly sourcing smoke red twice | Containment cell §2.3: separate droplet/IPs, kill switch auto-flips on double smoke failure; sourcing is entitled-tester-only so customer impact is bounded to testers |
| R-3 | A duplicate post reaches a customer account (reputation-fatal) | Any `uncertain` attempt resolving to two permalinks in reconciliation; kill-matrix regression in CI | Layered H-2/H-3 mechanisms (client_token, slot_claims, active-attempt index, publishing marker); kill-matrix on every merge; reconciliation matches by timestamp+caption before ever re-queueing |
| R-4 | Bulk token expiry (e.g. Meta invalidates tokens after a platform change) pauses half the fleet | `expired` transitions > 10/hour (maintenance rollup row) | Refresh job at < 10 days remaining; expiry is a *hold* state (H-7) with a customer recovery path, so the blast is a reconnect campaign, not data loss; operator console lists expired accounts for outreach |
| R-5 | Storage cost outgrows revenue | `workspace_storage` sum growth > 1 TB/month (weekly rollup) | Plan storage quotas enforced at upload (402 path, §2.6); rendition GC at 30 days; per-plan pricing includes storage allowance |
| R-6 | Instagram changes container/publish API semantics | Contract-pinning fixture drift test fails; weekly real-account run fails | §7.4 weekly real pass catches drift within 7 days; publishing state machine isolates the change to `runPublishAttempt()`; queue holds (not fails) on repeated `outside_temporary` |
| R-7 | Postgres restore does not actually work when needed | Weekly automated restore drill fails | The drill *is* the mitigation: restore is exercised weekly by machine and monthly by hand before any real disaster (§7.3); PITR plus 30 days of dumps in a second provider (R2) |
| R-8 | Founder is unavailable (bus factor 1) | — (structural) | Managed Postgres, three-droplet checked-in provisioning, runbooks per risk (Phase 9), SOPS age key escrowed physically, operator can pause workspaces/accounts and flip kill switches without code |
| R-9 | Stripe state divergence (customer charged, product shows wrong plan) | `subscriptions.last_synced_at` older than 24 h with live events; customer reports | Sync-from-truth pattern (§5.11) makes divergence self-healing on any event; nightly `billing` job re-syncs every non-free subscription (500 calls/night, trivial) |
| R-10 | Legal exposure from restricted sourcing (rights holders, platform ToS) | Takedown notices; Meta developer-account warning | Entitled-tester gating, provenance retained end-to-end, public DMCA path with a working takedown mechanism (§9.7), operator can revoke the capability instantly (H-10 hold semantics), sourcing isolated from the compliant public product in access, ops, and failure domain (§2.3) |

---

## 11. Explicit tradeoffs

1. **Publish latency after a slot, up to ~13 minutes on the worst-day burst** (§2.2
   drain math) instead of provisioning for instantaneous fan-out. Acceptable: the brief
   promises truthful progress, not to-the-second posting; the queue shows "publishing".
2. **Reconciliation window before needs-review is up to ~64 minutes** (6 backoff tries,
   §5.2). A human is alerted only after automated resolution is exhausted; the item is
   safely locked the whole time. Chosen to keep needs-review rare enough that the
   operator treats it seriously.
3. **Missed slots are skipped, not back-filled** (§5.5 step 6): after a pause/outage,
   publishing resumes at the *next* slot rather than firing a burst of catch-up posts.
   Weaker than "publish everything that was due," deliberately: catch-up bursts are the
   behavior most likely to look like spam to Instagram and to surprise the customer.
4. **Analytics freshness bounded by the refresh ladder** (last scheduled pull at 30
   days; manual refresh rate-limited to 1/10 min/account). Comparisons display
   `captured_at` staleness instead of pretending to be live.
5. **Single region, single Postgres primary.** A DO regional outage stops publishing
   until recovery; queued work holds and reconciliation runs on restart (the §5
   machinery is exactly a region-outage recovery). Multi-region is a non-goal in the
   brief and unaffordable at $274/month.
6. **SSE, not WebSockets**: a dropped event costs one ≤ 5 s reconnect+refetch of
   staleness (§1.11). Never wrong data, occasionally late by seconds.
7. **Fixed-window rate limiting** (§9.5) admits up to 2× burst at window edges versus
   sliding-window. The limits protect against abuse volumes where 2× is immaterial.
8. **Cleanup evidence is screenshots + read-back, not a platform receipt** — Instagram's
   web surface offers no receipt object; the read-only reconcile (§5.9) plus masked
   screenshots is the strongest evidence that surface can yield, disclosed to the
   customer in the run history UI.
9. **The restricted-session onboarding (§9.3) requires a live supervised login**, an
   ops-heavy step per entitled account. Chosen over any password-collection shortcut,
   which promise 5 forbids outright.
10. **Storage reconciliation is nightly**, so `workspace_storage.bytes_used` can drift
    from R2 truth for up to 24 h after a partial failure; quota enforcement therefore
    has a ≤ 24 h soft edge, corrected by the maintenance job's ListObjectsV2 sweep.
11. **Operator "mark true outcome" for a stuck-uncertain cleanup item (§5.9 step 5) is a
    human judgment** recorded in audit_log — a person, not a mechanism, is the last
    resort when Instagram's surface is unreadable. Scoped to read-locked items and fully
    attributable.

## 12. Where this is stronger than required

1. **Kill-matrix testing at named fault points on every merge** (§7.1) — the brief asks
   for correctness under crashes; compiling the crash points into the binary and
   asserting duplicate-count == 0 in CI makes the property regression-proof, for the
   cost of ~200 lines of failpoint plumbing.
2. **Append-only enforcement at the GRANT layer** for receipts, audit, snapshots, and
   Stripe events (§9.1) — evidence tampering (including by our own buggy code) is a
   database permission error, not a code-review hope. Zero runtime cost.
3. **Weekly automated restore drills** (§7.3) exceed the brief's "rehearsed
   restoration": restoration is continuously proven, and the drill doubles as backup
   monitoring. Cost: one scratch database for ~20 minutes a week.
4. **Contract-pinned fakes for Instagram's API and web surface** (§7.1) let the entire
   §4/§5 suite run on every merge with zero real-account risk; the weekly real-account
   pass then only has to catch drift, not correctness.
5. **DB-role blast-radius separation for the automation tier** (`role_automation`
   cannot read publish tokens or billing, §2.3/§9.1) — the brief requires separability;
   making it a GRANT means a Chromium compromise is contained by Postgres, not by code
   discipline.
6. **Deletion status page with a durable reference code** (§5.13) — the brief asks for
   "a status reference"; a self-serve page removes a whole class of support email for
   the two-person team.

## 13. Assumptions

Where the brief is silent, this plan decides:

1. Scale mid-points: 800 items/day average, 2,000 worst, 30% in the busiest hour, 40%
   video by count (§ preamble, §2.6).
2. Launch region NYC3; all customer timezones supported, infrastructure in one region.
3. Plans and prices: Free (1 account, 1 seat, 10 queued/account, 2 GB), Creator $19/mo
   or $190/yr (3 accounts, 2 seats, 25 queued, 25 GB), Studio $49/mo or $490/yr (10
   accounts, 5 seats + $5/extra, 100 queued, 100 GB, cleanup feature). Sourcing is never
   plan-purchasable — operator grant only.
4. Roles: exactly three (owner/admin/publisher) with the split defined in §3; one owner
   per workspace; ownership transfer is an operator action in v1.
5. Media caps: 1 GB/file, images JPEG/PNG/WebP, video MP4/MOV, Reels ≤ 15 min (platform
   ceiling) with product validation at probe time.
6. Product daily default 25 posts/account (below Instagram's API quota of 50) —
   user-adjustable 1–50.
7. Usage day = the account's local calendar day for our limit; Instagram's own rolling
   24 h limit is additionally consulted per publish (§5.6).
8. Slot semantics: slots older than 15 min never fire; no catch-up posting (§11.3);
   minimum 5-minute spacing between publishes per account; minimum schedule interval
   30 min.
9. Queue depth target for auto-refill capped at 100; sources per workspace capped at
   10; source check floor 60 min; sample size cap 25.
10. Reconciliation match rule: container status first, then timestamp ≥
    `publishing_marked_at` and exact frozen-caption match (§5.2).
11. Token refresh at < 10 days remaining, daily job; a token expired beyond refresh
    requires customer reconnection (no silent recovery exists on the platform).
12. The cleanup session is established by supervised customer login (§9.3); entitled
    customers accept an automation-consent policy version; sessions idle-expire at 30
    days.
13. Rights policy is a versioned document (`rights-2026-08`); re-acceptance is required
    per queue item via the composer checkbox (one click, recorded each time).
14. Beta invites expire in 7 days and are single-use; workspace-collaborator invites
    likewise.
15. Currency USD only; Stripe Tax for US sales tax; no invoicing beyond Stripe's
    hosted invoices.
16. Export format: zip of per-table JSON + original media, built asynchronously, link
    valid 24 h.
17. Notification email escalation is limited to: connection lost, uncertain outcome,
    payment failure, deletion lifecycle; everything else is in-app only.
18. Analytics metrics stored: reach, views, likes, comments, saves, shares (per media
    kind as Instagram exposes them); "totals derived where meaningful" = engagement sum
    and engagement-per-reach computed at read time, never stored.
19. Operator staff is exactly the two named people; Cloudflare Access allowlist is the
    roster (§9.6).
20. Recently-Deleted recovery window is treated as ~30 days but re-verified quarterly
    (§7.4) and always described to users as Instagram's window, not ours.
21. Backlog items (`source_posts.state='ready'`) never expire automatically; testers
    curate them manually.
22. The one-time Reels sample uses the same pool/proxy budget as recurring sources and
    counts against the workspace's sample rate limit (§9.5).
23. AI-free stack: no AI vendor account exists in v1 at all (§6) — any first AI feature
    begins with a fresh privacy review and its own budget line.
