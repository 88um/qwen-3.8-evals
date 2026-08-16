# ToolBox Poster Engineering Plan

## 1. Technology decisions

### 1.1 Primary database

**Choice:** PostgreSQL 16.

**Rejected:** SQLite. At launch scale (200 WAU, hundreds of connected accounts, thousands of queue items/day), SQLite's single-writer model would serialize all publishing workers, media preparation jobs, and API requests. The product requires `SELECT ... FOR UPDATE SKIP LOCKED` for work claiming, partial indexes for enforcing "one active publish per item," and row-level locking for concurrent queue manipulation.

**Why:** Every invariant in §4 depends on conditional updates under concurrency. Queue ordering, publish deduplication, and cleanup serialization all require row-level locking that SQLite cannot provide.

### 1.2 Backend framework

**Choice:** Next.js 14 with App Router, running on Node.js 20 LTS.

**Rejected:** Separate backend (e.g., Go + REST) with a separate frontend. This doubles deployment units, requires API versioning, and adds coordination overhead for a solo founder. Next.js Server Actions and API routes colocate data mutations with their UI, reducing the surface area for inconsistency.

**Why:** One deployable artifact, one language, one type system. Server Components eliminate most client-side data fetching complexity. The operator can deploy one process and scale it horizontally behind a load balancer.

### 1.3 Frontend state and real-time updates

**Choice:** React Server Components for initial render, Tanstack Query for cache/mutations, Server-Sent Events (SSE) for push updates.

**Rejected:** WebSockets via Socket.io. WebSockets require persistent connections, complicate horizontal scaling (sticky sessions or Redis pub/sub), and introduce reconnection state. SSE is HTTP, works through standard proxies, reconnects automatically, and one-way push is sufficient for status updates.

**Why:** Queue status, publishing progress, and notifications are read-only streams. SSE delivers them with minimal infrastructure. Tanstack Query handles optimistic updates and cache invalidation when the user takes action.

### 1.4 Background job processing

**Choice:** pg-boss (PostgreSQL-backed job queue).

**Rejected:** Redis-backed queues (BullMQ). Adding Redis introduces a second stateful service, doubles backup requirements, and creates split-brain risk between job state and application state. pg-boss stores jobs in PostgreSQL, so transactional enqueueing ("create queue item AND enqueue publish job" atomically) is possible.

**Why:** Atomic enqueueing with application writes prevents ghost jobs and orphan queue items. Job completion, retry state, and dead-letter inspection live in the same database as the application, queryable with SQL.

### 1.5 Object storage

**Choice:** Cloudflare R2.

**Rejected:** AWS S3. S3 egress is $0.09/GB; R2 egress is $0. At launch scale with media retrieval for preview, preparation, and publishing, egress dominates storage cost. R2 is S3-compatible, so migration is a configuration change.

**Why:** Budget is "a few hundred dollars per month." With 10 GB average storage growing 5 GB/month and heavy read access for media preparation, R2's zero egress keeps storage under $5/month. S3 equivalent would be $50+/month at the same read volume.

### 1.6 Hosting and compute

**Choice:** Fly.io with separate process groups (web, worker, scheduler).

**Rejected:** Vercel. Vercel's serverless functions have a 60-second timeout (300s on Pro), insufficient for media processing or browser automation. Background jobs require external infrastructure. Fly.io runs persistent processes, allows memory-heavy workers, and provides private networking between process groups.

**Why:** Media preparation and restricted browser automation require processes that run for minutes. Fly.io's pricing at $0.0000025/s for shared CPU and $6.50/GB-month RAM fits the budget. 1 web machine (512 MB), 2 workers (1 GB each), 1 scheduler (256 MB) = ~$20/month compute.

### 1.7 Browser automation for restricted sourcing

**Choice:** Playwright with persistent browser contexts, running in dedicated worker processes.

**Rejected:** Puppeteer. Playwright has better stealth defaults, first-party context persistence (`storageState`), and cross-browser support if Instagram ever requires different browser fingerprints.

**Why:** Instagram's Reels discovery and restricted account operations require browser automation. `browserContext.storageState({path})` persists cookies and localStorage but not DOM state, so every operation starts from a known navigation point.

### 1.8 Instagram API integration

**Choice:** Instagram Graph API via Facebook Business SDK for public operations (publish, analytics). Browser automation only for restricted sourcing features.

**Rejected:** Full browser automation for publishing. The Graph API provides stable, documented endpoints for professional account publishing with webhook callbacks. Browser automation for publishing would be fragile, rate-limited differently, and violate platform terms.

**Why:** Publishing through the official API is the product's public posture. Browser automation is isolated to the restricted sourcing capability, which is operator-controlled and not a public launch feature.

### 1.9 Authentication

**Choice:** NextAuth.js with email magic links, stored in PostgreSQL.

**Rejected:** Firebase Auth or Auth0. Both add external dependencies, increase latency, and complicate data-residency guarantees. At launch scale (1,000 users), self-hosted auth with magic links is simpler, cheaper, and keeps all user data in one database.

**Why:** Magic links require only an email provider (Resend, $0 at this volume). No password storage, no OAuth complexity for user identity. Instagram OAuth is separate (account connection), not user authentication.

### 1.10 Payment processing

**Choice:** Stripe Checkout and Billing Portal, with Stripe webhooks for subscription lifecycle.

**Rejected:** Paddle, Lemon Squeezy. Stripe has the most mature webhook system, idempotency-key support, and customer portal. The 2.9% + $0.30 fee is acceptable at low volume.

**Why:** Stripe Checkout means card details never touch our servers. The Billing Portal handles plan changes without custom UI. Webhook idempotency (using Stripe event IDs as idempotency keys) prevents duplicate billing state mutations.

### 1.11 Email

**Choice:** Resend.

**Rejected:** SendGrid, AWS SES. Resend has a generous free tier (3,000 emails/month), simple API, and React Email support for templated transactional emails. SES requires AWS account setup; SendGrid's free tier has degraded recently.

**Why:** Magic links, connection expiry alerts, and billing notifications fit within 3,000/month for 200 WAU. Cost: $0.

### 1.12 CDN and edge

**Choice:** Cloudflare (free tier) in front of Fly.io.

**Rejected:** No CDN. Static assets, media previews, and public pages benefit from edge caching. Cloudflare's free tier handles the launch traffic.

**Why:** Cost: $0. Provides DDoS protection, caching, and a stable domain while the origin may shift.

---

## 2. System architecture

### 2.1 Process groups

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              Cloudflare CDN                                  │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Fly.io Private Network                             │
│                                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌────────────┐ │
│  │   Web (x2)   │    │ Worker (x2)  │    │  Scheduler   │    │  Browser   │ │
│  │              │    │              │    │    (x1)      │    │  Worker    │ │
│  │ Next.js      │    │ pg-boss      │    │              │    │   (x1)     │ │
│  │ SSE streams  │    │ consumers    │    │ Cron jobs    │    │            │ │
│  │ API routes   │    │              │    │ pg-boss      │    │ Playwright │ │
│  │              │    │ Media prep   │    │ scheduling   │    │ contexts   │ │
│  │              │    │ Publishing   │    │              │    │            │ │
│  │              │    │ Analytics    │    │              │    │ Restricted │ │
│  │              │    │              │    │              │    │ sourcing   │ │
│  └──────────────┘    └──────────────┘    └──────────────┘    └────────────┘ │
│         │                   │                   │                   │        │
│         └───────────────────┴───────────────────┴───────────────────┘        │
│                                      │                                       │
│                                      ▼                                       │
│                          ┌────────────────────┐                              │
│                          │   PostgreSQL 16    │                              │
│                          │   (Fly Postgres)   │                              │
│                          └────────────────────┘                              │
│                                      │                                       │
└──────────────────────────────────────┼───────────────────────────────────────┘
                                       │
                                       ▼
                            ┌────────────────────┐
                            │  Cloudflare R2     │
                            │  (media storage)   │
                            └────────────────────┘
```

### 2.2 Responsibilities

| Process | Count | RAM | Responsibilities |
|---------|-------|-----|------------------|
| Web | 2 | 512 MB | HTTP requests, Server Components, API routes, SSE streams, enqueueing jobs |
| Worker | 2 | 1 GB | pg-boss consumers for: media preparation, publishing, analytics fetch, cleanup execution |
| Scheduler | 1 | 256 MB | Cron: enqueue scheduled publishes, enqueue analytics refreshes, lease expiry checks, daily backup verification |
| Browser Worker | 1 | 2 GB | Playwright browser contexts for restricted sourcing; isolated from publishing workers |

### 2.3 Work isolation

**Principle:** Heavy or risky work must not starve or endanger unrelated work.

| Work type | Queue name | Concurrency | Isolation mechanism |
|-----------|------------|-------------|---------------------|
| Media preparation | `media-prep` | 4 per worker | Separate queue; failure affects only that item |
| Publishing (Graph API) | `publish` | 8 per worker | Separate queue; per-account rate limit (25/hr) |
| Analytics fetch | `analytics` | 2 per worker | Low priority; backpressure via longer retry delay |
| Cleanup execution | `cleanup` | 1 per worker | Serialized per account via DB lock; cannot run concurrently with publish for same account |
| Restricted sourcing | `sourcing` | 1 (dedicated process) | Entirely separate process group; pg-boss queue isolated |

### 2.4 Communication patterns

1. **User action → Background job:** Transactional enqueue. Insert row + enqueue job in same transaction. Job references row ID.

2. **Job completion → User notification:** Job updates row status. SSE query polls for rows with `updated_at > last_seen` per workspace.

3. **Scheduled publish:** Scheduler cron (every 60s) queries `queue_items` for items whose `scheduled_at <= NOW()` and `status = 'ready'`. Enqueues `publish` job for each, updating status to `'publishing'` atomically.

4. **Instagram webhook → Receipt:** Webhook handler validates signature, finds matching `publish_attempt` by correlation ID, updates status, creates receipt.

### 2.5 Starvation prevention

- Each queue has independent concurrency. A stuck cleanup cannot block publishing.
- Browser Worker is a separate process group. A Playwright hang does not affect API-based publishing.
- Per-account rate limits are enforced in application code before enqueueing, not by queue backpressure.
- Long-running media preparation (video transcoding) uses job timeout of 5 minutes; exceeded jobs are failed and retryable.

---

## 3. Data model

```sql
-- Extensions
CREATE EXTENSION IF NOT EXISTS "pgcrypto";
CREATE EXTENSION IF NOT EXISTS "btree_gist";

-- Enums
CREATE TYPE user_role AS ENUM ('owner', 'admin', 'member');
CREATE TYPE workspace_status AS ENUM ('active', 'suspended');
CREATE TYPE account_status AS ENUM ('connected', 'disconnected', 'revoked', 'paused');
CREATE TYPE queue_item_status AS ENUM (
    'preparing', 'ready', 'scheduled', 'publishing', 'published', 
    'failed', 'needs_review', 'hidden', 'canceled'
);
CREATE TYPE publish_attempt_status AS ENUM ('pending', 'sent', 'succeeded', 'failed', 'uncertain');
CREATE TYPE cleanup_status AS ENUM ('pending', 'running', 'completed', 'failed', 'paused');
CREATE TYPE source_status AS ENUM ('pending_verification', 'active', 'paused', 'retrying', 'blocked');

-- Users
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    email_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX users_email_idx ON users (email) WHERE deleted_at IS NULL;

-- Sessions (NextAuth)
CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_token TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX sessions_token_idx ON sessions (session_token);
CREATE INDEX sessions_expires_idx ON sessions (expires_at);

-- Workspaces
CREATE TABLE workspaces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    slug TEXT NOT NULL UNIQUE,
    status workspace_status NOT NULL DEFAULT 'active',
    onboarding_completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ,
    CONSTRAINT slug_format CHECK (slug ~ '^[a-z0-9-]+$')
);

-- Workspace memberships
CREATE TABLE workspace_members (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role user_role NOT NULL DEFAULT 'member',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (workspace_id, user_id)
);
CREATE INDEX workspace_members_user_idx ON workspace_members (user_id);

-- Invitations
CREATE TABLE invitations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(32), 'hex'),
    role user_role NOT NULL DEFAULT 'member',
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    created_by UUID NOT NULL REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX invitations_token_idx ON invitations (token) WHERE accepted_at IS NULL;
CREATE INDEX invitations_email_idx ON invitations (email) WHERE accepted_at IS NULL;

-- Waitlist
CREATE TABLE waitlist_entries (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    context JSONB,
    invited_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Instagram accounts
CREATE TABLE instagram_accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    instagram_user_id TEXT NOT NULL,
    instagram_username TEXT NOT NULL,
    profile_picture_url TEXT,
    facebook_page_id TEXT NOT NULL,
    access_token_encrypted BYTEA NOT NULL,
    access_token_expires_at TIMESTAMPTZ,
    status account_status NOT NULL DEFAULT 'connected',
    daily_publish_limit INTEGER NOT NULL DEFAULT 25,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    disconnected_at TIMESTAMPTZ,
    UNIQUE (instagram_user_id)
);
CREATE INDEX instagram_accounts_workspace_idx ON instagram_accounts (workspace_id);

-- Prevent same Instagram account in multiple workspaces
CREATE UNIQUE INDEX instagram_accounts_one_workspace_idx 
    ON instagram_accounts (instagram_user_id) 
    WHERE status NOT IN ('disconnected', 'revoked');

-- Account daily usage (survives restarts)
CREATE TABLE account_daily_usage (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    date DATE NOT NULL,
    publish_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE (account_id, date)
);

-- Schedule rules
CREATE TABLE schedule_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    rule_type TEXT NOT NULL CHECK (rule_type IN ('fixed_time', 'interval')),
    days_of_week INTEGER[] NOT NULL DEFAULT '{0,1,2,3,4,5,6}',
    time_of_day TIME,
    interval_minutes INTEGER,
    window_start TIME,
    window_end TIME,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (
        (rule_type = 'fixed_time' AND time_of_day IS NOT NULL) OR
        (rule_type = 'interval' AND interval_minutes IS NOT NULL AND window_start IS NOT NULL AND window_end IS NOT NULL)
    )
);
CREATE INDEX schedule_rules_account_idx ON schedule_rules (account_id) WHERE enabled = TRUE;

-- Media files
CREATE TABLE media_files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    original_filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    storage_key TEXT NOT NULL UNIQUE,
    prepared_storage_key TEXT,
    width INTEGER,
    height INTEGER,
    duration_seconds NUMERIC(10,2),
    checksum_sha256 TEXT NOT NULL,
    upload_completed_at TIMESTAMPTZ,
    preparation_completed_at TIMESTAMPTZ,
    preparation_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);
CREATE INDEX media_files_workspace_idx ON media_files (workspace_id) WHERE deleted_at IS NULL;
CREATE INDEX media_files_checksum_idx ON media_files (workspace_id, checksum_sha256);

-- Queue items (frozen at queue time)
CREATE TABLE queue_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    account_id UUID NOT NULL REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    media_file_id UUID NOT NULL REFERENCES media_files(id),
    
    -- Frozen content (immutable after queueing)
    frozen_caption TEXT NOT NULL,
    frozen_media_key TEXT NOT NULL,
    frozen_media_type TEXT NOT NULL CHECK (frozen_media_type IN ('image', 'video', 'reel')),
    frozen_preparation_settings JSONB NOT NULL DEFAULT '{}',
    
    -- Source tracking (for restricted sourcing)
    source_type TEXT NOT NULL CHECK (source_type IN ('upload', 'sourced')),
    source_post_id TEXT,
    source_author TEXT,
    source_original_url TEXT,
    
    -- Queue state
    status queue_item_status NOT NULL DEFAULT 'preparing',
    queue_position INTEGER NOT NULL,
    scheduled_at TIMESTAMPTZ,
    
    -- Rights confirmation
    rights_confirmed_at TIMESTAMPTZ NOT NULL,
    rights_confirmed_by UUID NOT NULL REFERENCES users(id),
    rights_version INTEGER NOT NULL DEFAULT 1,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    -- Row version for optimistic locking
    row_version INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX queue_items_account_status_idx ON queue_items (account_id, status);
CREATE INDEX queue_items_account_position_idx ON queue_items (account_id, queue_position) 
    WHERE status IN ('ready', 'scheduled');
CREATE INDEX queue_items_scheduled_idx ON queue_items (scheduled_at) 
    WHERE status = 'scheduled' AND scheduled_at IS NOT NULL;

-- Prevent duplicate source posts in same account's active queue
CREATE UNIQUE INDEX queue_items_no_duplicate_source_idx 
    ON queue_items (account_id, source_post_id) 
    WHERE source_post_id IS NOT NULL 
    AND status NOT IN ('published', 'failed', 'canceled', 'hidden');

-- Publish attempts (audit trail)
CREATE TABLE publish_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id UUID NOT NULL REFERENCES queue_items(id) ON DELETE CASCADE,
    account_id UUID NOT NULL REFERENCES instagram_accounts(id),
    
    -- Correlation for webhook matching
    correlation_id TEXT NOT NULL UNIQUE DEFAULT encode(gen_random_bytes(16), 'hex'),
    
    status publish_attempt_status NOT NULL DEFAULT 'pending',
    
    -- Timestamps for lifecycle
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    sent_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    
    -- Response data
    instagram_media_id TEXT,
    instagram_permalink TEXT,
    error_code TEXT,
    error_message TEXT,
    
    -- Evidence (no tokens)
    request_hash TEXT,
    response_summary JSONB,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX publish_attempts_item_idx ON publish_attempts (queue_item_id);
CREATE INDEX publish_attempts_correlation_idx ON publish_attempts (correlation_id);

-- Only one active publish attempt per queue item
CREATE UNIQUE INDEX publish_attempts_one_active_idx 
    ON publish_attempts (queue_item_id) 
    WHERE status IN ('pending', 'sent');

-- Publish receipts (final proof)
CREATE TABLE publish_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    queue_item_id UUID NOT NULL REFERENCES queue_items(id),
    publish_attempt_id UUID NOT NULL REFERENCES publish_attempts(id),
    account_id UUID NOT NULL REFERENCES instagram_accounts(id),
    workspace_id UUID NOT NULL REFERENCES workspaces(id),
    
    -- Final state (immutable)
    instagram_media_id TEXT NOT NULL,
    instagram_permalink TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL,
    
    -- Frozen evidence
    frozen_caption TEXT NOT NULL,
    frozen_media_checksum TEXT NOT NULL,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX publish_receipts_item_idx ON publish_receipts (queue_item_id);
CREATE INDEX publish_receipts_account_idx ON publish_receipts (account_id);

-- Analytics snapshots (historical)
CREATE TABLE analytics_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id UUID NOT NULL REFERENCES publish_receipts(id) ON DELETE CASCADE,
    
    -- Metrics at snapshot time
    reach INTEGER,
    impressions INTEGER,
    likes INTEGER,
    comments INTEGER,
    saves INTEGER,
    shares INTEGER,
    plays INTEGER,
    
    snapshot_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX analytics_snapshots_receipt_idx ON analytics_snapshots (receipt_id);
CREATE INDEX analytics_snapshots_time_idx ON analytics_snapshots (receipt_id, snapshot_at DESC);

-- Restricted sources (operator-entitled only)
CREATE TABLE content_sources (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    account_id UUID NOT NULL REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    
    source_type TEXT NOT NULL CHECK (source_type IN ('account', 'hashtag', 'reels_feed')),
    source_identifier TEXT NOT NULL,
    
    -- Filters
    min_likes INTEGER,
    min_comments INTEGER,
    min_plays INTEGER,
    max_age_days INTEGER,
    media_types TEXT[] DEFAULT '{image,video,reel}',
    exclude_words TEXT[],
    max_candidates INTEGER NOT NULL DEFAULT 50,
    check_interval_hours INTEGER NOT NULL DEFAULT 24,
    
    status source_status NOT NULL DEFAULT 'pending_verification',
    last_checked_at TIMESTAMPTZ,
    next_check_at TIMESTAMPTZ,
    error_message TEXT,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    UNIQUE (workspace_id, account_id, source_type, source_identifier)
);

CREATE INDEX content_sources_workspace_idx ON content_sources (workspace_id);
CREATE INDEX content_sources_next_check_idx ON content_sources (next_check_at) 
    WHERE status = 'active';

-- Source candidates (backlog)
CREATE TABLE source_candidates (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_id UUID NOT NULL REFERENCES content_sources(id) ON DELETE CASCADE,
    workspace_id UUID NOT NULL REFERENCES workspaces(id),
    
    -- Original content identity
    instagram_post_id TEXT NOT NULL,
    author_username TEXT NOT NULL,
    original_url TEXT NOT NULL,
    original_caption TEXT,
    media_type TEXT NOT NULL,
    
    -- Observed metrics
    observed_likes INTEGER,
    observed_comments INTEGER,
    observed_plays INTEGER,
    
    -- State
    media_retrieved BOOLEAN NOT NULL DEFAULT FALSE,
    media_file_id UUID REFERENCES media_files(id),
    queued_to_item_id UUID REFERENCES queue_items(id),
    rejected_reason TEXT,
    
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    UNIQUE (source_id, instagram_post_id)
);

CREATE INDEX source_candidates_source_idx ON source_candidates (source_id);

-- Prevent same Instagram post from being queued to same account twice
CREATE UNIQUE INDEX source_candidates_no_duplicate_queue_idx 
    ON source_candidates (workspace_id, instagram_post_id) 
    WHERE queued_to_item_id IS NOT NULL;

-- Cleanup rules
CREATE TABLE cleanup_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    
    name TEXT NOT NULL,
    
    -- Selection criteria
    min_age_days INTEGER NOT NULL,
    max_reach INTEGER,
    max_engagement_rate NUMERIC(5,4),
    media_types TEXT[] DEFAULT '{image,video,reel}',
    
    -- Schedule (null = manual only)
    schedule_cron TEXT,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX cleanup_rules_account_idx ON cleanup_rules (account_id) WHERE enabled = TRUE;
CREATE INDEX cleanup_rules_next_run_idx ON cleanup_rules (next_run_at) 
    WHERE enabled = TRUE AND schedule_cron IS NOT NULL;

-- Protected posts (exempt from cleanup)
CREATE TABLE protected_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    receipt_id UUID NOT NULL REFERENCES publish_receipts(id) ON DELETE CASCADE,
    protected_by UUID NOT NULL REFERENCES users(id),
    protected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (receipt_id)
);

-- Cleanup runs
CREATE TABLE cleanup_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES instagram_accounts(id),
    rule_id UUID REFERENCES cleanup_rules(id),
    
    status cleanup_status NOT NULL DEFAULT 'pending',
    
    -- Frozen rule at confirmation time
    frozen_rule JSONB NOT NULL,
    frozen_selection_hash TEXT NOT NULL,
    
    -- Selection
    selected_receipt_ids UUID[] NOT NULL,
    confirmed_by UUID REFERENCES users(id),
    confirmed_at TIMESTAMPTZ,
    
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    
    items_processed INTEGER NOT NULL DEFAULT 0,
    items_succeeded INTEGER NOT NULL DEFAULT 0,
    items_failed INTEGER NOT NULL DEFAULT 0,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX cleanup_runs_account_idx ON cleanup_runs (account_id);

-- Only one active cleanup per account
CREATE UNIQUE INDEX cleanup_runs_one_active_idx 
    ON cleanup_runs (account_id) 
    WHERE status IN ('pending', 'running');

-- Cleanup items (per-post results)
CREATE TABLE cleanup_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id UUID NOT NULL REFERENCES cleanup_runs(id) ON DELETE CASCADE,
    receipt_id UUID NOT NULL REFERENCES publish_receipts(id),
    
    -- Frozen metrics at selection
    frozen_metrics JSONB NOT NULL,
    
    action TEXT NOT NULL CHECK (action IN ('archive', 'delete')),
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'succeeded', 'failed', 'uncertain')),
    
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    
    -- Evidence (no tokens)
    response_summary JSONB,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX cleanup_items_run_idx ON cleanup_items (run_id);

-- Subscriptions and billing
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    
    stripe_customer_id TEXT NOT NULL,
    stripe_subscription_id TEXT UNIQUE,
    
    plan_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN (
        'trialing', 'active', 'past_due', 'canceled', 'unpaid', 'incomplete', 'paused'
    )),
    
    current_period_start TIMESTAMPTZ,
    current_period_end TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT FALSE,
    
    -- Derived entitlements (denormalized for fast checks)
    max_accounts INTEGER NOT NULL DEFAULT 1,
    max_collaborators INTEGER NOT NULL DEFAULT 1,
    cleanup_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    sourcing_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    UNIQUE (workspace_id)
);

CREATE INDEX subscriptions_stripe_idx ON subscriptions (stripe_subscription_id);

-- Billing events (idempotent processing)
CREATE TABLE billing_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    processed_at TIMESTAMPTZ,
    processing_error TEXT,
    raw_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX billing_events_unprocessed_idx ON billing_events (created_at) 
    WHERE processed_at IS NULL;

-- Entitlement overrides (operator grants)
CREATE TABLE entitlement_overrides (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    
    entitlement TEXT NOT NULL,
    granted BOOLEAN NOT NULL DEFAULT TRUE,
    expires_at TIMESTAMPTZ,
    
    granted_by UUID NOT NULL REFERENCES users(id),
    reason TEXT,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    
    UNIQUE (workspace_id, entitlement)
);

-- Notifications
CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id),
    
    type TEXT NOT NULL,
    priority TEXT NOT NULL CHECK (priority IN ('low', 'normal', 'high')),
    title TEXT NOT NULL,
    body TEXT,
    
    -- Deep link
    link_type TEXT,
    link_id UUID,
    
    read_at TIMESTAMPTZ,
    dismissed_at TIMESTAMPTZ,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX notifications_workspace_unread_idx 
    ON notifications (workspace_id, created_at DESC) 
    WHERE read_at IS NULL AND dismissed_at IS NULL;

-- Feedback
CREATE TABLE feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id),
    user_id UUID REFERENCES users(id),
    
    type TEXT NOT NULL CHECK (type IN ('bug', 'confusing', 'idea', 'praise', 'other')),
    body TEXT NOT NULL,
    page_url TEXT,
    user_agent TEXT,
    
    resolved_at TIMESTAMPTZ,
    resolved_by UUID REFERENCES users(id),
    resolution_notes TEXT,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit log
CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id),
    user_id UUID REFERENCES users(id),
    
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id UUID,
    
    details JSONB,
    ip_address INET,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX audit_log_workspace_idx ON audit_log (workspace_id, created_at DESC);
CREATE INDEX audit_log_resource_idx ON audit_log (resource_type, resource_id);

-- Account connection requests (operator review)
CREATE TABLE connection_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id),
    
    instagram_username TEXT NOT NULL,
    reason TEXT,
    
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'declined')),
    reviewed_by UUID REFERENCES users(id),
    reviewed_at TIMESTAMPTZ,
    review_notes TEXT,
    internal_notes TEXT,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX connection_requests_pending_idx 
    ON connection_requests (created_at) 
    WHERE status = 'pending';

-- Browser sessions (restricted sourcing, encrypted)
CREATE TABLE browser_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id UUID NOT NULL REFERENCES instagram_accounts(id) ON DELETE CASCADE,
    
    -- Encrypted Playwright storage state
    storage_state_encrypted BYTEA NOT NULL,
    
    -- Never exposed, only used server-side
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX browser_sessions_account_idx ON browser_sessions (account_id);

-- Deletion requests (GDPR compliance)
CREATE TABLE deletion_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL,
    
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    
    -- Proof without PII
    completion_reference TEXT
);

-- Content rights versions
CREATE TABLE content_rights_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version INTEGER NOT NULL UNIQUE,
    content TEXT NOT NULL,
    effective_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- User rights acceptances
CREATE TABLE rights_acceptances (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    rights_version INTEGER NOT NULL REFERENCES content_rights_versions(version),
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ip_address INET,
    UNIQUE (user_id, rights_version)
);

-- Job state (pg-boss tables are managed by pg-boss, but we add our own tracking)
CREATE TABLE job_leases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type TEXT NOT NULL,
    resource_id UUID NOT NULL,
    worker_id TEXT NOT NULL,
    acquired_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    released_at TIMESTAMPTZ,
    UNIQUE (job_type, resource_id) 
);

CREATE INDEX job_leases_active_idx ON job_leases (job_type, resource_id) 
    WHERE released_at IS NULL;
CREATE INDEX job_leases_expired_idx ON job_leases (expires_at) 
    WHERE released_at IS NULL;

-- Functions for queue position management
CREATE OR REPLACE FUNCTION next_queue_position(p_account_id UUID) 
RETURNS INTEGER AS $$
    SELECT COALESCE(MAX(queue_position), 0) + 1 
    FROM queue_items 
    WHERE account_id = p_account_id 
    AND status IN ('preparing', 'ready', 'scheduled', 'publishing');
$$ LANGUAGE SQL STABLE;

-- Trigger for updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER queue_items_updated_at
    BEFORE UPDATE ON queue_items
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

CREATE TRIGGER subscriptions_updated_at
    BEFORE UPDATE ON subscriptions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();
```

---

## 4. Invariant enforcement map

| # | Invariant (from brief) | Mechanism | Evidence test |
|---|------------------------|-----------|---------------|
| 1 | Publishes only what customer intended: correct workspace and account | `queue_items.workspace_id` FK + `queue_items.account_id` FK enforced at insert; publish job reads both and validates against `instagram_accounts.workspace_id` match | `test_publish_rejects_mismatched_workspace`: Create queue item in workspace A, attempt publish claiming workspace B, assert job fails with `WORKSPACE_MISMATCH` |
| 2 | Publishes only what customer intended: reviewed media and caption | `queue_items.frozen_caption`, `frozen_media_key` are immutable after insert (no UPDATE columns); publish reads only frozen fields | `test_frozen_content_survives_account_settings_change`: Queue item, change account settings, publish, assert receipt contains original frozen values |
| 3 | Never publish twice from double-click | `publish_attempts_one_active_idx` partial unique index on `(queue_item_id) WHERE status IN ('pending', 'sent')`; second insert fails with unique violation | `test_concurrent_publish_requests_only_one_succeeds`: Parallel requests to publish same item, assert exactly one `publish_attempts` row exists |
| 4 | Never publish twice from concurrent workers | `SELECT ... FOR UPDATE` on `queue_items` row before status transition from `ready` to `publishing`; second worker's WHERE clause matches zero rows | `test_two_workers_same_item_one_wins`: Spawn two worker threads, each tries to claim same item, assert one gets row, one gets zero rows |
| 5 | Never publish twice from retries | `queue_items.status` must be `ready` to start attempt; transition to `publishing` is atomic with attempt creation; retry checks status before creating new attempt | `test_retry_after_success_blocked`: Publish succeeds, attempt retry, assert blocked by status check |
| 6 | Never publish twice from slow response | Post-send timeout transitions to `uncertain`/`needs_review`, not `failed`; `needs_review` items cannot be republished until operator reconciles | `test_timeout_after_send_creates_uncertain`: Mock 30s timeout after API call sent, assert status is `needs_review`, assert retry button disabled |
| 7 | Never publish twice from duplicate callback | Webhook handler is idempotent: `UPDATE publish_attempts SET status = 'succeeded' ... WHERE id = ? AND status = 'sent'`; already-succeeded row returns 0 rows updated | `test_duplicate_webhook_no_effect`: Send same webhook twice, assert one receipt, status unchanged after second |
| 8 | Queue tells truth: order visible | `queue_items.queue_position` integer, UI queries `ORDER BY queue_position`; reorder is `UPDATE ... SET queue_position = ?` with optimistic lock on `row_version` | `test_queue_order_displayed_correctly`: Insert items with positions 1,2,3, assert UI shows that order |
| 9 | Queue tells truth: progress visible | `queue_items.status` enum covers full lifecycle; SSE stream queries `WHERE updated_at > ?`; status transitions are audited in `audit_log` | `test_status_transitions_streamed`: Change status, assert SSE client receives update within 2s |
| 10 | Queue tells truth: failures visible | Failed items have `status = 'failed'`, error info in linked `publish_attempts.error_message`; UI shows failure reason | `test_failure_shows_reason`: Trigger API error, assert UI displays error message from attempt |
| 11 | Queue tells truth: item never silently disappears | No DELETE on `queue_items` for user actions; only `status` transitions; soft delete via `canceled` status with audit | `test_cancel_preserves_item`: Cancel item, assert row exists with `status = 'canceled'`, assert audit log entry |
| 12 | Queue tells truth: item never jumps accounts | `account_id` has no UPDATE path; FK constraint; publish job validates | `test_account_id_immutable`: Attempt to UPDATE account_id, assert DB error or application rejection |
| 13 | Queue tells truth: uncertain outcome visible | `needs_review` status distinct from `failed`; UI shows "Uncertain outcome - verify on Instagram" with Instagram link | `test_uncertain_status_ui`: Create uncertain item, assert specific UI treatment |
| 14 | Uncertainty never becomes second destructive action | `needs_review` blocks all retry/republish until operator marks resolved; `publish_attempts_one_active_idx` prevents new attempt while `sent` attempt exists | `test_uncertain_blocks_retry`: Item in `needs_review`, attempt retry, assert blocked |
| 15 | Holding never destroys work: pause | `account_status = 'paused'` stops scheduler from picking items; items remain `ready` | `test_pause_holds_queue`: Pause account, wait past scheduled time, assert items still `ready` |
| 16 | Holding never destroys work: disconnect | `account_status = 'disconnected'` blocks publishing; items remain in queue | `test_disconnect_preserves_queue`: Disconnect, assert items exist and status unchanged |
| 17 | Holding never destroys work: suspension | `workspace_status = 'suspended'` checked before any action; existing items preserved | `test_suspension_holds_work`: Suspend workspace, assert queue intact |
| 18 | Holding never destroys work: payment problem | Subscription status checked; over-limit items held, not deleted | `test_payment_failure_holds_accounts`: Fail payment, assert connected accounts preserved |
| 19 | Private access: passwords never requested | No password field in schema; Instagram OAuth only; UI copy audit | Schema review + `test_no_password_fields_exist`: grep schema for 'password' excluding 'instagram_password' (none should exist) |
| 20 | Private access: tokens not in logs | `access_token_encrypted` is BYTEA; logging config excludes `*_encrypted` columns; structured logging with allowlist | `test_logs_exclude_tokens`: Trigger auth flow, grep logs for token patterns, assert zero matches |
| 21 | Private access: tokens not in receipts | `publish_receipts` schema has no token columns; `publish_attempts.response_summary` is sanitized before storage | Schema review + `test_receipt_has_no_secrets`: Create receipt, assert no token-like strings |
| 22 | Private access: tokens not in browser | Access token used server-side only; never returned in API responses | `test_api_responses_no_tokens`: Call all auth-related endpoints, assert no access_token in JSON |
| 23 | Private access: tenant isolation | All queries include `workspace_id` predicate; RLS considered but explicit is auditable; media URLs are signed with workspace context | `test_cross_workspace_query_fails`: User in workspace A attempts to query workspace B item, assert 404 or empty |
| 24 | Private access: media links scoped | R2 presigned URLs include workspace_id in path; signed with 1-hour expiry | `test_media_url_expires`: Generate URL, wait 61 minutes, assert 403 |
| 25 | Idempotent billing events | `billing_events.stripe_event_id` UNIQUE; handler wrapped in `INSERT ... ON CONFLICT DO NOTHING`; actual processing checks `processed_at IS NULL` | `test_duplicate_stripe_event_no_effect`: Send same event twice, assert one `processed_at` set |
| 26 | Daily publish count survives restart | `account_daily_usage` table with `(account_id, date)` unique; count incremented transactionally with receipt creation | `test_usage_survives_restart`: Publish, restart worker, assert count intact |
| 27 | No duplicate source import | `source_candidates` has `UNIQUE (source_id, instagram_post_id)`; `queue_items` has partial unique on `(account_id, source_post_id)` for active items | `test_duplicate_source_collection_blocked`: Run collection twice concurrently, assert one candidate row |
| 28 | No duplicate refill | Queue refill transaction: `INSERT INTO queue_items SELECT ... FROM source_candidates WHERE queued_to_item_id IS NULL ... FOR UPDATE SKIP LOCKED` with immediate `UPDATE source_candidates SET queued_to_item_id = ?` | `test_concurrent_refill_no_duplicates`: Two refill jobs simultaneously, assert no duplicate queue items |
| 29 | Restricted features invisible without entitlement | `sourcing_enabled`, `cleanup_enabled` in `subscriptions`; API checks before returning data; UI conditionally renders; routes validate | `test_sourcing_hidden_without_entitlement`: User without sourcing entitlement, assert 404 on source endpoints, assert UI element absent |
| 30 | Single cleanup per account | `cleanup_runs_one_active_idx` partial unique index on `(account_id) WHERE status IN ('pending', 'running')` | `test_concurrent_cleanup_blocked`: Start cleanup, attempt second, assert unique violation |
| 31 | Cleanup uses confirmed selection | `cleanup_runs.frozen_selection_hash` = hash of `selected_receipt_ids` at confirmation; execution verifies hash before each item | `test_selection_change_invalidates_confirmation`: Confirm, add new post matching rule, re-run preview, assert hash differs, assert old run cannot proceed |
| 32 | Cleanup crash recovery | `cleanup_items` tracks per-item status; on restart, find `cleanup_runs` with `status = 'running'`, check for `uncertain` items, pause if found | `test_cleanup_crash_pauses_run`: Crash during cleanup, restart, assert run status is `paused`, assert notification created |
| 33 | Rights acceptance versioned | `queue_items.rights_version` FK to `content_rights_versions.version`; acceptance recorded per user | `test_rights_version_captured`: Queue item, assert `rights_version` matches current version |
| 34 | Deletion removes PII | Deletion job: DELETE cascade user data, UPDATE audit references to anonymized, DELETE media files, record `completion_reference` | `test_deletion_removes_pii`: Request deletion, complete, assert no user data queryable, assert `completion_reference` exists |

---

## 5. Failure-mode walkthrough

### 5.1 Crash before publish

**Scenario:** Worker crashes after claiming a queue item but before making any Instagram API call.

**What happens:**
1. Worker executes `SELECT id FROM queue_items WHERE account_id = ? AND status = 'ready' ORDER BY queue_position LIMIT 1 FOR UPDATE`.
2. Worker executes `UPDATE queue_items SET status = 'publishing' WHERE id = ? AND status = 'ready' RETURNING id`. Returns 1 row.
3. Worker executes `INSERT INTO publish_attempts (queue_item_id, account_id, status) VALUES (?, ?, 'pending') RETURNING id`.
4. Transaction commits.
5. Worker crashes before `status = 'sent'` or any API call.
6. `publish_attempts` row remains `status = 'pending'`.
7. Scheduler runs lease expiry check (every 60s): finds `publish_attempts` with `status = 'pending'` and `started_at < NOW() - INTERVAL '5 minutes'`.
8. Scheduler executes `UPDATE publish_attempts SET status = 'failed', error_message = 'Worker timeout before send' WHERE id = ? AND status = 'pending'`.
9. Scheduler executes `UPDATE queue_items SET status = 'ready' WHERE id = ? AND status = 'publishing'`.
10. Item is now retryable.

**Evidence:** `publish_attempts` row shows `status = 'failed'`, `error_message = 'Worker timeout before send'`, `sent_at IS NULL`. `audit_log` shows automatic recovery. `queue_items.status` is `ready`.

### 5.2 Crash after publish may have been accepted

**Scenario:** Worker crashes after calling Instagram API but before receiving response.

**What happens:**
1. Worker claims item, creates `publish_attempts` row, all committed.
2. Worker executes `UPDATE publish_attempts SET status = 'sent', sent_at = NOW() WHERE id = ?`. Committed.
3. Worker calls Instagram Graph API `POST /me/media` (container creation).
4. Worker crashes during HTTP request (connection reset, OOM, etc.).
5. `publish_attempts` row is `status = 'sent'`, `sent_at` set, no `completed_at`.
6. Scheduler finds `status = 'sent'` with `sent_at < NOW() - INTERVAL '5 minutes'`.
7. Scheduler executes `UPDATE publish_attempts SET status = 'uncertain' WHERE id = ? AND status = 'sent'`.
8. Scheduler executes `UPDATE queue_items SET status = 'needs_review' WHERE id = ?`.
9. Scheduler creates notification: "Publish outcome uncertain for [item]. Please verify on Instagram."

**Evidence:** `publish_attempts.status = 'uncertain'`, `publish_attempts.sent_at` set, `completed_at IS NULL`. `queue_items.status = 'needs_review'`. Notification exists with `type = 'uncertain_publish'`. Operator can query Instagram API with `source_id` to check if media exists.

### 5.3 Duplicate publish work

**Scenario:** Two workers simultaneously try to publish the same queue item.

**What happens:**
1. Worker A: `SELECT ... FOR UPDATE` on queue item. Acquires row lock.
2. Worker B: `SELECT ... FOR UPDATE` on same item. Blocks waiting for lock.
3. Worker A: `UPDATE queue_items SET status = 'publishing' WHERE id = ? AND status = 'ready' RETURNING id`. Returns 1 row. `INSERT INTO publish_attempts`. Commits. Lock released.
4. Worker B: Lock acquired. `UPDATE queue_items SET status = 'publishing' WHERE id = ? AND status = 'ready' RETURNING id`. Returns 0 rows (status already `publishing`). Worker B exits with "no work found."

**Evidence:** Exactly one row in `publish_attempts` for this `queue_item_id`. `queue_items` has exactly one status transition in `audit_log`.

### 5.4 Media preparation failure

**Scenario:** Video transcoding fails due to corrupt input.

**What happens:**
1. User uploads video. `media_files` row created with `upload_completed_at` set.
2. `queue_items` created with `status = 'preparing'`.
3. `media-prep` job claimed by worker.
4. Worker attempts FFmpeg transcode. FFmpeg returns error code.
5. Worker executes `UPDATE media_files SET preparation_error = 'Corrupt video: no valid frames found' WHERE id = ?`.
6. Worker executes `UPDATE queue_items SET status = 'failed' WHERE id = ? AND status = 'preparing'`.
7. Notification created: "Media preparation failed: Corrupt video."

**Evidence:** `media_files.preparation_error` contains FFmpeg error. `queue_items.status = 'failed'`. User can delete item and retry with different file.

### 5.5 Account revocation

**Scenario:** User revokes Instagram app permissions from Instagram settings.

**What happens:**
1. Instagram sends webhook to our callback endpoint: `deauthorize` event with user ID.
2. Handler validates webhook signature using app secret.
3. Handler executes `UPDATE instagram_accounts SET status = 'revoked', access_token_encrypted = NULL WHERE instagram_user_id = ?`.
4. Handler creates notification: "Instagram connection lost. Please reconnect."
5. Scheduler skips all items for revoked account (WHERE clause excludes `status != 'connected'`).
6. Queued items remain with original status; they are not deleted.
7. User reconnects via OAuth. If same workspace, `instagram_accounts.status` updated to `connected`, queue resumes.

**Evidence:** `instagram_accounts.status = 'revoked'`. `audit_log` shows revocation event with Instagram webhook correlation ID. Queued items exist with unchanged status.

### 5.6 Quota exhaustion

**Scenario:** Account reaches daily publish limit mid-queue.

**What happens:**
1. Scheduler attempts to enqueue publish job for next item.
2. Scheduler queries `SELECT publish_count FROM account_daily_usage WHERE account_id = ? AND date = CURRENT_DATE`.
3. Count is 25 (equals `instagram_accounts.daily_publish_limit`).
4. Scheduler skips item, logs "Daily limit reached for account [id]."
5. Item remains `status = 'scheduled'` or `ready`. Not failed.
6. Next day (00:00 in account timezone), new `account_daily_usage` row starts at 0.
7. Scheduler picks up item normally.

**Evidence:** `account_daily_usage.publish_count = 25`. Item still in queue with retryable status. No `publish_attempts` row created for skipped scheduling.

### 5.7 Schedule edits near slot

**Scenario:** User changes schedule rule 30 seconds before a scheduled publish.

**What happens:**
1. Item has `scheduled_at = 2024-01-15 10:00:00` based on old rule "10:00 AM daily."
2. User edits rule to "11:00 AM daily" at 09:59:30.
3. `UPDATE schedule_rules SET time_of_day = '11:00:00'`. Committed.
4. Application recalculates `scheduled_at` for all `status = 'ready'` or `status = 'scheduled'` items: `UPDATE queue_items SET scheduled_at = next_slot(...) WHERE account_id = ? AND status IN ('ready', 'scheduled')`.
5. Item now has `scheduled_at = 2024-01-15 11:00:00`.
6. Scheduler runs at 10:00:00, queries `WHERE scheduled_at <= NOW()`. Item does not match (scheduled_at is 11:00).
7. Item publishes at 11:00 as expected.

**Edge case:** If scheduler already claimed item at 09:59:55 and is mid-publish when rule changes, publish proceeds with frozen content. The rule change affects next item, not in-flight work.

**Evidence:** `queue_items.scheduled_at` reflects new rule. If publish was in-flight, `publish_receipts.published_at` is ~10:00. Audit log shows schedule edit timestamp.

### 5.8 Duplicate source collection

**Scenario:** Two collection jobs for the same source run concurrently due to scheduler race.

**What happens:**
1. Collection job A queries source, finds posts [P1, P2, P3].
2. Collection job B (duplicate) queries source, finds posts [P1, P2, P3].
3. Job A: `INSERT INTO source_candidates (source_id, instagram_post_id, ...) VALUES (?, 'P1', ...) ON CONFLICT (source_id, instagram_post_id) DO NOTHING`.
4. Job A inserts P1 successfully.
5. Job B: `INSERT INTO source_candidates (source_id, instagram_post_id, ...) VALUES (?, 'P1', ...) ON CONFLICT DO NOTHING`.
6. Job B insert returns 0 rows affected (conflict on unique constraint). Job B continues to P2.
7. All posts are collected exactly once regardless of race.

**Evidence:** `source_candidates` has exactly one row per `(source_id, instagram_post_id)`. No integrity errors.

### 5.9 Browser hang during cleanup

**Scenario:** Playwright browser hangs while archiving a post.

**What happens:**
1. Cleanup worker claims `cleanup_runs` row, sets `status = 'running'`.
2. Worker processes items sequentially. Item 1 succeeds.
3. Worker starts item 2. Calls Instagram API for archive.
4. HTTP request hangs. Worker timeout: 30 seconds.
5. Timeout fires. Worker catches timeout error.
6. Worker executes `UPDATE cleanup_items SET status = 'uncertain', error_message = 'Timeout during archive' WHERE id = ?`.
7. Worker executes `UPDATE cleanup_runs SET status = 'paused', items_failed = items_failed + 1 WHERE id = ?`.
8. Worker creates notification: "Cleanup paused: uncertain outcome on [post]. Please verify."
9. Remaining items are NOT processed. Run is paused.
10. Operator verifies on Instagram whether post was archived.
11. Operator marks item as `succeeded` or `failed`. If `failed`, run can resume.

**Evidence:** `cleanup_runs.status = 'paused'`. `cleanup_items` has one row with `status = 'uncertain'`. Subsequent items have `status = 'pending'`. Notification exists.

### 5.10 Changed cleanup selection

**Scenario:** User confirms cleanup, but before execution starts, a new post matches the rule.

**What happens:**
1. User previews cleanup rule. Sees 5 posts selected.
2. `cleanup_runs` created with `frozen_selection_hash = SHA256(sorted(receipt_ids))`, `selected_receipt_ids = [R1,R2,R3,R4,R5]`, `status = 'pending'`.
3. User confirms cleanup. `confirmed_at` set.
4. Before worker picks up run, new post (R6) becomes old enough to match rule.
5. Cleanup worker claims run. Worker recalculates selection using current rule.
6. New selection includes [R1,R2,R3,R4,R5,R6]. Hash differs from `frozen_selection_hash`.
7. Worker detects mismatch. Sets `status = 'failed'`, `error_message = 'Selection changed since confirmation'`.
8. Notification: "Cleanup canceled: selection changed. Please re-confirm."
9. No posts are modified.

**Evidence:** `cleanup_runs.status = 'failed'`. `cleanup_items` has zero rows with `status != 'pending'`. No archive/delete API calls made.

### 5.11 Repeated or out-of-order billing events

**Scenario:** Stripe sends `invoice.paid`, then `customer.subscription.updated`, then `invoice.paid` again (duplicate).

**What happens:**
1. Webhook 1 (`invoice.paid`, event ID `evt_123`): `INSERT INTO billing_events (stripe_event_id, ...) VALUES ('evt_123', ...)`. Succeeds. Handler processes, updates subscription.
2. Webhook 2 (`customer.subscription.updated`, event ID `evt_124`): Inserted, processed, subscription state updated.
3. Webhook 3 (`invoice.paid`, event ID `evt_123`, duplicate): `INSERT INTO billing_events ... ON CONFLICT (stripe_event_id) DO NOTHING`. Returns 0 rows. Handler exits early.
4. No duplicate processing.

**Out-of-order handling:** Handler applies Stripe's event data (which contains full object state), not deltas. Each event is processed independently. If subscription state arrives before payment confirmation, the payment confirmation event includes current subscription state and updates again (idempotent).

**Evidence:** `billing_events` has exactly one row per `stripe_event_id`. `subscriptions` reflects latest state. No duplicate charges or plan changes.

### 5.12 Storage failure

**Scenario:** R2 returns 500 error during media upload.

**What happens:**
1. User initiates upload via presigned URL.
2. R2 returns 500 during PUT.
3. Client retries (built into browser fetch with retry logic). Retry succeeds OR fails after 3 attempts.
4. If all retries fail, client reports error to API.
5. `media_files` row has `upload_completed_at IS NULL`.
6. UI shows "Upload failed. Please retry."
7. No `queue_items` row created (upload must complete first).
8. User can retry upload. Existing `media_files` row updated on success.

**Evidence:** `media_files.upload_completed_at IS NULL`. No orphan queue items. User sees actionable error.

### 5.13 Deletion interrupted halfway

**Scenario:** Deletion job crashes after deleting some data but before completing.

**What happens:**
1. Deletion job starts. `deletion_requests.status = 'processing'`.
2. Job deletes `sessions` rows (user logged out).
3. Job deletes `workspace_members` rows where user is only member (cascade to workspace).
4. Job crashes before deleting `media_files`.
5. Job restarts. Queries `deletion_requests WHERE status = 'processing'`.
6. Job resumes. Finds remaining data via user_id JOINs.
7. Job completes remaining deletions.
8. Job sets `status = 'completed'`, `completion_reference = 'del_...'`.

**Idempotency:** Each deletion step is idempotent (DELETE WHERE user_id = ?). Re-running on already-deleted data succeeds with 0 rows affected.

**Evidence:** `deletion_requests.status = 'completed'`. No data with `user_id = ?` exists. `completion_reference` is queryable proof.

---

## 6. AI strategy

**Decision:** AI is not included in v1.

**Rationale:**

1. **Captions are user-provided or sourced with original.** The product does not promise to generate captions. Users write captions during upload or accept sourced captions. No generation required.

2. **Media preparation is deterministic.** Aspect ratio adjustment, logo overlay, metadata stripping, and transcoding are FFmpeg operations. No vision model needed.

3. **Scheduling is rule-based.** Optimal posting time analysis is a v2 feature. v1 uses user-configured schedule rules.

4. **Content moderation scope.** Instagram's API handles content policy enforcement. We do not moderate beyond file format validation.

5. **Cost and complexity.** Adding AI for marginal features (caption suggestions, hashtag recommendations) would add $0.01-0.05 per operation, require prompt engineering maintenance, and introduce non-deterministic behavior that complicates debugging.

6. **Product differentiation is queue reliability, not AI features.** The five product promises are about correctness and safety, not intelligence.

**If AI were added (future consideration):**
- Caption suggestions: Claude Haiku, ~500 tokens in/out = $0.0005/suggestion, validate against original content to prevent fabrication
- Hashtag recommendations: embedding similarity on a curated set, no LLM call
- Content quality scoring: vision model, would require extensive calibration against engagement outcomes

These are v2 features after the core queue is proven reliable.

---

## 7. Testing and release confidence

### 7.1 On every commit (CI, ~5 minutes)

| Test type | Count | Coverage |
|-----------|-------|----------|
| Unit tests (business logic, no DB) | ~200 | Status transitions, queue position math, schedule calculation, timezone handling |
| Integration tests (test DB) | ~100 | CRUD operations, constraint enforcement, transaction isolation |
| API route tests (mock services) | ~80 | Request validation, auth checks, response shape |
| Component tests (Storybook) | ~60 | UI states: empty, loading, success, error, edge cases |

**Tools:** Vitest, Testing Library, Storybook, PostgreSQL in Docker.

**Blocking:** All must pass. Coverage threshold: 80% branches on business logic modules.

### 7.2 Requires real supporting services (staging, ~15 minutes)

| Test | Service | What it proves |
|------|---------|----------------|
| Stripe checkout flow | Stripe test mode | Subscription created, webhooks received, entitlements updated |
| Instagram OAuth | Instagram sandbox | Token exchange works, profile fetched, stored encrypted |
| R2 upload/download | R2 | Presigned URLs work, media persists, expires correctly |
| Email delivery | Resend | Magic link arrives, format correct |

**Frequency:** On merge to main, nightly.

### 7.3 Nightly drills (~30 minutes)

| Drill | Purpose |
|-------|---------|
| Chaos: kill worker mid-job | Verify recovery path for each job type |
| Chaos: drop DB connection | Verify transactions roll back, no partial state |
| Chaos: 5-minute network partition | Verify uncertain-state handling |
| Load: 1000 concurrent queue operations | Verify no deadlocks, measure p99 latency |
| Timezone: DST transition simulation | Verify schedule calculation across DST boundaries |
| Restore: backup → empty DB → verify | Prove backup is restorable, measure RTO |

### 7.4 Safe test Instagram accounts

| Account purpose | Operations allowed |
|-----------------|-------------------|
| Publish test | Create media containers, publish, delete (via test API) |
| Analytics test | Fetch insights (synthetic data in sandbox) |
| Cleanup test | Archive posts (test account only) |

**Requirements:**
- Test accounts are Facebook app-scoped, not real user accounts
- Test content is marked with `[TEST]` prefix
- All test publishes are deleted within 1 hour by cleanup job
- Test accounts excluded from billing and usage tracking

### 7.5 Invariant-specific tests

| Invariant | Test technique |
|-----------|---------------|
| No duplicate publish | Race condition test: 10 goroutines, same item, assert 1 receipt |
| Uncertain never retried | State machine fuzzer: random transitions, assert `needs_review` is terminal until explicit resolution |
| Tenant isolation | Property test: random user, random workspace, assert all queries return empty for non-member |
| Frozen content | Snapshot test: queue item, mutate account settings, publish, diff receipt vs. original |

---

## 8. Delivery phases

### Phase 0: Infrastructure foundation

**Build:**
- Fly.io configuration (web, worker, scheduler process groups)
- PostgreSQL provisioned with automated daily backups
- R2 bucket with lifecycle policy
- Cloudflare DNS and proxying
- CI/CD pipeline (GitHub Actions → Fly.io)
- Secrets management (Fly.io secrets, encrypted at rest)
- Monitoring baseline (Fly.io metrics, error tracking via Sentry)

**Exit criteria:**
- `fly deploy` succeeds for all process groups
- Database connection works from all processes
- R2 upload/download works with presigned URLs
- Backup runs and restore to staging environment succeeds
- Sentry receives test error

**Justification:** Nothing else can be built without infrastructure. This is the minimal viable runtime.

### Phase 1: Identity and workspaces

**Build:**
- User model, session management
- Magic link authentication
- Workspace creation and membership
- Invitation system (send, accept, expire)
- Basic middleware (auth, workspace context)

**Exit criteria:**
- User signs up via magic link, session created
- User creates workspace, becomes owner
- Owner invites collaborator, collaborator accepts
- Invitation expires after 7 days, link returns error
- User can belong to multiple workspaces
- Session expires, user redirected to login

**Justification:** Every subsequent feature requires authenticated users and workspace context.

### Phase 2: Instagram connection

**Build:**
- Instagram OAuth flow (via Facebook Graph API)
- Token encryption and storage
- Account selection (professional accounts only)
- Connection health display
- Revocation webhook handler
- Reconnection flow

**Exit criteria:**
- User initiates OAuth, selects professional account, account appears in workspace
- Access token is encrypted in DB, never returned to browser
- If user revokes from Instagram settings, webhook fires, account shows "revoked"
- User can reconnect same account, queue and history preserved
- Same Instagram account cannot be connected to two workspaces

**Justification:** Publishing requires a connected account. This proves the Instagram integration works before building queue.

### Phase 3: Media upload and preparation

**Build:**
- Presigned URL generation for R2
- Upload tracking (progress, completion)
- Media validation (format, size, duration limits)
- FFmpeg-based preparation (transcode, resize, metadata strip)
- Thumbnail generation
- Original preservation

**Exit criteria:**
- User uploads 50 MB video, sees progress, upload completes
- Corrupt file rejected with clear error
- 4K video transcoded to Instagram-compatible format
- Original file downloadable after preparation
- User can leave page during processing, return to see result

**Justification:** Queue items need prepared media. This must work before queue construction.

### Phase 4: Queue and scheduling (first end-to-end path)

**Build:**
- Queue item creation with frozen content
- Queue ordering (position, drag-and-drop)
- Schedule rules (fixed time, interval)
- Scheduler process (poll for due items)
- Publish job (Graph API media container → publish)
- Receipt creation
- SSE status updates
- Daily usage tracking

**Exit criteria:**
- User creates queue item, sees it in queue
- User reorders items, order persists
- User sets schedule "9:00 AM daily", item shows scheduled time
- Scheduled time arrives, item publishes to test Instagram account
- Receipt shows Instagram permalink, frozen caption
- Concurrent publish attempts: exactly one succeeds
- Worker crash before send: item returns to ready
- Worker crash after send: item shows "needs review"
- Daily limit (25) reached: remaining items held, not failed

**This is the first end-to-end phase.** All prior phases are justified as dependencies.

### Phase 5: Billing

**Build:**
- Stripe Checkout integration
- Stripe Billing Portal
- Webhook handlers (subscription created, updated, deleted, payment failed)
- Entitlement enforcement (account limits, collaborator limits)
- Plan comparison and upgrade/downgrade

**Exit criteria:**
- User on free plan, limited to 1 account
- User upgrades via Checkout, sees plan active
- Plan includes 5 accounts, user can connect 4 more
- Payment fails, subscription becomes `past_due`, publishing paused
- Webhook duplicate delivery: no duplicate state change
- Downgrade: excess accounts are disconnected but not deleted

**Justification:** Revenue enables continued operation. Entitlement enforcement protects business model.

### Phase 6: Analytics

**Build:**
- Instagram Insights API integration
- Snapshot storage (historical)
- Dashboard (per-account, per-post metrics)
- Manual refresh with rate limiting
- Best/worst post identification

**Exit criteria:**
- Published post shows reach, likes, comments after 24 hours
- Historical snapshots preserved (not overwritten)
- Dashboard shows trend over 30 days
- Manual refresh respects 1 request per 5 minutes per account
- Stale analytics (>24h) marked as "last updated X hours ago"

### Phase 7: Public site and waitlist

**Build:**
- Marketing pages (landing, pricing, security, privacy, terms)
- Waitlist signup
- Operator waitlist management

**Exit criteria:**
- Public can view product explanation without login
- Public can join waitlist with email
- Operator sees waitlist entries, can send invitations
- Invitation converts waitlist entry to user

### Phase 8: Operator tools

**Build:**
- Admin-only routes with elevated auth
- Dashboard (system health, queue depths, error rates)
- Workspace management (suspend, unsuspend)
- Item inspection (status, attempts, evidence)
- Safe retry for known-failed items
- Uncertain-outcome reconciliation

**Exit criteria:**
- Operator logs in with elevated privileges
- Dashboard shows connected accounts, pending publishes, failures
- Operator can inspect specific item, see all attempts
- Operator can retry item that failed pre-send
- Operator can mark uncertain item as succeeded/failed
- Non-operator user cannot access admin routes

### Phase 9: Restricted sourcing (entitlement-gated)

**Build:**
- Source management (create, verify, pause)
- Collection job (browser automation)
- Candidate backlog
- Queue refill (manual and automatic)
- Deduplication enforcement
- Entitlement gate

**Exit criteria:**
- Entitled workspace can add source by account/hashtag
- Source verification shows pending → active status
- Collection job finds posts, stores candidates
- User can refill queue from backlog
- Same post cannot appear twice in queue
- Non-entitled workspace cannot see or access sourcing features
- Browser worker crash: collection retries, no duplicate candidates

### Phase 10: Managed cleanup (entitlement-gated)

**Build:**
- Cleanup rule definition
- Selection preview with current metrics
- Confirmation with frozen selection
- Execution (archive photos, delete Reels)
- Per-item result tracking
- Pause on uncertain outcome
- Protected posts

**Exit criteria:**
- User defines cleanup rule (age > 30d, reach < 100)
- Preview shows matching posts with metrics
- User confirms, cleanup runs
- Feed photo is archived, Reel is deleted
- Worker crash during cleanup: run pauses, notification sent
- Selection changes before execution: old confirmation invalid
- Protected post never selected

### Phase 11: Deletion and export

**Build:**
- Data export (user's full data as JSON)
- Deletion request flow
- Background deletion job
- Completion proof

**Exit criteria:**
- User requests export, receives download link
- Export includes all user data, media URLs
- User requests deletion, confirms
- Deletion job removes all PII, preserves completion reference
- Deleted user cannot log in
- Workspace owned solely by deleted user is deleted

---

## 9. Security and privacy

### 9.1 Identity and authentication

| Control | Implementation |
|---------|----------------|
| Authentication | Magic link email, 15-minute expiry, single-use token |
| Session | HTTP-only, Secure, SameSite=Lax cookie, 30-day expiry |
| Session revocation | DELETE FROM sessions WHERE user_id = ? |
| Admin elevation | Separate `is_admin` flag, checked per-request |

### 9.2 Authorization

| Resource | Rule |
|----------|------|
| Workspace data | `workspace_members` JOIN required on every query |
| Admin routes | `is_admin = true` AND separate admin session cookie |
| Billing actions | `role IN ('owner', 'admin')` |
| Destructive actions | `role = 'owner'` |
| Entitlement-gated features | `subscriptions.{feature}_enabled = true` OR `entitlement_overrides` |

### 9.3 Secrets management

| Secret | Storage | Access |
|--------|---------|--------|
| Instagram access tokens | `access_token_encrypted` BYTEA, AES-256-GCM, key in Fly.io secrets | Server-side only, never returned to client |
| Browser session state | `storage_state_encrypted` BYTEA, same encryption | Browser worker only |
| Stripe webhook secret | Fly.io secrets | Webhook handler only |
| Database URL | Fly.io secrets | All processes |
| R2 credentials | Fly.io secrets | Web and worker processes |

### 9.4 Workspace isolation

All database queries include `WHERE workspace_id = ?` from session context. No cross-workspace JOINs in user-facing code.

Media URLs are presigned with workspace_id in the path: `/{workspace_id}/{media_id}/original.mp4`. Presigning validates workspace membership.

SSE streams filter by `workspace_id`. Notification queries include workspace predicate.

### 9.5 Billing security

- Card details never touch our servers (Stripe Checkout hosted)
- Webhook signature verified using `stripe.webhooks.constructEvent`
- Webhook handler is idempotent (event ID uniqueness)
- Billing state changes audit logged with actor

### 9.6 Abuse controls

| Threat | Control |
|--------|---------|
| Signup spam | Invite-only during beta |
| Brute force magic link | Rate limit: 3 emails per hour per address, token single-use |
| API abuse | Rate limit: 100 req/min per user, 1000 req/min per workspace |
| Media storage abuse | Per-workspace storage quota (10 GB free, 50 GB paid) |
| Queue flooding | Per-account queue limit (100 items) |
| Analytics API abuse | Manual refresh rate: 1 per 5 min per account |
| Source collection abuse | Entitled workspaces only, max 10 sources per account |

### 9.7 Audit and logging

| Event | What's logged |
|-------|--------------|
| Authentication | user_id, IP, success/failure, timestamp |
| Workspace actions | actor, action, resource, workspace_id, timestamp |
| Publish | queue_item_id, account_id, result (no tokens) |
| Cleanup | run_id, items processed, actor |
| Admin actions | admin_user_id, action, target, justification |

Logs retain for 90 days, then aged to cold storage. Tokens, passwords, and PII excluded via allowlist logging.

### 9.8 Data retention

| Data type | Retention |
|-----------|-----------|
| Active user data | Until deletion request |
| Queue history | 2 years |
| Publish receipts | 2 years |
| Analytics snapshots | 2 years |
| Audit log | 2 years |
| Deleted user data | 30 days (deletion request pending), then purged |
| Logs | 90 days hot, 1 year cold |

### 9.9 Export and deletion

Export: User requests export, background job collects all user data, generates JSON, uploads to R2 with 7-day presigned URL, emails link.

Deletion: User requests deletion, enters 30-day cooling period, deletion job runs, removes all PII, revokes tokens, deletes media, records completion reference.

### 9.10 Support access

Operators access admin panel with elevated credentials. Admin panel shows:
- User/workspace identifiers (not emails without explicit lookup)
- Queue and item status
- Error messages and evidence
- Redacted tokens (last 4 characters for identification)

Operators cannot see full tokens, passwords (none exist), or export user data without user-initiated export.

---

## 10. Risk register

| # | Risk | Early warning | Mitigation |
|---|------|---------------|------------|
| 1 | Instagram API deprecation or breaking change | API changelog, community reports, integration test failures | Instagram sandbox in CI; deprecation monitoring; graceful degradation path |
| 2 | Instagram rate limiting tightens | 429 errors in monitoring, customer reports of "stuck" publishes | Per-account rate limiting below Instagram's, queue-based backpressure, limit visibility in UI |
| 3 | Browser automation detected/blocked by Instagram | Increased CAPTCHA challenges, session invalidation rate | Stealth Playwright config, session rotation, operator alerting, fallback to manual session re-auth |
| 4 | Stripe webhook delivery failures | Webhook failure dashboard, subscription state drift | Webhook retry with exponential backoff, daily subscription state reconciliation job |
| 5 | R2 outage or data loss | R2 availability monitoring, failed upload rate | Daily backup verification, multi-region R2 option (future), user-facing upload retry |
| 6 | Fly.io regional outage | Health check failures, latency spikes | Multi-region deployment config (activatable), database replica in second region |
| 7 | Encryption key compromise | Anomalous token decryption patterns, external breach notification | Key rotation procedure documented, encrypted backup of rotation state |
| 8 | Operator account compromise | Unusual admin activity patterns, login from new location | Admin MFA required, admin action audit log, anomaly alerting |
| 9 | Source content generates legal complaints | DMCA notices, user reports | Immediate takedown procedure, source attribution for defense, entitled-only access |
| 10 | Solo founder unavailable | Service degradation without intervention | Runbook documentation, automated recovery for common failures, dead man's switch alerting |

---

## 11. Explicit tradeoffs

| Requirement | What v1 delivers | Why acceptable |
|-------------|------------------|----------------|
| "Status changes feel live without refresh" | SSE polling every 2 seconds, not true push | WebSocket complexity not justified at launch scale; 2s latency acceptable for queue status |
| "Optimal posting times" | User-defined rules only, no ML optimization | Optimization requires engagement data baseline; v2 feature after 3+ months of data |
| Multi-region deployment | Single Fly.io region (US East) | Budget is "few hundred $/month"; multi-region adds ~$100/month; latency from other regions acceptable for v1 |
| Automatic cleanup resume after uncertain | Manual reconciliation required | Destructive actions warrant human verification; automatic resume could delete posts erroneously |
| Browser session reuse across accounts | One session per account (not pooled) | Session pooling risks cross-account data leakage; explicit isolation preferred |
| Analytics completeness | Insights API data only, may be delayed | Instagram controls available metrics; product cannot promise data Instagram doesn't provide |

---

## 12. Where this is stronger than required

| Enhancement | Value | Cost |
|-------------|-------|------|
| Historical analytics snapshots | Users can see trends, not just current values; debugging "metrics dropped" complaints | ~5 KB per receipt per snapshot; at 1000 receipts × daily snapshots = 5 MB/month |
| Frozen content hash in receipts | Provable evidence that published content matched user approval; dispute resolution | SHA-256 of caption + media per receipt; trivial storage |
| Explicit `needs_review` status | Distinguishes "definitely failed" from "maybe succeeded"; prevents accidental republish | One additional enum value; no runtime cost |
| Per-item cleanup tracking | User sees exactly what happened to each post; partial completion visible | One row per item per run; ~100 bytes per row |
| Presigned URL expiry (1 hour) | Leaked URLs are time-limited; reduces exposure from accidental sharing | No cost; defense in depth |
| Content rights versioning | Audit trail if terms change; user's acceptance tied to version they saw | One table, one FK; legal protection |

---

## 13. Assumptions

| # | Assumption | Where made | Fallback if wrong |
|---|------------|------------|-------------------|
| 1 | Instagram Graph API supports professional account publishing with content container flow | §1.8, §4 | If deprecated, browser automation for all publishing (major architecture change) |
| 2 | R2 S3 compatibility includes all required operations (presigned PUT/GET, multipart) | §1.5, §3 | Switch to Backblaze B2 or AWS S3 |
| 3 | Fly.io PostgreSQL supports `pgcrypto` and custom extensions | §3 | Use Neon or Supabase managed Postgres |
| 4 | pg-boss scales to thousands of jobs/day without issue | §1.4 | Migrate to dedicated job queue (BullMQ + Redis) |
| 5 | Launch region is US East; latency from other regions (200-400ms) is acceptable | §11 | Add region when international users appear |
| 6 | FFmpeg can run in Fly.io worker containers (CPU-based transcoding acceptable) | §1.6 | Add GPU workers or external transcoding service |
| 7 | Instagram sandbox accounts are sufficient for testing | §7.4 | Create dedicated test account with app-in-development permissions |
| 8 | 25 publishes/day/account is Instagram's current limit | §3.6, §4 | Make limit configurable per account; monitor for changes |
| 9 | Stripe webhooks are reliable enough without a backup reconciliation | §5.11 | Add daily subscription state reconciliation job |
| 10 | Solo founder can respond to incidents within 4 hours during business hours | §10 | Hire part-time SRE or implement more aggressive auto-recovery |
| 11 | Browser automation is acceptable under Instagram's Terms of Service for restricted feature | §1.7, §3.5, §10 | Remove restricted sourcing capability entirely |
| 12 | Magic link email delivery is reliable enough for primary auth | §1.9 | Add backup auth method (passkey, OAuth) |
| 13 | 10 GB storage per free workspace, 50 GB per paid workspace is sufficient | §9.6 | Adjust quotas based on actual usage patterns |
| 14 | Users accept that Reel cleanup cannot be automatically undone | §3.8 | Add stronger confirmation or remove Reel cleanup |
| 15 | SSE reconnection handling is sufficient for status updates | §1.3 | Implement WebSocket if reconnection issues arise |
