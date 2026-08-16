# ToolBox Poster — Engineering Plan

---

## 1. Technology Decisions

### 1.1 Language and Runtime

**Choice:** TypeScript 5 throughout, Node.js 20 LTS.
**Rejected:** Python (FastAPI). Python has better ML tooling, but TypeScript's single-language stack lets one engineer share types, validation schemas (Zod), and business logic between the Next.js web layer and the Node.js worker processes without a code-generation step. The workers are I/O-bound (Instagram API, Postgres, R2); Python's async story adds friction without throughput benefit.
**Why:** Shared types enforce the contract between web and workers at compile time. A single-person engineering team cannot afford a bug category that only manifests at the boundary between two runtimes.

### 1.2 Web Framework

**Choice:** Next.js 14 (App Router), deployed as a Node.js server (not serverless).
**Rejected:** Remix. Both ship file-based routing and server components; Remix's architecture marginally favors progressive enhancement but has a smaller ecosystem and less mature App Router equivalent. The deciding factor is deployment: Next.js running as a long-lived Node.js process on Railway handles Server-Sent Events natively; serverless Next.js (Vercel) does not sustain SSE connections across function timeouts.
**Why:** SSE connections for live queue updates require persistent connections. The worker processes and web server share the same Postgres connection pool configuration without a separate runtime.

### 1.3 Database

**Choice:** PostgreSQL 16 (Railway managed, 1 GB RAM, 10 GB storage tier at $20/month).
**Rejected:** SQLite via Turso or Litestream. SQLite with Litestream is genuinely simpler and would survive at this launch scale for reads. Rejected because the queue worker claim pattern (`SELECT … FOR UPDATE SKIP LOCKED`) has no SQLite equivalent, and the invariant map in §4 depends on advisory locks and conditional updates under concurrent writers. PostgreSQL's row-level locking is load-bearing, not a preference.
**Why:** Every hard rule in §4 that involves "only one worker" or "only one attempt" resolves to a PostgreSQL transaction with `FOR UPDATE`. Removing that requires a distributed lock service, which adds cost and a new failure mode.

### 1.4 Job Queue

**Choice:** pg-boss 9 (PostgreSQL-backed job queue, npm package).
**Rejected:** BullMQ (Redis). BullMQ is mature and fast, but adds a second stateful service (Redis) that requires its own backup, monitoring, and recovery story. pg-boss runs entirely inside the existing Postgres instance; jobs are durable the moment the transaction that created them commits. This means creating a queue item and enqueuing its media-prep job is a single atomic transaction. With BullMQ, the job insertion and the Postgres row insertion are in different systems — if one commits and the other fails, work is either lost or orphaned.
**Why:** Transactional job creation eliminates an entire class of "item exists but job was never created" bugs that would otherwise require a periodic reconciliation sweep.

### 1.5 Object Storage

**Choice:** Cloudflare R2 (S3-compatible, $0.015/GB/month storage, $0 egress).
**Rejected:** AWS S3. S3 charges $0.09/GB egress to the internet. At launch, storage grows at roughly 200 WAU × 3 posts/week × 2 versions (original + prepared) × 8 MB average = 10 GB/month new storage. At 6 months: 60 GB stored, with serving peaks during publishing. R2 egress savings at 60 GB served/month: $5.40/month. Over 12 months that is $40 saved for zero additional operational work. The aws-sdk/client-s3 package works with R2 via a custom endpoint. `PutObjectCommand`, `GetObjectCommand`, and `DeleteObjectCommand` are the only operations used.
**Why:** Zero egress cost is a direct infrastructure budget reduction for a media-heavy product on a few-hundred-dollars-per-month budget.

### 1.6 Media Processing

**Choice:** FFmpeg 6 via the `fluent-ffmpeg` npm wrapper, running in the media worker process.
**Rejected:** AWS MediaConvert or Mux. MediaConvert costs $0.0075/minute for SD video. At 200 WAU × 3 videos/week × 30 seconds average = 900 minutes/week = $6.75/week = $27/month. FFmpeg on a Railway $10/month service (2 vCPUs, 2 GB RAM) handles 2 concurrent 30-second transcodes in under 60 seconds, well within the 5-minute pg-boss job timeout. The break-even vs. managed transcoding is roughly 3,600 minutes/month; launch volume is ~3,600 minutes/month, so FFmpeg wins now and has room to grow.
**Why:** FFmpeg is free, runs on existing infrastructure, and the budget constraint makes managed transcoding an unnecessary spend at launch scale. `ffprobe` (bundled with FFmpeg) provides format validation. The worker calls `ffmpeg().input(inputPath).outputOptions([...]).output(outputPath).run()` via fluent-ffmpeg.

### 1.7 Authentication

**Choice:** Lucia Auth v3 (self-hosted, framework-agnostic session library).
**Rejected:** Clerk. Clerk provides excellent UI components and handles OAuth flows, but costs $25/month at the Starter plan and stores user identity in Clerk's infrastructure — meaning deletion requests require a Clerk API call and trust in their deletion guarantees. Lucia Auth stores sessions in a `sessions` Postgres table; deletion is a `DELETE` statement that we own.
**Why:** Data sovereignty over user identity is required for the deletion workflow in §3.10 and the privacy guarantees in §9. Lucia Auth's `createSession`, `validateSession`, and `invalidateSession` are the only three functions called in the hot path.

### 1.8 Billing

**Choice:** Stripe (Billing, Customer Portal, Webhooks).
**Rejected:** Paddle. Paddle is a Merchant of Record and handles VAT automatically, which is appealing for a global launch. For an invite-only beta in one geographic region with a handful of paying customers, the MoR benefit is premature. Stripe has better webhook documentation, idempotency key support, and the customer portal handles plan changes without custom UI. Switching to Paddle later requires migrating subscription records but not rebuilding billing logic.
**Why:** Stripe's `stripe.webhooks.constructEvent(payload, sig, secret)` verifies every inbound event. The idempotent billing event log (§3) handles Stripe's "at least once" delivery guarantee.

### 1.9 Email

**Choice:** Resend (transactional email API).
**Rejected:** SendGrid. SendGrid's free tier is 100 emails/day; Resend's free tier is 3,000/month. At launch scale — invitations, publish failure alerts, billing receipts — 3,000 emails/month is sufficient. Resend's API is `POST /emails` with a JSON body; no SDK setup required.
**Why:** Free tier covers launch volume. The `resend` npm package's `emails.send({ from, to, subject, html })` is the only call used.

### 1.10 Real-Time Queue Updates

**Choice:** Server-Sent Events (SSE) over HTTP/1.1 from the Next.js server, backed by Postgres `LISTEN`/`NOTIFY`.
**Rejected:** WebSockets via socket.io. WebSockets require a sticky-session load balancer or a Redis pub/sub adapter when running multiple web instances. At launch, one Railway web instance handles all connections. SSE is a standard HTTP streaming response; the browser's `EventSource` API reconnects automatically. Workers call `pg_notify('queue_updates', json_build_object('workspace_id', workspace_id, 'item_id', queue_item_id)::text)` inside the transaction that changes a queue item's status. The Next.js SSE route holds a Postgres `LISTEN queue_updates` connection and fans out to connected clients filtered by workspace.
**Why:** SSE fits single-server deployment without a message broker. `pg_notify` is transactional: the notification fires only if the transaction commits, preventing phantom updates from rolled-back changes.

### 1.11 Restricted Sourcing Automation

**Choice:** Playwright (Node.js, Chromium), running in an isolated worker process.
**Rejected:** HTTP scraping via `got` + `cheerio`. Instagram's web responses are JavaScript-rendered; static HTTP scraping returns login walls or incomplete data. Playwright's `browserContext.storageState({ path })` persists cookies and localStorage to disk so the session survives process restarts. `browserContext.storageState` does not persist DOM state; the collection job renavigates to the target page on each run.
**Why:** Playwright is the only mechanism that can complete Instagram's JavaScript-rendered browsing experience without Instagram's API. The isolation (separate process, separate DB queue, separate R2 prefix, no access to customer tokens) is described in §2.

### 1.12 Hosting Platform

**Choice:** Railway (one web service + five worker services + managed Postgres).
**Rejected:** Render. Both are comparable PaaS options. Railway's `railway.toml` service definitions make it easier to manage six services from one git repository without writing separate Dockerfiles for each. Railway's Postgres add-on runs in the same region as the services, keeping Postgres latency under 2 ms for same-region deploys.
**Why:** Six processes from one repository with a $5–$20/month price per service fits the stated budget and single-engineer operation. Monthly estimate: web ($10) + 5 workers ($5 × 5 = $25) + Postgres ($20) + R2 (near $0 at launch) + Resend ($0) + Stripe (2.9% + $0.30 per transaction) = ~$55–$75/month fixed, well within budget.

---

## 2. System Architecture

Six independently running processes; work moves through Postgres (pg-boss jobs and status columns).

### 2.1 Web Process (Next.js)

Handles all customer-facing pages, the operator admin area, and API routes. Responsible for: authentication, workspace CRUD, invitation acceptance, IG OAuth redirect and callback, media upload initiation (issues presigned R2 URLs via `PutObjectCommand` with a 15-minute expiry), queue display, schedule management, subscription checkout (Stripe `checkout.sessions.create`), SSE endpoint for live updates, and all mutation endpoints. Enqueues pg-boss jobs for media preparation but does not execute them. Never executes FFmpeg, calls the Instagram Graph API, or runs Playwright.

### 2.2 Media Worker

Subscribes to the `media_prep` pg-boss queue. For each job: downloads the original from R2 via `GetObjectCommand` into a temp file, runs FFmpeg to produce a publish-ready version (resized, reformatted, metadata stripped, optional logo applied), uploads the prepared file to R2, updates `prepared_media.status = 'ready'`, and transitions the associated `queue_items` row from `preparing` to `ready`. Concurrency: 2 simultaneous FFmpeg jobs. Per-job timeout: 5 minutes (`expireInSeconds: 300` in pg-boss). On FFmpeg failure, sets `prepared_media.status = 'failed'` and `queue_items.status = 'failed'` with failure reason.

### 2.3 Publishing Worker

Subscribes to the `publish_next` pg-boss queue. On each tick (every 60 seconds via pg-boss `schedule`), queries for accounts with `ready` items past their scheduled slot, checks daily capacity, claims one item per account with `SELECT … FOR UPDATE SKIP LOCKED`, sets `status = 'publishing'` with a lease token and `lease_expires_at = now() + INTERVAL '10 minutes'`, calls the Instagram Graph API Content Publishing endpoint, and records the outcome. Concurrency: 10 concurrent claims across accounts. No FFmpeg, no Playwright. A separate pg-boss job (`lease_recovery`, runs every 5 minutes) finds items where `status = 'publishing' AND lease_expires_at < now()`, sets them to `needs_review`, and creates a high-priority notification.

### 2.4 Analytics Worker

Subscribes to the `fetch_analytics` pg-boss queue. Jobs are enqueued with 24-hour delay after publishing, then again at 7 days and 30 days. Calls Instagram Insights API (`/{ig-media-id}/insights`) with the token for that account, inserts an `analytics_snapshots` row. Rate-limited to 200 API calls/hour across all accounts (240 seconds minimum spacing enforced by pg-boss `singletonKey` per account). A manual-refresh request enqueues a job with `priority: 10`; pg-boss prevents duplicates via `singletonKey`.

### 2.5 Restricted Sourcing Worker (Isolated)

Entirely separate process with a separate pg-boss queue (`restricted_source_run`). Has no access to customer Instagram tokens (reads `instagram_tokens` only for the operator-owned scraping pool, not customer accounts). Playwright browsers run here only. Manages `browserContext.storageState({ path: '/data/sessions/{scraping_account_id}.json' })` for session persistence. Concurrency: 4 Chromium contexts (4 GB RAM container ÷ ~900 MB per Chromium context with headless mode, leaving 400 MB for the worker process). Rate limit: 60 page navigations/hour per scraping account. On discovery, inserts `source_candidates` rows; does not touch `queue_items` directly — the web layer or a separate refill job moves candidates into the queue on user request or auto-fill.

### 2.6 Cleanup Worker

Subscribes to the `cleanup_item` pg-boss queue. Processes one item per account per run (enforced by `cleanup_runs_one_active_per_account` index). Calls the Instagram Graph API (`DELETE /{ig-media-id}` for Reels, `POST /{ig-media-id}?archive=true` for feed photos). Records `request_sent_at` before sending, records `ig_response` after. On ambiguous outcome, sets `cleanup_run_items.status = 'uncertain'` and `cleanup_runs.status = 'needs_review'`; the unique index blocks any subsequent cleanup run for that account until the operator reconciles.

### 2.7 Work Isolation

Customer pages, media prep, and analytics share the same Postgres instance but have no overlapping pg-boss queues. The restricted sourcing worker connects to the same Postgres database but reads only from `restricted_sources`, `source_candidates`, and the scraping-pool token rows — it cannot read `instagram_tokens` rows where the `pool_type` is `customer`. A Postgres row-level check enforces this: `CREATE POLICY sourcing_worker_token_access ON instagram_tokens FOR SELECT TO sourcing_worker_role USING (pool_type = 'scraping')`. Heavy FFmpeg jobs run in the media worker process; they cannot delay publishing because they are in a different process with a separate pg-boss queue. Publishing claims (`SELECT FOR UPDATE SKIP LOCKED`) do not block media prep jobs because they target different tables.

---

## 3. Data Model

```sql
-- Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ─── Identity ────────────────────────────────────────────────────────────────

CREATE TABLE users (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email               TEXT NOT NULL UNIQUE,
    email_verified_at   TIMESTAMPTZ,
    name                TEXT NOT NULL,
    is_operator         BOOLEAN NOT NULL DEFAULT false,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at          TIMESTAMPTZ
);

CREATE TABLE sessions (
    id          TEXT PRIMARY KEY,           -- Lucia: 40-char random hex
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX sessions_user_id_idx ON sessions(user_id);

-- ─── Waitlist and Invitations ─────────────────────────────────────────────────

CREATE TABLE waitlist_entries (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email       TEXT NOT NULL UNIQUE,
    niche       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    invited_at  TIMESTAMPTZ,
    notes       TEXT          -- operator-only; never returned to requester
);

CREATE TABLE invitations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    token           TEXT NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(32), 'hex'),
    email           TEXT NOT NULL,
    workspace_id    UUID,               -- NULL = invitation creates a new workspace
    role            TEXT NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'admin', 'member')),
    invited_by      UUID REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL DEFAULT now() + INTERVAL '7 days',
    accepted_at     TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ
);
-- Only one un-consumed invitation per email at a time (enforced in application at INSERT
-- after SELECT WHERE accepted_at IS NULL AND revoked_at IS NULL AND email = $1).

-- ─── Workspaces ──────────────────────────────────────────────────────────────

CREATE TABLE workspaces (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    slug            TEXT NOT NULL UNIQUE,
    suspended_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at      TIMESTAMPTZ
);

CREATE TABLE workspace_members (
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'member')),
    joined_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at      TIMESTAMPTZ,
    PRIMARY KEY (workspace_id, user_id)
);

CREATE TABLE onboarding_profiles (
    workspace_id        UUID PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    operation_type      TEXT,   -- 'solo', 'agency', 'brand', 'theme_page', 'local_business'
    niche               TEXT,
    main_goal           TEXT,
    desired_cadence     TEXT,   -- 'daily', '3x_week', '1x_week'
    completed_at        TIMESTAMPTZ
);

-- ─── Entitlements ─────────────────────────────────────────────────────────────

CREATE TABLE workspace_entitlements (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    feature         TEXT NOT NULL CHECK (feature IN ('restricted_sourcing', 'managed_cleanup', 'extra_seats')),
    granted_by      UUID NOT NULL REFERENCES users(id),
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ,
    revoked_at      TIMESTAMPTZ,
    notes           TEXT
);
-- Only one active entitlement per feature per workspace.
CREATE UNIQUE INDEX entitlements_one_active
    ON workspace_entitlements(workspace_id, feature)
    WHERE revoked_at IS NULL;

-- ─── Instagram Accounts ───────────────────────────────────────────────────────

CREATE TABLE account_connection_requests (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    requested_by            UUID NOT NULL REFERENCES users(id),
    instagram_username      TEXT NOT NULL,
    reason                  TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'approved', 'declined')),
    operator_decision_at    TIMESTAMPTZ,
    operator_notes          TEXT,   -- never returned to requester
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE instagram_accounts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        UUID NOT NULL REFERENCES workspaces(id),
    instagram_user_id   TEXT NOT NULL UNIQUE, -- IG's stable ID; UNIQUE prevents same account in two workspaces
    username            TEXT NOT NULL,
    name                TEXT,
    profile_picture_url TEXT,
    account_type        TEXT NOT NULL DEFAULT 'BUSINESS' CHECK (account_type IN ('BUSINESS', 'CREATOR')),
    timezone            TEXT NOT NULL DEFAULT 'UTC',
    daily_publish_limit INTEGER NOT NULL DEFAULT 25
                            CHECK (daily_publish_limit BETWEEN 1 AND 25),
    paused              BOOLEAN NOT NULL DEFAULT false,
    paused_at           TIMESTAMPTZ,
    paused_by           UUID REFERENCES users(id),
    connection_status   TEXT NOT NULL DEFAULT 'connected'
                            CHECK (connection_status IN ('connected', 'expired', 'revoked', 'disconnected')),
    connected_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_health_check_at TIMESTAMPTZ,
    deleted_at          TIMESTAMPTZ
);
CREATE INDEX ig_accounts_workspace_idx ON instagram_accounts(workspace_id) WHERE deleted_at IS NULL;

CREATE TABLE instagram_tokens (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instagram_account_id        UUID NOT NULL REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    pool_type                   TEXT NOT NULL DEFAULT 'customer'
                                    CHECK (pool_type IN ('customer', 'scraping')),
    access_token_encrypted      BYTEA NOT NULL, -- AES-256-GCM, key in env var TOKEN_ENCRYPTION_KEY
    token_type                  TEXT NOT NULL DEFAULT 'long_lived',
    expires_at                  TIMESTAMPTZ,
    scopes                      TEXT[] NOT NULL DEFAULT '{}',
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at                  TIMESTAMPTZ
);
CREATE UNIQUE INDEX tokens_one_active_per_account
    ON instagram_tokens(instagram_account_id)
    WHERE revoked_at IS NULL;

-- Row-level security for scraping worker role
ALTER TABLE instagram_tokens ENABLE ROW LEVEL SECURITY;
CREATE POLICY sourcing_worker_token_access
    ON instagram_tokens FOR SELECT TO sourcing_worker_role
    USING (pool_type = 'scraping');

-- Daily publish counter; incremented in the same transaction that sets status='publishing'.
CREATE TABLE instagram_daily_publish_counts (
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    date                    DATE NOT NULL,
    publish_count           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (instagram_account_id, date)
);

-- ─── Media ────────────────────────────────────────────────────────────────────

CREATE TABLE media_files (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    uploaded_by             UUID NOT NULL REFERENCES users(id),
    r2_key                  TEXT NOT NULL UNIQUE, -- {workspace_id}/media/{id}.{ext}
    filename                TEXT NOT NULL,
    mime_type               TEXT NOT NULL,
    size_bytes              BIGINT NOT NULL CHECK (size_bytes > 0),
    duration_seconds        NUMERIC,
    width                   INTEGER,
    height                  INTEGER,
    checksum_sha256         TEXT NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'uploading'
                                CHECK (status IN ('uploading', 'ready', 'failed', 'deleted')),
    rights_accepted         BOOLEAN NOT NULL DEFAULT false,
    rights_accepted_at      TIMESTAMPTZ,
    rights_terms_version    TEXT,
    source_type             TEXT NOT NULL DEFAULT 'direct_upload'
                                CHECK (source_type IN ('direct_upload', 'restricted_source')),
    source_candidate_id     UUID,   -- FK added after source_candidates table
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at              TIMESTAMPTZ
);

CREATE TABLE prepared_media (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    media_file_id           UUID NOT NULL REFERENCES media_files(id),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    r2_key                  TEXT NOT NULL UNIQUE,
    preparation_settings    JSONB NOT NULL, -- snapshot of account settings at prep time
    status                  TEXT NOT NULL DEFAULT 'pending'
                                CHECK (status IN ('pending', 'processing', 'ready', 'failed')),
    failure_reason          TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    ready_at                TIMESTAMPTZ,
    UNIQUE (media_file_id, instagram_account_id)
);

-- ─── Queue ────────────────────────────────────────────────────────────────────

CREATE TABLE queue_items (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id                UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id        UUID NOT NULL REFERENCES instagram_accounts(id),
    status                      TEXT NOT NULL DEFAULT 'preparing'
                                    CHECK (status IN (
                                        'preparing', 'ready', 'publishing',
                                        'published', 'failed', 'needs_review',
                                        'hidden', 'canceled'
                                    )),
    position                    NUMERIC NOT NULL,
    caption                     TEXT,
    caption_version             INTEGER NOT NULL DEFAULT 1,
    -- frozen fields (written atomically when status transitions to 'publishing')
    frozen_at                   TIMESTAMPTZ,
    frozen_media_file_id        UUID REFERENCES media_files(id),
    frozen_prepared_media_id    UUID REFERENCES prepared_media(id),
    frozen_caption              TEXT,
    frozen_caption_version      INTEGER,
    frozen_account_settings     JSONB,  -- snapshot of relevant account settings
    -- lease (prevents duplicate workers)
    lease_token                 UUID UNIQUE,
    lease_expires_at            TIMESTAMPTZ,
    -- scheduling
    scheduled_at                TIMESTAMPTZ,
    -- provenance
    origin                      TEXT NOT NULL DEFAULT 'upload'
                                    CHECK (origin IN ('upload', 'source_backlog')),
    source_candidate_id         UUID,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at                TIMESTAMPTZ,
    deleted_at                  TIMESTAMPTZ
);
CREATE INDEX queue_items_account_status_pos
    ON queue_items(instagram_account_id, status, position)
    WHERE deleted_at IS NULL;
CREATE INDEX queue_items_ready_scheduled
    ON queue_items(instagram_account_id, scheduled_at)
    WHERE status = 'ready' AND deleted_at IS NULL;
-- Prevent duplicate queue entry from same source candidate.
CREATE UNIQUE INDEX queue_items_one_per_source
    ON queue_items(instagram_account_id, source_candidate_id)
    WHERE source_candidate_id IS NOT NULL AND status NOT IN ('canceled', 'published');

-- ─── Publish Attempts and Receipts ───────────────────────────────────────────

CREATE TABLE publish_attempts (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id           UUID NOT NULL REFERENCES queue_items(id),
    lease_token             UUID NOT NULL,  -- must equal queue_items.lease_token at INSERT time
    attempt_number          INTEGER NOT NULL DEFAULT 1,
    frozen_media_r2_key     TEXT NOT NULL,
    frozen_caption          TEXT NOT NULL,
    frozen_account_ig_id    TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_initiated_at    TIMESTAMPTZ,   -- moment HTTP request body was sent to IG
    request_completed_at    TIMESTAMPTZ,   -- moment HTTP response was fully received
    outcome                 TEXT CHECK (outcome IN ('success', 'failed', 'uncertain')),
    ig_media_id             TEXT,
    ig_error_code           INTEGER,
    ig_error_message        TEXT,
    raw_response            JSONB,
    UNIQUE (queue_item_id, lease_token)    -- one attempt per lease; retry gets a new lease
);

CREATE TABLE publish_receipts (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id               UUID NOT NULL UNIQUE REFERENCES queue_items(id),
    publish_attempt_id          UUID NOT NULL UNIQUE REFERENCES publish_attempts(id),
    workspace_id                UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id        UUID NOT NULL REFERENCES instagram_accounts(id),
    ig_media_id                 TEXT NOT NULL,
    ig_permalink                TEXT,
    published_at                TIMESTAMPTZ NOT NULL,
    frozen_caption              TEXT NOT NULL,
    frozen_media_r2_key         TEXT NOT NULL,
    frozen_account_ig_user_id   TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- No UPDATE permitted on publish_receipts (enforced by a RULE that raises an exception).
CREATE RULE publish_receipts_no_update AS ON UPDATE TO publish_receipts DO INSTEAD NOTHING;

-- ─── Schedules ────────────────────────────────────────────────────────────────

CREATE TABLE schedule_rules (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    rule_type               TEXT NOT NULL CHECK (rule_type IN ('fixed_times', 'interval')),
    -- fixed_times: {"days": [1,3,5], "times": ["09:00","15:00"]}
    -- interval:    {"days": [1,2,3,4,5], "window_start": "09:00", "window_end": "21:00", "interval_minutes": 240}
    rule_config             JSONB NOT NULL,
    active                  BOOLEAN NOT NULL DEFAULT true,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX schedule_rules_account_idx ON schedule_rules(instagram_account_id) WHERE active = true;

-- ─── Subscriptions and Billing ────────────────────────────────────────────────

CREATE TABLE subscriptions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL UNIQUE REFERENCES workspaces(id),
    stripe_customer_id      TEXT NOT NULL UNIQUE,
    stripe_subscription_id  TEXT UNIQUE,
    plan_tier               TEXT NOT NULL DEFAULT 'free'
                                CHECK (plan_tier IN ('free', 'starter', 'pro', 'agency')),
    billing_interval        TEXT CHECK (billing_interval IN ('month', 'year')),
    status                  TEXT NOT NULL DEFAULT 'active'
                                CHECK (status IN ('active', 'trialing', 'past_due', 'canceled', 'unpaid', 'incomplete')),
    current_period_start    TIMESTAMPTZ,
    current_period_end      TIMESTAMPTZ,
    cancel_at_period_end    BOOLEAN NOT NULL DEFAULT false,
    extra_seats             INTEGER NOT NULL DEFAULT 0 CHECK (extra_seats >= 0),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE billing_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_event_id TEXT NOT NULL UNIQUE,   -- idempotency key: INSERT ... ON CONFLICT DO NOTHING
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ─── Analytics ────────────────────────────────────────────────────────────────

CREATE TABLE analytics_snapshots (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    publish_receipt_id      UUID NOT NULL REFERENCES publish_receipts(id),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    snapshot_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    reach                   INTEGER,
    impressions             INTEGER,
    likes                   INTEGER,
    comments                INTEGER,
    saves                   INTEGER,
    shares                  INTEGER,
    video_views             INTEGER,
    video_plays             INTEGER,
    raw_metrics             JSONB
);
CREATE INDEX analytics_receipt_time ON analytics_snapshots(publish_receipt_id, snapshot_at DESC);

-- ─── Notifications ────────────────────────────────────────────────────────────

CREATE TABLE notifications (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    user_id                 UUID REFERENCES users(id),
    type                    TEXT NOT NULL,
    priority                TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('high', 'normal')),
    title                   TEXT NOT NULL,
    body                    TEXT NOT NULL,
    action_url              TEXT,
    instagram_account_id    UUID REFERENCES instagram_accounts(id),
    queue_item_id           UUID REFERENCES queue_items(id),
    read_at                 TIMESTAMPTZ,
    dismissed_at            TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX notifications_workspace_unread
    ON notifications(workspace_id, created_at DESC)
    WHERE dismissed_at IS NULL;

-- ─── Operator Audit Log ───────────────────────────────────────────────────────

CREATE TABLE operator_audit_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    operator_id     UUID NOT NULL REFERENCES users(id),
    action          TEXT NOT NULL,
    target_type     TEXT,
    target_id       UUID,
    before_state    JSONB,
    after_state     JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_target ON operator_audit_log(target_type, target_id);

-- ─── Restricted Sourcing ─────────────────────────────────────────────────────

CREATE TABLE restricted_sources (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    source_type             TEXT NOT NULL CHECK (source_type IN ('account', 'hashtag', 'reels_feed')),
    source_identifier       TEXT NOT NULL,
    media_types             TEXT[] NOT NULL DEFAULT ARRAY['image','video'],
    max_age_days            INTEGER,
    min_likes               INTEGER,
    min_comments            INTEGER,
    min_plays               INTEGER,
    excluded_caption_words  TEXT[] NOT NULL DEFAULT '{}',
    max_candidates_per_run  INTEGER NOT NULL DEFAULT 20 CHECK (max_candidates_per_run BETWEEN 1 AND 100),
    check_interval_hours    INTEGER NOT NULL DEFAULT 24 CHECK (check_interval_hours >= 4),
    auto_fill_enabled       BOOLEAN NOT NULL DEFAULT false,
    auto_fill_target_depth  INTEGER NOT NULL DEFAULT 5 CHECK (auto_fill_target_depth BETWEEN 1 AND 30),
    status                  TEXT NOT NULL DEFAULT 'pending_verification'
                                CHECK (status IN ('pending_verification','active','paused','retrying','blocked')),
    verified_at             TIMESTAMPTZ,
    last_run_at             TIMESTAMPTZ,
    next_run_at             TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE source_candidates (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    restricted_source_id    UUID NOT NULL REFERENCES restricted_sources(id) ON DELETE CASCADE,
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    source_ig_media_id      TEXT NOT NULL,
    source_ig_user_id       TEXT NOT NULL,
    source_ig_username      TEXT NOT NULL,
    source_permalink        TEXT NOT NULL,
    source_caption          TEXT,
    media_type              TEXT NOT NULL CHECK (media_type IN ('IMAGE', 'VIDEO', 'CAROUSEL_ALBUM')),
    observed_likes          INTEGER,
    observed_comments       INTEGER,
    observed_plays          INTEGER,
    discovered_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    status                  TEXT NOT NULL DEFAULT 'backlog'
                                CHECK (status IN ('backlog', 'filtered_out', 'queued', 'skipped')),
    media_downloaded        BOOLEAN NOT NULL DEFAULT false,
    media_file_id           UUID REFERENCES media_files(id),
    CONSTRAINT no_duplicate_source UNIQUE (instagram_account_id, source_ig_media_id)
);
ALTER TABLE media_files ADD CONSTRAINT media_files_source_candidate_fk
    FOREIGN KEY (source_candidate_id) REFERENCES source_candidates(id);

-- ─── Managed Cleanup ─────────────────────────────────────────────────────────

CREATE TABLE cleanup_rules (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id            UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id    UUID NOT NULL REFERENCES instagram_accounts(id),
    name                    TEXT NOT NULL,
    media_types             TEXT[] NOT NULL DEFAULT ARRAY['image','video'],
    min_age_days            INTEGER NOT NULL CHECK (min_age_days >= 1),
    max_reach               INTEGER,
    max_likes               INTEGER,
    max_comments            INTEGER,
    max_saves               INTEGER,
    schedule_enabled        BOOLEAN NOT NULL DEFAULT false,
    schedule_cron           TEXT,
    schedule_timezone       TEXT,
    active                  BOOLEAN NOT NULL DEFAULT true,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE protected_posts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    publish_receipt_id  UUID NOT NULL UNIQUE REFERENCES publish_receipts(id),
    protected_by        UUID NOT NULL REFERENCES users(id),
    protected_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE cleanup_runs (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id                UUID NOT NULL REFERENCES workspaces(id),
    instagram_account_id        UUID NOT NULL REFERENCES instagram_accounts(id),
    cleanup_rule_id             UUID REFERENCES cleanup_rules(id),
    triggered_by                UUID REFERENCES users(id),
    trigger_type                TEXT NOT NULL CHECK (trigger_type IN ('manual', 'scheduled')),
    frozen_rule                 JSONB NOT NULL,
    status                      TEXT NOT NULL DEFAULT 'pending_confirmation'
                                    CHECK (status IN (
                                        'pending_confirmation', 'confirmed',
                                        'running', 'completed', 'stopped', 'needs_review'
                                    )),
    confirmed_at                TIMESTAMPTZ,
    confirmation_selection_hash TEXT,   -- SHA-256 of sorted publish_receipt_id list at confirm time
    started_at                  TIMESTAMPTZ,
    completed_at                TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Prevents two concurrent cleanup runs per account.
CREATE UNIQUE INDEX cleanup_runs_one_active
    ON cleanup_runs(instagram_account_id)
    WHERE status IN ('pending_confirmation','confirmed','running','needs_review');

CREATE TABLE cleanup_run_items (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cleanup_run_id      UUID NOT NULL REFERENCES cleanup_runs(id),
    publish_receipt_id  UUID NOT NULL REFERENCES publish_receipts(id),
    frozen_metrics      JSONB NOT NULL,
    frozen_criteria     JSONB NOT NULL,
    ig_media_id         TEXT NOT NULL,
    media_type          TEXT NOT NULL CHECK (media_type IN ('IMAGE', 'VIDEO', 'REEL')),
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN (
                                'pending','processing','archived','deleted',
                                'failed','uncertain','skipped'
                            )),
    action_taken        TEXT CHECK (action_taken IN ('archive','delete')),
    request_sent_at     TIMESTAMPTZ,
    completed_at        TIMESTAMPTZ,
    ig_response         JSONB,
    failure_reason      TEXT
);

-- ─── Deletion and Feedback ────────────────────────────────────────────────────

CREATE TABLE deletion_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    workspace_id    UUID REFERENCES workspaces(id),
    request_type    TEXT NOT NULL CHECK (request_type IN ('user', 'workspace')),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'in_progress', 'completed')),
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    reference_code  TEXT NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(8), 'hex')
);

CREATE TABLE feedback_submissions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id),
    user_id         UUID NOT NULL REFERENCES users(id),
    category        TEXT NOT NULL CHECK (category IN ('bug','confusing','idea','praise','other')),
    body            TEXT NOT NULL,
    page_url        TEXT,
    page_context    JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

## 4. Invariant Enforcement Map

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| A queue item publishes at most once | `UPDATE queue_items SET status='publishing', lease_token=$token WHERE id=$id AND status='ready'` returns exactly 1 row or 0 (worker exits); `UNIQUE (queue_item_id, lease_token)` on `publish_attempts` prevents a second attempt with the same lease | Test: two worker goroutines race on same `queue_item_id`; assert exactly one `publish_attempts` row is created and IG mock receives exactly one request |
| Daily publish limit cannot be forgotten on restart | `instagram_daily_publish_counts` row is upserted (`INSERT … ON CONFLICT DO UPDATE SET publish_count = publish_count + 1`) inside the same transaction that sets `status='publishing'`; the count table is not a cache — it is the source of truth; the worker reads `publish_count` from this table before claiming | Test: kill the publishing worker mid-transaction; assert `publish_count` is unchanged; restart worker; assert it respects the pre-kill count |
| Same IG post cannot enter two workspaces | `UNIQUE (instagram_user_id)` on `instagram_accounts` is `DEFERRABLE INITIALLY IMMEDIATE`; the OAuth callback checks `SELECT 1 FROM instagram_accounts WHERE instagram_user_id=$1 AND workspace_id != $2` before inserting | Test: attempt to connect the same IG account to two workspaces in concurrent requests; assert second INSERT raises a unique violation |
| Uncertain publish outcome cannot auto-retry | Worker checks `outcome` on `publish_attempts`; if `outcome = 'uncertain'`, it sets `queue_items.status = 'needs_review'`; the scheduler's candidate query filters `WHERE status = 'ready'`; `needs_review` is not in that set | Test: seed a `queue_items` row with `status='needs_review'`; run the scheduler loop 3 times; assert zero new `publish_attempts` are created |
| Frozen content matches what the user approved | When transitioning to `publishing`, the worker reads `caption`, `media_file_id`, `prepared_media_id` from the `queue_items` row inside a `SELECT FOR UPDATE` and writes them to `frozen_*` fields in the same transaction; subsequent caption edits increment `caption_version` but do not touch `frozen_caption` | Test: update `caption` on a `queue_items` row after its `frozen_at` is set; assert `frozen_caption` is unchanged |
| No two workers claim the same queue item | `UPDATE … WHERE status='ready' … RETURNING *` is atomic; Postgres row-level lock on the targeted row prevents concurrent claims | Test: 10 concurrent workers, 1 ready item; assert exactly 1 `frozen_at` is set |
| Stale publish lease does not block queue forever | `lease_recovery` pg-boss job (every 5 minutes) executes `UPDATE queue_items SET status='needs_review', lease_token=NULL WHERE status='publishing' AND lease_expires_at < now()` | Test: set `lease_expires_at` to 1 minute ago; wait for recovery job; assert status is `needs_review` |
| Source content not imported twice into same account | `UNIQUE (instagram_account_id, source_ig_media_id)` on `source_candidates`; concurrent collection jobs use `INSERT … ON CONFLICT DO NOTHING` | Test: two concurrent sourcing runs for the same account and same source post; assert exactly one `source_candidates` row exists |
| Source candidate not queued twice for same account | `UNIQUE INDEX queue_items_one_per_source` on `(instagram_account_id, source_candidate_id)` where status is not terminal | Test: call refill twice concurrently for same candidate; assert one `queue_items` row created |
| Billing events are idempotent | `INSERT INTO billing_events … ON CONFLICT (stripe_event_id) DO NOTHING`; subscription state is derived from the event payload, not accumulated | Test: replay the same Stripe `invoice.payment_succeeded` event 3 times; assert `subscriptions` row is updated exactly once |
| Customer tokens never appear in logs | `access_token_encrypted` is `BYTEA` (raw AES-256-GCM ciphertext); decryption happens only in the publishing worker's in-memory token fetch function; the Next.js web process has no access to `TOKEN_ENCRYPTION_KEY`; structured logging middleware strips any field named `token`, `access_token`, or `encrypted` | Test: grep application log output in integration tests; assert no string matches the decrypted token value |
| Cleanup does not run while a prior cleanup is uncertain | `UNIQUE INDEX cleanup_runs_one_active` prevents a second INSERT while any `cleanup_runs` row for that account has status in `('pending_confirmation','confirmed','running','needs_review')` | Test: seed a `needs_review` cleanup run; attempt to start a new cleanup; assert INSERT fails with a unique constraint violation |
| Cleanup confirmation invalidated if selection changes | `confirmation_selection_hash` (SHA-256 of sorted `publish_receipt_id` list) is stored at confirm time; before executing each item, worker recomputes hash; if it differs, worker sets status to `needs_review` | Test: confirm a run, then mark one selected post as protected; assert next execution step detects hash mismatch and halts |
| Workspace isolation on all user-facing queries | Every API route handler calls `verifyWorkspaceMembership(userId, workspaceId)` which executes `SELECT 1 FROM workspace_members WHERE workspace_id=$1 AND user_id=$2 AND removed_at IS NULL`; returns 403 if 0 rows; all data queries include `AND workspace_id=$workspaceId` | Test: seed two workspaces with separate queue items; call queue list API with workspace A's session using workspace B's ID; assert 403 |
| Entitlement check is server-side, not UI-only | Restricted sourcing and managed cleanup API routes call `hasEntitlement(workspaceId, feature)` which queries `workspace_entitlements WHERE workspace_id=$1 AND feature=$2 AND revoked_at IS NULL`; middleware raises 403 if no row found | Test: call restricted sourcing API route with a workspace that has no entitlement; assert 403 even if the client sends a crafted request |
| Cleanup action not repeated on crash | `cleanup_run_items.request_sent_at` is set before the HTTP request; on recovery, items with `request_sent_at IS NOT NULL AND status='processing'` are set to `uncertain` and the run halts with `needs_review` | Test: kill cleanup worker after `request_sent_at` is written but before `ig_response` is recorded; assert item becomes `uncertain` on recovery |
| Subscriber state survives repeated/out-of-order webhooks | Stripe webhook handler: `INSERT INTO billing_events … ON CONFLICT DO NOTHING`; subscription update applies only if `stripe_event_id` not already in table; subscription status is written from the full current state in the event, not incremented | Test: deliver `customer.subscription.updated` then `customer.subscription.created` in wrong order; assert final subscription row matches the `updated` event |
| Private media links are narrow and expiring | Media served via a Next.js API route that validates workspace membership then returns a presigned R2 URL (`GetObjectCommand` signed with `expiresIn: 300` seconds) | Test: obtain a presigned URL for workspace A's media; assert URL expires after 300 seconds; assert URL cannot be used for workspace B's media key |
| Deleted user's data is removed, not disguised | `deletion_requests` worker: removes `users` row, removes `sessions`, removes `media_files` and R2 objects, cancels Stripe subscription via `stripe.subscriptions.cancel`, revokes IG tokens via Graph API `DELETE /me/permissions`; `reference_code` remains in `deletion_requests` with status `completed` and no PII fields | Test: trigger a user deletion; assert `users` row is deleted; assert `media_files` rows are deleted; assert R2 objects are gone; assert `deletion_requests` row remains with no email or name |

---

## 5. Failure-Mode Walkthrough

**Scenario 1: Worker process crashes before calling the Instagram API.**

1. Worker runs `UPDATE queue_items SET status='publishing', lease_token=$uuid, lease_expires_at=now()+INTERVAL'10 minutes', frozen_at=now(), frozen_caption=$caption, frozen_media_r2_key=$key, frozen_account_settings=$settings WHERE id=$id AND status='ready' RETURNING *` — this commits.
2. Worker inserts a `publish_attempts` row with `lease_token`, `frozen_*` fields, `created_at`, but `request_initiated_at IS NULL` — then the process crashes before `request_initiated_at` is written.
3. The `lease_recovery` pg-boss job runs within 5 minutes, finds `status='publishing' AND lease_expires_at < now()`, executes `UPDATE queue_items SET status='needs_review', lease_token=NULL WHERE …`.
4. Because `publish_attempts.request_initiated_at IS NULL`, the operator can confirm no HTTP request reached Instagram.
5. Operator (or an auto-reconciliation policy for items with `request_initiated_at IS NULL`) sets the item back to `ready` and it re-enters the scheduler.

**Evidence:** `publish_attempts` row exists with `request_initiated_at IS NULL` and `outcome IS NULL`; `queue_items.status = 'needs_review'`; `operator_audit_log` records the reconciliation.

---

**Scenario 2: Worker crashes after the Instagram API call may have been accepted.**

1. Worker writes `request_initiated_at = now()` to `publish_attempts` (committed).
2. Worker sends HTTP request to IG Content Publishing API (`POST /{ig-user-id}/media` then `POST /{ig-user-id}/media_publish`).
3. Worker crashes or loses network before reading the response.
4. `lease_recovery` job fires, finds the stale lease, checks `publish_attempts.request_initiated_at IS NOT NULL AND outcome IS NULL`, sets `queue_items.status = 'needs_review'` and `publish_attempts.outcome = 'uncertain'`.
5. Operator opens the item in the admin console; the console shows the IG account, the frozen caption, `request_initiated_at`, and a link to manually verify on Instagram.
6. Operator checks Instagram directly, confirms the post either appeared or did not.
7. Operator clicks "Mark as Published" (links the IG media ID, creates `publish_receipts` row) or "Mark as Failed" (sets `queue_items.status = 'failed'`). Both actions are written to `operator_audit_log`.

**Evidence:** `publish_attempts.outcome = 'uncertain'`; `publish_attempts.request_initiated_at` shows when the request was sent; `operator_audit_log` row with the reconciliation action.

---

**Scenario 3: Two concurrent workers attempt to publish the same queue item.**

1. Worker A and Worker B both query for ready items in the same scheduling tick.
2. Worker A executes `UPDATE queue_items SET status='publishing', lease_token=$tokenA WHERE id=$id AND status='ready'` — row is locked.
3. Worker B's identical `UPDATE` waits for Worker A's row lock.
4. Worker A commits. Worker B's `UPDATE` now evaluates `WHERE status='ready'` — the row now has `status='publishing'`, so 0 rows are updated. Worker B exits.
5. Only one `publish_attempts` row is created (Worker A's), because the `UNIQUE (queue_item_id, lease_token)` constraint allows only one attempt per lease, and Worker B never obtained a lease.

**Evidence:** Exactly one `publish_attempts` row for the `queue_item_id`; Worker B's pg-boss job completes with no side effects.

---

**Scenario 4: Media preparation fails (FFmpeg returns non-zero exit code).**

1. Media worker picks up `media_prep` job, downloads original from R2 via `GetObjectCommand`.
2. FFmpeg encounters an incompatible codec or corrupt file; the `fluent-ffmpeg` `.on('error', handler)` callback fires.
3. Worker sets `prepared_media.status = 'failed'`, `failure_reason = error.message`, and `queue_items.status = 'failed'` with the same reason in a single transaction.
4. Worker creates a `notifications` row with `type = 'media_prep_failed'`, `priority = 'high'`.
5. User sees the item in `failed` state with the failure reason. They can fix the media (re-upload a new `media_files` row), which enqueues a fresh `media_prep` job — a new `prepared_media` row is created, and the `queue_items` row is updated to reference the new `media_file_id` and set back to `preparing`.

**Evidence:** `prepared_media.status = 'failed'`; `queue_items.status = 'failed'`; `notifications` row created; no duplicate `queue_items` row for the retry.

---

**Scenario 5: Instagram token expires or is revoked mid-queue.**

1. Publishing worker claims a `queue_items` row, fetches the decrypted token from `instagram_tokens`.
2. IG Graph API returns `{"error": {"code": 190}}` (token expired).
3. Worker sets `instagram_accounts.connection_status = 'expired'`, `instagram_tokens.revoked_at = now()`.
4. Worker sets `queue_items.status = 'ready'` (releases the lease — no publish was attempted after `request_initiated_at`).
5. Worker creates a `notifications` row with `type = 'account_connection_expired'`, `priority = 'high'`.
6. The scheduler ignores this account until `connection_status = 'connected'`.
7. User follows the recovery path (re-OAuth), which inserts a new `instagram_tokens` row. Account returns to `connected`. Queued items remain intact; scheduler resumes.

**Evidence:** `instagram_accounts.connection_status = 'expired'`; `queue_items.status = 'ready'` (not failed); `notifications` row exists; `operator_audit_log` records no destructive action.

---

**Scenario 6: Account reaches its daily publish limit mid-queue.**

1. Publishing worker checks `instagram_daily_publish_counts` for today's count against `instagram_accounts.daily_publish_limit`.
2. Count equals limit. Worker does not claim any queue items for this account.
3. Scheduler logs a `notifications` row with `type = 'daily_limit_reached'` if one was not already created today (checked by `SELECT 1 FROM notifications WHERE instagram_account_id=$1 AND type='daily_limit_reached' AND created_at > date_trunc('day', now())`).
4. The scheduler's next run after midnight (UTC) finds `publish_count = 0` for the new date and resumes publishing.

**Evidence:** `instagram_daily_publish_counts` row for the account and date; `queue_items` remain in `ready` status, not `failed`; scheduler resumes next calendar day.

---

**Scenario 7: User edits the schedule near a slot that is seconds away.**

1. User submits a `PUT /accounts/:id/schedule` request at 08:59:55. The API writes new `schedule_rules` rows in a transaction.
2. At 09:00:00 the scheduler runs, computes next eligible slot using the updated rules, and finds the new next slot is 09:00:00 today (or 09:00:00 tomorrow, depending on the new rule). The computation is `nextSlot(rules, accountTimezone, now)` — a pure function with no state outside the rules and current time.
3. If the next slot computed from the new rules is in the past (e.g., changed from 09:00 to 08:00 and current time is 09:01), the scheduler treats that slot as missed and advances to the next future slot. The "missed slot" rule: `nextSlotAfter(slots, referenceTime)` returns the first slot strictly after `referenceTime`.
4. No double post occurs because the scheduler never compares against a cached previous slot — it always recomputes from the current rule set and the set of `queue_items` where `published_at` is non-null today.

**Evidence:** `schedule_rules.updated_at` timestamp; `queue_items.published_at` distribution; no two posts within the same computed slot window on the same day.

---

**Scenario 8: Duplicate source collection runs for the same account.**

1. Two `restricted_source_run` pg-boss jobs fire concurrently for the same `restricted_sources` row (e.g., a stale retry overlaps a scheduled run).
2. Both jobs complete Playwright navigation and produce candidate lists with overlapping `source_ig_media_id` values.
3. Both workers execute `INSERT INTO source_candidates (instagram_account_id, source_ig_media_id, …) … ON CONFLICT (instagram_account_id, source_ig_media_id) DO NOTHING`.
4. The `UNIQUE` constraint ensures exactly one row per `(instagram_account_id, source_ig_media_id)` pair. The second worker's inserts silently no-op.
5. No duplicate candidates enter the backlog.

**Evidence:** `source_candidates` count equals the deduplicated union of both workers' results; no two rows share the same `(instagram_account_id, source_ig_media_id)`.

---

**Scenario 9: Browser hang during a cleanup action.**

1. Cleanup worker sets `cleanup_run_items.status = 'processing'` and `request_sent_at = now()` in a transaction.
2. Worker calls Instagram Graph API to archive or delete the post.
3. The HTTP request times out after 30 seconds (hard timeout in Node.js `AbortController` with 30,000 ms signal).
4. Worker catches the timeout error. Because `request_sent_at` is written, it cannot know whether the action was accepted.
5. Worker sets `cleanup_run_items.status = 'uncertain'`, `failure_reason = 'request_timeout'`.
6. Worker sets `cleanup_runs.status = 'needs_review'`.
7. The `cleanup_runs_one_active` unique index prevents any new cleanup run for this account.
8. Operator investigates on Instagram directly and reconciles.

**Evidence:** `cleanup_run_items.request_sent_at IS NOT NULL` and `status = 'uncertain'`; `cleanup_runs.status = 'needs_review'`; new cleanup run insert fails with unique constraint violation.

---

**Scenario 10: Cleanup selection changes between preview and confirmation.**

1. User previews a cleanup run; the API returns a list of `publish_receipt_id` values. The UI computes `SHA-256(sorted ids)` client-side and sends it in the confirmation request body.
2. Before the user clicks confirm, another session marks one of the selected posts as protected (`INSERT INTO protected_posts`).
3. User submits confirmation. The API server re-queries the eligible posts (re-runs the cleanup rule against current analytics and protection status), recomputes `SHA-256(sorted ids)`, and compares it against the submitted hash.
4. Hashes differ. API returns HTTP 409 with a body explaining the selection changed. The old confirmation is not recorded in `cleanup_runs`. The user must re-preview.

**Evidence:** `cleanup_runs.confirmed_at IS NULL` (no run was confirmed); `cleanup_runs` row either was never inserted or has `status = 'pending_confirmation'`; the `protected_posts` row is present.

---

**Scenario 11: Repeated or out-of-order Stripe billing events.**

1. Stripe delivers `customer.subscription.updated` (event ID `evt_001`) setting plan to `pro`.
2. Stripe re-delivers `evt_001` due to a timeout on the first acknowledgment.
3. Webhook handler executes `INSERT INTO billing_events (stripe_event_id, ...) VALUES ('evt_001', ...) ON CONFLICT (stripe_event_id) DO NOTHING` — returns 0 rows inserted.
4. Handler detects 0 inserted rows and returns HTTP 200 immediately without updating `subscriptions`.
5. Stripe also delivers `customer.subscription.created` (event ID `evt_000`, older) out of order after the `updated` event.
6. `evt_000` is inserted (not a duplicate). The handler applies the state from `evt_000`'s payload to `subscriptions`. Because Stripe event payloads contain full subscription state (not diffs), the final `subscriptions` row reflects `evt_000`'s state — this is wrong for out-of-order delivery.
7. Mitigation: `UPDATE subscriptions SET ... WHERE stripe_subscription_id=$1 AND current_period_start <= $event_period_start` — the `AND` condition prevents an older event from overwriting a newer subscription state.

**Evidence:** `billing_events` table has exactly one row per `stripe_event_id`; `subscriptions.current_period_start` matches the most recent event's period.

---

**Scenario 12: R2 object storage is unreachable during media preparation.**

1. Media worker downloads original file from R2. `GetObjectCommand` throws a network error.
2. Worker catches the error, does not set `prepared_media.status = 'processing'`.
3. pg-boss retries the `media_prep` job with exponential backoff: attempt 1 at T+0, attempt 2 at T+30s, attempt 3 at T+120s, attempt 4 at T+480s, attempt 5 at T+1920s (all via `retryBackoff: true, retryLimit: 5` in pg-boss job options).
4. If all 5 attempts fail, pg-boss marks the job `failed`. Worker sets `prepared_media.status = 'failed'`, `failure_reason = 'storage_unreachable'`, and `queue_items.status = 'failed'`.
5. User can trigger a retry (which creates a new `media_prep` job) once R2 is available.

**Evidence:** pg-boss `archive` table shows 5 failed attempts with backoff timestamps; `prepared_media.failure_reason = 'storage_unreachable'`; `queue_items.status = 'failed'`.

---

**Scenario 13: User deletion request interrupted halfway.**

1. `deletion_requests` worker begins: sets `status = 'in_progress'`, `started_at = now()`.
2. Worker deletes `sessions` rows, revokes IG tokens, cancels Stripe subscription, starts deleting `media_files` rows and R2 objects.
3. Worker crashes after deleting 30 of 50 media files.
4. On restart, the worker queries `deletion_requests WHERE status = 'in_progress'`. It re-runs the entire deletion sequence. Every step is idempotent: `DELETE FROM sessions WHERE user_id=$1` deletes 0 rows if already deleted; Stripe `subscriptions.cancel` is idempotent on an already-canceled subscription; R2 `DeleteObjectCommand` returns success even if the object is already gone.
5. Worker completes, sets `deletion_requests.status = 'completed'`, `completed_at = now()`. The `users` row is deleted last (after all dependent data is removed).

**Evidence:** `deletion_requests.status = 'completed'`; `users` row absent; `media_files` rows absent; R2 objects absent; `deletion_requests.reference_code` still present for status lookup.

---

## 6. AI Strategy

No AI features are included in v1.

The product's defining value is precision: posts publish to the correct account with the exact caption and media the user approved. Adding AI caption generation introduces a text hallucination failure mode that contradicts promise 1 ("It publishes only what the customer intended"). The product brief explicitly states "AI is not required to fulfill this brief."

All operations the brief describes — media resizing, aspect ratio correction, metadata stripping, schedule computation, queue ordering, analytics passthrough, content deduplication — are deterministic and well-served by FFmpeg, pure functions, and database queries.

Cost check if AI were added: Caption generation at 200 WAU × 3 posts/week × (500 tokens in / 200 tokens out) at Claude Haiku 4.5 rates ($0.80/$4.00 per million tokens) = 200 × 3 × $0.00040 + 200 × 3 × $0.00080 = $0.48 + $0.96 = $1.44/week = $75/year. Cost alone does not disqualify it. Hallucination risk, the absence of a validation mechanism for brand voice, and the quality measurement problem (what does "correct caption" mean at launch with no baseline?) do. No AI in v1.

Revisit post-launch when: the operator can define a golden-set evaluation dataset of 100 approved captions, measure accuracy at >95%, and add a mandatory human-review step before any AI-generated caption enters the queue.

---

## 7. Testing and Release Confidence

### 7.1 On Every Commit (CI, < 5 minutes)

- TypeScript compiler (`tsc --noEmit`) across all packages.
- ESLint with the project rule set.
- Vitest unit tests: pure functions (schedule computation, `nextSlotAfter`, `computeSelectionHash`, deduplication logic, entitlement checks). No database, no network.
- Zod schema validation tests: valid and invalid payloads for every API request type.

### 7.2 On Every Commit (CI, < 15 minutes, require real Postgres)

- Integration tests using a Postgres test database (`pg-boss` in test mode, transactions rolled back after each test):
  - Queue claim race: 10 concurrent workers on 1 item, assert exactly 1 claim.
  - Daily count upsert: concurrent increments from 3 connections, assert final count is 3.
  - Billing event idempotency: insert same `stripe_event_id` twice, assert 1 row.
  - Invitation gate: attempt account creation without a valid invitation token, assert 403.
  - Entitlement enforcement: call restricted sourcing API route without entitlement, assert 403.
  - Cross-workspace query isolation: workspace A session queries workspace B data, assert 403.
  - Stale lease recovery: seed `status='publishing'` with expired `lease_expires_at`, run recovery job, assert `status='needs_review'`.

### 7.3 Nightly (require real services, slow)

- Media preparation: upload a 10 MB JPEG and a 60-second MP4 in each supported aspect ratio; assert `prepared_media.status = 'ready'` and output dimensions within spec.
- FFmpeg timeout drill: send a corrupt MP4; assert `prepared_media.status = 'failed'` within 5 minutes.
- R2 round-trip: upload, download, delete an object; assert checksum matches.
- Schedule computation: test all DST transition dates for all supported timezones using pre-computed expected slots.
- Deletion completeness: create a user, workspace, 5 media files, 3 queue items; run deletion; assert all rows and R2 objects are absent.
- Stripe webhook replay: replay 20 historical Stripe test-mode events in random order; assert final subscription state matches the most recent event.
- Restore drill: restore the most recent pg_dump backup to a staging Postgres instance; assert row counts match production within 5%.

### 7.4 Against Safe Test Instagram Accounts (before each release)

- Connect a test professional account via IG OAuth; assert `connection_status = 'connected'`.
- Upload an image, prepare it, add it to queue; assert `queue_items.status = 'ready'`.
- Trigger "publish now"; assert `queue_items.status = 'published'`, `publish_receipts` row exists, IG media ID resolves to a real post.
- Revoke the test account's token via the IG developer console; assert `connection_status = 'expired'` within 1 hour (next health check cycle) and a notification is created.
- Test the daily limit: set `daily_publish_limit = 1`, publish once, attempt a second publish the same day; assert second item remains `ready`.
- Analytics fetch: assert `analytics_snapshots` row is created within 25 hours of publish.
- Run the timezone drill: set account timezone to `America/New_York`, create a fixed-time rule for a slot 2 minutes in the future; assert the post publishes within 3 minutes of the slot.

### 7.5 Ambiguous Outcome Drill (quarterly)

- Seed a `publish_attempts` row with `request_initiated_at IS NOT NULL` and `outcome IS NULL`; assert `lease_recovery` sets `status = 'needs_review'`; assert the operator console shows the item with reconciliation options; exercise both "Mark as Published" and "Mark as Failed" paths.

---

## 8. Delivery Phases

### Phase 0 — Foundation

**What gets built:** Postgres schema (users, sessions, workspaces, workspace_members, invitations, waitlist_entries, subscriptions skeleton). Lucia Auth integration. Invitation acceptance flow. Waitlist signup page. Public marketing site (pricing, safety posture, content-rights rules). Operator admin area (basic auth with `is_operator = true` check, not a separate auth system). Railway infrastructure: web service + Postgres.

**Justification for this phase before end-to-end:** You cannot test account connection or publishing without users and workspaces. The operator admin area is needed to issue the first invitation.

**Exit criteria:**
- A new user cannot create an account without a valid invitation token (verified by HTTP 403 response to a direct signup attempt).
- An operator can create an invitation, the invited user accepts it, and the resulting `workspace_members` row has `role = 'owner'`.
- The waitlist form stores a row in `waitlist_entries`.
- The public marketing site is reachable and passes Lighthouse accessibility score ≥ 90.

---

### Phase 1 — Instagram Account Connection

**What gets built:** IG OAuth 2.0 flow (Meta Graph API: `/oauth/authorize`, `/oauth/access_token`, `GET /me/accounts`). Token encryption and storage. Account health check job (every 60 minutes via pg-boss, calls `GET /{ig-user-id}?fields=id,username,profile_picture_url`). Account dashboard showing connection status, profile, and daily capacity. Account request pre-connection flow.

**Justification:** Cannot upload or publish without a connected account.

**Exit criteria:**
- A user connects a test professional Instagram account; `instagram_accounts.connection_status = 'connected'` and `instagram_tokens` row exists with non-null encrypted token.
- The same IG account cannot be connected to a second workspace (assert 409 response on second attempt).
- Health check job fires within 70 minutes; sets `last_health_check_at`.
- Token revocation (via IG developer console) is detected within 70 minutes; `connection_status = 'expired'`.

---

### Phase 2 — Upload, Prepare, and Queue

**What gets built:** Presigned R2 upload URL endpoint (15-minute expiry, `PutObjectCommand`). Upload progress via `XMLHttpRequest` upload events. Media validation on server after upload (ffprobe via `fluent-ffmpeg.ffprobe()`). Media worker process (pg-boss consumer). FFmpeg preparation pipeline. Queue list UI (per account). Caption editor. Queue item status display (all states). "Publish next item now" manual trigger. Queue reordering (drag-and-drop, updates `position` in bulk via `UPDATE … SET position = $n WHERE id = ANY($ids)` in one transaction). Rights acceptance modal (versioned terms, stored in `media_files.rights_terms_version`). Bulk hide/restore/remove.

**Justification:** Required before Phase 3's publish path can have any items to publish.

**Exit criteria:**
- Upload a 20 MB MP4 video; `media_files.status = 'ready'` after upload; `prepared_media.status = 'ready'` after preparation; no duplicate `prepared_media` row on re-trigger.
- Upload a corrupt file; assert `media_files.status = 'failed'` with a human-readable reason; a retry creates a new `media_files` row, not a second `queue_items` row.
- Reorder 5 queue items; assert `position` values are distinct and the new order is reflected in the API response.
- Rights acceptance checkbox is required; assert `queue_items` row cannot be created if `media_files.rights_accepted = false`.

---

### Phase 3 — Publishing, Receipts, and Live Status (First End-to-End Phase)

**What gets built:** Publishing worker process. Instagram Graph API content publishing (two-step: create container → publish). Publish attempt recording. Receipt creation. `needs_review` flow and operator reconciliation. SSE endpoint and `pg_notify`. Notification creation (publish failure, uncertain outcome, daily limit). Real-time queue status in the UI.

**Exit criteria:**
- Upload an image to a test account, queue it, trigger "publish now"; `queue_items.status = 'published'`, `publish_receipts` row exists, IG media ID resolves on Instagram.
- Two concurrent "publish now" clicks for the same item produce exactly one `publish_attempts` row.
- Kill the publishing worker while `status = 'publishing'`; assert lease recovery fires within 10 minutes; assert `status = 'needs_review'`; assert operator can reconcile.
- SSE connection receives a status update event within 3 seconds of `pg_notify` firing.

---

### Phase 4 — Scheduling

**What gets built:** Schedule rule UI (fixed-time and interval types, timezone picker, preview of upcoming runs). Scheduler tick job (pg-boss schedule, every 60 seconds). DST-aware next-slot computation using `luxon`'s `DateTime.fromISO` in the configured timezone. Pause/resume UI and API.

**Exit criteria:**
- Create a fixed-time rule for 2 minutes in the future; assert the next ready item publishes within 3 minutes of the target slot.
- Pause an account; add a ready item; wait 10 minutes; assert no publish occurred; resume; assert publish fires at the next slot.
- Set timezone to `America/New_York`, create a rule for `02:30` on the DST spring-forward date; assert the scheduler skips the non-existent local time and advances to the next valid slot.
- Edit a schedule rule; assert previously scheduled `scheduled_at` values on ready items are recomputed on the next scheduler tick.

---

### Phase 5 — Analytics and Library

**What gets built:** Analytics worker process. Analytics refresh jobs (enqueued at +24h, +7d, +30d from publish). Library view (per account, published posts with metrics). `analytics_snapshots` display showing change over time. Manual refresh (with rate-limit guard: 1 manual refresh per account per 15 minutes, enforced by pg-boss `singletonKey` with `singletonHours: 0.25`). Best/worst post views.

**Exit criteria:**
- After publishing a test post, `analytics_snapshots` row is created within 25 hours.
- Manual refresh queues a job; a second manual refresh within 15 minutes is silently no-op'd (singletonKey conflict).
- Library shows all published posts with captions, media thumbnails, and most recent snapshot metrics.
- Old snapshots are not overwritten; assert row count grows with each refresh cycle.

---

### Phase 6 — Billing and Plan Enforcement

**What gets built:** Stripe customer and subscription creation. Checkout session flow (`stripe.checkout.sessions.create`). Stripe Customer Portal link (`stripe.billingPortal.sessions.create`). Webhook handler (all subscription events, with idempotency via `billing_events`). Plan limit enforcement (max connected accounts, max collaborators checked against `subscriptions.plan_tier`). Downgrade behavior (accounts above new limit enter a `held` state — publishing pauses but items not deleted). Billing page UI.

**Exit criteria:**
- Complete a Stripe test-mode checkout; `subscriptions.status = 'active'`, `plan_tier` updated.
- Replay the same Stripe webhook event 3 times; assert `subscriptions` row updated exactly once.
- Exceed the free plan's account limit; assert attempt to connect a new account returns 403 with plan upgrade prompt.
- Cancel subscription in Stripe; assert workspace remains on paid features until `current_period_end`, then downgrades.

---

### Phase 7 — Notifications, Self-Service, and Operator Completeness

**What gets built:** In-app notification center (SSE-driven, read/dismiss state). Feedback submission form. Data export (GDPR: zip of user data, media file links, queue history). Account deletion flow. Operator dashboard (waitlist, invitations, connection requests, workspace health, queue item inspector). Operator safe retry actions with `operator_audit_log`.

**Exit criteria:**
- A high-priority notification appears in the UI within 3 seconds of the triggering event.
- User exports their data; zip contains a JSON file with all `queue_items`, `publish_receipts`, and `analytics_snapshots` for their workspace; all entries belong to their workspace only.
- User requests deletion; within 10 minutes all PII is removed and only `deletion_requests.reference_code` remains.
- Operator can view a queue item's full lifecycle, determine if retry is safe, and execute retry; `operator_audit_log` records the action.

---

### Phase 8 — Restricted Sourcing (Operator-Enabled Only)

**What gets built:** Restricted sourcing worker (Playwright, isolated process, separate Railway service with 4 GB RAM). Source management UI (visible only to workspaces with `restricted_sourcing` entitlement). Backlog display. Manual refill. Auto-fill logic. Source candidate deduplication. Rights confirmation flow for sourced media.

**Exit criteria:**
- A workspace without the entitlement cannot see or access the sourcing UI (assert 403 on direct API call).
- Create an account source; assert candidates appear in backlog with provenance (source username, permalink, metrics).
- Two concurrent collection runs for the same source; assert no duplicate `source_candidates` rows.
- Auto-fill with target depth 3; assert exactly 3 queue items are created, not more.
- Remove the entitlement; assert the sourcing UI disappears; existing candidates and queue items remain.

---

### Phase 9 — Managed Cleanup (Operator-Enabled Only)

**What gets built:** Cleanup rule UI (visible only to workspaces with `managed_cleanup` entitlement). Protected post toggle. Cleanup preview with `confirmation_selection_hash`. Cleanup worker. Scheduled cleanup via pg-boss cron. Item-by-item result display. Stop button.

**Exit criteria:**
- Create a cleanup rule targeting posts with < 10 likes older than 7 days; preview shows the matching posts; confirm; assert cleanup worker processes them one at a time.
- Mark one post as protected between preview and confirm; assert confirmation returns 409 (hash mismatch).
- Kill the cleanup worker after `request_sent_at` is written; assert item becomes `uncertain`; assert a new cleanup run for the same account cannot start.
- The cleanup history record shows the frozen rule, each item's frozen metrics, the action taken, and the IG response — no IG access token appears in the record.

---

## 9. Security and Privacy

**Identity and sessions.** Lucia Auth sessions are stored in Postgres with a 30-day expiry. Session IDs are 40-character random hex strings generated by `crypto.getRandomValues`. Session cookies are `HttpOnly`, `Secure`, `SameSite=Lax`. Operator sessions (`users.is_operator = true`) are kept in a separate cookie (`__op_session`) with `SameSite=Strict` and a 4-hour expiry. The operator area enforces a second TOTP check on first access per session (stored as `op_mfa_verified_at` in the session's Postgres row).

**Authorization.** Every API route handler calls `verifyWorkspaceMembership(userId, workspaceId)` before accessing workspace data. Destructive actions (disconnect account, delete media, change subscription) require `role IN ('owner', 'admin')`. Billing actions require `role = 'owner'`. A check for the role is a `SELECT role FROM workspace_members WHERE workspace_id=$1 AND user_id=$2 AND removed_at IS NULL`, not a value cached in the session.

**Workspace isolation.** Every query that returns workspace data includes `AND workspace_id = $workspaceId`. The workspace ID comes from the verified session, not from the URL or request body. Presigned R2 URLs are generated server-side for the specific `r2_key` after membership verification; the browser never receives a wildcard credential.

**Secrets management.** `TOKEN_ENCRYPTION_KEY` (32-byte AES-256-GCM key), `DATABASE_URL`, `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `RESEND_API_KEY`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `IG_APP_SECRET` are stored as Railway environment variables (encrypted at rest). They are never written to logs. The web process does not have `TOKEN_ENCRYPTION_KEY` — only the publishing worker and sourcing worker have it. This is enforced by separate Railway service environment variable sets.

**Private media.** Original and prepared media are stored in R2 under keys like `{workspace_id}/media/{file_id}/{filename}` — not publicly accessible. All media access goes through a Next.js API route that verifies membership and returns a presigned URL with `expiresIn: 300`. There is no public media bucket policy.

**Restricted browser sessions.** Playwright session files (`browserContext.storageState`) are stored in a Railway volume mounted to the sourcing worker only, at `/data/sessions/{scraping_account_id}.json`. The file contains IG session cookies. The web process cannot read this volume. Session files are never logged, never returned in API responses, and never written to Postgres or R2.

**Stripe billing callbacks.** All inbound Stripe webhooks are verified via `stripe.webhooks.constructEvent(rawBody, signature, STRIPE_WEBHOOK_SECRET)`. The raw request body is read as `Buffer` (not parsed JSON) before verification. The endpoint returns HTTP 200 immediately after inserting the `billing_events` row; processing happens asynchronously in a pg-boss job.

**Abuse controls.** Rate limiting on the invitation acceptance endpoint: 5 attempts per IP per hour via an in-memory `Map<string, {count, resetAt}>` (acceptable at single-server launch; does not survive restarts, but the invitation token is single-use and expiring, so brute force on the token is the primary risk and is mitigated by token length: 64 hex characters = 256 bits of entropy). Media upload presigned URLs are workspace-scoped and expire in 15 minutes. IG OAuth state parameter is a 32-byte random hex nonce stored in the user's session for CSRF prevention.

**Audit.** All operator actions are written to `operator_audit_log` with `before_state` and `after_state`. Sensitive fields (tokens, encrypted data) are excluded from before/after snapshots. The operator admin area requires re-authentication (TOTP) for any write action.

**Retention and export.** Media files are retained until the user deletes them or requests account deletion. Analytics snapshots are retained indefinitely (they are small — approximately 200 bytes per snapshot, 3 snapshots per post, ~60 bytes per metric row at 200 posts/month = 36 KB/month). User data export packages all `queue_items`, `publish_receipts`, `analytics_snapshots`, and `media_files` metadata (not the R2 objects themselves, which are linked by signed URL valid for 24 hours) into a zip file generated server-side and returned as a download.

**Deletion.** The deletion worker runs the following sequence, idempotently: (1) revoke all `instagram_tokens` via Graph API `DELETE /me/permissions` and set `revoked_at`; (2) cancel Stripe subscription via `stripe.subscriptions.cancel`; (3) delete `sessions` rows; (4) delete `media_files` rows and associated R2 objects via `DeleteObjectCommand`; (5) delete `workspace_members` rows; (6) delete the `users` row; (7) set `deletion_requests.status = 'completed'`. The `deletion_requests` row is kept with only `id`, `reference_code`, `request_type`, `status`, `completed_at` — no email, no user ID, no names.

---

## 10. Risk Register

| Risk | Early Warning Signal | Mitigation in This Plan |
|---|---|---|
| Meta changes the Content Publishing API in a breaking way | `ig_error_code` frequency spike in `publish_attempts`; publishing failure rate above 5% | API version pinned in all Graph API calls (`/v21.0/`); monitor Meta changelog weekly; `needs_review` status preserves work without data loss |
| Instagram bans the scraping pool accounts used by the restricted sourcing worker | `source_candidates` insert rate drops to 0; Playwright navigation failure rate rises above 20% | Scraping pool is operator-managed (not customer accounts); rate limit set to 60 navigations/hour per account; capability is operator-enabled only and can be disabled without affecting the main product |
| Meta enforces ToS against ToolBox Poster as a platform, not individual accounts | A C&D letter or App Review rejection | Restricted sourcing is invite-only and isolated; public product is upload-only and uses the official Graph API; legal counsel review before restricted sourcing Phase 8 begins |
| Single Railway Postgres instance fails (disk full or crash) | Railway health check fails; pg-boss jobs stop processing | Daily `pg_dump` to R2 (pg-boss job at 03:00 UTC); restore drill in nightly CI; Railway's own WAL backup as a secondary layer; alert if dump job fails |
| Publishing worker crashes with `status='publishing'` and no `request_initiated_at` set | `needs_review` notifications spike without corresponding IG posts | `lease_recovery` job fires every 5 minutes; items with `request_initiated_at IS NULL` are automatically safe to re-queue; operator dashboard shows the distinction |
| Stripe webhook delivery gap (all webhooks fail for >24 hours) | `billing_events` table shows no new rows for >6 hours | Idempotent `billing_events` table handles replay; Stripe retries webhooks for 72 hours; manual reconciliation via Stripe dashboard is the fallback |
| Storage cost grows faster than expected (video-heavy users) | R2 storage exceeds 500 GB/month (triggers alert at $7.50/month) | Per-workspace storage quotas enforced at upload time; quota check against `SUM(size_bytes) FROM media_files WHERE workspace_id=$1 AND deleted_at IS NULL`; plan tier defines the cap |
| Legal action from a content creator whose content was sourced and reposted | DMCA takedown notice received | Provenance is preserved in `source_candidates` (source username, permalink, observed attribution); DMCA process documented in public terms; operator can remove content and revoke entitlement |
| Invite-only beta does not convert to paid users | 0 paid conversions after 60 days of beta access | Early pilot cohort of 10 paying customers targeted before Phase 6 launches; pricing page live in Phase 0; feedback submission flow live in Phase 7 |
| One-person engineering team is unavailable for > 48 hours | Publishing failure rate rises; no one responds to operator alerts | Runbook in git repository covers the 5 most common operator actions; pg-boss jobs retry automatically; work is held (not destroyed) on any outage; `lease_recovery` runs without operator intervention |

---

## 11. Explicit Tradeoffs

1. **Notification delivery is eventual, not real-time for background events.** SSE connections are scoped to the browser tab. A user who closes the browser and returns 2 hours later will see all notifications in the notification center (loaded from Postgres), but did not receive a push alert while away. Push notifications (web push or email for urgent events) are deferred to post-launch. Acceptable because the operator support flow (dashboard, feedback) covers the most critical cases.

2. **Analytics data is collected at fixed intervals, not continuously.** Snapshots are taken at +24h, +7d, +30d from publish. Instagram Insights data between those windows is not captured. This means time-series granularity is 3 points per post. The brief asks for "change over time" — 3 points is technically "over time" but is coarser than the brief implies. Acceptable because more frequent polling would exhaust Instagram's rate limits and the benefit is low for the launch audience.

3. **Queue position is a float, not a guaranteed integer sequence.** Reordering uses fractional positions (Lexorank-style). After ~50 reorders between the same two items, float precision degrades. The application layer detects when adjacent `position` values differ by less than `1e-9` and runs a full reindex of the account's queue (renumber all items 1000, 2000, 3000, …) in a single transaction. This reindex is not visible to the user. The tradeoff: a rare, brief lock on the account's queue items. Acceptable at launch scale.

4. **Restricted sourcing session files are stored on a Railway ephemeral volume.** If the volume is lost (Railway volume failure or service rebuild), all scraping sessions must be re-authenticated manually by the operator. This is a recovery burden but not a data loss event — no customer content is in the sessions file. The customer's already-discovered candidates remain in `source_candidates`.

5. **Daily publish count is not pre-warmed from Instagram's API.** On first deploy or recovery, `instagram_daily_publish_counts` starts at 0. If the Instagram account has already published posts today through another tool, the count will be underestimated. The mitigation is a `connection_health_check` job that calls `GET /{ig-user-id}/content_publishing_limit` (Instagram's own daily limit endpoint) and reconciles the count. This job runs on account connect and once per hour. The window between the check and the next publish where the count could be stale is at most 60 minutes. During that window, overcount is possible but bounded by the 60-minute reconciliation interval.

6. **Single Postgres instance with no read replica.** All reads and writes go to one Railway Postgres instance. At launch scale (200 WAU, hundreds of publishes/day), query load is under 50 queries/second, well within a single instance's capacity. A read replica is not provisioned until Postgres CPU exceeds 70% for 3 consecutive days (monitored by Railway metrics).

7. **No DMCA takedown automation for restricted sourcing.** When a takedown notice arrives, the operator must manually remove the content and revoke the entitlement if needed. Automated DMCA processing is deferred to post-launch. Acceptable because the restricted sourcing feature is invite-only beta.

---

## 12. Where This Is Stronger Than Required

1. **`publish_receipts` are immutable.** The brief requires a durable receipt; it does not require immutability enforcement at the database level. This plan adds a Postgres `RULE` that converts `UPDATE ON publish_receipts` to a no-op, making accidental mutation via an errant SQL query impossible. This provides a stronger audit guarantee than the brief requires.

2. **`request_initiated_at` distinguishes crash-before-send from crash-after-send.** The brief requires that uncertain outcomes enter reconciliation. This plan's `publish_attempts.request_initiated_at` field further distinguishes "definitely did not reach Instagram" (can be automatically re-queued) from "may have reached Instagram" (requires manual reconciliation). The operator gets a triage signal without guessing.

3. **Billing events are an append-only log, not just idempotent state updates.** The brief requires that repeated billing events not corrupt state. This plan also preserves the full history of Stripe events in `billing_events`, which makes debugging subscription state disputes possible without contacting Stripe support.

4. **Cleanup selection hash is verified both at confirmation and at execution time.** The brief requires that the selection be verified before running. This plan re-verifies the hash before processing each item (not just at the start of the run), catching cases where a post is protected or deleted mid-run.

5. **Token encryption key is not accessible to the web process.** The brief requires that tokens not leak. This plan goes further by ensuring the web process does not have the decryption key at all (separate Railway service environment variable), so a compromised web process cannot decrypt customer tokens even with full database access.

---

## 13. Assumptions

1. The Instagram Graph API Content Publishing endpoint accepts a media container ID and publishes it within 30 seconds under normal conditions. If IG processing takes longer than 30 seconds, the attempt is treated as uncertain.

2. Instagram's `content_publishing_limit` endpoint (`GET /{ig-user-id}/content_publishing_limit?fields=config,quota_usage`) accurately reflects the remaining daily quota. This is used for daily count reconciliation.

3. "Professional Instagram accounts" means Business or Creator accounts connected to a Facebook Page. Personal accounts are excluded (a stated non-goal).

4. The initial geographic region is the continental United States. All timezone handling is tested for US timezones. Non-US timezones work correctly (using `luxon`) but are not in the test matrix.

5. The operator is a co-founder with read access to the Railway dashboard and Postgres console. "Non-technical operator" means no custom SQL queries; the operator dashboard must expose all support actions as UI buttons. The operator never needs a developer console for routine support.

6. Maximum video duration for publishing is 60 seconds for Reels and 60 minutes for feed videos, per Instagram's current limits. FFmpeg preparation enforces these limits at preparation time, not at publish time.

7. Stripe is the only payment processor. No cryptocurrency, bank transfer, or invoice-based billing.

8. "A few hundred dollars per month" means $500/month maximum infrastructure spend. The plan targets $75–$150/month at launch scale, leaving 70%+ headroom.

9. Backups run daily at 03:00 UTC using `pg_dump | gzip` piped to R2 via `PutObjectCommand`. Backup retention is 30 days (30 objects in R2 at ~50 MB each compressed = 1.5 GB = $0.023/month).

10. Invite-only beta means no self-serve account creation. The operator manually sends invitations via the admin dashboard. There is no automated waitlist-to-invitation pipeline in v1.

11. "Collaborators" means workspace members with `role = 'admin'` or `role = 'member'`. The distinction: `owner` can delete the workspace and change billing; `admin` can connect/disconnect accounts and manage queue; `member` can add to queue and view analytics only.

12. A single `workspace_entitlements` row with `feature = 'extra_seats'` covers additional paid seats beyond the plan's base collaborator allowance. The number of extra seats is stored in `subscriptions.extra_seats`, not in a separate entitlement count.

13. Cloudflare R2 is accessible from Railway's US-East region with latency under 50 ms for upload/download operations (assumption based on R2's US-East bucket colocation; verified in Phase 2 nightly tests).

14. The restricted sourcing worker uses Instagram accounts owned and managed by the operator (not customer accounts) for Playwright browsing sessions. These accounts are enrolled in the `instagram_tokens` table with `pool_type = 'scraping'`.

15. "Managed cleanup" archives feed photos and sends Reels to Instagram's Recently Deleted — it does not permanently delete immediately. The distinction between "archive" and "Recently Deleted" is as described in §3.8. The plan assumes no Instagram API for immediate permanent deletion exists; the `DELETE /{ig-media-id}` Graph API endpoint is the only mechanism and it moves Reels to Recently Deleted.
