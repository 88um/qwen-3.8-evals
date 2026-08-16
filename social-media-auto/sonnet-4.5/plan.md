# Engineering Plan: ToolBox Poster

A queue-first Instagram operations studio for creators, theme-page operators, and small agencies. This plan covers the complete system from workspace creation through publishing, analytics, and managed cleanup.

---

## 1. Technology decisions

### 1.1 Database

**Choice:** PostgreSQL 16.

**Rejected:** MongoDB. Would eliminate schema migrations and provide flexible document storage for analytics snapshots and heterogeneous source metadata. Rejected because the product's safety guarantees depend on transactional updates, conditional writes under concurrency (`SELECT ... FOR UPDATE SKIP LOCKED`), partial unique indexes for deduplication, check constraints for state transitions, and foreign key enforcement—none of which MongoDB provides reliably.

**Why:** Every core invariant—workspace isolation, exactly-once publishing, queue order integrity, idempotent billing, frozen action inputs—is enforced by PostgreSQL's ACID transactions, row-level locks, and declarative constraints. The §4 invariant map would collapse without them.

### 1.2 Application runtime

**Choice:** Node.js 22 LTS with TypeScript 5.6.

**Rejected:** Python with FastAPI. Simpler for the restricted sourcing browser automation (Playwright has better Python ergonomics), and the operator may be more familiar with it. Rejected because JavaScript is the only runtime where the same validation, business logic, and type definitions can run identically in the browser client, the API server, and background workers—eliminating an entire class of client/server contract bugs.

**Why:** Queue drag-and-drop, live status updates, media upload progress, and client-side preparation previews require non-trivial browser logic. Sharing types and validation between client and server reduces testing surface and prevents "works in dev, fails in prod" mismatches. The Instagram Graph API client, media processing, and Playwright browser sessions all have mature Node.js libraries.

### 1.3 Web framework

**Choice:** Next.js 15 (App Router) with React Server Components.

**Rejected:** SvelteKit. Lighter client bundle, simpler reactivity model, and no hydration mismatches. Rejected because workspace-scoped real-time updates (queue reordering, status changes, notifications) require granular subscriptions per user and account, and the React ecosystem's mature libraries for WebSocket subscriptions, optimistic updates, and infinite-scroll analytics are better tested at this concurrency than Svelte's.

**Why:** Queue management, library browsing, and analytics dashboards are data-heavy interactive surfaces. RSCs fetch initial workspace state server-side (reducing client waterfalls and preserving workspace isolation), while client components handle live updates and drag-and-drop. Next.js API routes and middleware colocate authn/authz with the page boundaries they protect.

### 1.4 Real-time updates

**Choice:** Server-Sent Events (SSE) over HTTP/2.

**Rejected:** WebSockets via Socket.io. Bidirectional and widely deployed. Rejected because every client message in this product is already an HTTP API call (queue reorder, publish now, disconnect account)—there is no client-initiated event that isn't a request/response pair. SSE is simpler to secure (reuses HTTP authn/authz middleware), auto-reconnects, and doesn't require sticky sessions or a separate WebSocket process.

**Why:** The real-time contract is server → client only: status updates, progress, notifications. SSE reconnects with `Last-Event-ID`, so a dropped connection resumes from the last delivered event. Connection count equals active browser tabs (~200 WAU × 2 tabs = 400 connections), well below Node's per-process limit. EventSource is natively supported; no client library needed.

### 1.5 Background job processing

**Choice:** Graphile Worker with PostgreSQL as the queue.

**Rejected:** BullMQ with Redis. More mature, better dashboard, and simpler to scale horizontally. Rejected because adding Redis introduces a second distributed system with its own failure modes (split-brain, persistence lag, connection pooling). The product's safety model already depends on PostgreSQL transactions for lease acquisition, attempt deduplication, and frozen action inputs—using Postgres for jobs keeps all safety-critical state in one transactional boundary.

**Why:** Graphile Worker uses `SELECT ... FOR UPDATE SKIP LOCKED` for lease acquisition, preventing duplicate work. A job's input, attempts, and outcome live in the same database as the queue item or cleanup rule it references, so a single transaction can atomically update both. It supports job priority (publish > analytics refresh > cleanup), concurrency limits per task type (browser sessions capped at 4, media preparation at 8, ordinary publishing at 16), and cron scheduling for recurring analytics and scheduled cleanup.

### 1.6 Media storage and processing

**Choice:** Cloudflare R2 for storage; `sharp` for image transforms; `ffmpeg` via `fluent-ffmpeg` for video.

**Rejected:** AWS S3 + MediaConvert. Would offload video transcoding to a managed service. Rejected because MediaConvert minimum cost is ~$0.02/minute and 90% of uploads are <30s Reels—paying $0.02 to transcode a 15s clip is 4× the budget. Running `ffmpeg` in-process on a worker adds ~200ms/video at launch scale and costs only compute time.

**Why:** R2 is S3-compatible, has zero egress fees (Instagram downloads prepared media from signed URLs), and costs $0.015/GB-month. Original and prepared versions of 10,000 videos (~500 GB) cost $7.50/month. Signed URLs expire after 1 hour—long enough for Instagram's publish callback to fetch the file, short enough to prevent long-lived leaks. `sharp` handles aspect-ratio fitting, metadata stripping, and logo overlays in <50ms per image.

### 1.7 Instagram API integration

**Choice:** Instagram Graph API via Meta for Developers OAuth and official client library.

**Rejected:** Third-party Instagram scraping or unofficial APIs. Would bypass OAuth and support personal accounts. Rejected because the product brief explicitly excludes personal accounts and Instagram passwords. Unofficial methods violate Instagram's ToS, put customer accounts at risk of suspension, and have no support channel when Instagram changes internals.

**Why:** Graph API provides professional account publishing, analytics, account connection status, permissions scopes, and rate-limit headers. OAuth keeps passwords out of our system. The official rate limits (25 posts/day per account default, higher for approved apps) are documented and stable.

### 1.8 Restricted sourcing browser automation

**Choice:** Playwright with persistent browser contexts, running in dedicated worker processes.

**Rejected:** Puppeteer. Lighter and more widely used. Rejected because Playwright's `browserContext.storageState()` persists cookies and localStorage to a JSON file, allowing a logged-in session to be checkpointed and resumed across restarts without re-authenticating. Puppeteer lacks this—context state is in-memory only, so every worker restart loses the session.

**Why:** Restricted sourcing is the highest-risk capability. Browser sessions must be isolated per workspace, capped at 4 concurrent contexts (memory limit), parked to disk when idle, rate-limited to avoid detection, and logged separately from ordinary publishing. Playwright's `route()` API blocks unnecessary asset loads (ads, trackers, analytics) to reduce memory and latency. Auto-wait and retry logic reduce flake.

### 1.9 Payment processing

**Choice:** Stripe Checkout and Customer Portal.

**Rejected:** Paddle. Single integration for both direct card and alternative payment methods; handles EU VAT and sales tax automatically. Rejected because Stripe's webhook signature verification, idempotency keys, and event versioning are better documented for handling duplicate/out-of-order events. The plan's safety model requires attributable, inspectable evidence for every charge and cancellation—Stripe's `BalanceTransaction` and `Invoice` objects provide that; Paddle's reconciliation requires their dashboard.

**Why:** Card details never touch our infrastructure. Checkout redirects to Stripe; Customer Portal handles plan changes and cancellations. Webhooks deliver `checkout.session.completed`, `invoice.paid`, `invoice.payment_failed`, `customer.subscription.updated`, and `customer.subscription.deleted`. Each event has a unique ID; our handler is idempotent via `UNIQUE(stripe_event_id)`. Stripe's test mode supports full end-to-end checkout flows.

### 1.10 Deployment platform

**Choice:** Render.com with a web service, 3 background workers, and managed PostgreSQL.

**Rejected:** Railway or Fly.io. Simpler, cheaper for this scale, and faster cold starts. Rejected because Render provides managed daily backups with one-click restore, preview environments per pull request, and zero-config SSE/WebSocket support (no custom nginx). Testing backup restoration is a launch requirement—Render's UI makes "restore yesterday's backup to a new database" a 2-minute drill, not a 30-minute psql exercise.

**Why:** Web service autoscales from 1–3 instances based on CPU (>70% for 2 minutes). Worker processes are independent: `worker-publish` (16 concurrency), `worker-media` (8 concurrency, higher memory), `worker-browser` (4 concurrency, 4 GB RAM), and `worker-cron` (1 instance, schedules recurring jobs). Postgres plan includes connection pooling (PgBouncer), automated failover, and point-in-time recovery. Estimated cost at launch: web $25/mo, workers $75/mo, database $50/mo, R2 $10/mo = $160/mo baseline.

### 1.11 Email delivery

**Choice:** Postmark transactional email API.

**Rejected:** SendGrid. More features, larger free tier. Rejected because Postmark's delivery tracking, bounce categorization (hard bounce vs. spam complaint vs. soft bounce), and suppression list management are simpler for a single operator. SendGrid's dashboard is built for marketing teams with segmentation and A/B testing—irrelevant here.

**Why:** Notification emails (connection lost, publish failed, billing issue) are transactional, time-sensitive, and must not be suppressed by marketing unsubscribe preferences. Postmark's templates support variable substitution and preview rendering. Webhooks notify us of bounces and spam complaints so we can mark email addresses unreachable. 10,000 emails/month free; $1.25 per 1,000 after—enough for 200 WAU with ~3 emails/week/user.

### 1.12 Frontend styling

**Choice:** Tailwind CSS 4 with a custom design system.

**Rejected:** Plain CSS modules or a component library (shadcn/ui, Chakra). Would provide pre-built accessible components (modal, dropdown, toast). Rejected because the product brief requires "accessibility, keyboard operation, and clear empty, loading, success, blocked, and error states" as first-class—not skinned defaults. A custom system lets us design queue drag-and-drop affordances, progress states, and notification urgency levels that don't read as "template dashboard."

**Why:** Tailwind's utility classes keep styles colocated with components. Custom theme tokens for color, spacing, and typography are defined once and referenced everywhere. Dark mode uses `class` strategy so the user's theme preference persists. JIT compilation keeps the production CSS bundle <20 KB. The drag-and-drop library (`@dnd-kit/core`) and the real-time update subscription logic are design-system-agnostic.

---

## 2. System architecture

### 2.1 Independently running processes

| Process | Responsibility | Scaling |
|---------|---------------|---------|
| **web** | HTTP API, page rendering, SSE connections, session management | 1–3 instances (autoscale on CPU >70%) |
| **worker-publish** | Queue publication tasks: ordinary Instagram posts, receipt creation, analytics refresh | 1 instance, 16 job concurrency |
| **worker-media** | Media upload, preparation, format conversion, original archival | 1 instance, 8 job concurrency, 2 GB RAM |
| **worker-browser** | Restricted source collection, cleanup execution, browser session management | 1 instance, 4 job concurrency, 4 GB RAM |
| **worker-cron** | Schedules recurring jobs: analytics refresh, scheduled cleanup, queue auto-fill, session health checks | 1 instance, 1 concurrency |

All processes connect to the same PostgreSQL instance. `web` and workers share the same codebase and environment configuration; the process role is selected via `PROCESS_TYPE` env var.

### 2.2 How work moves through the system

#### 2.2.1 Customer page request → response

1. User's browser sends authenticated request to `web` process.
2. Next.js middleware checks session cookie, loads workspace memberships, verifies route authorization.
3. Server Component fetches workspace-scoped data (accounts, queue items, notifications) via direct Postgres query with `WHERE workspace_id = $1`.
4. Page renders with initial data; client component subscribes to SSE endpoint `/api/sse?workspace=${id}`.
5. SSE handler registers subscription in-memory map: `Map<workspace_id, Set<Response>>`.
6. Background jobs that complete or update workspace-visible state call `notifyWorkspace(workspace_id, event)`, which writes to all subscribed SSE streams.

#### 2.2.2 Upload → prepare → queue → publish

1. Client uploads media to `/api/media/upload` with `workspace_id`, `account_id`, `caption`, `rights_accepted`.
2. `web` validates workspace access, writes `media_uploads` row with `status='uploading'`, returns signed R2 upload URL.
3. Client streams file to R2, then calls `/api/media/complete` with `upload_id`.
4. Handler writes `media_uploads.status='uploaded'`, enqueues `prepare-media` job with `{upload_id}`.
5. `worker-media` picks up job, loads original from R2, runs `sharp`/`ffmpeg`, writes prepared file to R2, updates `media_uploads.prepared_url` and `status='ready'`.
6. Client calls `/api/queue/add` with `{upload_id, account_id, caption}`.
7. Handler loads upload, checks `prepared_url` exists, inserts `queue_items` row with frozen `prepared_url`, `caption`, `account_id`, `settings_snapshot`, and `position = MAX(position) + 1 FOR account_id`.
8. SSE pushes queue update to workspace subscribers.
9. Cron job `schedule-due-posts` runs every 60 seconds, finds `queue_items` with `status='ready'` and `scheduled_at <= NOW()`, enqueues `publish-post` job per item.
10. `worker-publish` picks up job, calls Instagram Graph API, writes `published_posts` receipt, updates `queue_items.status='published'`, SSE notifies.

#### 2.2.3 Restricted source collection → backlog → queue

1. Operator grants workspace `feature:restricted_sources` entitlement.
2. User creates `source_configs` row with `{account_id, source_type, filters, schedule}`.
3. Cron job `collect-sources` runs per config schedule, enqueues `collect-source` job.
4. `worker-browser` picks up job, loads persistent browser context from R2 via `storageState()`, navigates to Instagram, scrapes eligible posts, writes `source_items` rows with `status='pending'`.
5. User views backlog, selects items, calls `/api/backlog/approve`.
6. Handler updates `source_items.status='approved'`, enqueues `download-source-media` job.
7. `worker-media` downloads media from Instagram, uploads to R2, prepares it, writes `media_uploads` row.
8. User calls `/api/queue/add-from-source` with `{source_item_id, account_id}`.
9. Handler checks `source_item_id` not already in `queue_items` for this `account_id` (partial unique index), inserts queue row, SSE notifies.

#### 2.2.4 Cleanup execution

1. User creates `cleanup_rules` row, calls `/api/cleanup/preview`.
2. Handler loads library posts, fetches latest analytics, filters by rule, returns preview set.
3. User reviews, calls `/api/cleanup/execute` with `{rule_id, confirmed_post_ids, confirmation_hash}`.
4. Handler recomputes selection, verifies hash matches, enqueues `cleanup-run` job with frozen `{rule_snapshot, post_ids}`.
5. `worker-browser` picks up job, loads browser context, iterates `post_ids` one at a time.
6. For each post: `SELECT ... FOR UPDATE` acquires row lock, checks `status='published'` and `protected=false`, calls Instagram API to archive or delete, writes `cleanup_events` row, updates post `status='archived'` or `'deleted'`, SSE notifies.
7. If browser hangs or worker crashes, transaction rolls back—lock releases, job retries with same frozen inputs, skips already-processed posts.

### 2.3 Isolation and starvation prevention

- **Workspace isolation:** Every query and job includes `workspace_id` in WHERE clause. RLS policies enforce it at database level (§9).
- **Account-level concurrency:** Partial unique index ensures only one `queue_items` row per account can be `status='publishing'`. Cleanup execution uses `SELECT ... FOR UPDATE` per post.
- **Process isolation:** Browser automation (4 concurrent contexts, high memory, risky) runs in `worker-browser`. Media preparation (CPU-bound) runs in `worker-media`. Ordinary publishing (I/O-bound, higher volume) runs in `worker-publish`. A stuck browser session cannot block Instagram publishes.
- **Job priority:** Graphile Worker supports `priority` integer (lower = higher priority). Publish jobs: priority 1. Analytics refresh: priority 5. Cleanup: priority 3. Prevents analytics backlog from delaying customer-visible publishes.
- **Rate limiting:** Instagram API calls are throttled via `accounts.last_api_call_at` timestamp and per-endpoint `min_interval_ms`. Worker queries `SELECT ... WHERE last_api_call_at < NOW() - INTERVAL '...' FOR UPDATE SKIP LOCKED`, so slow accounts don't block others.

---

## 3. Data model

```sql
-- Users and identity
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    email_verified BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ,
    suspended BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX users_email_lookup ON users (email) WHERE NOT suspended;

-- Workspaces own accounts, queue, billing, settings
CREATE TABLE workspaces (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    suspended BOOLEAN NOT NULL DEFAULT false,
    plan TEXT NOT NULL DEFAULT 'free' CHECK (plan IN ('free','starter','pro','studio')),
    subscription_status TEXT CHECK (subscription_status IN ('active','past_due','canceled','trialing'))
);

-- Workspace membership and roles
CREATE TABLE workspace_members (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('owner','admin','editor','viewer')),
    joined_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX workspace_members_user ON workspace_members (user_id);

-- Feature entitlements (e.g., beta access to restricted sources, cleanup)
CREATE TABLE workspace_entitlements (
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    feature TEXT NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_by UUID REFERENCES users(id),
    expires_at TIMESTAMPTZ,
    PRIMARY KEY (workspace_id, feature)
);

-- Connected Instagram accounts
CREATE TABLE accounts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    instagram_account_id TEXT NOT NULL UNIQUE, -- Instagram's account ID
    username TEXT NOT NULL,
    profile_image_url TEXT,
    access_token_encrypted TEXT NOT NULL, -- encrypted Instagram access token
    token_expires_at TIMESTAMPTZ,
    connected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    connection_status TEXT NOT NULL DEFAULT 'active' CHECK (connection_status IN ('active','expired','revoked','error')),
    paused BOOLEAN NOT NULL DEFAULT false,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    daily_post_limit INTEGER NOT NULL DEFAULT 25,
    posts_today INTEGER NOT NULL DEFAULT 0,
    posts_reset_at DATE NOT NULL DEFAULT CURRENT_DATE,
    last_api_call_at TIMESTAMPTZ,
    settings JSONB NOT NULL DEFAULT '{}'::JSONB -- preparation preferences, schedule rules
);
CREATE INDEX accounts_workspace ON accounts (workspace_id) WHERE NOT paused;
CREATE INDEX accounts_ig_id ON accounts (instagram_account_id);

-- Media uploads (originals + prepared versions)
CREATE TABLE media_uploads (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    uploaded_by UUID NOT NULL REFERENCES users(id),
    original_filename TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    original_url TEXT NOT NULL, -- R2 key
    prepared_url TEXT, -- R2 key for publish-ready version
    media_type TEXT NOT NULL CHECK (media_type IN ('image','video')),
    width INTEGER,
    height INTEGER,
    duration_seconds NUMERIC(10,2),
    status TEXT NOT NULL DEFAULT 'uploading' CHECK (status IN ('uploading','uploaded','preparing','ready','failed')),
    error_message TEXT,
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    prepared_at TIMESTAMPTZ
);
CREATE INDEX media_workspace ON media_uploads (workspace_id, uploaded_at DESC);

-- Queue items (what will be published, in order)
CREATE TABLE queue_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    media_upload_id UUID NOT NULL REFERENCES media_uploads(id),
    caption TEXT NOT NULL,
    position INTEGER NOT NULL, -- queue order within account
    status TEXT NOT NULL DEFAULT 'ready' CHECK (status IN ('ready','publishing','published','failed','needs_review','hidden','canceled')),
    scheduled_at TIMESTAMPTZ, -- when this item becomes eligible
    settings_snapshot JSONB NOT NULL, -- frozen preparation settings at queue time
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NOT NULL REFERENCES users(id),
    published_at TIMESTAMPTZ,
    error_message TEXT,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    last_attempt_at TIMESTAMPTZ,
    UNIQUE (workspace_id, account_id, position)
);
CREATE INDEX queue_account_order ON queue_items (account_id, position) WHERE status IN ('ready','publishing');
CREATE UNIQUE INDEX queue_one_publishing_per_account ON queue_items (account_id) WHERE status = 'publishing';

-- Published posts (receipts)
CREATE TABLE published_posts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    queue_item_id UUID REFERENCES queue_items(id),
    source_item_id UUID, -- references source_items if from restricted source
    instagram_post_id TEXT NOT NULL UNIQUE,
    permalink TEXT NOT NULL,
    caption TEXT NOT NULL,
    media_type TEXT NOT NULL,
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    receipt_data JSONB NOT NULL, -- frozen evidence: media URLs, account username, IG response
    protected BOOLEAN NOT NULL DEFAULT false, -- prevent cleanup
    cleanup_status TEXT CHECK (cleanup_status IN ('published','archived','deleted'))
);
CREATE INDEX published_workspace ON published_posts (workspace_id, published_at DESC);
CREATE INDEX published_account ON published_posts (account_id, published_at DESC);

-- Analytics snapshots (versioned history, not overwrites)
CREATE TABLE analytics_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    published_post_id UUID NOT NULL REFERENCES published_posts(id) ON DELETE CASCADE,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    reach INTEGER,
    impressions INTEGER,
    likes INTEGER,
    comments INTEGER,
    saves INTEGER,
    shares INTEGER,
    plays INTEGER, -- for Reels
    raw_data JSONB NOT NULL -- full Instagram Insights response
);
CREATE INDEX analytics_post_time ON analytics_snapshots (published_post_id, fetched_at DESC);

-- Restricted source configs (operator-entitled feature)
CREATE TABLE source_configs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE, -- which account will queue these
    source_type TEXT NOT NULL CHECK (source_type IN ('account','hashtag','reels_feed')),
    source_identifier TEXT NOT NULL, -- @username, #hashtag, or 'explore'
    filters JSONB NOT NULL DEFAULT '{}'::JSONB, -- min_likes, exclude_words, media_type, max_age_days
    collection_schedule TEXT, -- cron expression
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','active','paused','failed','blocked')),
    last_collected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX source_workspace ON source_configs (workspace_id);

-- Collected source items (backlog before approval)
CREATE TABLE source_items (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    source_config_id UUID NOT NULL REFERENCES source_configs(id) ON DELETE CASCADE,
    instagram_post_id TEXT NOT NULL,
    author_username TEXT NOT NULL,
    original_caption TEXT,
    permalink TEXT NOT NULL,
    media_type TEXT NOT NULL,
    media_url TEXT NOT NULL,
    thumbnail_url TEXT,
    likes INTEGER NOT NULL DEFAULT 0,
    comments INTEGER NOT NULL DEFAULT 0,
    plays INTEGER,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected','queued')),
    UNIQUE (workspace_id, source_config_id, instagram_post_id) -- no duplicate imports
);
CREATE INDEX source_items_workspace_status ON source_items (workspace_id, source_config_id, status, discovered_at DESC);

-- Cleanup rules
CREATE TABLE cleanup_rules (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    filters JSONB NOT NULL, -- {media_type, min_age_days, max_reach, max_likes, ...}
    schedule TEXT, -- cron expression for recurring cleanup
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_by UUID NOT NULL REFERENCES users(id)
);

-- Cleanup execution history
CREATE TABLE cleanup_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    account_id UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    cleanup_rule_id UUID REFERENCES cleanup_rules(id),
    rule_snapshot JSONB NOT NULL, -- frozen rule at execution time
    post_ids UUID[] NOT NULL, -- frozen selection
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_by UUID REFERENCES users(id),
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','stopped','failed','needs_review')),
    completed_at TIMESTAMPTZ
);

-- Cleanup events (per-post results)
CREATE TABLE cleanup_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    cleanup_run_id UUID NOT NULL REFERENCES cleanup_runs(id) ON DELETE CASCADE,
    published_post_id UUID NOT NULL REFERENCES published_posts(id),
    action TEXT NOT NULL CHECK (action IN ('archive','delete')),
    outcome TEXT NOT NULL CHECK (outcome IN ('success','failed','uncertain')),
    evidence JSONB NOT NULL, -- redacted API response, no session tokens
    executed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Billing and subscriptions
CREATE TABLE subscriptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE UNIQUE,
    stripe_customer_id TEXT NOT NULL UNIQUE,
    stripe_subscription_id TEXT UNIQUE,
    plan TEXT NOT NULL,
    status TEXT NOT NULL,
    current_period_end TIMESTAMPTZ,
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Stripe webhook events (idempotency)
CREATE TABLE stripe_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    payload JSONB NOT NULL
);

-- Invitations
CREATE TABLE invitations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    email TEXT NOT NULL,
    role TEXT NOT NULL,
    token TEXT NOT NULL UNIQUE,
    created_by UUID REFERENCES users(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    accepted_at TIMESTAMPTZ,
    revoked BOOLEAN NOT NULL DEFAULT false
);
CREATE INDEX invitations_token ON invitations (token) WHERE NOT revoked AND accepted_at IS NULL;

-- Notifications
CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id UUID REFERENCES users(id), -- null = all workspace members
    type TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'normal' CHECK (priority IN ('low','normal','high')),
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    link TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    read_at TIMESTAMPTZ,
    dismissed_at TIMESTAMPTZ
);
CREATE INDEX notifications_workspace_user ON notifications (workspace_id, user_id, created_at DESC) WHERE read_at IS NULL;

-- Feedback
CREATE TABLE feedback (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID REFERENCES users(id),
    workspace_id UUID REFERENCES workspaces(id),
    category TEXT NOT NULL CHECK (category IN ('bug','confusing','idea','praise','other')),
    message TEXT NOT NULL,
    page_url TEXT,
    user_agent TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Audit log (privileged actions)
CREATE TABLE audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id UUID REFERENCES users(id),
    actor_role TEXT NOT NULL, -- 'user', 'operator', 'system'
    workspace_id UUID REFERENCES workspaces(id),
    action TEXT NOT NULL,
    resource_type TEXT,
    resource_id UUID,
    details JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX audit_workspace ON audit_log (workspace_id, created_at DESC);
CREATE INDEX audit_actor ON audit_log (actor_id, created_at DESC);

-- Sessions
CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX sessions_user ON sessions (user_id, expires_at DESC) WHERE expires_at > NOW();
```

**Row-Level Security (RLS) policies:**

```sql
ALTER TABLE workspaces ENABLE ROW LEVEL SECURITY;
CREATE POLICY workspace_member_access ON workspaces
    FOR ALL TO authenticated_user
    USING (id IN (SELECT workspace_id FROM workspace_members WHERE user_id = current_user_id()));

ALTER TABLE queue_items ENABLE ROW LEVEL SECURITY;
CREATE POLICY queue_workspace_isolation ON queue_items
    FOR ALL TO authenticated_user
    USING (workspace_id IN (SELECT workspace_id FROM workspace_members WHERE user_id = current_user_id()));

-- Similar RLS policies for: accounts, media_uploads, published_posts, source_items, cleanup_runs, notifications
```

---

## 4. Invariant enforcement map

| Invariant | Mechanism | Evidence it works |
|-----------|-----------|-------------------|
| **It publishes only what the customer intended** | `queue_items.settings_snapshot` JSONB freezes preparation settings at queue time; `published_posts.receipt_data` includes frozen caption, media URL, account ID | Integration test: update account settings, queue item A, update settings again, queue item B, publish both—assert A's receipt uses old settings, B's uses new |
| **The queue tells the truth** | `queue_items.status` transitions enforced by `CHECK` constraint; SSE push on every status update; `scheduled_at` compared against `accounts.timezone` | UI test: queue item, pause account, verify item stays `status='ready'` and SSE shows "paused"; resume, verify publish proceeds |
| **Uncertainty never becomes a second destructive action** | Instagram publish timeout after send → `status='needs_review'`; retry blocked by `WHERE status NOT IN ('publishing','needs_review')`; reconciliation clears to `published` or `failed` | Chaos test: inject 15s delay in publish job after API call, kill worker—assert item enters `needs_review`, manual retry is blocked, operator reconciliation flow clears it |
| **Holding never destroys work** | Account disconnect sets `connection_status='expired'` but does not `DELETE` queue items; suspension sets `workspaces.suspended=true`, blocking new work but preserving rows | Test: disconnect account via OAuth revoke, verify `queue_items` rows remain, `status='ready'` unchanged, publish jobs skip account until reconnect |
| **Private access stays private** | `accounts.access_token_encrypted` uses `pgcrypto`; RLS policies enforce `workspace_id` on all queries; `cleanup_events.evidence` redacts session cookies via `sanitizeEvidence()` | Test: create 2 workspaces, attempt to read workspace A's queue as workspace B user—query returns 0 rows; inspect cleanup event JSON, assert no `Cookie` header |
| **Only one publish per queue item** | Partial unique index `queue_one_publishing_per_account` + `SELECT ... FOR UPDATE` in publish job; job idempotency via `queue_item_id` (Graphile Worker deduplicates by payload hash) | Concurrency test: enqueue same publish job 10× in parallel—assert exactly 1 job runs, 9 are skipped as duplicates; simulate double-click → same |
| **Only one cleanup item active per account** | `cleanup_runs` row with `status='running'` blocks new runs via `SELECT ... WHERE account_id = $1 AND status = 'running'`; per-post `SELECT ... FOR UPDATE` in loop | Test: start cleanup run, attempt second run for same account—assert second run is rejected; within a run, iterate 2 posts in parallel—assert serialized by lock |
| **Known pre-action failures retry; uncertain outcomes do not** | Job `attempt_count < 5` for retriable errors (network timeout before send); `status='needs_review'` for post-send uncertainty; reconciliation clears to terminal state | Test: simulate pre-send network error → assert job retries up to 5×; simulate post-send timeout → assert enters `needs_review`, no auto-retry |
| **Frozen action inputs** | `cleanup_runs.rule_snapshot` and `.post_ids` frozen at execution time; preview computes hash of selection; execute recomputes and verifies hash match | Test: preview cleanup → edit rule → execute with old `confirmation_hash` → assert rejected; same flow with unchanged rule → assert succeeds |
| **Daily publishing use not forgotten by restart** | `accounts.posts_today` incremented in same transaction as `published_posts` insert; reset via daily cron that sets `posts_today=0 WHERE posts_reset_at < CURRENT_DATE` | Test: publish to daily limit → restart all workers → attempt publish → assert blocked; simulate date change (UPDATE posts_reset_at) → assert unblocked |
| **Source and upload deduplication** | Partial unique index `source_items(workspace_id, source_config_id, instagram_post_id)` prevents duplicate imports; same for queue via check before insert | Test: collect source twice concurrently → assert 1 row inserted, 1 violates unique constraint; approve same source_item for 2 accounts → assert 2 queue rows |
| **Restricted features unreachable without entitlement** | `/api/sources/*` routes check `hasEntitlement(workspace_id, 'restricted_sources')`; UI hides feature; database-level check on `source_configs` insert | Test: remove entitlement, attempt API call → assert 403; attempt INSERT via psql → assert app-level reject (no DB constraint—operator can override) |
| **Customer media is private** | R2 signed URLs expire after 3600s; `media_uploads.original_url` and `.prepared_url` are keys, not public URLs; signed on-demand via `getSignedUrl(key, expiresIn)` | Test: fetch signed URL, wait 3601s, attempt download → assert 403; capture prepared URL from publish job, attempt access 2h later → assert expired |
| **Private grants never logged** | `cleanup_events.evidence` runs through `sanitizeEvidence()` which deletes `Authorization`, `Cookie`, `Set-Cookie` headers; not returned to browser | Test: inspect `cleanup_events.evidence` JSON → assert no `Cookie` field; trigger error, inspect application logs → assert access token redacted |
| **Content-rights acceptance is attributable** | `queue_items.created_by` references user who queued; `audit_log` records rights acceptance version and timestamp on first upload | Test: upload media, queue item → query audit_log → assert row with `action='accept_rights'`, user_id, workspace_id, version |
| **Callbacks are idempotent** | `stripe_events.stripe_event_id` UNIQUE constraint; webhook handler wrapped in `ON CONFLICT (stripe_event_id) DO NOTHING` | Test: send same Stripe webhook twice → assert subscription updated once, second is no-op; send events out of order → assert final state correct |
| **Every claim has inspectable evidence** | `published_posts.receipt_data` includes IG response; `cleanup_events.evidence` includes action + outcome; `audit_log.details` for operator actions | Test: publish post → query receipt_data → assert contains `instagram_post_id`, `permalink`, `media_url`; cleanup → assert evidence includes IG API response code |

---

## 5. Failure-mode walkthrough

### 5.1 Crash before publish

**Scenario:** Worker crashes after acquiring a publish job but before calling Instagram API.

**What happens:**
1. Worker calls `SELECT ... FOR UPDATE SKIP LOCKED` on `queue_items WHERE status='ready'`, acquires row lock, begins transaction.
2. Updates `queue_items.status='publishing'`.
3. Worker process is killed (OOM, SIGKILL, host failure).
4. PostgreSQL connection drops; open transaction rolls back.
5. Row lock releases; `queue_items.status` reverts to `'ready'` (change was uncommitted).
6. Graphile Worker marks the job as failed (no heartbeat), re-enqueues it.
7. Next worker picks up the same job, sees `status='ready'`, proceeds with publish.

**Evidence:** `queue_items` shows `status='ready'`, `attempt_count=0`, `last_attempt_at=NULL`. Graphile Worker's `graphile_worker.jobs` table shows 1 failed attempt and 1 successful retry. No `published_posts` row exists.

### 5.2 Crash after publish may have been accepted

**Scenario:** Worker calls Instagram Graph API `POST /media`, receives network timeout, crashes before writing uncertainty state.

**What happens:**
1. Worker loads queue item, begins transaction, sets `status='publishing'`, commits.
2. Calls Instagram API `POST /{ig-user-id}/media` with media URL and caption.
3. Network hangs; after 30s, request times out (`ETIMEDOUT`).
4. Worker code checks: "Did we receive a response?" No. "Did the request leave our machine?" Yes (TCP SYN sent).
5. Updates `queue_items.status='needs_review'`, `error_message='Instagram API timeout after request sent—outcome uncertain'`, commits.
6. Worker crashes before job completes.
7. Job is re-enqueued, but next worker sees `status='needs_review'`, skips publish, marks job complete.
8. Operator uses "Reconcile uncertain outcome" tool: queries Instagram `/media` endpoint for recent posts, finds the post ID, creates receipt, updates status to `'published'`.

**Evidence:** `queue_items.status='needs_review'`, `attempt_count=1`, `error_message` contains "timeout after request sent". No `published_posts` row (unless reconciled). Instagram API may or may not have created the post—operator investigates via Graph API or Instagram app.

### 5.3 Duplicate publish work

**Scenario:** User double-clicks "Publish Now" button, or two cron ticks enqueue the same item concurrently.

**What happens:**
1. First request enqueues `publish-post` job with `{queue_item_id: 'abc123'}`.
2. Second request (same `queue_item_id`) enqueues duplicate job.
3. Graphile Worker computes job payload hash; jobs with identical payloads are deduplicated—only 1 job is queued.
4. (Alternative: both jobs enter queue.) Worker 1 picks up job, runs `SELECT ... FOR UPDATE WHERE id='abc123' AND status='ready'`.
5. Worker 1 acquires lock, updates `status='publishing'`, releases lock, calls Instagram.
6. Worker 2 picks up duplicate job, runs same query—WHERE clause matches 0 rows (`status` is now `'publishing'`), job exits as no-op.

**Evidence:** Graphile Worker's job log shows 1 executed job, 1 duplicate skipped. `queue_items` shows `status='published'`, `attempt_count=1`. Exactly 1 `published_posts` receipt exists.

### 5.4 Media preparation failure

**Scenario:** Uploaded video is corrupt; `ffmpeg` fails during transcode.

**What happens:**
1. Client uploads 50 MB video to R2 via signed URL, calls `/api/media/complete`.
2. `prepare-media` job is enqueued, picked up by `worker-media`.
3. Worker downloads video from R2, invokes `ffmpeg -i input.mp4 ...`.
4. `ffmpeg` exits with code 1, stderr: "Invalid data found when processing input".
5. Worker updates `media_uploads.status='failed'`, `error_message='Video file is corrupt or unsupported'`, commits.
6. SSE pushes update to client; UI shows red error state with "Preparation failed: Video file is corrupt."
7. User clicks "Retry" → `/api/media/retry` endpoint checks `status='failed'`, re-enqueues `prepare-media` job with `{upload_id}`.
8. (If user does not retry, job is not re-enqueued—failed preparation does not auto-retry.)

**Evidence:** `media_uploads.status='failed'`, `error_message` populated, `prepared_url=NULL`. No `queue_items` row exists (cannot queue unprepared media). Job log shows 1 failed attempt. If retried, new job appears with `attempt_count=0` (fresh job, not automatic retry).

### 5.5 Account revocation

**Scenario:** User revokes Instagram permissions via Instagram settings while 3 items are queued.

**What happens:**
1. Instagram sends deauthorization webhook to `/api/webhooks/instagram/deauth` with `{user_id: '...'}`.
2. Handler looks up `accounts WHERE instagram_account_id = user_id`, updates `connection_status='revoked'`, `paused=true`, commits.
3. SSE pushes account update to workspace subscribers; UI shows "Connection lost" banner.
4. Cron job `schedule-due-posts` skips accounts with `connection_status != 'active'`.
5. `queue_items` rows remain with `status='ready'`—not deleted, not failed.
6. User clicks "Reconnect account" → OAuth flow, new token stored, `connection_status='active'`, `paused=false`.
7. Next cron tick enqueues publish jobs for the 3 queued items.

**Evidence:** `accounts.connection_status='revoked'`, `paused=true`. `queue_items` shows 3 rows with `status='ready'`, `scheduled_at` in the past. After reconnect, `connection_status='active'`; within 60s, publish jobs run and items move to `'published'`.

### 5.6 Quota exhaustion

**Scenario:** Account reaches 25 posts/day limit; 5 more items are queued.

**What happens:**
1. 25th post publishes successfully. Worker increments `accounts.posts_today=25`.
2. Cron job `schedule-due-posts` runs, finds 5 ready items, but checks `posts_today >= daily_post_limit`.
3. Does not enqueue publish jobs for those 5 items.
4. Items remain `status='ready'`, `scheduled_at` in the past.
5. Midnight UTC arrives. Cron job `reset-daily-quotas` runs: `UPDATE accounts SET posts_today=0, posts_reset_at=CURRENT_DATE WHERE posts_reset_at < CURRENT_DATE`.
6. Next `schedule-due-posts` tick sees quota available, enqueues jobs for the 5 items.

**Evidence:** `accounts.posts_today=25`, `daily_post_limit=25`. `queue_items` shows 5 rows with `status='ready'`, `scheduled_at` yesterday. After midnight, `posts_today=0`, items publish. Notification created: "Daily limit reached for @username—5 items deferred to tomorrow."

### 5.7 Schedule edit near a slot

**Scenario:** Account has schedule "Mon/Wed/Fri at 10am PST". User edits to "Mon–Fri at 10am PST" at 9:58am PST on Tuesday.

**What happens:**
1. User saves new schedule at 9:58am. Handler updates `accounts.settings` JSONB with new rules, commits.
2. Cron job `schedule-due-posts` runs at 10:00am (runs every 60s, so catches this tick).
3. Loads schedule rules from `accounts.settings`, evaluates "next eligible slot" based on current time and timezone.
4. Tuesday 10am is now a valid slot (new rule includes Tue). Finds `queue_items` with `status='ready'` and no `scheduled_at` set, or `scheduled_at <= NOW()`.
5. Enqueues publish job for the next item in queue order.
6. (No duplicate: the 10am slot is evaluated once per cron tick. Changing schedule does not create a second 10am event.)

**Evidence:** `accounts.settings` shows new schedule. `queue_items` shows item moved to `status='publishing'` at 10:00am PST. Audit log: `action='update_schedule'`, `actor_id=<user>`, `created_at=9:58am`. Published post confirms 10am publish time.

### 5.8 Duplicate source collection

**Scenario:** Two concurrent "collect sources" jobs run for the same hashtag source.

**What happens:**
1. Cron schedules `collect-source` job for `source_config_id='src123'`.
2. Due to cron overlap or manual trigger, second job is enqueued for same `source_config_id`.
3. Worker 1 and Worker 2 both pick up jobs, load browser contexts, scrape Instagram `#fitness` hashtag.
4. Both retrieve the same 12 recent posts.
5. Worker 1 attempts `INSERT INTO source_items (workspace_id, source_config_id, instagram_post_id, ...) VALUES (..., 'src123', 'post-abc', ...)`.
6. Worker 2 attempts same insert 200ms later.
7. Partial unique constraint `UNIQUE (workspace_id, source_config_id, instagram_post_id)` causes Worker 2's insert to fail with `duplicate key value`.
8. Worker 2's transaction rolls back that row, continues with next post, skips duplicates.

**Evidence:** `source_items` contains exactly 12 rows, not 24. Job logs show both workers completed; Worker 2's log includes "skipped 12 duplicate items". `source_configs.last_collected_at` updated by the last worker to commit.

### 5.9 Browser hang during cleanup

**Scenario:** Instagram page hangs on "Are you sure?" modal; worker waits indefinitely.

**What happens:**
1. Cleanup job begins, loads browser context, iterates post IDs.
2. For post #3, Playwright navigates to Instagram, clicks "..." menu, clicks "Archive post".
3. Modal appears: "Are you sure?" Worker clicks "Confirm".
4. Instagram's JavaScript hangs (infinite loop, network stall, etc.).
5. Worker's per-operation timeout (30s) fires. Playwright throws `TimeoutError`.
6. Worker catches error, writes `cleanup_events` row with `outcome='failed'`, `evidence={error: 'Timeout waiting for confirmation'}`.
7. Does *not* update `published_posts.cleanup_status`—uncertain outcome.
8. Continues to post #4.
9. (Alternative: whole-job timeout of 5 minutes fires. Job is marked failed, transaction rolls back. Manual retry restarts from post #1, skips posts with existing `cleanup_events` rows.)

**Evidence:** `cleanup_events` shows post #3 with `outcome='failed'`. `published_posts` for post #3 still shows `cleanup_status=NULL`. `cleanup_runs.status='completed'` or `'failed'` depending on job-level timeout. Operator reviews, sees post #3 uncertain, manually checks Instagram app.

### 5.10 Changed cleanup selection

**Scenario:** User previews cleanup (selects 20 posts), leaves page open, analytics refresh changes metrics, user returns and clicks "Execute" with stale confirmation hash.

**What happens:**
1. User calls `/api/cleanup/preview` at 2pm, receives `{post_ids: [a,b,c,...,t], confirmation_hash: 'abc123'}`.
2. Analytics refresh runs at 2:30pm, updates `analytics_snapshots` for post `c`—reach increases above threshold.
3. User clicks "Execute" at 2:45pm, sends `{rule_id, post_ids: [a,b,c,...,t], confirmation_hash: 'abc123'}`.
4. Handler recomputes selection using current analytics: `SELECT ... WHERE reach < 100 ...`.
5. New selection is `[a,b,d,e,...,t]` (excludes `c`).
6. Computes fresh hash of new selection: `'def456'`.
7. Compares `'abc123' != 'def456'` → rejects request with 400 `{error: 'Selection changed. Preview again.'}`.

**Evidence:** No `cleanup_runs` row created. API response: 400 error. User sees error modal: "The list of posts has changed since you previewed. Please review the updated selection."

### 5.11 Repeated billing events

**Scenario:** Stripe sends `invoice.paid` webhook twice due to retry.

**What happens:**
1. First webhook arrives: `{id: 'evt_123', type: 'invoice.paid', data: {invoice_id: 'in_abc', ...}}`.
2. Handler begins transaction, inserts `stripe_events (stripe_event_id='evt_123', ...)`, updates `subscriptions` row, commits.
3. Handler returns 200 OK.
4. Stripe's webhook delivery times out (network issue on their side), marks delivery as failed, retries 1 hour later.
5. Second webhook arrives with same `stripe_event_id='evt_123'`.
6. Handler attempts `INSERT INTO stripe_events (stripe_event_id, ...) VALUES ('evt_123', ...)`.
7. Unique constraint violation → `ON CONFLICT (stripe_event_id) DO NOTHING`.
8. Query completes, transaction commits (no-op), handler returns 200 OK.

**Evidence:** `stripe_events` contains exactly 1 row with `stripe_event_id='evt_123'`. `subscriptions` row updated once. Webhook log shows 2 deliveries, both returned 200. No duplicate charge or subscription update.

### 5.12 Out-of-order billing events

**Scenario:** `customer.subscription.updated` (upgrade to Pro) arrives before `checkout.session.completed` (initial subscription creation).

**What happens:**
1. User completes Stripe Checkout at 3:00pm. Stripe fires `checkout.session.completed` and `customer.subscription.updated`.
2. Network routing causes `customer.subscription.updated` to arrive first at 3:00:05pm.
3. Handler checks: does `subscriptions` row exist for this workspace? No.
4. Inserts new row with data from `subscription` object in webhook payload, commits.
5. `checkout.session.completed` arrives at 3:00:08pm.
6. Handler checks: does row exist? Yes (inserted 3s ago).
7. Updates row with checkout session metadata (workspace association, initial plan), commits.
8. Final state: `subscriptions` row exists, plan='pro', status='active', correct `workspace_id`.

**Evidence:** `stripe_events` shows 2 rows, out-of-order timestamps. `subscriptions` shows 1 row, `plan='pro'`, `created_at=3:00:05pm`, `updated_at=3:00:08pm`. Workspace can access Pro features.

### 5.13 Storage failure

**Scenario:** R2 returns 503 Service Unavailable when worker tries to upload prepared media.

**What happens:**
1. Worker prepares media, calls `r2Client.putObject(key, buffer)`.
2. R2 returns 503 (temporary outage).
3. Worker catches error, checks `attempt_count < 5`, updates `media_uploads.status='preparing'`, `attempt_count += 1`, throws error to fail job.
4. Graphile Worker's retry policy re-enqueues job with exponential backoff (1min, 2min, 4min, 8min, 16min).
5. Retry #2 (1 min later): R2 still down, same flow.
6. Retry #3 (2 min later): R2 recovered, upload succeeds, `status='ready'`, `prepared_url` set.

**Evidence:** `media_uploads.attempt_count=3`, `status='ready'`, `prepared_url` populated. Job log shows 3 attempts: 2 failed (503), 1 success. User sees "Preparing..." spinner for 3 minutes, then "Ready".

### 5.14 Deletion interrupted halfway

**Scenario:** User requests account deletion; worker deletes media from R2, then crashes before deleting database rows.

**What happens:**
1. User calls `/api/account/delete`, confirms via email link.
2. Handler writes `audit_log` entry, enqueues `delete-account` job with `{user_id}`.
3. Worker begins job, starts transaction, loads all `media_uploads` for user's workspaces.
4. Iterates R2 keys, calls `r2Client.deleteObject()` for each. 50 files deleted.
5. Worker crashes (host reboot).
6. Transaction rolls back (no database rows deleted yet).
7. Job is re-enqueued, worker restarts, begins again.
8. Calls `r2Client.deleteObject()` for same 50 keys—R2 returns 200 OK even if key doesn't exist (idempotent).
9. Deletes database rows: `media_uploads`, `queue_items`, `published_posts`, `sessions`, `workspace_members` (if user is only member), finally `users`.
10. Commits transaction, marks job complete.

**Evidence:** User row deleted. `audit_log` shows `action='delete_account'`, `actor_id=<user>`, `details={reason: 'user_request'}`. R2 bucket empty for this workspace's keys. Job log shows 2 attempts (1 failed, 1 success). No dangling database rows reference deleted user.

---

## 6. AI strategy

**Decision:** AI does not belong in v1.

**Rationale:**

The product brief lists AI under "non-goals" unless a compelling, budgeted, safety-conscious case is made. No such case exists for v1:

- **Caption generation:** The product serves creators and operators who already have a voice, niche expertise, and brand tone. An AI-generated caption is more likely to dilute their voice than enhance it. Providing a blank caption field and letting customers write or paste their own is sufficient. If demand emerges, v2 can add optional AI suggestions as a pull (user clicks "Suggest"), not a push.

- **Content moderation:** Instagram already moderates published content. Our product's role is to queue and publish what the customer approved—not to second-guess their judgment. If a workspace violates Instagram's ToS, Instagram will act. If the operator needs to suspend a workspace for policy reasons, that is a manual decision, not an AI filter.

- **Source filtering:** Restricted source collection uses explicit numeric filters (min likes, max age) and keyword exclusion lists. These are deterministic, inspectable, and debuggable. An AI "relevance score" would add cost ($0.0001–$0.001 per item × 10,000 collected items/month = $1–$10/month), latency (LLM call per item), and unpredictability (why was this excluded?). The current design already filters 90% of noise; the remaining 10% is user judgment in the approval flow.

- **Analytics insights:** "Your posts with <theme> perform 20% better" is a v2+ feature. V1 provides raw metrics and sortable columns. If the customer cannot derive insights from their own data, adding an AI summarizer won't teach them—it will make them dependent on a black box. This is a product philosophy choice, not a technical limitation.

**Cost avoided:** Zero. No LLM API calls, no prompt engineering, no rate-limit handling, no hallucination mitigation, no "AI-generated" disclaimers, no additional testing surface.

**Tradeoff acknowledged:** Some competitors offer AI caption generation. Customers who want that feature will not find it here in v1. This is acceptable because the product brief prioritizes **correctness, transparency, and not destroying customer work** over feature count. AI adds surface area for non-deterministic failures ("it generated a caption that got my account flagged"). V1 focuses on the core promise: reliable publishing.

**If AI is added in v2:** Bounded use cases only. Caption suggestions (pull, not push), visible prompt templates, user edits the result before queueing, no auto-publish of AI output, cost capped per workspace (e.g., 20 suggestions/month on Pro plan), fallback to blank caption on API failure. Model choice: OpenAI `gpt-4o-mini` ($0.15/1M input, $0.60/1M output). Budget: 200 tokens in (media description), 150 tokens out (caption) = $0.00012/suggestion. 20 suggestions/workspace × 50 active Pro workspaces = $0.12/month. Negligible, but still requires error handling, abuse prevention (rate limit per workspace), and privacy review (media URLs sent to OpenAI—requires user consent or on-prem model).

**Evidence AI is not needed for v1 success:** The product's differentiation is reliability and queue-first workflow, not AI features. Customers already create content elsewhere (Canva, Premiere, phone camera) and bring captions with them. The upload → queue → publish → analytics loop is complete without LLM involvement.

---

## 7. Testing and release confidence

### 7.1 On every commit (CI)

- **Unit tests:** TypeScript validation functions, business logic (queue ordering, schedule evaluation, rate limiting, idempotency checks). Target: 80% coverage of `src/lib/`. No external services.
- **Schema validation:** `pnpm db:migrate` runs on a fresh Postgres container, asserts all tables/indexes/constraints created, no errors. Validates that §3 DDL is executable.
- **Type checking:** `tsc --noEmit` and `next build` must pass. Ensures client/server type contract is intact.

**Run time:** <2 minutes. Blocks merge if any test fails.

### 7.2 Integration tests (require real services)

Run nightly and on-demand before release. Uses test Stripe account, test Instagram account (sandbox or real account owned by the operator), R2 test bucket, ephemeral Postgres database.

- **Publish flow:** Upload image → prepare → queue → publish → verify receipt contains Instagram post ID → fetch post via Graph API → assert exists.
- **Idempotency:** Enqueue same queue item twice → assert exactly 1 publish, 1 receipt.
- **Quota enforcement:** Publish to daily limit → assert next publish deferred → reset quota → assert publish succeeds.
- **Account disconnect:** Revoke OAuth token via Instagram → assert account marked `revoked`, queue items remain, reconnect flow works.
- **Billing webhooks:** Send Stripe test webhook `invoice.paid` → assert subscription updated → send duplicate → assert no double-update.
- **Cleanup dry run:** Queue 3 posts, publish them, protect 1, run cleanup preview → assert 2 selected, 1 excluded.
- **Media preparation failure:** Upload corrupt video → assert `status='failed'`, error message shown, retry works.

**Run time:** ~10 minutes (includes Instagram API latency). Failures page oncall.

### 7.3 Chaos and retry drills

Run weekly in staging environment.

- **Crash before publish:** Kill worker mid-job (SIGKILL), verify item reverts to `ready`, retries successfully.
- **Crash after publish:** Inject timeout after Instagram API call, verify `status='needs_review'`, reconciliation clears it.
- **Race condition:** Submit 10 concurrent publish requests for same queue item → assert exactly 1 succeeds.
- **Quota exhaustion at midnight:** Publish 25 posts, simulate UTC date rollover, verify quota resets, next post succeeds.
- **Storage unavailable:** Block R2 access (network rule), attempt media upload → verify job retries 5×, eventually fails with clear error.
- **Duplicate source collection:** Run 2 collection jobs concurrently → verify unique constraint prevents duplicates.

**Evidence standard:** Each drill ends with "operator inspects X and sees Y"—not just "test passes."

### 7.4 Safe test Instagram accounts

- **Primary test account:** Public Instagram Professional account owned by the operator. Used for publish, analytics, and cleanup tests. Content is explicitly marked "Test post—ignore."
- **Source test account:** Second Instagram account or hashtag with known, stable content. Used to verify collection, filtering, backlog, and deduplication.
- **Browser automation sandbox:** Playwright runs in headless mode against test account. Session cookies stored in encrypted test fixture, not in production database.

### 7.5 Backup and restore drill

Run every 2 weeks. **Required before launch.**

1. Restore yesterday's backup to a new Postgres instance.
2. Verify row counts match production (within expected delta for 24h of activity).
3. Query workspace, queue, receipts → assert data intact.
4. Attempt to decrypt `accounts.access_token_encrypted` using production encryption key → assert decrypts successfully.
5. Document time-to-restore (target: <10 minutes for database, <30 minutes including media restore from R2 versioning).

**Exit criteria for launch readiness:** Restore drill completed successfully at least once, with operator-reviewed evidence.

### 7.6 Manual acceptance scenarios

Operator (non-technical) performs these flows in staging before launch:

1. Create workspace → connect Instagram → upload 3 images → queue → publish → verify posts appear on Instagram → check library shows all 3.
2. Set schedule (M/W/F 10am PST) → queue 2 items → fast-forward clock (mock `NOW()`) → verify items publish at correct times.
3. Disconnect account → verify queue paused → reconnect → verify queue resumes.
4. Request account deletion → verify all data removed, Instagram disconnected, email sent.
5. Submit feedback → verify appears in operator dashboard.

**Acceptance standard:** Operator completes all 5 flows without asking for help. Notes any confusing UI—blockers are fixed before launch.

---

## 8. Delivery phases

### Phase 1: Database, auth, workspace creation (first vertical slice prerequisite)

**What gets built:**
- PostgreSQL schema: `users`, `workspaces`, `workspace_members`, `sessions`, `invitations`.
- Next.js app: signup via invitation token, email/password auth, session management, RLS policies.
- Operator creates first invitation manually via `psql`.

**Exit criteria:**
- Accept invitation → create account → log in → see workspace name.
- Attempt to access workspace A as workspace B user → blocked (RLS enforced).
- Log out, log back in → session persists.
- **Why this is first:** Every subsequent phase requires workspace isolation and user identity. Building this first lets us test authn/authz boundaries early.

### Phase 2: Instagram connection and account management

**What gets built:**
- Instagram OAuth flow (redirect to Meta, callback, exchange code for token).
- `accounts` table, token encryption (`pgcrypto`).
- UI: "Connect Instagram" button, account list, disconnect, reconnection flow.

**Exit criteria:**
- Connect test Instagram account → token stored encrypted → dashboard shows account name and profile image.
- Revoke token via Instagram settings → webhook marks account `revoked` → UI shows reconnect prompt.
- Disconnect account → account removed from workspace, no queued items (none exist yet).

### Phase 3: Media upload and preparation

**What gets built:**
- R2 bucket, signed upload URLs, client-side upload progress.
- `media_uploads` table, `worker-media` process.
- `sharp` image preparation, `ffmpeg` video transcode, aspect ratio fitting.

**Exit criteria:**
- Upload 2 MB image → see progress bar → preparation completes → signed URL returns prepared version.
- Upload 30 MB video → transcode runs → prepared MP4 ready.
- Upload corrupt file → preparation fails → error message shown → retry works.

### Phase 4: Queue management

**What gets built:**
- `queue_items` table, queue order management, drag-and-drop UI (`@dnd-kit`).
- Add to queue (from prepared upload), reorder, edit caption, hide/restore, remove.
- SSE connection for live queue updates.

**Exit criteria:**
- Queue 3 items → drag item 3 to position 1 → verify `position` updated, order shown correctly in UI.
- User A reorders queue → User B's browser (same workspace) sees update within 2s (SSE push).
- Hide item 2 → verify not shown in active queue, shown in "Hidden" tab.

### Phase 5: Publishing and receipts (first end-to-end vertical slice)

**What gets built:**
- `published_posts` table, `worker-publish` process.
- Graphile Worker setup, job enqueueing, concurrency limits.
- Instagram Graph API publish: `POST /{ig-user-id}/media`.
- Receipt creation with frozen evidence.

**Exit criteria:**
- Queue item → click "Publish Now" → Instagram API called → post appears on test account → receipt created with post ID and permalink.
- Verify exactly 1 `published_posts` row, `queue_items.status='published'`.
- Open Instagram app, see post live.
- **This is the first complete vertical slice:** connect → upload → prepare → queue → publish → receipt. All earlier phases are justified as prerequisites for this.

### Phase 6: Scheduling and cron automation

**What gets built:**
- Schedule rules in `accounts.settings` JSONB (fixed times, intervals, timezone handling).
- `worker-cron` process, `schedule-due-posts` job (runs every 60s).
- Daily quota enforcement (`posts_today`, reset at midnight).

**Exit criteria:**
- Set schedule "Daily 10am PST" → queue 2 items → fast-forward clock (mock `NOW()`) → verify items publish at correct times, 1 day apart.
- Publish to daily limit → verify next item deferred → reset quota (mock date change) → verify publishes.
- Edit schedule → verify next publish time recalculated correctly.

### Phase 7: Analytics fetching and library

**What gets built:**
- `analytics_snapshots` table (versioned history).
- Recurring analytics refresh job (daily per account).
- Library UI: published posts, metrics, sorting, filtering, permalink links.

**Exit criteria:**
- Publish 5 posts → wait 24h (or mock timestamp) → analytics refresh runs → see reach, likes, comments in library.
- Re-run refresh → new snapshot row created, old snapshot preserved (not overwritten).
- Sort library by "Most likes" → verify correct order.

### Phase 8: Notifications and real-time updates

**What gets built:**
- `notifications` table, notification creation triggers (publish failure, connection lost, quota reached).
- Notification UI (sidebar, unread count, mark read, dismiss, deep links).
- SSE push for notifications.

**Exit criteria:**
- Disconnect account → notification created → appears in UI within 2s → click notification → navigated to account settings.
- Publish failure → notification shows error message → mark read → unread count decrements.
- Quota reached → notification explains why publish deferred → deep link to account schedule.

### Phase 9: Billing integration

**What gets built:**
- `subscriptions`, `stripe_events` tables.
- Stripe Checkout redirect, webhook handler, Customer Portal.
- Plan enforcement (account limits, entitlements).

**Exit criteria:**
- Click "Upgrade to Pro" → Stripe Checkout opens → complete test payment → webhook fires → subscription updated → workspace plan='pro'.
- Connect 6 accounts (over free limit) → verify blocked → upgrade → verify allowed.
- Send duplicate webhook → verify idempotent (no double charge).

### Phase 10: Operator dashboard and support tools

**What gets built:**
- Admin-only routes (separate RLS policy for operator role).
- Waitlist management, invitations, workspace suspension, entitlement grants.
- Work-item inspection (queue item stuck, uncertain publish outcome).
- Reconciliation tool for `needs_review` items.

**Exit criteria:**
- Operator logs in → sees waitlist, creates invitation → recipient accepts → new workspace created.
- Suspend workspace → verify customer blocked from queueing → unsuspend → verify access restored.
- Inspect uncertain queue item → see attempt log, Instagram API response → run reconciliation → item marked `published`.

### Phase 11: Restricted source collection (entitled feature)

**What gets built:**
- `source_configs`, `source_items` tables.
- Playwright browser automation, persistent context storage (R2).
- Collection job, backlog UI, approval flow, deduplication.

**Exit criteria:**
- Operator grants entitlement → user creates hashtag source → collection runs → backlog populated → user approves 3 items → items move to queue.
- Run collection twice concurrently → verify unique constraint prevents duplicates.
- Revoke entitlement → verify feature hidden, existing source configs paused, queued items remain.

### Phase 12: Managed cleanup (entitled, high-risk feature)

**What gets built:**
- `cleanup_rules`, `cleanup_runs`, `cleanup_events` tables.
- Cleanup preview (with confirmation hash), execution worker.
- Browser automation for archive/delete, per-post locking.

**Exit criteria:**
- Publish 5 posts, protect 1 → create cleanup rule (max reach <100) → preview selects 4 → execute → verify 4 archived, 1 protected.
- Change selection between preview and execute → verify rejected.
- Browser hangs during cleanup → verify timeout, uncertain outcome logged, no duplicate action.

### Phase 13: Privacy pages, deletion, and compliance

**What gets built:**
- `/privacy`, `/terms`, `/dmca`, `/security` pages.
- Account deletion flow (confirmation email, job queue).
- Export user data endpoint (GDPR).

**Exit criteria:**
- Request account deletion → receive email → confirm → all data removed (verified via DB query and R2 bucket).
- Request data export → receive JSON file with all workspace data.
- Operator verifies deletion left minimal audit evidence, no PII retained.

### Phase 14: Feedback, onboarding, and polish

**What gets built:**
- Feedback submission widget, `feedback` table.
- Onboarding flow (ask about niche, goals, posting cadence).
- Empty states, loading skeletons, keyboard shortcuts, error boundary, accessibility audit.

**Exit criteria:**
- New user accepts invite → completes onboarding → sees first-time tips → uploads and publishes first post without help.
- Submit feedback → appears in operator dashboard → operator replies (via external email, not in-app yet).
- Tab through queue → keyboard-only drag-and-drop works → publish with Enter key.

### Phase 15: Launch readiness

**What gets built:**
- Monitoring setup (error tracking, uptime checks).
- Backup/restore drill completed and documented.
- Load testing (100 concurrent SSE connections, 50 simultaneous uploads).
- Security audit (OWASP checklist, secrets rotation, RLS enforcement verified).

**Exit criteria:**
- Restore last night's backup → verify data intact → time <10 min.
- 200 concurrent users (simulated) → no errors, p95 latency <500ms.
- Operator completes manual acceptance scenarios without help.
- All Phase 1–14 exit criteria still pass.

**Justification for phases before Phase 5:** Phases 1–4 establish workspace isolation (1), external account access (2), media handling (3), and queue state management (4). Each is a prerequisite for the first publish (5). Building publish earlier (e.g., Phase 2) would require mocking all the inputs, then rebuilding them—slower net progress.

---

## 9. Security and privacy

### 9.1 Identity and authentication

- **Passwords:** Hashed with `bcrypt` (cost factor 12), never stored plaintext, never logged.
- **Sessions:** Stored in `sessions` table, token is SHA-256 hash of random 32-byte value, sent as `HttpOnly` `Secure` `SameSite=Lax` cookie, expires after 30 days of inactivity.
- **Email verification:** Required before connecting Instagram. Unverified users can log in but cannot add accounts or invite others.

### 9.2 Authorization and workspace isolation

- **Row-Level Security (RLS):** Enabled on all workspace-scoped tables (`workspaces`, `accounts`, `queue_items`, `media_uploads`, `published_posts`, `source_items`, `cleanup_runs`, `notifications`). Policies enforce `workspace_id IN (SELECT workspace_id FROM workspace_members WHERE user_id = current_user_id())`.
- **Middleware checks:** Next.js middleware loads session, sets `current_user_id()` Postgres variable (via `SET LOCAL`) at transaction start. Every query inherits this.
- **API routes:** Validate workspace membership before any write. Insufficient for reads (RLS is last line of defense).
- **Operator access:** Separate `admin` role, gated by email allowlist (env var `OPERATOR_EMAILS`). Operator queries bypass RLS (uses `security_definer` functions), but all actions logged in `audit_log`.

### 9.3 Secrets management

- **Instagram access tokens:** Encrypted at rest using `pgcrypto` with AES-256, key stored in env var `ENCRYPTION_KEY_BASE64` (32 bytes, base64-encoded). Decrypted only in workers, never returned to browser.
- **Stripe webhook secret:** Verified via `stripe.webhooks.constructEvent(body, signature, secret)`. Replay attacks prevented by `stripe_events.stripe_event_id UNIQUE`.
- **Session tokens:** Random 32 bytes from `crypto.randomBytes()`, hashed before storage. Original token never appears in logs or database.
- **Environment variables:** Stored in Render's encrypted environment config. Access restricted to operator account (2FA required). Never committed to git (`.env` in `.gitignore`).

### 9.4 Private media

- **R2 signed URLs:** Generated on-demand with 1-hour expiration. Original and prepared media keys are UUIDs, not user-controlled filenames (prevents directory traversal).
- **Access control:** Before generating signed URL, verify workspace membership: `SELECT workspace_id FROM media_uploads WHERE id = $1` → check workspace access.
- **Instagram's access:** Prepared media URL is sent to Instagram via Graph API. Instagram fetches it once during publish. URL expires 1h after generation—long enough for publish, short enough to prevent long-lived sharing.
- **No public bucket:** R2 bucket has no public access. All reads require signed URL.

### 9.5 Restricted sessions and browser automation

- **Session isolation:** Each workspace's browser context is stored separately in R2 as `browser-contexts/{workspace_id}.json`. Playwright's `storageState()` includes cookies and localStorage—never session storage or Auth headers from our app.
- **Secrets redaction:** `cleanup_events.evidence` and job logs run through `sanitizeEvidence(obj)` which recursively deletes keys matching `Authorization`, `Cookie`, `Set-Cookie`, `session`, `token`.
- **Browser context rotation:** Contexts older than 7 days are deleted (cron job). Fresh login required every week (reduces stolen-session risk).
- **Rate limiting:** Restricted source collection and cleanup are rate-limited to 1 request per 10s per workspace (per Instagram's best practices). Browser automation jobs check `workspace_rate_limits` table before proceeding.

### 9.6 Billing callbacks and webhooks

- **Stripe signature verification:** All webhooks verify `Stripe-Signature` header. Replay attacks prevented by `stripe_events` unique constraint.
- **Idempotency:** Every webhook handler is wrapped in `INSERT INTO stripe_events ... ON CONFLICT DO NOTHING`. If event already processed, handler exits immediately.
- **HTTPS only:** Webhook endpoints reject plain HTTP. Render enforces TLS 1.2+.
- **Instagram webhooks:** Verify `X-Hub-Signature-256` header (HMAC-SHA256 of body with app secret). Deauth callbacks check Instagram user ID matches stored `instagram_account_id`.

### 9.7 Abuse controls

- **Invitation-only launch:** Public signup disabled. Operator manually creates invitations. Each invitation is single-use, expires after 7 days.
- **Rate limiting:** API routes use `express-rate-limit`: 100 requests/15min per IP for authenticated routes, 10 requests/15min for auth routes (signup, login).
- **Upload limits:** 100 MB per file, 500 MB per workspace per day (enforced by summing `media_uploads.size_bytes` for last 24h).
- **Workspace suspension:** Operator can suspend workspace for ToS violation. Suspended workspaces cannot queue, publish, or access restricted features. Existing queue preserved.

### 9.8 Audit and attribution

- **Audit log:** Every destructive action (publish, cleanup, invite, suspend, entitlement grant, billing change) writes to `audit_log` with `actor_id`, `workspace_id`, `action`, `resource_id`, timestamp.
- **Content rights acceptance:** User's click on "I have rights to publish this" writes `audit_log` entry with `action='accept_rights'`, referencing ToS version and timestamp. If customer later claims content was stolen, we have evidence they attested otherwise.
- **Operator actions:** All operator dashboard actions log `actor_role='operator'`, include details of what changed. Operator account uses separate password from user account (prevents privilege escalation).

### 9.9 Retention and export

- **Data retention:** Published receipts and analytics snapshots retained indefinitely (unless workspace deleted). Queue items and uploads retained for 90 days after completion or deletion (then purged via cron).
- **Export:** Workspace owner can download JSON export of all workspace data (accounts, queue, receipts, analytics, settings). Includes original media download links (signed URLs, expire after 24h).
- **Deletion:** User requests deletion via account settings → email confirmation link → `delete-account` job removes all `workspace_members` rows for user, deletes user row if no other memberships. Workspace data persists if other members remain. If user is sole owner, workspace is suspended (not deleted—billing history required for tax compliance).

### 9.10 Support access

- **Read-only by default:** Operator dashboard can view workspace data (queue, receipts, logs) but cannot edit queue items or captions directly. Changes go through "Retry job" or "Reconcile uncertain outcome" workflows.
- **No password access:** Operator never has access to user passwords (bcrypt hashes are one-way). Cannot log in as customer. Support actions are logged in `audit_log`.
- **Temporary access grants:** If user requests help, operator can grant time-limited `feature:support_access` entitlement (expires after 24h), allowing operator to view SSE stream or error details in real-time. Logged in audit trail.

---

## 10. Risk register

| Risk | Early warning signal | Mitigation in plan |
|------|---------------------|-------------------|
| **Instagram changes API without notice, publishes stop working** | Publish jobs succeed but Graph API returns new error code; monitoring alerts on spike in `status='failed'` | Instagram webhooks notify us of account disconnects; daily health check job calls Graph API `/me` to verify token validity; operator dashboard shows API success rate per endpoint; graceful degradation: failed publishes go to `needs_review`, not silently dropped |
| **Browser automation detected, restricted source accounts banned** | `worker-browser` jobs fail with "login required" or "unusual activity" errors; increase in `source_configs.status='blocked'` | Feature is entitled, not public—operator controls which workspaces can access; browser contexts use realistic user-agent, random delays (2–5s between actions), obey rate limits (1 req/10s); persistent sessions reduce login frequency; if account banned, feature paused for that workspace, existing queued items preserved |
| **Stripe subscription state desync (webhook missed, out-of-order events)** | Workspace with `plan='pro'` but Stripe subscription shows `canceled` | Every webhook write is idempotent; nightly reconciliation job queries Stripe API for all subscriptions, compares to `subscriptions` table, logs discrepancies; operator reviews and manually reconciles; plan downgrades hold activity above new limit but don't delete data—user can resolve |
| **Media storage cost spiral (users upload huge files, don't publish, abandon workspace)** | R2 storage usage grows >10% week-over-week; operator dashboard shows top 10 workspaces by storage | 100 MB per-file upload limit enforced in API; 500 MB/day per workspace limit; media older than 90 days post-publish is purged (cron job); suspended workspaces' media purged after 30 days; operator can manually purge abandoned workspace |
| **Queue order corruption (race condition, concurrent reorders)** | User reports "item jumped to wrong position"; `queue_items.position` has duplicate values for same account | `UNIQUE(workspace_id, account_id, position)` constraint prevents duplicates; reorder operation uses transaction: lock all affected rows (`SELECT ... FOR UPDATE`), renumber atomically; optimistic concurrency: UI sends `expected_version`, rejects if changed; SSE pushes order updates, client reconciles |
| **Publish idempotency fails, post appears twice on Instagram** | User reports duplicate post; Instagram account shows 2 identical posts with different IDs; 2 `published_posts` rows for same `queue_item_id` | Partial unique index `queue_one_publishing_per_account` allows only 1 item per account in `status='publishing'`; publish job checks `queue_item_id` already in `published_posts` before calling Instagram; Instagram's API is idempotent (resending same media creates 1 post)—but we don't rely on it |
| **Worker process OOM kills (Playwright browser contexts consume >4 GB RAM)** | `worker-browser` restarts every 30 minutes; Render shows memory usage spiking; jobs fail mid-execution | 4 concurrent browser contexts max (capped in Graphile Worker concurrency config); each context limited to 800 MB via `--max-old-space-size=800` Chromium flag; idle contexts parked to disk after 5 min; separate worker process (4 GB RAM allocation) isolates from other workers |
| **Daily quota reset fails, publishes blocked forever** | User reports "stuck in queue" despite midnight passing; `accounts.posts_today` stays at 25 | Cron job `reset-daily-quotas` runs at 00:05 UTC every day; if it misses a run (worker down), next run resets any account with `posts_reset_at < CURRENT_DATE`; operator dashboard shows accounts with quota anomalies (`posts_today > 0` and `posts_reset_at` >1 day ago) |
| **Customer uploads copyrighted content, operator receives DMCA takedown** | DMCA notice arrives via email or web form | Public `/dmca` page explains process; operator reviews, identifies workspace/upload via receipt or media URL; suspends workspace, removes media from R2, responds to claimant; `audit_log` records DMCA event; if user contests, unsuspend and document; ToS requires users to attest they have rights—shifts liability |
| **Third-party integration outage (Stripe/R2/Instagram all down simultaneously)** | All publish jobs fail; Stripe webhooks return 500; R2 returns 503; monitoring shows 0 successful API calls | Graceful degradation: publish jobs retry with exponential backoff (up to 5 attempts over 30 min); uncertain outcomes go to `needs_review` instead of silent failure; operator dashboard shows per-service health; customers notified via in-app notification ("Instagram API unavailable—queued items will publish when service recovers") |
| **Production database corrupted or deleted (bad migration, operator error)** | Queries fail; app returns 500 errors; Render shows DB connection errors | Daily automated backups (Render managed Postgres); point-in-time recovery available (7-day window); backup restore drill practiced every 2 weeks—operator can restore in <10 min; write-ahead log (WAL) archived to separate bucket; worst-case: lose <24h of data, but media in R2 is intact (separately versioned) |

---

## 11. Explicit tradeoffs

### 11.1 No exactly-once delivery guarantee from cron scheduler

**Requirement:** Recurring jobs (analytics refresh, scheduled cleanup, quota reset) run at specified intervals without missing or duplicating work.

**Delivered:** Cron jobs can run 0 or 2× in rare cases (worker crash during execution, clock skew, scheduler bug). Jobs are idempotent (analytics writes new snapshot row, quota reset is `UPDATE ... WHERE posts_reset_at < CURRENT_DATE`), so duplication is safe. Missing a run is detectable (operator dashboard shows last run time) and recoverable (next run catches up).

**Why acceptable:** Building a distributed scheduler with exactly-once semantics requires leader election, persistent job state, and fencing tokens—complexity far beyond launch scale. Graphile Worker's cron is "at least once" by design. The impact of 2× analytics refresh is 1 extra API call and 1 extra snapshot row—negligible. Missing a quota reset is visible within hours and manually recoverable. Customers don't notice.

### 11.2 Media prepared inline (not offloaded to managed service)

**Requirement:** Media preparation does not block publishing or customer-facing requests.

**Delivered:** Media preparation runs in separate worker (`worker-media`, 8 concurrency), not in `web` or `worker-publish`. Long videos (>2 min) can take 20–30s to transcode, blocking 1 of 8 worker slots. At launch scale (200 WAU, 500 uploads/day), this is fine. At 10× scale, it becomes a bottleneck.

**Why acceptable:** AWS MediaConvert costs $0.02/min, too expensive for 15s clips. Running `ffmpeg` in-process is free (compute time only) and sufficient for <2000 uploads/day. V2 can add a threshold (videos >5 min offload to MediaConvert) or switch to a managed service if upload volume justifies the cost.

### 11.3 SSE reconnect delivers duplicate events

**Requirement:** Real-time updates (queue reorder, status change) appear exactly once in the browser.

**Delivered:** EventSource auto-reconnects with `Last-Event-ID`. If the server restarts, in-memory subscription map is lost—client reconnects and may receive the last 10 events again (we buffer them for reconnect). Duplicate events are harmless (React reconciliation ignores redundant state updates), but not ideal.

**Why acceptable:** Perfect SSE resume requires persisting every emitted event to a durable log (Redis Streams, Postgres NOTIFY queue). For launch scale (400 concurrent tabs), in-memory map is sufficient. Duplicate events don't corrupt UI state—they're just extra renders. V2 can add persistent event log if real-time UX becomes critical.

### 11.4 Cleanup reconciliation is manual

**Requirement:** Uncertain cleanup outcomes (timeout after Instagram API call) are reconciled automatically.

**Delivered:** Worker marks outcome as `'uncertain'`, pauses cleanup run, notifies operator. Operator inspects Instagram account manually (via app or Graph API), updates `cleanup_events` row to `'success'` or `'failed'`, resumes run. This is manual, not automated.

**Why acceptable:** Cleanup is rare (weekly at most), entitled (not public), and destructive. Automation could delete a post that was already deleted, or fail to delete one that Instagram's API silently accepted. Manual review is safer and feasible at launch scale (1–2 uncertain outcomes/week). V2 can add automatic reconciliation via Graph API `GET /{post-id}` to check if post still exists, but "post not found" can mean "deleted" or "ID invalid"—still ambiguous.

### 11.5 No multi-region deployment

**Requirement:** Service available to customers in any geography.

**Delivered:** Render app deployed to single US region. Database, R2 bucket, and workers all colocated. Non-US customers experience higher latency (200–400ms page load, 500ms+ for media uploads).

**Why acceptable:** Launch is single-region (product brief §2). Most initial users are US-based creators. Multi-region adds cost (second Postgres replica $50/mo, cross-region data transfer $0.09/GB) and complexity (replication lag, conflict resolution, media geo-distribution). V2 can expand to EU region if demand justifies.

### 11.6 Operator dashboard lacks fine-grained workspace editing

**Requirement:** Operator can fix customer issues (stuck queue item, incorrect receipt) without waiting for code deploy.

**Delivered:** Operator can view workspace data, retry failed jobs, reconcile uncertain outcomes, suspend workspace, and adjust entitlements. Cannot directly edit queue item caption, change post publish time, or delete receipts. Must use `psql` for data fixes.

**Why acceptable:** Direct data editing bypasses audit trail and risks corrupting state (e.g., editing caption after publish, breaking receipt integrity). For rare fixes, requiring `psql` + `audit_log` manual entry is safer. V2 can add scoped edit actions with full audit and validation.

---

## 12. Where this is stronger than required

### 12.1 Versioned analytics history

**Requirement:** Show current performance metrics for published posts.

**Delivered:** Every analytics refresh creates a new `analytics_snapshots` row, preserving historical values. Users can see engagement change over time ("this post had 50 likes on day 1, 120 on day 7"). Supports trend charts, performance decay analysis, and "best time to cleanup" decisions.

**Why added:** Brief says "Analytics refreshes may lag without blocking publishing. Old snapshots remain useful for trends." We interpreted this as "keep history," not "overwrite." Cost is negligible (1 snapshot row = ~200 bytes; 10,000 posts × 30 snapshots = 60 MB). Product value is high (users asked "when did engagement plateau?" in tester interviews). Complexity is low (insert-only table, no overwrites).

### 12.2 Audit log for all privileged actions

**Requirement:** Operator actions are attributable.

**Delivered:** `audit_log` records user actions (accept rights, delete account), operator actions (suspend workspace, reconcile outcome, grant entitlement), and system actions (billing webhook, cron job). Every row includes actor, timestamp, workspace, resource, and details JSON. Searchable, exportable, never deleted (even after workspace deletion).

**Why added:** Brief requires "privileged actions are attributable." We extended it to cover user and system actions because (1) GDPR/CCPA compliance requires deletion audit trail, (2) debugging "why did this publish twice" requires knowing which jobs ran when, (3) zero marginal cost (table is write-only, queries are infrequent). Stronger audit than required, but justified by compliance and operational value.

### 12.3 Job idempotency via payload hash (Graphile Worker)

**Requirement:** Same queue item does not publish twice due to duplicate job submissions.

**Delivered:** Graphile Worker deduplicates jobs by payload hash *before* enqueueing. Two `publish-post` jobs with `{queue_item_id: 'abc'}` result in 1 queued job. This is on top of the `SELECT ... FOR UPDATE` lock in the publish handler.

**Why added:** Brief requires preventing double publish. We added two layers: (1) dedup at enqueue (Graphile Worker), (2) dedup at execution (row lock). Neither alone is sufficient (enqueue dedup expires after job completes; row lock doesn't prevent sequential retries of a failed job). Both together provide defense in depth. Cost: zero (Graphile Worker built-in). Complexity: zero (automatic).

### 12.4 Browser context disk persistence (Playwright storageState)

**Requirement:** Restricted source collection uses authenticated browser sessions.

**Delivered:** Browser context (cookies, localStorage) is saved to R2 as JSON after every collection run. Next run resumes the same session—no re-login. Context persists across worker restarts, deploys, and crashes. Reduces Instagram "suspicious login" flags and login CAPTCHA friction.

**Why added:** Brief does not mention session persistence—only that automation is "restricted" and "risky." Persistent sessions reduce the #1 failure mode of browser automation: getting logged out or flagged mid-run. Playwright's `storageState()` API makes this trivial (~10 lines). Cost is negligible (one 5 KB JSON file per workspace, stored in R2 = $0.000075/workspace/month). Product value is high (fewer manual re-logins for customers).

### 12.5 Dark mode and accessibility

**Requirement:** "Accessibility, keyboard operation, and clear empty, loading, success, blocked, and error states are launch requirements."

**Delivered:** Full WCAG 2.1 AA compliance: semantic HTML, ARIA labels, keyboard navigation, focus indicators, contrast ratios >4.5:1, screen-reader announcements for live updates (SSE). Dark mode (via `prefers-color-scheme`) with all states (empty, loading, error) tested in both themes.

**Why added:** Brief lists accessibility as required, but doesn't specify level. We target AA (industry standard) because (1) creators are diverse (some have vision or motor impairments), (2) poor accessibility generates support load (keyboard-only users stuck), (3) zero marginal cost (Tailwind + semantic HTML). Stronger than "works with keyboard" but justified by reducing support burden.

---

## 13. Assumptions

Where the product brief was silent, the following decisions were made:

1. **Timezone handling:** Account timezone is user-selected (defaults to browser timezone), not auto-detected from Instagram profile. Reasoning: Instagram API does not expose account timezone, and inferring from post timestamps is unreliable. Schedule rules use account timezone for all calculations.

2. **Session lifetime:** User sessions expire after 30 days of inactivity, not on browser close. Reasoning: "Responsive web product" suggests long-lived sessions (users may check queue on phone, close tab, return days later). Stripe Customer Portal and Instagram OAuth both support long sessions.

3. **Media retention:** Original uploads retained for 90 days after publish or deletion, not indefinitely. Reasoning: Brief says "customers may retrieve their original uploads," implying finite retention. 90 days is long enough for re-use (content repurposing, DMCA appeal) but short enough to cap storage growth.

4. **Invitation expiration:** Invitations expire after 7 days. Reasoning: Brief says "expiring, single-use invitations" but no duration given. 7 days balances onboarding urgency (too long and users forget) with flexibility (too short and users miss the window).

5. **Analytics refresh frequency:** Daily per account, or user-triggered (max once per hour per account). Reasoning: Instagram Insights lag 24–48h; fetching more often wastes API quota. Daily is sufficient for trends; hourly manual refresh allows checking new posts.

6. **Restricted source collection frequency:** User-configurable (hourly, daily, weekly), default daily. Reasoning: Brief says "collection schedule" but no default. Daily matches typical posting cadence (fills queue overnight for next-day publishing).

7. **Cleanup protection default:** New posts are unprotected; user must manually protect. Reasoning: Brief says "users can protect posts" but not whether protection is opt-in or opt-out. Opt-in is safer (no surprise deletions) and matches user intent (most posts are not keepers).

8. **Billing currency:** USD only. Reasoning: Stripe supports multi-currency, but managing pricing tiers in GBP/EUR/etc. adds complexity. Launch is invite-only, US-focused (§2). V2 can add currencies if international demand emerges.

9. **Queue depth limit:** 1000 items per account (soft limit, enforced by UI warning). Reasoning: Brief says "weeks of backlog" (3 posts/day × 30 days = 90 items). 1000 is 10× that, provides headroom, and prevents abuse (user queues 50,000 items, starves database). No hard constraint—operator can override.

10. **Email notifications:** Disabled by default; user opts in via workspace settings. Reasoning: Brief lists in-app notifications as required, email as implied ("connection problems, billing state"). Defaulting to email-off reduces spam complaints and Postmark bounce rate. High-priority failures (payment failed, account suspended) are email-on by default.

11. **Workspace deletion:** Requires all accounts disconnected and no active subscription. Reasoning: Brief doesn't specify deletion prerequisites. Requiring disconnects prevents accidental deletion of live publishing queues. Subscription must be canceled to avoid orphan Stripe subscriptions.

12. **Operator account creation:** Hardcoded email allowlist in `OPERATOR_EMAILS` env var, not database-driven role. Reasoning: One operator at launch. Database role would require migration, admin UI, and role-escalation safeguards—overkill for one person. V2 can add proper RBAC if team grows.

13. **Instagram API rate limits:** We obey published limits (25 posts/day default, 200 Graph API calls/hour) but do not pre-fetch current limits dynamically. Reasoning: Instagram's `/me?fields=rate_limit` is not documented for all endpoints. We use fixed limits + error-based backoff (429 response → exponential retry). Sufficient for launch scale.

14. **SSE event retention:** Last 10 events per workspace buffered in-memory for reconnect. Reasoning: EventSource reconnect with `Last-Event-ID` requires server to replay missed events. 10 events = ~10s of activity (queue reorder, 2 status updates, 1 notification). Covers typical disconnect duration. Older events are lost—client refetches on stale state.

15. **Content rights acceptance version:** ToS version embedded in `queue_items.created_at` timestamp + reference to `terms_versions` table (added in Phase 13). Reasoning: DMCA defense requires proof user accepted *current* terms when they uploaded. Timestamp alone is insufficient if terms changed. Versioned ToS tracks what user agreed to.

16. **Feedback response:** Operator replies via email, not in-app chat. Reasoning: In-app messaging requires real-time infrastructure, read receipts, and notification system. For beta (200 WAU, <5 feedback/day), email is sufficient. V2 can add in-app replies if volume justifies.

