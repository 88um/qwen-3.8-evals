# Engineering Plan — ToolBox Poster

---

## 1. Technology decisions

### 1.1 Language and runtime

**Choice:** TypeScript 5 on Node.js 20 LTS.
**Rejected:** Go. Go produces a single statically linked binary with lower memory per process and
native concurrency. Rejected because the product is a full-stack web application: the frontend
is React, the API is Next.js route handlers, and the workers share domain types with both. A
single language across frontend, API, and workers eliminates a type-translation layer and halves
the surface a solo developer maintains. Node.js 20 LTS is supported until April 2026;
Playwright, Sharp, and pg-boss are all native Node.js libraries.
**Why:** development velocity for a one-person team outweighs Go's runtime advantages at this
scale (≤200 WAU, ≤5,000 queue items/day).

### 1.2 Web framework

**Choice:** Next.js 14 (App Router).
**Rejected:** Pages Router. Pages Router handles layout nesting via the `getLayout` convention,
which requires manual composition and loses automatic parallel route loading. Rejected because
the product's navigation is workspace → account → queue/library/analytics, a three-level nesting
that App Router's `app/(workspace)/[accountId]/queue/page.tsx` layout structure handles natively.
Server Components serve the read-heavy pages (library, analytics, operator dashboard) without
shipping their rendering JavaScript to the client.
**Why:** nested layouts and Server Components reduce both the code and the client bundle for a
product with deep navigation and mixed read/write pages.

### 1.3 Database

**Choice:** PostgreSQL 16.
**Rejected:** SQLite. SQLite is simpler to operate (single file, no daemon) and fast for
read-heavy workloads. Rejected because concurrent writers from the web process and 3 worker
processes require row-level locking. The invariant enforcement map (§4) depends on
`SELECT … FOR UPDATE SKIP LOCKED` for job claiming and on partial unique indexes
(`CREATE UNIQUE INDEX … WHERE …`) for at-most-one constraints. SQLite has neither.
**Why:** the safety invariants are enforced by conditional updates under concurrency; that
requirement outranks the operational simplicity SQLite would buy.

### 1.4 ORM / query builder

**Choice:** Drizzle ORM.
**Rejected:** Prisma. Prisma provides a higher-level query API and automatic migrations, but
its query engine ships as a ~15 MB Rust binary sidecar, and its API abstracts the generated SQL.
Rejected because invariant-critical queries (`SELECT … FOR UPDATE SKIP LOCKED`,
`INSERT … ON CONFLICT DO NOTHING RETURNING id`, raw `NOTIFY`) must produce predictable SQL.
Drizzle's `sql` template tag passes raw SQL with type-safe bindings via
`sql\`SELECT … FOR UPDATE SKIP LOCKED\``, and its schema definitions generate CREATE TABLE
statements via `drizzle-kit generate`.
**Why:** the plan requires verifiable SQL for every safety mechanism; Drizzle provides type
safety without hiding the SQL.

### 1.5 Job queue

**Choice:** pg-boss 10 (PostgreSQL-backed job queue).
**Rejected:** BullMQ (Redis-backed). BullMQ is faster for high-throughput job processing and
provides richer dashboard tooling. Rejected because adding Redis introduces a second stateful
service to operate, and pg-boss stores jobs in the same PostgreSQL database used for application
data. This allows transactional job creation: enqueuing a publish job and updating
`queue_items.status` in the same `BEGIN … COMMIT` block, preventing orphaned or phantom jobs.
pg-boss provides named queues with per-queue concurrency, exponential retry, scheduled jobs via
`boss.schedule()` (cron syntax), and `SKIP LOCKED`-based dequeue.
**Why:** a single datastore eliminates split-brain between the job queue and application state.

### 1.6 Object storage

**Choice:** Cloudflare R2.
**Rejected:** AWS S3. S3 is the most mature object store with the broadest tooling. Rejected
because R2 charges zero egress. At steady state (540 GB stored, ~500 K Class B reads/month for
media previews and Instagram container creation), S3 costs $12.42 storage + $0.18 Class B +
~$45 egress ($0.09/GB × 500 GB) = ~$57.60/month. R2 costs $8.10 storage + $0.18 Class B +
$0 egress = $8.28/month. R2 uses the S3-compatible API (`@aws-sdk/client-s3`
`PutObjectCommand`, `GetObjectCommand`, `DeleteObjectsCommand`), so migration is a
configuration change.
**Why:** egress cost at the media-preview volume of this product makes R2 7× cheaper than S3.

### 1.7 Media processing

**Choice:** Sharp 0.33 (images) + FFmpeg 6 (video), running locally on the VPS.
**Rejected:** AWS MediaConvert (cloud-based). MediaConvert charges $0.015/minute for on-demand
transcoding. At 40 videos/day averaging 30 seconds: 40 × 0.5 min × $0.015 = $0.30/day =
$9/month. Local FFmpeg uses CPU already paid for by the VPS. Rejected because the VPS has 8
vCPUs and the worker caps media preparation concurrency at 2, leaving 6 vCPUs for the web
server and other workers.
**Why:** the VPS CPU is a sunk cost; local processing is free at this scale.

Image operations use Sharp's API:
- Resize: `sharp(input).resize({width: 1080, fit: 'inside'})`
- Aspect ratio: `sharp(input).resize({width: 1080, height: 1350, fit: 'cover'})` (4:5)
- Logo overlay: `sharp(input).composite([{input: logoBuffer, gravity: 'southeast'}])`
- Strip metadata: `sharp(input).withMetadata({orientation: undefined})`

Video operations use FFmpeg via child process:
- Transcode to H.264: `-c:v libx264 -preset medium -crf 23 -c:a aac -b:a 128k`
- Reels aspect ratio (9:16): `-vf "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2"`
- Logo overlay: `-vf "overlay=W-w-20:H-h-20"` (bottom-right, 20px margin)
- Duration cap: `-t 90` (Instagram Reels maximum)

### 1.8 Instagram integration

**Choice:** Meta Graph API (Content Publishing API).
**Rejected:** No alternative exists for legitimate, password-free publishing to professional
Instagram accounts. Third-party wrappers (instagrapi, etc.) use credential-based login or
private APIs, which violate the product's promise that "Instagram passwords are never requested
by the public product" and risk account suspension.
**Why:** the Graph API is the only authorized path.

Publishing flow uses three endpoints:
1. Container creation: `POST /{ig-user-id}/media` with `image_url` or `video_url` + `caption`.
2. Container status (video only): `GET /{container-id}?fields=status_code` — poll until
   `status_code === 'FINISHED'`, timeout after 300 seconds, poll interval 5 seconds (60 polls max).
3. Publish: `POST /{ig-user-id}/media_publish` with `creation_id`.
4. Permalink: `GET /{media-id}?fields=permalink,timestamp`.

Analytics: `GET /{media-id}/insights?metric=reach,impressions,likes,comments,saves,shares,plays`.

Token lifecycle: short-lived token from OAuth → exchange via
`GET /oauth/access_token?grant_type=fb_exchange_token` for a 60-day long-lived token. Refresh
before expiry via the same endpoint.

### 1.9 Browser automation (restricted sourcing)

**Choice:** Playwright 1.44.
**Rejected:** Puppeteer. Both provide headless Chromium automation. Playwright's
`browserContext.storageState({path})` persists cookies and localStorage for session reuse across
worker restarts, and `browserContext.newContext({storageState})` restores them. Puppeteer has
`page.cookies()` / `page.setCookie()` but does not persist localStorage in a single call.
Playwright's `page.route(url, handler)` intercepts network requests for selective media download
without loading the full page DOM, reducing memory and detection surface.
**Why:** session persistence and network interception are both critical for the source worker's
operational model; Playwright provides both as first-class APIs.

### 1.10 Payments

**Choice:** Stripe (Checkout Sessions + Customer Portal + Webhooks).
**Rejected:** Paddle (merchant of record). Paddle handles tax remittance and reduces compliance
burden, but charges ~5% of revenue vs Stripe's 2.9% + $0.30. At $3,000 MRR (200 users ×
$15 average), Paddle costs $150/month vs Stripe's $147/month — roughly equal. Rejected because
Stripe's webhook event granularity (`invoice.paid`, `customer.subscription.updated`,
`customer.subscription.deleted`, `checkout.session.completed`) maps directly to the idempotent
billing event processing in §4 and §5. Paddle's webhook schema is coarser.
**Why:** event-level granularity for idempotent billing state management.

Card details never touch the product. Stripe Checkout (`stripe.checkout.sessions.create()`)
redirects to Stripe's hosted page. Stripe Customer Portal
(`stripe.billingPortal.sessions.create()`) handles plan changes and cancellation.

### 1.11 Authentication

**Choice:** NextAuth.js 5 with email magic links (`EmailProvider`).
**Rejected:** Clerk. Clerk provides drop-in auth UI with MFA, social login, and user
management, but introduces an external dependency on the authentication critical path. At
launch, Clerk's free tier (10,000 MAU) is sufficient, but a Clerk outage blocks all logins.
NextAuth.js stores sessions in the existing PostgreSQL database (via `@auth/drizzle-adapter`),
adding no external runtime dependency.
**Why:** zero external runtime dependencies for authentication. Magic links eliminate password
storage and management entirely.

Session lifetime: 30 days of inactivity. Session token stored as `token_hash` (SHA-256) in the
`sessions` table; the raw token is in an `HttpOnly`, `Secure`, `SameSite=Lax` cookie.

### 1.12 Real-time updates

**Choice:** Server-Sent Events (SSE) with PostgreSQL `LISTEN/NOTIFY`.
**Rejected:** WebSockets via Socket.IO. WebSockets provide bidirectional communication, but
every status update in this product flows server → client (queue status, publish progress,
notifications). WebSocket bidirectionality is unused, and Socket.IO adds ~50 KB to the client
bundle and requires sticky sessions or a Redis adapter for multi-process. SSE works over
standard HTTP, auto-reconnects via the browser's `EventSource` API, and needs no additional
infrastructure.
**Why:** unidirectional updates over plain HTTP, with no additional services.

Mechanism: when a worker updates a queue item status, it calls
`NOTIFY workspace_{id}, '{event_type}:{payload_json}'` in the same transaction. The Next.js
SSE route handler holds a PostgreSQL connection with `LISTEN workspace_{id}` and streams
matching events to all connected clients for that workspace. One `LISTEN` connection per
Next.js process fans out to all SSE clients. At 200 concurrent SSE connections, Node.js holds
200 long-lived HTTP connections — well within its capacity (the default `maxConnections` is
unbounded; the OS limit is set to 4096 via `ulimit -n`).

Client-side integration: the SSE event handler calls
`queryClient.invalidateQueries({queryKey: ['queue-items', accountId]})` (React Query), which
triggers a re-fetch of the affected data. No manual DOM manipulation.

### 1.13 Hosting

**Choice:** Hetzner CPX41 VPS (8 vCPU AMD EPYC, 16 GB RAM, 240 GB NVMe SSD),
Ashburn, Virginia.
**Rejected:** Fly.io. Fly.io provides a managed container platform with automatic TLS,
global edge networking, and simple scaling. At equivalent resources (4 shared vCPU, 8 GB RAM),
Fly.io costs ~$50/month, and adding a Fly Postgres cluster adds ~$30/month. The Hetzner VPS
at $28.79/month includes 8 dedicated vCPUs and 16 GB RAM — 4× the compute at ~43% the cost.
Fly.io's global edge is unnecessary for a single-region launch.
**Why:** the budget is "a few hundred dollars per month"; the VPS leaves $270/month for storage,
email, and growth. Assumption: the target geographic region is the United States (§13).

### 1.14 Reverse proxy and TLS

**Choice:** Caddy 2.
**Rejected:** Nginx. Nginx requires manual certbot installation, Let's Encrypt registration,
and a renewal cron job. Caddy obtains and renews TLS certificates automatically via its built-in
ACME client (the `tls` Caddyfile directive). Caddy's `reverse_proxy` directive passes SSE
connections without additional configuration; Nginx requires explicit `proxy_set_header Upgrade`,
`proxy_set_header Connection`, and `proxy_read_timeout 86400` for long-lived connections.
**Why:** automatic TLS and zero-config SSE passthrough reduce operational tasks for a solo
operator.

### 1.15 Transactional email

**Choice:** Resend.
**Rejected:** Amazon SES. SES is cheaper per email ($0.10/1,000) but requires manual domain
verification via DNS TXT records and has no built-in template system. Resend's free tier covers
3,000 emails/month and provides a React-based template API (`@react-email/components`) that
integrates with the existing TypeScript stack. At launch scale (magic links + notifications,
~500 emails/month), the free tier is sufficient. Upgrade to the $20/month tier (50,000
emails/month) when volume exceeds 3,000.
**Why:** zero marginal cost at launch, integrated template API.

### 1.16 Token encryption

**Choice:** AES-256-GCM via Node.js `crypto.createCipheriv('aes-256-gcm', key, iv)`.
**Rejected:** PostgreSQL `pgcrypto`. `pgcrypto` performs encryption inside the database process,
meaning a database backup or compromise exposes both the encrypted data and the encryption
context. Application-level encryption keeps the key in an environment variable on the VPS,
separate from the database. A database dump contains only ciphertext.
**Why:** defense in depth — the encryption key and the encrypted data reside in different
systems.

Two separate keys:
- `ENCRYPTION_KEY`: encrypts Instagram access tokens. Available to `web` and `worker` processes.
- `SOURCE_ENCRYPTION_KEY`: encrypts browser session state files for the source worker. Available
  only to the `source-worker` process.

### 1.17 Timezone computation

**Choice:** Luxon 3 (`luxon.DateTime`).
**Rejected:** `date-fns-tz`. `date-fns-tz` requires the developer to manually handle ambiguous
times during DST fall-back (when a local time occurs twice). Luxon's
`DateTime.fromObject({hour: 14, minute: 0}, {zone: 'America/New_York'})` resolves ambiguous
times to the first occurrence by default, and spring-forward gaps (nonexistent times) produce an
invalid DateTime (`dt.isValid === false`), which the scheduler detects and skips.
**Why:** correct DST handling without manual ambiguity resolution.

---

## 2. System architecture

Three independently running processes share PostgreSQL and R2:

```
┌─────────────────────────────────────────────────────────────┐
│  Hetzner VPS (8 vCPU, 16 GB RAM)                            │
│                                                             │
│  ┌──────────┐  ┌────────────────┐  ┌──────────────────────┐ │
│  │  Caddy   │──│  web (Next.js) │  │  worker (pg-boss)    │ │
│  │  :80/443 │  │  :3000         │  │                      │ │
│  └──────────┘  │  - App pages   │  │  Queues:             │ │
│                │  - API routes   │  │  - media-prepare (2) │ │
│                │  - SSE endpoint │  │  - publish (4)       │ │
│                │  - OAuth cb     │  │  - analytics (2)     │ │
│                │  - Stripe wh    │  │  - cleanup (1)       │ │
│                └────────────────┘  │  - token-refresh (1)  │ │
│                                    │                      │ │
│                                    │  Cron schedules:      │ │
│                                    │  - check-schedules 60s│ │
│                                    │  - refresh-analytics  │ │
│                                    │    every 6h           │ │
│                                    │  - check-tokens 24h   │ │
│                                    │  - check-sources 60s  │ │
│                                    │  - check-auto-refill  │ │
│                                    │    5min               │ │
│                                    │  - check-cleanup-     │ │
│                                    │    schedules 60s      │ │
│                                    └──────────────────────┘ │
│                                                             │
│  ┌──────────────────────────────────┐                       │
│  │  source-worker (Playwright)      │                       │
│  │  Docker: mem_limit 4 GB, cpus 2  │                       │
│  │  Queue: source-collect (2)       │                       │
│  │  Max 4 browser contexts          │                       │
│  │  No ENCRYPTION_KEY               │                       │
│  │  R2 write-only to sources/       │                       │
│  └──────────────────────────────────┘                       │
│                                                             │
│  ┌──────────────┐                                           │
│  │  PostgreSQL 16│  ← all three processes connect here      │
│  │  :5432        │                                          │
│  └──────────────┘                                           │
└─────────────────────────────────────────────────────────────┘
         │
         │  S3-compatible API
         ▼
  ┌──────────────┐
  │ Cloudflare R2 │
  │               │
  │ Buckets:      │
  │ - media/      │  (originals + prepared, keyed by workspace)
  │ - sources/    │  (source-collected media, keyed by workspace)
  │ - backups/    │  (daily pg_dump)
  └──────────────┘
```

### Process responsibilities

**web** (Next.js 14, App Router):
- Serves the public site, app, and operator area.
- API routes for all mutations: upload, queue, schedule, publish-now, cleanup, billing.
- OAuth callback handlers: Instagram (Facebook Login) and Stripe (checkout return URL).
- Stripe webhook receiver at `/api/webhooks/stripe`.
- SSE endpoint at `GET /api/events?workspaceId={id}`.
- Never performs media processing, Instagram publishing, or browser automation.

**worker** (Node.js + pg-boss):
- Consumes named queues with independent concurrency (parenthesized numbers above).
- `media-prepare` (concurrency 2): download original from R2, process with Sharp/FFmpeg,
  upload prepared version to R2, update `media_uploads.status`.
- `publish` (concurrency 4): claim queue item via `SELECT … FOR UPDATE`, create Instagram
  container, poll status, publish, create receipt, update `queue_items.status`, increment
  `account_daily_usage`, `NOTIFY` workspace.
- `analytics-refresh` (concurrency 2): call Instagram Insights API per published post,
  insert `analytics_snapshots` row.
- `cleanup-execute` (concurrency 1): process one `cleanup_run_items` row at a time per
  account — archive or delete via Instagram API, record result.
- `token-refresh` (concurrency 1): call Meta token exchange endpoint for tokens expiring
  within 7 days, update encrypted token.
- Scheduled jobs via `boss.schedule()`: see cron intervals in diagram.

**source-worker** (Node.js + Playwright, isolated Docker container):
- Consumes `source-collect` queue (concurrency 2).
- Launches Playwright browser contexts (max 4 contexts × ~800 MB each = 3.2 GB peak,
  within the 4 GB container memory limit).
- Picks a session from the `source_sessions` pool, restores via
  `browser.newContext({storageState: decryptedState})`.
- Navigates to the source (account page, hashtag page, or Reels feed).
- Extracts candidate metadata (post ID, author, caption, media URL, engagement).
- Downloads media to R2 `sources/{workspace_id}/{candidate_id}`.
- Inserts `source_candidates` rows with `ON CONFLICT (instagram_account_id, external_post_id) DO NOTHING`.
- Persists updated browser state via `context.storageState()` → encrypt → write to
  `/data/source-sessions/{session_id}.enc`.

### How work moves between processes

**Upload → prepare → ready:**
web receives multipart upload → streams to R2 `media/{workspace_id}/originals/{upload_id}` via
`PutObjectCommand` → inserts `media_uploads` row (status `uploading` → `uploaded`) → enqueues
`media-prepare` job in the same transaction → returns `upload_id` to client. Worker picks up
`media-prepare` → downloads original from R2 → processes → uploads prepared to R2
`media/{workspace_id}/prepared/{upload_id}` → updates `media_uploads.status = 'ready'` and
`prepared_storage_key` → `NOTIFY workspace_{id}`.

**Queue → schedule → publish → receipt:**
User queues an item → web inserts `queue_items` + `queue_item_snapshots` rows → returns.
Worker cron `check-schedules` fires every 60s → for each active, unpaused account, computes
next due time from `schedule_rules` using Luxon in the account's timezone → checks
`schedule_executions` for dedup → if no execution exists for this slot:
`INSERT INTO schedule_executions … ON CONFLICT DO NOTHING RETURNING id` → if inserted,
enqueues `publish` job. Alternatively, user clicks "publish now" → web enqueues `publish` job
directly. Worker picks up `publish` → claims item (`SELECT … FOR UPDATE WHERE status = 'ready'`,
update to `publishing`) → creates Instagram container → polls status → publishes → creates
`publish_receipts` row → updates `queue_items.status = 'published'` → increments
`account_daily_usage` → `NOTIFY workspace_{id}`.

**Source → backlog → queue:**
Worker cron `check-sources` fires every 60s → finds sources where
`last_checked_at + check_interval_minutes < now()` → enqueues `source-collect` job.
Source-worker picks up job → browses Instagram → inserts `source_candidates` → updates
`sources.last_checked_at`. User manually selects candidates → web creates `queue_items` +
snapshots, sets `source_candidates.status = 'queued'`. Or: worker cron `check-auto-refill`
fires every 5 min → for accounts with `auto_refill_enabled` and queue depth below target →
moves candidates from backlog to queue.

**Cleanup → execute → record:**
User confirms cleanup → web inserts `cleanup_runs` (with `selection_hash`) +
`cleanup_run_items` → enqueues `cleanup-execute` job. Worker processes items one at a time:
claims item (`SELECT … FOR UPDATE WHERE status = 'pending'`) → calls Instagram API (archive
for photos, delete for Reels) → records result → `NOTIFY`. If uncertain outcome → run pauses
(`needs_reconciliation`).

### Starvation prevention

- Publishing (core product) has 4 concurrent slots, independent of media preparation.
- Media preparation has 2 slots, CPU-bound. Limiting to 2 prevents starving the web server
  (which shares the VPS's 8 CPUs).
- Source collection runs in a separate container with capped CPU (2 cores) and memory (4 GB).
  A runaway Playwright process cannot consume web server or worker resources.
- Cleanup runs 1 item per account sequentially. A slow Instagram API response delays that
  account's cleanup but not publishing, media prep, or other accounts' cleanups.
- Analytics refresh has 2 slots and runs every 6 hours. A slow Instagram Insights API does
  not affect publishing.
- The web process has no worker responsibilities. A blocked worker does not degrade page loads.

### Failure isolation

- source-worker crash: publishing, media prep, and web server continue. Source collection
  jobs remain in pg-boss and are processed on restart.
- worker crash: web server continues serving pages. All pending jobs remain in PostgreSQL.
  pg-boss's `expireInSeconds` (300s) marks stale jobs for retry on restart.
- web crash: workers continue processing. SSE connections drop; clients auto-reconnect via
  `EventSource`.
- R2 outage: uploads fail (user sees error). Media prep fails (retries 3×). Publishing of
  already-prepared content continues if Instagram has cached the container's media URL.
- PostgreSQL outage: all processes fail. Mitigated by daily backups and VPS-level storage
  reliability (NVMe RAID on Hetzner).

---

## 3. Data model

All timestamps are `TIMESTAMPTZ` (UTC). All primary keys are `UUID`. Drizzle ORM schema mirrors
this DDL; `drizzle-kit generate` produces these statements.

```sql
-- =====================
-- IDENTITY & WORKSPACES
-- =====================

CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT NOT NULL,
    name            TEXT NOT NULL,
    avatar_url      TEXT,
    role            TEXT NOT NULL DEFAULT 'customer'
                    CHECK (role IN ('customer', 'operator')),
    suspended_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_unique ON users (lower(email));

CREATE TABLE sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash      TEXT NOT NULL UNIQUE,
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX sessions_user ON sessions (user_id);

CREATE TABLE workspaces (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    suspended_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workspace_members (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'publisher')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, user_id)
);

CREATE TABLE onboarding_responses (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id),
    what_they_operate TEXT,
    niche           TEXT,
    main_goal       TEXT,
    desired_cadence TEXT,
    completed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ======================
-- WAITLIST & INVITATIONS
-- ======================

CREATE TABLE waitlist_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           TEXT NOT NULL,
    name            TEXT,
    notes           TEXT,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'invited', 'declined')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX waitlist_email_unique ON waitlist_entries (lower(email));

CREATE TABLE invitations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID REFERENCES workspaces(id),
    email           TEXT NOT NULL,
    token_hash      TEXT NOT NULL UNIQUE,
    role            TEXT NOT NULL DEFAULT 'publisher'
                    CHECK (role IN ('owner', 'admin', 'publisher')),
    created_by      UUID NOT NULL REFERENCES users(id),
    accepted_at     TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ====================
-- INSTAGRAM ACCOUNTS
-- ====================

CREATE TABLE connection_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id),
    ig_username     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'declined')),
    operator_notes  TEXT,
    user_reason     TEXT,
    reviewed_by     UUID REFERENCES users(id),
    reviewed_at     TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE instagram_accounts (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    ig_user_id              TEXT NOT NULL UNIQUE,
    ig_username             TEXT NOT NULL,
    ig_profile_picture_url  TEXT,
    access_token_encrypted  BYTEA NOT NULL,
    access_token_iv         BYTEA NOT NULL,
    access_token_tag        BYTEA NOT NULL,
    token_expires_at        TIMESTAMPTZ NOT NULL,
    connection_status       TEXT NOT NULL DEFAULT 'active'
                            CHECK (connection_status IN ('active', 'expired', 'revoked', 'disconnected')),
    publishing_paused       BOOLEAN NOT NULL DEFAULT false,
    daily_publish_limit     INTEGER NOT NULL DEFAULT 25,
    timezone                TEXT NOT NULL DEFAULT 'UTC',
    auto_refill_enabled     BOOLEAN NOT NULL DEFAULT false,
    auto_refill_target      INTEGER NOT NULL DEFAULT 10,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE account_preparation_settings (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id) UNIQUE,
    format_for_reels        BOOLEAN NOT NULL DEFAULT false,
    fit_aspect_ratio        TEXT CHECK (fit_aspect_ratio IN ('1:1', '4:5', '9:16')),
    strip_metadata          BOOLEAN NOT NULL DEFAULT true,
    logo_storage_key        TEXT,
    logo_position           TEXT DEFAULT 'bottom_right'
                            CHECK (logo_position IN
                              ('top_left','top_right','bottom_left','bottom_right','center')),
    banner_storage_key      TEXT,
    caption_template        TEXT,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============
-- CONTENT RIGHTS
-- =============

CREATE TABLE content_rights_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version         INTEGER NOT NULL UNIQUE,
    text_content    TEXT NOT NULL,
    published_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE content_rights_acceptances (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id),
    version_id      UUID NOT NULL REFERENCES content_rights_versions(id),
    accepted_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =============
-- MEDIA UPLOADS
-- =============

CREATE TABLE media_uploads (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    uploaded_by             UUID NOT NULL REFERENCES users(id),
    original_filename       TEXT NOT NULL,
    content_type            TEXT NOT NULL,
    file_size_bytes         BIGINT NOT NULL,
    original_storage_key    TEXT NOT NULL,
    prepared_storage_key    TEXT,
    media_type              TEXT NOT NULL CHECK (media_type IN ('image', 'video')),
    width                   INTEGER,
    height                  INTEGER,
    duration_seconds        NUMERIC,
    status                  TEXT NOT NULL DEFAULT 'uploading'
                            CHECK (status IN ('uploading','uploaded','preparing','ready','failed')),
    failure_reason          TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX media_uploads_workspace ON media_uploads (workspace_id);

-- =======================
-- QUEUE & SCHEDULE
-- =======================

CREATE TABLE queue_items (
    id                              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id                    UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id            UUID NOT NULL REFERENCES instagram_accounts(id),
    media_upload_id                 UUID NOT NULL REFERENCES media_uploads(id),
    position                        INTEGER NOT NULL,
    caption                         TEXT NOT NULL DEFAULT '',
    status                          TEXT NOT NULL DEFAULT 'preparing'
                                    CHECK (status IN
                                      ('preparing','ready','publishing','published',
                                       'hidden','failed','needs_review')),
    content_rights_acceptance_id    UUID NOT NULL REFERENCES content_rights_acceptances(id),
    source_candidate_id             UUID REFERENCES source_candidates(id),
    created_by                      UUID NOT NULL REFERENCES users(id),
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    row_version                     INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX queue_items_account_status ON queue_items (instagram_account_id, status);
CREATE UNIQUE INDEX queue_items_position_unique
    ON queue_items (instagram_account_id, position)
    WHERE status IN ('preparing', 'ready');

CREATE TABLE queue_item_snapshots (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id               UUID NOT NULL REFERENCES queue_items(id) UNIQUE,
    caption_frozen              TEXT NOT NULL,
    media_storage_key_frozen    TEXT NOT NULL,
    media_type_frozen           TEXT NOT NULL,
    ig_account_id_frozen        UUID NOT NULL,
    ig_username_frozen          TEXT NOT NULL,
    preparation_settings_frozen JSONB NOT NULL DEFAULT '{}',
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE schedule_rules (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    rule_type               TEXT NOT NULL CHECK (rule_type IN ('fixed_time', 'interval')),
    days_of_week            INTEGER[] NOT NULL DEFAULT '{0,1,2,3,4,5,6}',
    time_of_day             TIME,
    interval_minutes        INTEGER,
    window_start            TIME,
    window_end              TIME,
    enabled                 BOOLEAN NOT NULL DEFAULT true,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX schedule_rules_account ON schedule_rules (instagram_account_id);

CREATE TABLE schedule_executions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    scheduled_date          DATE NOT NULL,
    scheduled_local_time    TIME NOT NULL,
    scheduled_utc           TIMESTAMPTZ NOT NULL,
    queue_item_id           UUID REFERENCES queue_items(id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (instagram_account_id, scheduled_date, scheduled_local_time)
);

CREATE TABLE account_daily_usage (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    usage_date              DATE NOT NULL,
    publish_count           INTEGER NOT NULL DEFAULT 0,
    UNIQUE (instagram_account_id, usage_date)
);

-- ======================
-- PUBLISHING & RECEIPTS
-- ======================

CREATE TABLE publish_attempts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id       UUID NOT NULL REFERENCES queue_items(id),
    attempt_number      INTEGER NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN
                          ('pending','container_created','container_ready',
                           'publishing','published','failed','uncertain')),
    ig_container_id     TEXT,
    ig_media_id         TEXT,
    ig_permalink        TEXT,
    error_category      TEXT CHECK (error_category IN
                          ('customer_fixable','temporary','permission_expired',
                           'account_limit','invalid_media','uncertain')),
    error_detail        TEXT,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    UNIQUE (queue_item_id, attempt_number)
);
CREATE UNIQUE INDEX publish_attempts_one_active
    ON publish_attempts (queue_item_id)
    WHERE status NOT IN ('published', 'failed');

CREATE TABLE publish_receipts (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id           UUID NOT NULL REFERENCES queue_items(id) UNIQUE,
    publish_attempt_id      UUID NOT NULL REFERENCES publish_attempts(id),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    ig_media_id             TEXT NOT NULL,
    ig_permalink            TEXT NOT NULL,
    caption_frozen          TEXT NOT NULL,
    media_storage_key_frozen TEXT NOT NULL,
    media_type_frozen       TEXT NOT NULL,
    ig_username_frozen      TEXT NOT NULL,
    ig_status               TEXT NOT NULL DEFAULT 'live'
                            CHECK (ig_status IN ('live', 'archived', 'deleted')),
    protected               BOOLEAN NOT NULL DEFAULT false,
    published_at            TIMESTAMPTZ NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX publish_receipts_workspace ON publish_receipts (workspace_id);
CREATE INDEX publish_receipts_account ON publish_receipts (instagram_account_id);

-- ===========
-- ANALYTICS
-- ===========

CREATE TABLE analytics_snapshots (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    publish_receipt_id  UUID NOT NULL REFERENCES publish_receipts(id),
    ig_media_id         TEXT NOT NULL,
    reach               INTEGER,
    impressions         INTEGER,
    likes               INTEGER,
    comments            INTEGER,
    saves               INTEGER,
    shares              INTEGER,
    plays               INTEGER,
    snapshot_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX analytics_snapshots_receipt_time
    ON analytics_snapshots (publish_receipt_id, snapshot_at);

-- =====================
-- RESTRICTED SOURCING
-- =====================

CREATE TABLE source_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    label           TEXT NOT NULL,
    session_file_path TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active', 'cooldown', 'blocked', 'inactive')),
    last_used_at    TIMESTAMPTZ,
    cooldown_until  TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sources (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    source_type             TEXT NOT NULL
                            CHECK (source_type IN ('account','hashtag','reels_feed','one_time_sample')),
    source_identifier       TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'pending_verification'
                            CHECK (status IN
                              ('pending_verification','active','paused','retrying','blocked','removed')),
    media_type_filter       TEXT CHECK (media_type_filter IN ('image', 'video')),
    max_age_days            INTEGER,
    min_likes               INTEGER,
    min_comments            INTEGER,
    min_plays               INTEGER,
    exclude_words           TEXT[],
    max_candidates_per_check INTEGER NOT NULL DEFAULT 20,
    check_interval_minutes  INTEGER NOT NULL DEFAULT 360,
    last_checked_at         TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX sources_workspace ON sources (workspace_id);

CREATE TABLE source_candidates (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id               UUID NOT NULL REFERENCES sources(id),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    external_post_id        TEXT NOT NULL,
    external_permalink      TEXT,
    external_author         TEXT,
    external_caption        TEXT,
    media_type              TEXT NOT NULL,
    media_storage_key       TEXT,
    observed_likes          INTEGER,
    observed_comments       INTEGER,
    observed_plays          INTEGER,
    discovered_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    status                  TEXT NOT NULL DEFAULT 'backlog'
                            CHECK (status IN ('backlog','filtered','queued','rejected')),
    UNIQUE (instagram_account_id, external_post_id)
);

-- ==========
-- CLEANUP
-- ==========

CREATE TABLE cleanup_rules (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    media_type_filter       TEXT CHECK (media_type_filter IN ('image', 'video')),
    min_age_days            INTEGER NOT NULL,
    min_reach               INTEGER,
    min_likes               INTEGER,
    min_comments            INTEGER,
    min_saves               INTEGER,
    schedule_type           TEXT CHECK (schedule_type IN ('daily', 'weekly')),
    schedule_time           TIME,
    enabled                 BOOLEAN NOT NULL DEFAULT false,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cleanup_runs (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    cleanup_rule_id         UUID NOT NULL REFERENCES cleanup_rules(id),
    rule_snapshot           JSONB NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'confirmed'
                            CHECK (status IN
                              ('confirmed','running','paused','completed',
                               'stopped','needs_reconciliation')),
    requested_by            UUID NOT NULL REFERENCES users(id),
    confirmed_at            TIMESTAMPTZ NOT NULL,
    selection_hash          TEXT NOT NULL,
    started_at              TIMESTAMPTZ,
    completed_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX cleanup_runs_one_active
    ON cleanup_runs (instagram_account_id)
    WHERE status IN ('confirmed', 'running', 'needs_reconciliation');

CREATE TABLE cleanup_run_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cleanup_run_id      UUID NOT NULL REFERENCES cleanup_runs(id),
    publish_receipt_id  UUID NOT NULL REFERENCES publish_receipts(id),
    ig_media_id         TEXT NOT NULL,
    action              TEXT NOT NULL CHECK (action IN ('archive', 'delete')),
    metrics_snapshot    JSONB NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN
                          ('pending','processing','completed','failed','uncertain','skipped')),
    ig_response_code    INTEGER,
    processed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- =======================
-- PLANS & BILLING
-- =======================

CREATE TABLE plans (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                        TEXT NOT NULL UNIQUE,
    stripe_price_id_monthly     TEXT,
    stripe_price_id_annual      TEXT,
    max_accounts                INTEGER NOT NULL,
    max_collaborators           INTEGER NOT NULL,
    storage_bytes               BIGINT NOT NULL,
    has_cleanup                 BOOLEAN NOT NULL DEFAULT false,
    has_restricted_sourcing     BOOLEAN NOT NULL DEFAULT false,
    is_public                   BOOLEAN NOT NULL DEFAULT true,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subscriptions (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id                UUID NOT NULL REFERENCES workspaces(id) UNIQUE,
    plan_id                     UUID NOT NULL REFERENCES plans(id),
    stripe_subscription_id      TEXT UNIQUE,
    stripe_customer_id          TEXT NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active','past_due','canceled','incomplete')),
    current_period_start        TIMESTAMPTZ,
    current_period_end          TIMESTAMPTZ,
    cancel_at_period_end        BOOLEAN NOT NULL DEFAULT false,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE billing_events (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_event_id     TEXT NOT NULL UNIQUE,
    event_type          TEXT NOT NULL,
    workspace_id        UUID REFERENCES workspaces(id),
    payload             JSONB NOT NULL,
    processed_at        TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workspace_entitlements (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id),
    feature         TEXT NOT NULL,
    granted_by      UUID NOT NULL REFERENCES users(id),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);
CREATE UNIQUE INDEX workspace_entitlements_active
    ON workspace_entitlements (workspace_id, feature)
    WHERE revoked_at IS NULL;

-- ======================
-- NOTIFICATIONS & FEEDBACK
-- ======================

CREATE TABLE notifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id),
    user_id         UUID REFERENCES users(id),
    type            TEXT NOT NULL,
    priority        TEXT NOT NULL DEFAULT 'normal'
                    CHECK (priority IN ('normal', 'high')),
    title           TEXT NOT NULL,
    body            TEXT,
    link_path       TEXT,
    read_at         TIMESTAMPTZ,
    dismissed_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX notifications_workspace_unread
    ON notifications (workspace_id, created_at)
    WHERE read_at IS NULL;

CREATE TABLE feedback (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    workspace_id    UUID REFERENCES workspaces(id),
    category        TEXT NOT NULL
                    CHECK (category IN ('bug','confusing','idea','praise','other')),
    body            TEXT NOT NULL,
    page_url        TEXT,
    page_context    JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ===========
-- AUDIT & DELETION
-- ===========

CREATE TABLE audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id        UUID NOT NULL REFERENCES users(id),
    workspace_id    UUID REFERENCES workspaces(id),
    action          TEXT NOT NULL,
    target_type     TEXT NOT NULL,
    target_id       UUID NOT NULL,
    detail          JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_workspace_time ON audit_log (workspace_id, created_at);

CREATE TABLE deletion_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'completed')),
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ
);
```

---

## 4. Invariant enforcement map

| # | Invariant | Mechanism | Test |
|---|-----------|-----------|------|
| I1 | Publishes only what the customer intended | `queue_item_snapshots` freezes caption, media key, account, and prep settings at queue time. `publish` worker reads from snapshot, never from live `account_preparation_settings` or `queue_items.caption`. | `test_publish_uses_snapshot`: change `account_preparation_settings.fit_aspect_ratio` after queueing item; publish; assert published media matches snapshot's `media_storage_key_frozen`, not the re-prepared version. |
| I2 | Queue tells the truth — status | `queue_items.status` CHECK constraint restricts to 7 known states. Every status transition calls `NOTIFY workspace_{id}` in the same transaction. SSE handler forwards notification within 2 seconds. | `test_queue_status_enum`: INSERT a queue item with status `'bogus'`; assert CHECK violation. `test_sse_status_push`: publish an item; assert SSE event received by a connected test client within 2 seconds. |
| I3 | No double publish (idempotent) | Partial unique index `publish_attempts_one_active` on `(queue_item_id) WHERE status NOT IN ('published','failed')` allows at most 1 non-terminal attempt. Worker claims with `SELECT … FOR UPDATE WHERE status = 'ready'`; second worker's WHERE matches 0 rows. | `test_concurrent_publish_claim`: spawn 10 concurrent goroutines each calling `claim_next_item(account_id)`; assert exactly 1 returns a row; assert `publish_attempts` has exactly 1 non-failed row. |
| I4 | Uncertain outcome → needs_review, no auto-retry | Worker: if `publish_attempts.status = 'publishing'` and `ig_container_id IS NOT NULL` and the API call timed out, set `publish_attempts.status = 'uncertain'` and `queue_items.status = 'needs_review'`. The `publish_attempts_one_active` index prevents creating a new attempt while the uncertain one exists. | `test_uncertain_blocks_retry`: mock Instagram `media_publish` to timeout; assert `queue_items.status = 'needs_review'`; attempt to enqueue a new publish job; assert it exits with "item in needs_review". |
| I5 | Holding preserves work | No status transition goes from any state to `'deleted'`. `queue_items` rows are removed only by explicit `DELETE` from user action. Pausing sets `instagram_accounts.publishing_paused = true`; scheduler skips paused accounts without touching queue items. | `test_pause_preserves_queue`: queue 5 items; pause account; assert all 5 items still exist with status `'ready'`. `test_disconnect_preserves_queue`: disconnect account; assert items unchanged. |
| I6 | Workspace isolation (tenant) | Every API route handler calls `requireWorkspaceMember(session, workspaceId)`, which queries `workspace_members` for the session's user_id and the requested workspace_id. Returns 403 if no row. R2 storage keys are prefixed with `{workspace_id}/`. | `test_cross_workspace_access`: create items in workspace A and B; authenticate as workspace A member; call queue API for workspace B; assert 403. Read media URL for workspace B's item; assert 403. |
| I7 | Single active publication per queue item | `publish_attempts_one_active` partial unique index: at most 1 row per `queue_item_id` where status is not terminal. | `test_one_active_attempt`: insert `publish_attempts` with status `'pending'` for item X; insert another with status `'pending'` for item X; assert unique violation on the second INSERT. |
| I8 | Single cleanup per account | `cleanup_runs_one_active` partial unique index on `(instagram_account_id) WHERE status IN ('confirmed','running','needs_reconciliation')`. | `test_one_cleanup_per_account`: create cleanup run for account A; create another for account A; assert unique violation. |
| I9 | Pre-action failure retries; post-action ambiguity does not | Worker checks `publish_attempts`: if `ig_container_id IS NULL`, the API was never called — retry permitted. If `ig_container_id IS NOT NULL` and status is `'publishing'`, the publish may have succeeded — set `'uncertain'`, block retry. | `test_pre_action_retry`: kill worker before Instagram API call; restart; assert new attempt succeeds. `test_post_action_no_retry`: kill worker after `media_publish` call; restart; assert item enters `'needs_review'`, no new attempt created. |
| I10 | Frozen inputs for approved actions | `queue_item_snapshots.caption_frozen`, `media_storage_key_frozen`, etc. are immutable after INSERT (no UPDATE path in application code). `cleanup_runs.rule_snapshot` JSONB stores the cleanup rule as confirmed. | `test_snapshot_immutability`: queue item; update `queue_items.caption`; publish; assert receipt's `caption_frozen` matches the original snapshot, not the updated caption. |
| I11 | Daily usage survives restart | `account_daily_usage` persisted in PostgreSQL with `UNIQUE (instagram_account_id, usage_date)`. Incremented in the same transaction as receipt creation: `INSERT INTO account_daily_usage … ON CONFLICT … DO UPDATE SET publish_count = publish_count + 1`. | `test_usage_survives_restart`: publish 3 items; restart worker process; publish 4th; assert `account_daily_usage.publish_count = 4`. |
| I12 | No duplicate source content per account | `UNIQUE (instagram_account_id, external_post_id)` on `source_candidates`. Collection uses `INSERT … ON CONFLICT … DO NOTHING`. | `test_source_dedup`: run source collection; run again with overlapping results; assert `source_candidates` count equals unique external_post_ids, not double. |
| I13 | Restricted features invisible without entitlement | Middleware `requireEntitlement(workspaceId, feature)` queries `workspace_entitlements` (where `revoked_at IS NULL` and `(expires_at IS NULL OR expires_at > now())`) and `plans` (via `subscriptions`). Returns 403 if neither grants the feature. UI queries entitlements from session and conditionally renders. | `test_restricted_api_without_entitlement`: call `POST /api/sources` without entitlement; assert 403. `test_restricted_api_with_entitlement`: grant entitlement; call same route; assert 200. |
| I14 | Customer media private | R2 objects have no public ACL. Media served via signed URLs generated by `getSignedUrl(s3Client, new GetObjectCommand({…}), {expiresIn: 3600})` (`@aws-sdk/s3-request-presigner`). The signing endpoint checks workspace membership before generating the URL. | `test_media_url_expiry`: generate signed URL; wait 3601 seconds; fetch URL; assert HTTP 403. `test_media_cross_workspace`: generate URL as workspace A; fetch as unauthenticated client; assert 403 (URL works only for the signed time, no auth on R2 side, but the signing endpoint enforced membership). |
| I15 | No secrets in logs or receipts | Structured logger (`pino`) configured with `redact: ['*.access_token*', '*.token*', '*.secret*', '*.password*', '*.cookie*']`. `publish_receipts` schema has no token columns. `audit_log.detail` JSONB is constructed explicitly (allow-list fields, never `JSON.stringify(request)`). | `test_no_tokens_in_logs`: publish an item; grep all log output for the test account's decrypted access token string; assert 0 matches. `test_receipt_schema`: inspect `publish_receipts` columns; assert no column name contains 'token' or 'secret'. |
| I16 | Content rights versioned and attributable | `content_rights_acceptances` links user + workspace + version. `queue_items.content_rights_acceptance_id` is NOT NULL FK. No queue item can be created without a valid acceptance. | `test_queue_requires_rights`: attempt to insert `queue_items` with `content_rights_acceptance_id = NULL`; assert NOT NULL violation. `test_rights_version_tracking`: create new rights version; queue item; assert `content_rights_acceptances.version_id` matches the version active at acceptance time. |
| I17 | External callbacks idempotent | `billing_events.stripe_event_id UNIQUE`. Processing: `INSERT … ON CONFLICT (stripe_event_id) DO NOTHING RETURNING id` — if no row returned, event was already processed, handler returns 200. Subscription updates use `WHERE updated_at < $event_timestamp` to prevent stale events from overwriting newer state. | `test_duplicate_webhook`: send same Stripe event payload twice; assert `billing_events` has 1 row; assert subscription state unchanged after second delivery. `test_out_of_order_events`: send `subscription.updated` (T1) then `subscription.deleted` (T2 > T1); then resend `subscription.updated` (T1); assert final status is `'canceled'` (the older event is a no-op). |

---

## 5. Failure-mode walkthrough

### F1 — Crash before publish (no side effect)

**Scenario:** Worker crashes after claiming a queue item but before calling the Instagram API.

1. Worker executes `SELECT id FROM queue_items WHERE instagram_account_id = $1 AND status = 'ready' ORDER BY position LIMIT 1 FOR UPDATE SKIP LOCKED` — row lock acquired.
2. In the same transaction: `UPDATE queue_items SET status = 'publishing'` and `INSERT INTO publish_attempts (queue_item_id, attempt_number, status) VALUES ($1, 1, 'pending')`. Transaction commits.
3. Worker process crashes before calling `POST /{ig-user-id}/media`.
4. pg-boss marks the job as expired after 300 seconds (`expireInSeconds: 300` in job options).
5. pg-boss's retry policy (3 retries, backoff: 60s → 240s → 960s) re-enqueues the job.
6. New worker picks up the retried job. Reads `publish_attempts WHERE queue_item_id = $1 AND status NOT IN ('published','failed')`: finds 1 row with `status = 'pending'`, `ig_container_id IS NULL`.
7. Since `ig_container_id IS NULL`, no Instagram side effect occurred. Worker marks the old attempt as `'failed'` and creates a new attempt with `attempt_number = 2`, proceeding normally.
8. If all 3 retries exhaust without a successful publish: worker sets `queue_items.status = 'failed'`, `publish_attempts.error_category = 'temporary'`.

**Evidence:** `publish_attempts` shows attempt 1 with `status = 'failed'`, `ig_container_id IS NULL`. pg-boss `job` table shows `retrylimit = 3`, `retrycount > 0`. If retries succeeded: `publish_receipts` row exists.

### F2 — Crash after publish may have been accepted (ambiguous side effect)

**Scenario:** Worker calls `POST /{ig-user-id}/media_publish` and the HTTP response never arrives.

1. Worker has a `publish_attempts` row with `status = 'container_ready'`, `ig_container_id = 'CID123'`.
2. Worker updates `publish_attempts.status = 'publishing'`. Transaction commits.
3. Worker calls `POST /{ig-user-id}/media_publish?creation_id=CID123` with a 30-second `AbortController` timeout.
4. Timeout fires. No HTTP response received.
5. Worker detects: `publish_attempts.status = 'publishing'` AND `ig_container_id IS NOT NULL` — this is the ambiguous state.
6. Worker sets `publish_attempts.status = 'uncertain'` and `queue_items.status = 'needs_review'`. Transaction commits.
7. Worker does NOT retry the publish. The `publish_attempts_one_active` index blocks any new non-terminal attempt while `'uncertain'` exists.
8. Worker creates a notification: `INSERT INTO notifications (workspace_id, type, priority, title, link_path) VALUES ($1, 'publish_uncertain', 'high', 'Post may have published — review needed', '/queue/{item_id}')`.
9. Operator or user checks Instagram manually. If the post exists, they reconcile: operator calls `PATCH /api/operator/reconcile/{attempt_id}` which sets `publish_attempts.status = 'published'`, creates a `publish_receipts` row, and sets `queue_items.status = 'published'`. If the post does not exist: operator marks the attempt as `'failed'`, allowing a new attempt.

**Evidence:** `publish_attempts` shows `status = 'uncertain'`, `ig_container_id = 'CID123'`, `ig_media_id IS NULL`. `queue_items.status = 'needs_review'`. `notifications` table has a `priority = 'high'` row for this workspace.

### F3 — Duplicate publish work (concurrent claim)

**Scenario:** User clicks "publish now" while the scheduler simultaneously enqueues a publish job for the same account.

1. Two pg-boss `publish` jobs exist for the same account.
2. Worker A picks up job 1 via `SKIP LOCKED`. Executes: `SELECT id FROM queue_items WHERE instagram_account_id = $1 AND status = 'ready' ORDER BY position LIMIT 1 FOR UPDATE SKIP LOCKED`.
3. Worker A gets item X. Updates `status = 'publishing'`. Inserts `publish_attempts` row. Commits.
4. Worker B picks up job 2. Same SELECT query — item X's `status` is now `'publishing'`, so the `WHERE status = 'ready'` clause excludes it. If another `'ready'` item Y exists, Worker B gets Y (correct behavior — each job publishes the next available item). If no `'ready'` items remain, the SELECT returns 0 rows. Worker B acknowledges the job as complete (no-op).
5. Item X is published exactly once.

**Evidence:** `publish_attempts` has exactly 1 row for item X. `publish_receipts` has exactly 1 row for item X. Worker B's log shows either "claimed item Y" or "no ready items, exiting."

### F4 — Media preparation failure

**Scenario:** FFmpeg exits with error code 1 during video transcoding.

1. Worker picks up `media-prepare` job for upload U.
2. Worker downloads original from R2 `media/{workspace_id}/originals/{U}`.
3. Worker spawns FFmpeg child process. FFmpeg exits with code 1 (e.g., corrupt codec headers).
4. Worker sets `media_uploads.status = 'failed'`, `failure_reason = 'FFmpeg exit code 1: Invalid data found when processing input'`.
5. All `queue_items` referencing `media_upload_id = U` remain at `status = 'preparing'`. They do not advance to `'ready'` because the worker only sets `'ready'` on queue items when `media_uploads.status = 'ready'`.
6. Worker creates notification: `'Media preparation failed for [filename]: Invalid video format'`.
7. User retries by clicking "Retry preparation" → web resets `media_uploads.status = 'uploaded'` and enqueues a new `media-prepare` job. No duplicate queue item is created because the existing `queue_items` row already references this `media_upload_id`.

**Evidence:** `media_uploads.status = 'failed'`, `failure_reason` populated. No `queue_items` with `status = 'ready'` for this upload. Notification exists.

### F5 — Account revocation

**Scenario:** User revokes ToolBox Poster's access from Instagram settings.

1. Worker (or web server) calls Instagram API with this account's token. Instagram returns HTTP 400 with `OAuthException` code 190: "access token is not valid."
2. Error handler sets `instagram_accounts.connection_status = 'revoked'` and `publishing_paused = true`.
3. Scheduler's `check-schedules` cron skips this account: `WHERE publishing_paused = false AND connection_status = 'active'` excludes it.
4. All `queue_items` for this account remain at their current status. No items are deleted or failed.
5. Notification: `'Instagram access revoked for @{username}. Reconnect to resume publishing.'` with `link_path = '/accounts/{id}/reconnect'`.
6. User clicks reconnect → goes through Facebook OAuth → new token obtained. Web updates `access_token_encrypted`, `token_expires_at`, `connection_status = 'active'`, `publishing_paused = false`. The `UNIQUE (ig_user_id)` constraint on `instagram_accounts` ensures the same internal account row is updated (matched by `ig_user_id`), preserving queue and history references.

**Evidence:** `instagram_accounts.connection_status = 'revoked'` (before reconnect). Queue items unchanged. Notification present. After reconnect: `connection_status = 'active'`, publishing resumes on next scheduler tick.

### F6 — Quota exhaustion

**Scenario:** Account reaches 25 publishes in one day.

1. Scheduler enqueues a `publish` job for this account.
2. Worker claims next ready item. Before calling Instagram API: `SELECT publish_count FROM account_daily_usage WHERE instagram_account_id = $1 AND usage_date = CURRENT_DATE`.
3. `publish_count = 25`. Worker reads `instagram_accounts.daily_publish_limit = 25`.
4. `25 >= 25` → worker does not call Instagram API. Releases the claimed item back to `status = 'ready'` (rollback the transaction that changed it to `'publishing'`). Acknowledges the pg-boss job as complete.
5. Scheduler's next tick: computes next due slot. Checks daily usage again. Still at limit. Does not enqueue a new publish job.
6. Notification: `'Daily limit reached for @{username}. 25/25 posts today. Publishing resumes tomorrow.'`
7. Next day (midnight in account timezone, computed by Luxon): `account_daily_usage` row for the new date does not exist → treated as `publish_count = 0`. Scheduler enqueues publish jobs normally.
8. If Instagram itself returns a rate limit error (HTTP 429): worker parses the response, sets `account_daily_usage.publish_count = daily_publish_limit`, and follows the same deferral logic.

**Evidence:** `account_daily_usage` shows `publish_count = 25` for today. Queue items remain `'ready'`. Notification exists. Next-day publishing proceeds.

### F7 — Schedule edits near a scheduled slot

**Scenario:** User changes schedule from "9:00 AM daily" to "2:00 PM daily" at 8:55 AM, 5 minutes before the 9:00 AM slot.

1. User saves new schedule rule. Web updates `schedule_rules` in a transaction.
2. Scheduler's `check-schedules` cron fires within 60 seconds.
3. Scheduler computes the next due slot for this account using the updated rules: next slot is 2:00 PM today.
4. Scheduler checks `schedule_executions` for `(account_id, today, '09:00')`: no row exists (the old 9:00 AM slot was never executed because the scheduler hadn't fired since 8:55 AM). No execution was enqueued for 9:00 AM.
5. Scheduler checks `schedule_executions` for `(account_id, today, '14:00')`: no row exists. Inserts `ON CONFLICT DO NOTHING RETURNING id` → succeeds. Enqueues publish job for 2:00 PM.
6. The 9:00 AM post never fires.

**Edge case — schedule already fired at 9:00 AM (changed at 9:01 AM):**
1. `schedule_executions` already has a row for `(account_id, today, '09:00')`.
2. User changes to 2:00 PM. Scheduler computes: 9:00 AM already executed, next is 2:00 PM. Inserts execution for `'14:00'`. Post published at both 9:00 AM and 2:00 PM — both are valid schedule slots; the first under the old rules, the second under the new rules.

**DST fall-back (1:30 AM occurs twice):**
1. Schedule rule: "Post at 1:30 AM America/New_York."
2. Scheduler computes `DateTime.fromObject({hour: 1, minute: 30}, {zone: 'America/New_York'})` — Luxon resolves to the first occurrence (EDT).
3. `schedule_executions` UNIQUE on `(account_id, date, local_time)` with local_time `'01:30'`: inserted once. The second occurrence of 1:30 AM (EST) computes the same `scheduled_local_time = '01:30'` → `ON CONFLICT DO NOTHING` → no second publish.

**DST spring-forward (2:30 AM does not exist):**
1. `DateTime.fromObject({hour: 2, minute: 30}, {zone: 'America/New_York'})` on spring-forward day: `dt.isValid === false`.
2. Scheduler skips this slot. Next valid slot is 3:00 AM (or the next rule's time).

**Evidence:** `schedule_executions` shows exactly 1 row per (account, date, local_time). No double posts for DST transitions.

### F8 — Duplicate source collection

**Scenario:** Manual "collect now" and scheduled collection fire concurrently.

1. Source worker A picks up job first. Browses Instagram. Finds posts P1, P2, P3.
2. Worker A: `INSERT INTO source_candidates (instagram_account_id, external_post_id, …) VALUES ($acct, 'P1', …) ON CONFLICT (instagram_account_id, external_post_id) DO NOTHING`. Inserts P1, P2, P3 (3 rows).
3. Source worker B picks up second job. Browses Instagram. Finds P1, P2, P3, P4.
4. Worker B: same INSERT for P1, P2, P3 → `ON CONFLICT DO NOTHING` (0 new rows). INSERT for P4 → succeeds (1 new row).
5. Final `source_candidates` count for this account: 4 (P1, P2, P3, P4). Each `external_post_id` exists exactly once.

**Evidence:** `SELECT COUNT(*) FROM source_candidates WHERE instagram_account_id = $1` returns 4. `SELECT COUNT(DISTINCT external_post_id) …` also returns 4.

### F9 — Browser hang during cleanup

**Scenario:** Cleanup worker calls Instagram API to archive a post and the connection hangs.

1. Worker claims `cleanup_run_items` row for item X: `SELECT … FOR UPDATE WHERE status = 'pending'` → `UPDATE status = 'processing'`. Commits.
2. Worker calls `POST /{ig-media-id}` with body `{media_status: 'ARCHIVED'}` and a 30-second `AbortController` timeout.
3. Timeout fires. HTTP response never received.
4. Worker determines: the archive may have succeeded (POST was sent, response unknown).
5. Worker sets `cleanup_run_items.status = 'uncertain'` for item X. Sets `cleanup_runs.status = 'needs_reconciliation'`. Commits.
6. Worker stops processing this run. Remaining items stay at `status = 'pending'`.
7. `cleanup_runs_one_active` partial unique index blocks any new cleanup run for this account.
8. Notification: `'Cleanup paused for @{username} — item needs review. Archive may have succeeded but was not confirmed.'`
9. Operator checks Instagram. Marks item as `'completed'` or `'failed'`. Then sets `cleanup_runs.status = 'running'` to resume, or `'stopped'` to abort.

**Evidence:** `cleanup_run_items` shows `status = 'uncertain'` for item X, `processed_at IS NULL`. `cleanup_runs.status = 'needs_reconciliation'`. Remaining items `status = 'pending'`. Notification with `priority = 'high'`.

### F10 — Changed cleanup selection after confirmation

**Scenario:** User confirms a cleanup of items A, B, C. Before execution begins, user protects item B.

1. User confirms cleanup: `cleanup_runs` created with `selection_hash = SHA-256(sort(['A','B','C']).join(','))`.
2. User protects item B: `UPDATE publish_receipts SET protected = true WHERE id = 'B'`.
3. Cleanup execution begins. Worker loads the run.
4. Before processing each item, worker checks: `SELECT protected FROM publish_receipts WHERE id = $1`.
5. Item A: `protected = false` → process (archive/delete) → `status = 'completed'`.
6. Item B: `protected = true` → `cleanup_run_items.status = 'skipped'`. No Instagram API call made.
7. Item C: `protected = false` → process → `status = 'completed'`.
8. Run completes with 2 completed, 1 skipped.

**Alternative — structural selection change (items added or removed from the run):**
1. At execution start, worker recomputes: load all `cleanup_run_items` IDs for this run, sort, hash.
2. Compares to `cleanup_runs.selection_hash`. If they differ (e.g., operator removed an item), sets `cleanup_runs.status = 'stopped'`, processes 0 items.
3. Notification: `'Cleanup selection changed since confirmation. Please re-confirm.'`

**Evidence:** `cleanup_run_items` for B shows `status = 'skipped'`. `publish_receipts` for B shows `protected = true`. If selection hash mismatch: `cleanup_runs.status = 'stopped'`, all items remain `'pending'`.

### F11 — Repeated or out-of-order billing events

**Scenario:** Stripe sends `invoice.paid` twice, then a stale `customer.subscription.updated` arrives.

1. Stripe webhook arrives: `invoice.paid` with `stripe_event_id = 'evt_001'`, `created = T2`.
2. Web verifies signature: `stripe.webhooks.constructEvent(body, sig, webhookSecret)`.
3. `INSERT INTO billing_events (stripe_event_id, event_type, payload) VALUES ('evt_001', 'invoice.paid', $payload) ON CONFLICT (stripe_event_id) DO NOTHING RETURNING id` → returns `id` (new row).
4. Processing: `UPDATE subscriptions SET status = 'active', current_period_start = $start, current_period_end = $end, updated_at = $T2 WHERE stripe_subscription_id = $sub_id AND updated_at < $T2` → 1 row updated.
5. Stripe retries: same event `evt_001`.
6. INSERT → `ON CONFLICT DO NOTHING` → returns no row. Handler returns HTTP 200 immediately.
7. Later: `customer.subscription.updated` arrives with `stripe_event_id = 'evt_002'`, `created = T1` (where T1 < T2 — this event was created earlier but delivered later).
8. INSERT succeeds (new `stripe_event_id`).
9. Processing: `UPDATE subscriptions SET … updated_at = $T1 WHERE … AND updated_at < $T1`. Since `updated_at = T2` and `T2 > T1`, the WHERE clause matches 0 rows. No update occurs. The stale event is a no-op.

**Evidence:** `billing_events` has 2 rows (`evt_001`, `evt_002`). `subscriptions.updated_at = T2` (the most recent event's timestamp). The stale event's processing produced 0 affected rows (logged by the handler).

### F12 — Storage failure (R2 outage)

**Scenario:** Cloudflare R2 returns HTTP 503 during a media upload.

1. User uploads media. Web streams to R2 via `PutObjectCommand`.
2. R2 returns HTTP 503 Service Unavailable.
3. Web catches the S3 error. `media_uploads.status` remains `'uploading'` (the transaction that would advance it to `'uploaded'` is not committed).
4. Web returns HTTP 502 to the client: `{error: 'Media storage temporarily unavailable. Retry in a few minutes.'}`.
5. No `media-prepare` job is enqueued (status never reached `'uploaded'`).
6. User retries the upload. Web streams to R2 again using the same `media_uploads.id`. If R2 is back: upload succeeds, status advances, prep job enqueued.
7. If R2 is down during media preparation (worker downloads original): worker retries the download 3 times with backoff (30s, 120s, 480s). After 3 failures: `media_uploads.status = 'failed'`, `failure_reason = 'Storage unavailable after 3 retries (630s elapsed)'`.
8. If R2 is down during publishing (Instagram fetches the signed media URL): Instagram container creation fails with a media-fetch error. Worker treats this as `error_category = 'temporary'` and retries via pg-boss's retry policy.

**Evidence:** `media_uploads.status = 'uploading'` (no advancement). Worker logs show retry attempts with timestamps. After R2 recovery: upload retried successfully, `status = 'ready'`.

### F13 — Deletion interrupted halfway

**Scenario:** User requests account deletion. Worker crashes after deleting some R2 objects.

1. `deletion_requests` row created with `status = 'pending'`.
2. Worker picks up `account-deletion` job. Begins processing:
   a. Revoke Instagram tokens: `DELETE https://graph.facebook.com/{user-id}/permissions?access_token={token}`. Success or "already revoked" — idempotent.
   b. Cancel Stripe subscription: `stripe.subscriptions.cancel(subId)`. Success or "already canceled" — idempotent.
   c. Delete R2 objects: `DeleteObjectsCommand` in batches of 1000, keyed by `media/{workspace_id}/*`.
3. Worker crashes at step (c) — 2 of 5 batches completed.
4. pg-boss retries the job.
5. Retry: step (a) returns "already revoked." Step (b) returns "already canceled." Step (c) issues `DeleteObjectsCommand` for all keys — already-deleted objects return success (S3 DELETE is idempotent per the S3 specification: `DeleteObject` for a nonexistent key returns 204).
6. Steps (d)-(f): database deletes use `DELETE FROM {table} WHERE workspace_id = $1` — 0 rows affected for already-deleted tables, normal deletion for remaining tables. User row anonymized: `UPDATE users SET email = 'deleted-' || id, name = 'Deleted User', avatar_url = NULL WHERE id = $1`.
7. `deletion_requests.status = 'completed'`, `completed_at = now()`.

**Evidence:** `deletion_requests.status = 'completed'`. `users` row anonymized. R2 bucket has 0 objects with the workspace prefix (`ListObjectsV2Command` returns empty). `workspace_members`, `queue_items`, etc. have 0 rows for the workspace. Stripe subscription status is `'canceled'`.

---

## 6. AI strategy

AI does not belong in v1.

1. The product's core value chain is deterministic: queue → schedule → publish → receipt. Every step produces an inspectable, reproducible result. AI introduces probabilistic outputs that conflict with the first product promise ("publishes only what the customer intended"). A hallucinated hashtag or a subtly reworded caption that the user did not review violates the contract.

2. The brief explicitly conditions AI inclusion on "a compelling, budgeted, safety-conscious case."

3. Cost at launch scale: caption generation using Claude Haiku at ~1 K input tokens + ~0.5 K output tokens per caption × 500 captions/day = 500 × ($0.80 / 1M × 1000 + $4.00 / 1M × 500) = 500 × ($0.0008 + $0.002) = $1.40/day = $42/month. This exceeds the infrastructure budget ($37.70/month) and provides marginal value — most users have their own captions or copy from sources.

4. The product already provides the features that matter without AI: deterministic media preparation (resize, crop, logo overlay), rule-based scheduling, and human-authored captions.

5. The first AI feature to evaluate post-launch is optimal posting time recommendation, because it is low-risk (a suggestion displayed in UI, not an automated action), bounded in cost (1 inference per account per day), and leverages analytics data the product already collects. It does not belong in v1 because it requires months of analytics history to produce meaningful recommendations.

---

## 7. Testing and release confidence

### On every commit (CI, target: <5 minutes)

- TypeScript compilation: `tsc --noEmit`.
- Lint: ESLint with `@typescript-eslint` and `eslint-plugin-drizzle`.
- Unit tests (Vitest): pure functions for schedule computation (including DST edge cases), selection hash computation, status transition validation, signed URL generation, encryption/decryption round-trip.
- Integration tests (Vitest + `testcontainers` for PostgreSQL 16): all partial unique indexes (attempt duplicate INSERTs, assert constraint violation), `SELECT … FOR UPDATE SKIP LOCKED` concurrency simulation (parallel transactions), billing event dedup (duplicate `stripe_event_id`), cascade deletes, CHECK constraint coverage (every invalid status value).

### On every PR (CI, target: <15 minutes)

All of the above, plus:
- API route integration tests (Next.js test server + supertest): authentication enforcement (unauthenticated → 401), workspace isolation (cross-workspace → 403), entitlement enforcement (restricted API without entitlement → 403), input validation (oversized caption → 400, invalid media type → 400).
- End-to-end queue flow (mocked Instagram API via `msw`): upload image → prepare → queue (snapshot created) → publish (mock returns `ig_media_id`) → receipt created → status transitions verified.
- Invariant tests: every row of the §4 table has a corresponding test file. The CI job fails if any invariant test file is missing or failing.

### Nightly (CI, target: <60 minutes)

- **Full E2E against Instagram sandbox:** Meta provides a test app and test users for Content Publishing API. The nightly run: connect test account → upload test image → prepare → queue → schedule → publish → verify post exists via `GET /{media-id}` → fetch insights → archive post (cleanup).
- **Crash/retry drill:** start publisher worker → mock Instagram to respond slowly (10s) → kill worker process mid-publish → restart → assert reconciliation: item enters `needs_review` if post-action, retries successfully if pre-action.
- **Race condition drill:** 10 parallel workers compete to claim the same queue item. Assert: exactly 1 `publish_attempts` row, exactly 1 receipt (or `needs_review`).
- **Quota drill:** publish 25 items → assert 26th deferred → assert `account_daily_usage.publish_count = 25` → simulate next day → assert publishing resumes.
- **Timezone drill:** create schedule rule at 2:30 AM America/New_York → simulate spring-forward (2:30 AM doesn't exist) → assert slot skipped → simulate fall-back (1:30 AM happens twice) → assert slot fires once.
- **Restore drill:** `pg_dump` the test database → `DROP` and `CREATE` a new database → `pg_restore` → run a smoke test (queue an item, publish, verify receipt). This proves the backup is restorable, not just writable.
- **Ambiguous outcome drill:** mock Instagram `media_publish` to drop the connection after accepting the request → assert `publish_attempts.status = 'uncertain'` → assert `queue_items.status = 'needs_review'` → assert notification created.

### Before major release (manual, against safe test Instagram accounts)

- Full lifecycle: connect → upload (image + video) → prepare → queue → schedule → publish → analytics (wait 6 hours) → cleanup (archive 1 post).
- Token refresh: set token expiry to 1 hour in test → wait → assert auto-refresh.
- Revocation: revoke app access from Instagram settings → assert `connection_status = 'revoked'` → reconnect → assert queue preserved.
- Multi-account: connect 3 test accounts → publish to each → verify tenant isolation (each receipt references correct account).

---

## 8. Delivery phases

### Phase 0 — Foundation

**What gets built:** PostgreSQL schema (all tables), Docker Compose configuration (4 services: caddy, web, worker, postgres), Next.js project scaffold, NextAuth.js magic link authentication, workspace creation, workspace member management, public site skeleton (landing page, waitlist form, legal page placeholders).

**Exit criteria:**
- A user receives a magic link email (via Resend), clicks it, and lands on an authenticated dashboard.
- A workspace exists in `workspaces` with the user as owner in `workspace_members`.
- The waitlist form submits an entry to `waitlist_entries`.
- `docker compose up` starts all services on a clean VPS. Caddy terminates TLS.
- The PostgreSQL schema matches the DDL in §3 (verified by `drizzle-kit check`).

**Justification for placing before Phase 1:** authentication and workspaces are prerequisites for every subsequent feature. The schema must exist before any data is written.

### Phase 1 — Connect + Upload + Prepare

**What gets built:** Instagram account connection (Facebook Login OAuth flow, token exchange, account selection, `instagram_accounts` creation), connection request flow, media upload (streaming to R2), media preparation worker (Sharp + FFmpeg), `media_uploads` lifecycle, `account_preparation_settings`.

**Exit criteria:**
- User connects a professional Instagram account via Facebook Login. `instagram_accounts` row exists with `connection_status = 'active'` and an encrypted access token.
- Connecting the same `ig_user_id` from a second workspace fails with a clear error.
- User uploads a JPEG image (5 MB) and an MP4 video (30 MB). Both appear in `media_uploads` with `status = 'ready'` within 30 seconds (image) / 120 seconds (video).
- The prepared image is resized to 1080px width. The prepared video is transcoded to H.264.
- Uploading a corrupt file (truncated MP4) results in `status = 'failed'` with a human-readable `failure_reason`.
- The original file is retrievable from R2 at `media/{workspace_id}/originals/{upload_id}`.

**Justification for placing before Phase 2:** publishing requires a connected account and prepared media. This phase establishes both.

### Phase 2 — Queue + Schedule + Publish + Receipt (first end-to-end)

**This is the first end-to-end phase.** A user can connect an account (Phase 1), upload and prepare media (Phase 1), queue it, set a schedule, and see it published to Instagram with a receipt.

**What gets built:** queue item creation (with snapshot freezing), queue reordering (drag-and-drop via `@dnd-kit/core`), schedule rules (fixed time + interval), the scheduler cron job, the publish worker (container create → poll → publish → receipt), `publish_attempts` state machine, `account_daily_usage` tracking, `queue_items` status transitions, SSE for live status updates, "publish now" action.

**Exit criteria:**
- User queues a prepared item. `queue_items` row at `status = 'ready'` and `queue_item_snapshots` row with frozen caption and media key.
- User sets a schedule: "Post at 2:00 PM on Monday, Wednesday, Friday, timezone America/New_York." `schedule_rules` row exists. Schedule preview shows next 5 upcoming times.
- At the scheduled time (within 60 seconds), the item publishes. `publish_receipts` row exists with `ig_media_id`, `ig_permalink`, `caption_frozen`. The post is visible on Instagram.
- User clicks "Publish Now" on the next ready item. It publishes within 30 seconds.
- Changing the caption after queueing does not change the frozen snapshot (verified by publishing and checking the receipt).
- Queue status updates appear in the browser without page refresh (SSE verified by observing the status change within 2 seconds).
- Daily usage counter increments correctly: publish 2 items, assert `account_daily_usage.publish_count = 2`.
- Publishing at the daily limit (25) defers the next item without failing it.

### Phase 3 — Library + Analytics

**What gets built:** library view (published posts list with media, caption, permalink, publish date), analytics refresh worker (Instagram Insights API), `analytics_snapshots` table population, analytics dashboard (reach, likes, comments, saves, shares, plays; change over time; best/worst posts), manual refresh button (rate-limited to 1 per account per hour).

**Exit criteria:**
- Library shows all published posts with their Instagram permalink, caption, and media thumbnail.
- Within 6 hours of publishing, `analytics_snapshots` has a row for the post with non-null `reach` and `likes`.
- Analytics dashboard displays a chart of reach over time for a selected account.
- Best/worst posts sorted by reach are displayed correctly.
- Manual refresh enqueues an `analytics-refresh` job and populates a new snapshot row within 60 seconds.
- A second manual refresh within 60 minutes is rejected with HTTP 429.

### Phase 4 — Plans + Billing + Entitlements

**What gets built:** `plans` seed data (Free: 1 account, 0 collaborators, 1 GB; Pro: 3 accounts, 2 collaborators, 10 GB, $15/month or $144/year; Studio: 10 accounts, 5 collaborators, 50 GB, cleanup enabled, $39/month or $374/year), Stripe integration (Checkout Session, Customer Portal, Webhooks), `subscriptions` lifecycle, `workspace_entitlements` (operator-granted), entitlement enforcement middleware, billing page.

**Exit criteria:**
- Free workspace is created with 1-account limit enforced (connecting a 2nd account returns 403 with "upgrade required").
- User completes Stripe Checkout and `subscriptions.status = 'active'` with `plan_id` matching Pro.
- Stripe webhook `invoice.paid` is processed idempotently (send twice, assert 1 `billing_events` row).
- Downgrade from Studio to Pro: cleanup UI disappears (entitlement check), existing cleanup history remains.
- Operator grants `restricted_sourcing` entitlement via operator UI: workspace can access source APIs.
- Billing page shows current plan, renewal date, account usage (2/3), and "Manage Subscription" link to Stripe Portal.

### Phase 5 — Notifications + Feedback + Settings + Deletion

**What gets built:** notification system (SSE-pushed, read/dismiss states, deep links), feedback form (bug, confusing, idea, praise, other), user settings (name, email change), workspace settings (name, slug), account deletion (full erasure per F13), data export (ZIP with JSON manifest + media files + CSV).

**Exit criteria:**
- Publishing failure creates a notification with `priority = 'high'` that appears in the notification bell without page refresh.
- Notification deep-links to the relevant queue item.
- Feedback submission creates a `feedback` row with `page_url` and `page_context` populated.
- Account deletion: request → processing → completed. User row anonymized. R2 objects deleted. Stripe subscription canceled.
- Data export downloads a ZIP containing all queue items (JSON), publish receipts (CSV), and original media files.

### Phase 6 — Operator Dashboard

**What gets built:** operator area (separate route group, `role = 'operator'` middleware, 8-hour session), waitlist management (approve/decline/invite), invitation management, connection request review, user/workspace management (suspend/unsuspend), system health (PostgreSQL connection, R2 connectivity, pg-boss queue depths, last publish timestamp, last analytics refresh), reconciliation tools (mark uncertain publishes as published or failed), temporary entitlement management, feedback review.

**Exit criteria:**
- Operator logs in, lands on health dashboard. All 5 health checks show green.
- Operator approves a waitlist entry → invitation email sent → user registers.
- Operator views an uncertain publish → clicks "Mark as published" with `ig_media_id` → receipt created, item moves to `published`.
- Operator suspends a workspace → all API calls for that workspace return 403 → queue items remain.
- Operator grants `cleanup` entitlement to a workspace → workspace can access cleanup UI.
- pg-boss queue depth displayed: `publish: 0 pending`, `media-prepare: 2 pending`.

### Phase 7 — Restricted Sourcing (entitled, isolated)

**What gets built:** source-worker Docker container, Playwright integration, source session pool (operator-managed), source creation + verification, source collection job, `source_candidates` backlog UI, manual queue-from-backlog, auto-refill, one-time Reels sample.

**Exit criteria:**
- Source-worker container starts, isolated (no `ENCRYPTION_KEY`, 4 GB memory limit).
- Operator adds a session to the pool. Operator creates a source (type: account, identifier: @test).
- Source collection runs. `source_candidates` rows created with `external_post_id`, `external_author`, media downloaded to R2 `sources/` prefix.
- Duplicate collection produces 0 new rows for already-seen posts.
- User views backlog, selects 3 candidates, clicks "Add to queue" → 3 `queue_items` created with `source_candidate_id` reference.
- Auto-refill: set target to 5, queue has 2 items, auto-refill adds 3 from backlog.
- One-time sample: user requests sample → source-worker collects → candidates appear in backlog → no recurring source created (source `status = 'removed'` after completion).
- Removing the workspace's `restricted_sourcing` entitlement → source APIs return 403 → existing backlog and queued items remain.

### Phase 8 — Managed Cleanup (entitled, high-risk)

**What gets built:** cleanup rule creation, cleanup preview (with analytics freshness check: `analytics_snapshots.snapshot_at` within 24 hours), confirmation (with selection hash), cleanup execution worker, item-by-item progress (SSE), stop/resume, post protection, scheduled cleanup (daily/weekly), cleanup history with redacted evidence.

**Exit criteria:**
- User creates cleanup rule: "Archive images older than 30 days with fewer than 100 likes."
- Preview shows 5 matching posts with their current analytics (each metric timestamped). Posts with `analytics_snapshots.snapshot_at` older than 24 hours show a "stale data" warning.
- User confirms. `cleanup_runs` created with `selection_hash`.
- Execution: items processed one at a time. SSE event after each item. 3 of 5 archived, 1 protected (skipped), 1 is a Reel (action = `delete`, user saw warning about Recently Deleted).
- User protects a post mid-run → next item check skips it.
- Stop: user stops run → remaining items stay `'pending'` → `cleanup_runs.status = 'stopped'`.
- Cleanup history shows: rule snapshot, selected items, metrics used, actor, each result. No session cookies or tokens in history.
- Scheduled cleanup: rule enabled for daily at 3:00 AM → runs automatically, re-checks permissions and analytics freshness before proceeding.

---

## 9. Security and privacy

### Identity and authentication

- Magic link email authentication via NextAuth.js `EmailProvider`.
- Session tokens stored as SHA-256 hashes in `sessions.token_hash`. Raw token in `HttpOnly`, `Secure`, `SameSite=Lax` cookie.
- Customer session lifetime: 30 days of inactivity. Operator session lifetime: 8 hours.
- No passwords stored anywhere in the system.
- Rate limit on magic link requests: 5 per email per hour, enforced by counting `sessions.created_at` for the email in the last 60 minutes.

### Authorization and roles

- Three workspace roles: `owner` (billing, destructive admin, invitations, all publishing), `admin` (invitations, queue management, all publishing), `publisher` (queue management, upload, publish).
- Middleware `requireWorkspaceRole(session, workspaceId, minimumRole)` checks `workspace_members` on every API route.
- Entitlement middleware `requireEntitlement(workspaceId, feature)` checks both plan-based features (via `subscriptions` + `plans`) and operator-granted features (via `workspace_entitlements`).
- Hiding a button in the UI is not enforcement. Every restricted capability is enforced at the API route level.

### Workspace isolation

- Every database query that returns user-visible data includes `WHERE workspace_id = $workspaceId` extracted from the authenticated session.
- R2 storage keys are prefixed with `{workspace_id}/` (media) or `{workspace_id}/` (sources). Signed URLs are generated only after workspace membership verification.
- SSE connections are scoped to a workspace: `LISTEN workspace_{id}`. Events from workspace A are never delivered to workspace B's connection.
- pg-boss job payloads include `workspaceId`. Workers verify workspace ownership before processing.

### Secrets management

- Instagram access tokens: AES-256-GCM encrypted at application level. Columns: `access_token_encrypted` (ciphertext), `access_token_iv` (initialization vector), `access_token_tag` (authentication tag). Key: `ENCRYPTION_KEY` environment variable, 32 bytes, generated by `crypto.randomBytes(32).toString('hex')`.
- Source session browser state: AES-256-GCM encrypted with `SOURCE_ENCRYPTION_KEY` (separate from `ENCRYPTION_KEY`). Stored as files at `/data/source-sessions/{session_id}.enc`. Only the source-worker container has access to this key.
- Stripe webhook secret: `STRIPE_WEBHOOK_SECRET` environment variable. Verified via `stripe.webhooks.constructEvent()`.
- All secrets are in environment variables, never in code, database, or logs.

### Private media

- R2 objects have no public ACL. All access via signed URLs.
- Signed URL generation: `getSignedUrl(s3Client, new GetObjectCommand({Bucket, Key}), {expiresIn: 3600})` from `@aws-sdk/s3-request-presigner`. URLs expire after 3600 seconds (1 hour).
- The signing API route checks workspace membership before generating the URL.
- For Instagram publishing: a signed URL (1-hour expiry) is provided as `image_url` or `video_url` in the container creation call. Instagram fetches the media directly from R2.

### Restricted sessions

- Playwright browser state (cookies, localStorage) for restricted sourcing is stored only in the source-worker container's encrypted session files. It is never:
  - Returned in any API response.
  - Stored in `publish_receipts`, `audit_log`, or any user-visible table.
  - Included in structured logs (pino redact config covers `'*.cookie*'`, `'*.session*'`).
- The source-worker container has no access to `ENCRYPTION_KEY` (cannot decrypt Instagram access tokens) and has write-only access to the R2 `sources/` prefix.

### Billing callback security

- Stripe webhook endpoint verifies the `Stripe-Signature` header using `stripe.webhooks.constructEvent(body, sig, STRIPE_WEBHOOK_SECRET)`. Requests with invalid signatures are rejected with HTTP 400.
- Idempotent processing via `billing_events.stripe_event_id UNIQUE` (§4, I17).
- The endpoint accepts only event types it knows: `checkout.session.completed`, `invoice.paid`, `customer.subscription.updated`, `customer.subscription.deleted`, `invoice.payment_failed`. Unknown types are logged and acknowledged with HTTP 200 (no processing).

### Abuse controls

- Upload rate limit: 10 uploads per workspace per minute (counted in memory with a sliding window; lost on web server restart, which briefly allows a burst — acceptable at this scale).
- Publish-now rate limit: 5 per account per hour (checked via `SELECT COUNT(*) FROM publish_attempts WHERE instagram_account_id = $1 AND started_at > now() - interval '1 hour'`).
- Source collection: max 20 candidates per check, min 360-minute interval between checks per source. Max 10 sources per workspace.
- Cleanup: max 1 active run per account (enforced by partial unique index).
- Magic link: 5 per email per hour.
- Waitlist: 1 submission per email (UNIQUE index).
- Invitation: expires after 72 hours.

### Audit

- `audit_log` table records: actor_id, workspace_id, action, target_type, target_id, detail (JSONB with allow-listed fields).
- Logged actions: publish, cleanup, suspend, unsuspend, reconcile, grant_entitlement, revoke_entitlement, invitation_create, invitation_accept, account_connect, account_disconnect, delete_request.
- All operator actions are logged with `actor_id` pointing to the operator's user record.
- Retention: 2 years. Audit log rows are excluded from workspace deletion (anonymized: `actor_id` references the anonymized user row).

### Retention and storage

- Media (R2): retained until workspace deletion or explicit user removal.
- Analytics snapshots: retained indefinitely (small rows: ~100 bytes each).
- Audit log: 2 years.
- Sessions: expired sessions deleted by a daily cleanup job (pg-boss cron).
- pg-boss completed jobs: automatically expired after 30 days (pg-boss `archiveCompletedAfterSeconds: 2592000`).

### Export

- User requests export via `POST /api/workspace/export` → enqueues an `export` job.
- Worker generates a ZIP containing:
  - `manifest.json`: workspace name, export date, item counts.
  - `queue-items.csv`: all queue items with status, caption, timestamps.
  - `receipts.csv`: all publish receipts with ig_permalink, caption, publish date.
  - `analytics.csv`: latest analytics snapshot per receipt.
  - `media/`: all original media files, named by upload ID.
- ZIP is uploaded to R2 with a 24-hour signed URL. Notification delivered to user with download link.

### Deletion

- Per F13 walkthrough: idempotent, all-or-nothing. Revoke tokens → cancel subscription → delete R2 objects → delete database rows → anonymize user → mark completed.
- `deletion_requests` table provides the status reference without retaining deleted data.
- Minimum non-identifying evidence retained: `deletion_requests` row with `user_id` (pointing to anonymized user), `requested_at`, `completed_at`.

### Support access

- The operator area uses the same auth system with `users.role = 'operator'`.
- Operator cannot view decrypted Instagram access tokens (no UI or API exposes decrypted tokens).
- Operator can view: queue item status, publish attempt history, receipt details, analytics, cleanup history. All scoped to the workspace they are investigating.
- All operator actions are logged in `audit_log`.

---

## 10. Risk register

| # | Risk | Early Warning | Mitigation in Plan |
|---|------|---------------|--------------------|
| 1 | Meta deprecates Content Publishing API or revokes our app access | Meta developer platform deprecation notices; API version sunset timeline (Graph API versions are supported for 2 years) | API version pinned; the product uses only documented, stable endpoints. Migration path: adopt replacement API within the 2-year sunset window. |
| 2 | Instagram rate limits tighten, reducing publish capacity below customer expectations | HTTP 429 frequency increases; Meta developer blog posts | `account_daily_usage` is configurable per account. Limit is a deferral, not a failure. Operator can reduce limits preemptively. |
| 3 | Meta bans our app for restricted sourcing browser automation | App review rejection; `source_sessions` enter `'blocked'` state | Restricted sourcing is isolated in a separate container and entitlement. Removal stops sourcing but does not affect the public publishing product. No code path in the `web` or `worker` processes depends on source-worker availability. |
| 4 | Single VPS hardware failure (disk, RAM, network) | UptimeRobot HTTP health check (`GET /api/health`) fails 3 consecutive times (3 minutes) → SMS alert to founder | Daily PostgreSQL backup to R2 (`pg_dump --format=custom | gzip | PutObjectCommand`). Media already stored in R2 (off-VPS). Recovery: provision new Hetzner VPS, restore from backup, redeploy Docker Compose. RTO: 1 hour. RPO: 24 hours. |
| 5 | Stripe webhook delivery failure causes subscription state desync | Stripe Dashboard → Webhooks → Failed deliveries count > 0 | Stripe retries failed webhooks for 72 hours with exponential backoff. Idempotent processing (§4, I17). Manual reconciliation: operator compares Stripe dashboard to `subscriptions` table and forces state update. |
| 6 | Encryption key compromise leaks Instagram access tokens | No reliable early warning for key compromise | Key rotation procedure: generate new key → re-encrypt all tokens (`SELECT id, access_token_encrypted, access_token_iv, access_token_tag FROM instagram_accounts` → decrypt with old key → encrypt with new key → UPDATE) → swap `ENCRYPTION_KEY` env var → restart. Old key is valid for reads during the rotation window (dual-key read support in the decrypt function). |
| 7 | Media storage costs exceed budget as users accumulate backlog | R2 billing dashboard; monthly cost projection (45 GB/month growth at $0.015/GB = $0.675/month incremental) | Storage quota per plan: Free 1 GB, Pro 10 GB, Studio 50 GB. Upload rejected with 402 when quota exceeded (`SELECT SUM(file_size_bytes) FROM media_uploads WHERE workspace_id = $1`). Storage cost trajectory: $8.10/month at 540 GB (12-month steady state). |
| 8 | Customer uploads copyrighted content; DMCA takedown received | DMCA takedown notice to the security contact email | Content rights acceptance required before every upload (`queue_items.content_rights_acceptance_id NOT NULL`). DMCA response: operator removes specific media, notifies user, logs action. If repeated: workspace suspended. Legal pages include copyright policy. |
| 9 | Restricted sourcing used for mass content theft at scale | Per-workspace source count > 10 (limit enforced); per-source candidates > 20/check (limit enforced); operator reviews source creation | Source limits: 10 sources per workspace, 20 candidates per check, minimum 6-hour interval. Operator must verify sources before activation. Source attribution retained (`source_candidates.external_author`, `external_permalink`). Instant revocation: set `workspace_entitlements.revoked_at`. |
| 10 | Founder unavailability (bus factor = 1) | N/A (cannot detect in advance) | Automated daily backups (no manual step). All infrastructure credentials stored in a shared 1Password vault accessible by the non-technical operator. Runbook document covers: restarting Docker Compose, checking health endpoint, suspending a workspace, accessing Stripe Dashboard. The operator can perform triage without SSH or database access. |

---

## 11. Explicit tradeoffs

### T1 — Single-region deployment

The brief says "no requirement for global, multi-region operation at launch." The plan deploys to a single Hetzner datacenter (Ashburn, Virginia). A datacenter outage causes full downtime. Media in R2 is globally distributed (Cloudflare's network), so media is recoverable even during a VPS outage. The single-region tradeoff is acceptable because the brief targets one geographic region and a one-person operation; multi-region doubles the infrastructure complexity (database replication, DNS failover, container orchestration).

### T2 — 24-hour RPO for database

PostgreSQL backups are daily `pg_dump` to R2 at 03:00 UTC. Data loss window: up to 24 hours. WAL archiving to R2 could provide point-in-time recovery (sub-minute RPO) but adds continuous WAL upload, retention management, and a more complex restore procedure. The 24-hour RPO is acceptable because at launch scale (hundreds of publishes/day), a 24-hour data loss is reconstructable: Instagram posts still exist on Instagram, media still exists in R2, and users can re-queue lost items. Upgrade to WAL archiving when daily publish volume exceeds 1,000.

### T3 — No real-time collaborative editing

SSE pushes status changes (queue status, publish progress, notifications) to all workspace members. Two users editing the same caption concurrently see last-write-wins (the `row_version` column on `queue_items` detects conflicts: `UPDATE … WHERE row_version = $expected`, returns 0 rows if stale, client refetches and shows the other user's edit). Full CRDT/OT collaborative editing is not implemented. This is acceptable because the brief's "feel live" requirement targets queue status and publish progress, not simultaneous text editing. At launch, most workspaces have 1-2 users.

### T4 — Source worker has unrestricted internet access

The source-worker Docker container can reach any internet host, not just Instagram domains. Network-level domain restriction on Docker requires iptables rules or a transparent proxy, adding operational complexity. The isolation is at the credential level (no `ENCRYPTION_KEY`, no customer media R2 access) and resource level (4 GB memory, 2 CPUs). This is acceptable because the source worker's code only connects to Instagram; a compromised source worker cannot access encrypted Instagram tokens or customer media.

### T5 — Schedule changes take effect within 60 seconds

The scheduler cron fires every 60 seconds. A schedule change made 1 second before a slot may not prevent the slot from firing (if the scheduler already inserted a `schedule_executions` row). The worst case: one post publishes on the old schedule's last slot. This is acceptable because the user can pause the account (instant effect) if they need to prevent any publishing. The 60-second granularity matches the product's "queue, not calendar" positioning — precision below 1 minute is not the product's value proposition.

### T6 — Analytics freshness not guaranteed by SLA

The brief says cleanup previews use "recent-enough analytics." The plan requires `analytics_snapshots.snapshot_at` within 24 hours at confirmation time but does not guarantee refreshes complete within any SLA. If the Instagram Insights API is slow or rate-limited, analytics may be older than 24 hours. The cleanup preview displays the actual `snapshot_at` timestamp, and the user decides whether to proceed. This is acceptable because the user has full visibility and makes the final confirmation.

### T7 — Upload rate limit resets on web server restart

The 10-uploads-per-minute rate limit is tracked in-process memory (a `Map` keyed by workspace_id with a sliding window). A web server restart resets the counter, briefly allowing a burst. At launch scale with 200 WAU and a single web server process, restarts are infrequent (deployments only). Moving the rate limit to PostgreSQL (a `rate_limit_events` table) adds a write per upload for minimal benefit. This is acceptable because the rate limit protects against accidental bursts, not determined abuse.

---

## 12. Where this is stronger than required

### S1 — Idempotent deletion process

The brief requires "a status reference without retaining the deleted data in disguise." The plan (F13) makes every deletion step idempotent: Instagram token revocation, Stripe cancellation, R2 deletion, and database deletion all succeed or return "already done" on retry. A crash at any point in the deletion process does not require manual database cleanup — the retried job completes correctly. Cost: no additional code (all external APIs and `DELETE FROM` are inherently idempotent). Value: eliminates a class of support requests where a user reports "my deletion is stuck."

### S2 — Selection hash for cleanup confirmation

The brief says "if the selection changes before execution, the old confirmation is no longer valid" but does not specify a detection mechanism. The plan computes `SHA-256(sorted(item_ids).join(','))` at confirmation time and stores it in `cleanup_runs.selection_hash`. At execution time, the worker recomputes the hash and compares. A mismatch stops the run without processing any items. Cost: one SHA-256 computation (~5 microseconds) per confirmation and execution start. Value: cryptographic detection of selection tampering or race conditions.

### S3 — Timestamp-ordered billing event processing

The brief says "repeated or out-of-order billing notifications must result in one understandable subscription state." The plan uses `WHERE updated_at < $event_timestamp` on subscription updates, which prevents stale events from overwriting newer state even when Stripe delivers events out of order. Simple deduplication (by event ID) only prevents exact duplicates; the timestamp ordering handles the harder case of different events arriving in reverse chronological order. Cost: one additional WHERE clause per webhook. Value: correct subscription state without manual reconciliation, even under Stripe delivery anomalies.

### S4 — Queryable audit log

The brief requires "privileged actions are attributable to an actor." The plan provides a queryable `audit_log` table indexed by `(workspace_id, created_at)` with structured `detail` JSONB. This goes beyond attribution (who did it) to provide investigation (what was the state at that time). Cost: one INSERT per privileged action (~100 bytes). At 5,000 actions/week: 26 MB/year. Value: the operator can investigate customer issues without reading raw database rows.

### S5 — DST double-post prevention via local-time dedup

The brief says "daylight saving changes… must not cause surprise double posts." The plan deduplicates schedule executions by `(account_id, date, local_time)` rather than by UTC timestamp. During DST fall-back, when a local time occurs twice (at two different UTC times), the UNIQUE constraint fires on the second occurrence. This is stronger than the brief requires because a UTC-based dedup would allow the second occurrence. Cost: one additional column (`scheduled_local_time TIME`) on `schedule_executions`. Value: zero double-posts during DST transitions, without requiring the user to understand DST.

---

## 13. Assumptions

1. **Target geographic region:** The United States. The VPS is in Hetzner's Ashburn, Virginia datacenter. Timezone defaults and DST rules use IANA timezone database and are region-agnostic. *(Referenced in §1.13.)*

2. **Professional Instagram accounts only:** "Professional" means Business or Creator account types as defined by Meta. Personal accounts are excluded per the brief. *(Referenced in §1.8.)*

3. **Plan pricing and limits:** Free: 1 account, 0 collaborators, 1 GB storage, $0. Pro: 3 accounts, 2 collaborators, 10 GB, $15/month or $144/year. Studio: 10 accounts, 5 collaborators, 50 GB, cleanup, $39/month or $374/year. Restricted sourcing is operator-entitled, not plan-based. *(Referenced in §8 Phase 4.)*

4. **Daily publish limit default:** 25 posts per account per day. Instagram's actual limit is undocumented and varies. The limit is configurable per account by the operator. *(Referenced in §3, §5 F6.)*

5. **Accepted media formats:** Images: JPEG, PNG (per Instagram Content Publishing API). Videos: MP4, MOV (H.264 codec). Other formats rejected at upload time. *(Referenced in §1.7.)*

6. **Session lifetime:** Customer sessions: 30 days of inactivity. Operator sessions: 8 hours. No sliding refresh — after expiry, a new magic link is required. *(Referenced in §1.11, §9.)*

7. **Domain and TLS:** A single custom domain (e.g., toolboxposter.com). TLS via Let's Encrypt, managed by Caddy. *(Referenced in §1.14.)*

8. **Magic link authentication only:** No password, no social login for app authentication. Instagram OAuth is used only for account connection, not app login. *(Referenced in §1.11.)*

9. **Non-technical operator uses web UI:** The operator uses the operator area of the web application for all support and admin tasks. No CLI, SSH, or database console access is required for normal operations. *(Referenced in §8 Phase 6.)*

10. **Content rights text provided externally:** The text of the content rights acceptance is written by legal counsel and entered via the operator UI. The plan provides the versioned storage mechanism (`content_rights_versions` table) but not the legal text. *(Referenced in §3.)*

11. **Export format:** ZIP file containing `manifest.json`, `queue-items.csv`, `receipts.csv`, `analytics.csv`, and a `media/` directory of original files. No proprietary format. *(Referenced in §9.)*

12. **Analytics refresh interval:** Every 6 hours per account. Instagram Insights API rate limit: 200 calls per user per hour. At 500 published posts refreshed 4×/day: 2,000 calls/day = ~83 calls/hour, within the limit. *(Referenced in §2.)*

13. **Source session pool managed by operator:** The operator maintains Instagram browsing sessions (cookies) for restricted sourcing. These are entered via the operator UI and stored encrypted. The product does not collect Instagram passwords for this purpose. *(Referenced in §2, §3.)*

14. **Backup schedule and retention:** Daily `pg_dump --format=custom` piped through `gzip` and uploaded to R2 `backups/` prefix at 03:00 UTC. Retention: 30 daily backups. Operator verifies restore capability monthly by restoring to a temporary database on the VPS. *(Referenced in §9.)*

15. **Reels duration limit:** Instagram Reels maximum duration is 90 seconds. Videos longer than 90 seconds are trimmed by FFmpeg (`-t 90`). *(Referenced in §1.7.)*

16. **Invitation expiry:** 72 hours. After expiry, a new invitation must be sent. *(Referenced in §9.)*

17. **Luxon DST resolution:** Luxon 3's `DateTime.fromObject()` resolves ambiguous local times (DST fall-back) to the first occurrence. Nonexistent local times (DST spring-forward) produce `isValid === false`. The scheduler relies on these behaviors. *(Referenced in §1.17, §5 F7.)*

18. **pg-boss job retention:** Completed jobs expire after 30 days (`archiveCompletedAfterSeconds: 2592000`). Failed jobs are retained for investigation via the pg-boss `job` table. *(Referenced in §9.)*

19. **Health endpoint checks:** `GET /api/health` returns 200 if PostgreSQL responds to `SELECT 1` within 5 seconds, R2 responds to `HeadBucketCommand` within 5 seconds, and the most recent pg-boss job completion (`SELECT MAX(completedon) FROM pgboss.job`) is within the last 10 minutes. UptimeRobot checks this endpoint every 60 seconds; 3 consecutive failures trigger an SMS. *(Referenced in §10.)*

20. **Concurrent SSE connections:** Up to 200 concurrent SSE connections to the web server process. Node.js `EventSource` connections are long-lived HTTP responses; 200 is well within Node.js's default capacity. If concurrent connections exceed 1,000, a separate SSE process or Redis PubSub fan-out is needed (out of scope for v1). *(Referenced in §1.12.)*
