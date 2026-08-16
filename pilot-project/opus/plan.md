# Pilot Engineering Plan

## 1. Technology Decisions

### 1.1 Primary Language and Runtime

**Choice:** TypeScript on Node.js 22 LTS.

**Rejected:** Python with FastAPI. Python's async model is mature and the ML ecosystem is stronger. Rejected because Playwright's Node.js bindings are the primary development target with the most complete API surface (`browserContext.storageState()`, `page.waitForLoadState()`, stealth plugins). The browser automation path is the product's core differentiator; optimizing for it outweighs Python's ecosystem advantages elsewhere.

**Why:** Browser automation reliability is the critical path. Secondary benefits: single language across web frontend and backend simplifies the one-person operation.

### 1.2 Web Framework

**Choice:** Hono on Node.js with server-side rendering via htmx + Handlebars templates.

**Rejected:** Next.js. Provides excellent DX and React ecosystem access. Rejected because the product has no complex client-side interactivity requirements (the review screen is form-heavy but not interactive-UI-heavy), and Next.js's server component model adds deployment complexity for a self-hosted single-machine setup. The htmx approach delivers "live-feeling" updates via SSE without a build step or hydration budget.

**Why:** Operational simplicity for a one-operator deployment. htmx's `hx-ext="sse"` provides real-time status updates; Handlebars partials handle the form rendering. Total frontend JS payload: ~14 KB (htmx) vs ~100+ KB for React hydration.

### 1.3 Database

**Choice:** PostgreSQL 16.

**Rejected:** SQLite. Genuinely simpler, zero-ops, embedded, and adequate for the read path at this scale. Rejected because the invariants in §4 require `SELECT ... FOR UPDATE SKIP LOCKED` for worker job claiming, `SERIALIZABLE` isolation for credit holds, and partial unique indexes for enforcing "one active application per job per user." SQLite lacks row-level locking and its `BEGIN EXCLUSIVE` would serialize all writes.

**Why:** The no-double-submit and credit-safety invariants require database-level concurrency primitives that only a real RDBMS provides. PostgreSQL is the simplest choice that meets this bar.

### 1.4 Job Queue

**Choice:** PostgreSQL-backed queue using `SKIP LOCKED` pattern (no separate queue system).

**Rejected:** Redis + BullMQ. Production-proven, excellent dashboard, rich retry semantics. Rejected because it introduces a second stateful system. At the target scale (tens to low hundreds of jobs/day), the polling overhead of a Postgres-based queue is negligible (~1 query/second/worker), and keeping queue state in the same transaction as application state eliminates distributed-transaction edge cases.

**Why:** The credit-hold and application-state transitions must be atomic with job claiming. A single-database design makes this trivial; a separate queue requires two-phase patterns.

### 1.5 Browser Automation

**Choice:** Playwright with `playwright-extra` and `puppeteer-extra-plugin-stealth`.

**Rejected:** Puppeteer. Mature, widely used, lighter weight. Rejected because Playwright's `browserContext.storageState({path})` for session persistence and `page.waitForLoadState('networkidle')` for SPA detection are better documented and more reliable in testing. The stealth plugin is compatible via `playwright-extra`.

**Why:** The product must fill real ATS forms that may have bot detection. Playwright's API surface for waiting, interception, and storage is the most complete.

### 1.6 AI Provider

**Choice:** Anthropic Claude Sonnet 4 as primary, Claude Haiku 4.5 for bulk/cheap tasks.

**Rejected:** OpenAI GPT-4o. Comparable quality, slightly cheaper on some tasks. Rejected because Anthropic's prompt caching (up to 90% cost reduction on repeated prefixes) is more aggressive, and the product has high prompt-prefix reuse (the profile + job description is repeated across resume, cover letter, and form-filling). The effective per-application cost is lower.

**Why:** Cost control at scale. Prompt caching reduces the marginal cost of multi-step generation pipelines.

### 1.7 PDF Generation

**Choice:** Typst compiled to PDF.

**Rejected:** Puppeteer/Playwright print-to-PDF. Works without dependencies. Rejected because print-to-PDF produces inconsistent pagination, cannot do multi-column layouts cleanly, and handles non-Latin scripts poorly without extensive CSS workarounds. Typst is a single ~20 MB binary with native Unicode/font support.

**Why:** Professional document quality is a product requirement. Typst's markup is simpler than LaTeX, produces deterministic output, and handles CJK/Arabic/Cyrillic without font-embedding gymnastics.

### 1.8 DOCX Generation

**Choice:** `docx` npm package (direct OOXML construction).

**Rejected:** Pandoc. More complete, handles edge cases. Rejected because Pandoc is a 100+ MB dependency with complex installation, and the product generates a constrained document structure (resume/cover letter templates, not arbitrary documents). Direct OOXML construction for these known templates is ~50 lines of code.

**Why:** Minimal dependencies for a constrained use case.

### 1.9 File Storage

**Choice:** Local filesystem with encrypted-at-rest volume (LUKS on Linux).

**Rejected:** S3-compatible object storage (MinIO, Backblaze B2). Better durability story, easier backups. Rejected because the target is a single self-hosted machine; adding an object store is operational overhead without corresponding benefit until multi-machine scaling. Files are <10 MB each (resumes, generated docs); a local directory handles this trivially.

**Why:** Operational simplicity. The encryption-at-rest requirement is met by the volume layer, not the application.

### 1.10 Hosting

**Choice:** Single Hetzner dedicated server (AX42: 8-core Ryzen, 64 GB RAM, 2× 512 GB NVMe) at ~€50/month.

**Rejected:** Cloud VMs (AWS EC2, GCP). More flexible scaling, managed services available. Rejected because the target workload (4 concurrent browser contexts, PostgreSQL, web server) fits comfortably in a single machine, and dedicated hardware at Hetzner is 3-5× cheaper than equivalent cloud compute. At this scale, "scaling" means moving to a bigger machine, not horizontal distribution.

**Why:** Budget constraint. €50/month is well under the "few hundred dollars" ceiling and provides 16× the compute of a $100/month cloud VM.

### 1.11 Deployment

**Choice:** Single-binary deployment via Docker Compose with three services (web, worker, postgres).

**Rejected:** Kubernetes. Industry standard for container orchestration. Rejected because Kubernetes' operational overhead (control plane, networking, ingress, secrets management) exceeds the benefit for a single-machine deployment. Docker Compose provides restart policies, resource limits, and log aggregation without the complexity.

**Why:** One operator. Docker Compose is learnable in an afternoon; Kubernetes is not.

### 1.12 Secrets Management

**Choice:** Environment variables loaded from `.env` file, encrypted via `age` with the operator's key.

**Rejected:** HashiCorp Vault. Proper secrets management with rotation, audit, dynamic credentials. Rejected because Vault is a separate stateful service requiring its own backup and operational procedures. For a single-operator system with a handful of secrets (Stripe key, database password, AI API key), encrypted env files are sufficient.

**Why:** Operational simplicity. The threat model does not include malicious insiders; the secrets are protected from disk theft by volume encryption and from application compromise by process isolation.

---

## 2. System Architecture

### 2.1 Process Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Host Machine                              │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │   Web       │  │   Worker    │  │  Scraper    │             │
│  │  (Hono)     │  │  (Browser)  │  │  (Browser)  │             │
│  │  Port 3000  │  │  Headless   │  │  Headless   │             │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘             │
│         │                │                │                     │
│         └────────────────┼────────────────┘                     │
│                          │                                       │
│                   ┌──────▼──────┐                               │
│                   │  PostgreSQL │                               │
│                   │  Port 5432  │                               │
│                   └─────────────┘                               │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │  Encrypted Volume (LUKS)                                 │   │
│  │  /data/files     - uploaded resumes, generated docs      │   │
│  │  /data/postgres  - database files                        │   │
│  │  /data/traces    - browser automation recordings         │   │
│  └─────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 Web Process

**Responsibilities:**
- HTTP request handling (authentication, API, page rendering)
- SSE connections for live status updates
- Enqueueing jobs (writes to `job_queue` table)
- Serving static assets

**Does not:**
- Run browser automation
- Call AI APIs directly (these are job-queue tasks)
- Hold long-running connections beyond SSE

**Concurrency:** Single Node.js process, single-threaded event loop. At 200 WAU with ~10 concurrent users peak, this handles ~100 req/s with headroom. If CPU-bound work emerges, the fix is to move it to workers, not to cluster the web process.

### 2.3 Worker Process

**Responsibilities:**
- Polls `job_queue` for browser automation tasks (application preparation, submission)
- Runs Playwright browser contexts
- Calls AI APIs for form-filling, document generation
- Updates application state and emits events

**Resource isolation:** The worker runs in a separate container with memory limit 8 GB. Each Playwright browser context consumes ~800 MB; the worker runs at most 4 concurrent contexts (4 × 800 MB = 3.2 GB, leaving 4.8 GB for Node.js heap and overhead).

**Concurrency model:**
- 4 parallel job processors (one per browser context slot)
- Each processor: claim job → execute → release context → repeat
- Context pool pre-warms 2 contexts on startup; scales to 4 under load

**Timeouts:**
- Per-page-action timeout: 30 seconds (covers slow form loads)
- Per-job timeout: 5 minutes (covers entire prepare or submit cycle)
- Stale-job reclaim: jobs in `processing` state for >10 minutes are reclaimed by a reaper query

### 2.4 Scraper Process

**Responsibilities:**
- Polls `scrape_queue` for company board refresh tasks
- Navigates to career pages, extracts job listing URLs
- Fetches individual job pages, extracts structured data
- Updates `jobs` table with new/changed/closed postings

**Isolation:** Separate container, memory limit 4 GB. Runs 2 concurrent browser contexts. Scraping is lower priority than application submission; if resources are constrained, scraper yields.

**Schedule:**
- Each company board is refreshed every 4 hours (configurable per-board)
- `cron_schedules` table tracks last-refresh and next-refresh times
- On startup, scraper processes any overdue boards immediately

### 2.5 Inter-Process Communication

All communication is via PostgreSQL:
- **Job queue:** Workers poll `job_queue` table with `SELECT ... FOR UPDATE SKIP LOCKED`
- **Events:** Workers insert into `events` table; web process polls for SSE delivery
- **Status:** Application state changes are committed; web reads current state on each request

No direct IPC, no Redis pubsub, no message broker. Latency for status updates is polling interval (500 ms for SSE event queries). This is acceptable for "live-feeling" but not real-time.

### 2.6 How Browser Work Doesn't Starve Everything Else

1. **Process isolation:** Web, worker, and scraper are separate OS processes in separate containers. A browser OOM in the worker cannot crash the web server.

2. **Resource caps:** Docker Compose sets `mem_limit: 8g` on worker, `mem_limit: 4g` on scraper, `mem_limit: 2g` on web. Cgroups enforce these.

3. **Context pooling:** Worker never spawns more than 4 browser contexts simultaneously. The pool is explicit, not demand-driven.

4. **Timeout enforcement:** Jobs exceeding 5 minutes are force-killed via `page.close()` and marked failed. The reaper query catches any that slip through (process crash during execution).

5. **Scraper backpressure:** If job queue depth exceeds 20, scraper pauses new board refreshes. Applications take priority over discovery.

---

## 3. Data Model

```sql
-- Users and authentication
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    email_verified BOOLEAN NOT NULL DEFAULT FALSE,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE INDEX users_email_idx ON users (email) WHERE deleted_at IS NULL;

CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    last_active_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX sessions_user_id_idx ON sessions (user_id);
CREATE INDEX sessions_expires_at_idx ON sessions (expires_at);

-- Profile versioning
CREATE TABLE profile_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    version_number INTEGER NOT NULL,
    is_draft BOOLEAN NOT NULL DEFAULT TRUE,
    published_at TIMESTAMPTZ,
    data JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, version_number)
);

CREATE INDEX profile_versions_user_latest_idx ON profile_versions (user_id, version_number DESC);
CREATE INDEX profile_versions_user_published_idx ON profile_versions (user_id) 
    WHERE is_draft = FALSE;

-- EEO data quarantine (separate table, never joined with AI operations)
CREATE TABLE user_eeo_data (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Application preferences (reusable answers)
CREATE TABLE user_preferences (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    work_authorization TEXT,
    requires_sponsorship BOOLEAN,
    willing_to_relocate BOOLEAN,
    relocation_locations TEXT[],
    notice_period_days INTEGER,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT DEFAULT 'USD',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Uploaded files
CREATE TABLE files (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    file_type TEXT NOT NULL CHECK (file_type IN ('resume_upload', 'generated_resume', 'generated_cover_letter', 'submission_proof')),
    original_filename TEXT,
    storage_path TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    checksum_sha256 TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX files_user_type_idx ON files (user_id, file_type);

-- Company boards and jobs
CREATE TABLE company_boards (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    board_url TEXT NOT NULL UNIQUE,
    board_type TEXT NOT NULL CHECK (board_type IN ('greenhouse', 'lever', 'workday', 'custom')),
    scrape_config JSONB NOT NULL DEFAULT '{}',
    last_scraped_at TIMESTAMPTZ,
    next_scrape_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scrape_interval_hours INTEGER NOT NULL DEFAULT 4,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX company_boards_next_scrape_idx ON company_boards (next_scrape_at) 
    WHERE is_active = TRUE;

CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    board_id UUID NOT NULL REFERENCES company_boards(id),
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    location_raw TEXT,
    locations_parsed JSONB,
    remote_type TEXT CHECK (remote_type IN ('remote', 'hybrid', 'onsite', 'unknown')),
    employment_type TEXT CHECK (employment_type IN ('full_time', 'part_time', 'contract', 'intern', 'unknown')),
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT,
    description_raw TEXT NOT NULL,
    description_structured JSONB,
    requirements_extracted JSONB,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    UNIQUE (board_id, external_id)
);

CREATE INDEX jobs_board_active_idx ON jobs (board_id) WHERE closed_at IS NULL;
CREATE INDEX jobs_last_seen_idx ON jobs (last_seen_at);

-- User-job interactions
CREATE TABLE user_job_dismissals (
    user_id UUID NOT NULL REFERENCES users(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, job_id)
);

CREATE TABLE user_job_bookmarks (
    user_id UUID NOT NULL REFERENCES users(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    bookmarked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, job_id)
);

-- Match cache (precomputed rankings)
CREATE TABLE match_scores (
    user_id UUID NOT NULL REFERENCES users(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id),
    score REAL NOT NULL,
    explanation_json JSONB NOT NULL,
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, job_id)
);

CREATE INDEX match_scores_user_score_idx ON match_scores (user_id, score DESC);

-- Applications (core state machine)
CREATE TABLE applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id),
    status TEXT NOT NULL CHECK (status IN (
        'queued',           -- user requested, awaiting preparation
        'preparing',        -- worker is generating documents / reading form
        'ready',            -- form draft ready for review
        'awaiting_otp',     -- submission started, waiting for user OTP
        'submitting',       -- worker is filling and submitting
        'submitted',        -- successfully submitted
        'failed',           -- terminal failure
        'canceled'          -- user canceled
    )),
    auto_submit BOOLEAN NOT NULL DEFAULT FALSE,
    form_snapshot_id UUID,
    form_answers_draft JSONB,
    form_answers_final JSONB,
    submission_proof_file_id UUID REFERENCES files(id),
    resume_file_id UUID REFERENCES files(id),
    cover_letter_file_id UUID REFERENCES files(id),
    error_message TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    row_version INTEGER NOT NULL DEFAULT 1
);

-- Enforce: only one non-terminal application per user per job
CREATE UNIQUE INDEX applications_one_active_per_job
    ON applications (user_id, job_id)
    WHERE status NOT IN ('submitted', 'failed', 'canceled');

CREATE INDEX applications_user_status_idx ON applications (user_id, status);
CREATE INDEX applications_status_idx ON applications (status);

-- Form snapshots (captures employer form structure at review time)
CREATE TABLE form_snapshots (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    form_url TEXT NOT NULL,
    form_structure JSONB NOT NULL,  -- field names, types, options, required flags
    structure_hash TEXT NOT NULL,    -- SHA256 of canonical form structure
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX form_snapshots_application_idx ON form_snapshots (application_id);

-- Application events (audit trail)
CREATE TABLE application_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id),
    event_type TEXT NOT NULL,
    event_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX application_events_application_idx ON application_events (application_id, created_at);

-- OTP handling
CREATE TABLE otp_requests (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id),
    requested_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL,
    otp_value TEXT,  -- NULL until user provides
    provided_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX otp_requests_pending_idx ON otp_requests (application_id) 
    WHERE consumed_at IS NULL;

-- Credits and billing
CREATE TABLE credit_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    entry_type TEXT NOT NULL CHECK (entry_type IN ('purchase', 'hold', 'charge', 'release', 'refund', 'welcome_bonus')),
    amount INTEGER NOT NULL,  -- positive for credits added, negative for holds/charges
    balance_after INTEGER NOT NULL,
    reference_id UUID,  -- application_id for hold/charge/release
    stripe_payment_intent_id TEXT,
    idempotency_key TEXT UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX credit_ledger_user_idx ON credit_ledger (user_id, created_at DESC);
CREATE INDEX credit_ledger_reference_idx ON credit_ledger (reference_id);

-- Current balance materialized for fast reads (single source of truth is ledger)
CREATE TABLE user_balances (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    available_credits INTEGER NOT NULL DEFAULT 0 CHECK (available_credits >= 0),
    held_credits INTEGER NOT NULL DEFAULT 0 CHECK (held_credits >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Job queue (Postgres-based)
CREATE TABLE job_queue (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type TEXT NOT NULL CHECK (job_type IN (
        'prepare_application',
        'submit_application', 
        'generate_documents',
        'compute_matches',
        'scrape_board'
    )),
    payload JSONB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'completed', 'failed')) DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    claimed_by TEXT,
    claimed_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    scheduled_for TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX job_queue_pending_idx ON job_queue (priority DESC, created_at)
    WHERE status = 'pending' AND scheduled_for <= NOW();
CREATE INDEX job_queue_processing_idx ON job_queue (claimed_at)
    WHERE status = 'processing';

-- Notifications
CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    notification_type TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    reference_id UUID,
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX notifications_user_unread_idx ON notifications (user_id, created_at DESC)
    WHERE read_at IS NULL;

-- Admin audit log
CREATE TABLE admin_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    admin_user_id UUID NOT NULL REFERENCES users(id),
    action_type TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id UUID NOT NULL,
    action_data JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX admin_actions_created_idx ON admin_actions (created_at DESC);

-- Deletion audit (anonymized record of deletions)
CREATE TABLE deletion_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    original_user_id_hash TEXT NOT NULL,  -- SHA256 of original user ID
    deletion_type TEXT NOT NULL CHECK (deletion_type IN ('user_requested', 'admin')),
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    records_deleted JSONB NOT NULL  -- counts by table, no PII
);
```

---

## 4. Invariant Enforcement Map

| Invariant | Mechanism | Evidence It Works |
|-----------|-----------|-------------------|
| **Truthfulness: every generated claim traces to profile** | Document generation prompt includes only published profile JSON. The prompt template has no access to raw resume text. The LLM output is post-processed: claims are cross-checked against profile keys using a validator function (`validateClaimsAgainstProfile()`) that rejects any assertion not present in profile data. | Test `truthfulness_validator_test.ts`: given a profile with 3 jobs and a generated resume with a fabricated 4th job, the validator returns `{valid: false, violations: [{claim: "...", source: null}]}`. Test `prompt_isolation_test.ts`: mock the prompt builder and assert raw resume text never appears in the prompt string. |
| **No unauthorized submission** | `applications` table `status` column state machine. Transition from `ready` to `submitting` requires `form_answers_final IS NOT NULL`. The `form_answers_final` column is populated only by the `POST /applications/:id/submit` endpoint, which requires authenticated user matching `application.user_id`. | Test `submission_auth_test.ts`: attempt to POST submit on another user's application returns 403. Test `state_machine_test.ts`: attempt to transition `queued` → `submitting` directly throws `InvalidStateTransition`. Integration test: worker job for `submit_application` on an application where `form_answers_final IS NULL` throws and records failure event. |
| **Auto-submit only when explicitly granted and all fields confident** | `applications.auto_submit` flag set only at `POST /applications` create time via explicit `auto_submit: true` param. Worker checks: `if (app.auto_submit && allFieldsConfident(formDraft)) proceed; else set status='ready'`. `allFieldsConfident()` returns false if any required field has `confidence < 0.9` or any sensitive field (consent, EEO-adjacent) is present. | Test `auto_submit_gating_test.ts`: create application with `auto_submit=true`, mock form with one required field at confidence 0.7 → application ends at `ready` not `submitting`. Test `sensitive_field_detection_test.ts`: form with "Do you agree to background check?" field → `allFieldsConfident()` returns false regardless of confidence. |
| **No double-submit** | (1) Partial unique index `applications_one_active_per_job` prevents two non-terminal applications for same user+job. (2) `SELECT ... FOR UPDATE` on application row before submission. (3) `row_version` column with optimistic locking: `UPDATE applications SET status='submitting', row_version=row_version+1 WHERE id=$1 AND row_version=$2` returns 0 rows if concurrent update. (4) Worker idempotency: if form already shows "application received" confirmation, mark submitted without re-filling. | Test `double_submit_race_test.ts`: spawn two worker threads claiming same job simultaneously; exactly one succeeds, other fails with "row version mismatch" or "already submitting". Integration test: manually set application to `submitting`, invoke worker job → worker detects existing attempt and either waits or fails gracefully. |
| **Form-drift detection** | Form structure captured at prepare time as `form_snapshots.structure_hash`. At submit time, worker re-fetches form, computes hash. If `hash_at_submit != form_snapshot.structure_hash`, submission blocked with `form_changed` error, application moves to `ready` for user re-review. | Test `form_drift_test.ts`: mock form returns different fields on second fetch → worker throws `FormDriftError` and application status is `ready`. Integration test with recorded responses: first call returns form v1, second returns form v2 → blocked. |
| **Credit hold atomicity** | `SERIALIZABLE` transaction wrapping: (1) check `user_balances.available_credits >= 1`, (2) decrement `available_credits`, increment `held_credits`, (3) insert `credit_ledger` entry with `entry_type='hold'`, (4) insert application with `status='queued'`. On commit success, application exists with hold. On any failure, no hold and no application. | Test `credit_hold_test.ts`: in transaction, hold 1 credit, rollback → balance unchanged, no ledger entry. Concurrent test: two transactions attempt to hold last credit → exactly one succeeds (serialization failure on other). |
| **Credit charge only on success** | `submit_application` worker job, on confirmed submission: single transaction with `UPDATE credit_ledger SET entry_type='charge' WHERE reference_id=$appId AND entry_type='hold'` then decrement `held_credits` in `user_balances`. On failure/cancel: `UPDATE credit_ledger SET entry_type='release' WHERE reference_id=$appId AND entry_type='hold'` then move credit back to `available`. | Test `credit_charge_test.ts`: submit application, verify ledger shows hold→charge transition, balances reflect charge. Test `credit_release_test.ts`: cancel application, verify hold→release, available_credits restored. |
| **No negative balance** | `CHECK (available_credits >= 0)` on `user_balances`. Hold transaction aborts if this constraint would be violated. | Test `negative_balance_test.ts`: attempt hold with 0 credits → `CHECK constraint violated`, transaction rolls back. |
| **Idempotent webhook handling** | `credit_ledger.idempotency_key` column with UNIQUE constraint. Stripe webhook handler extracts event ID, uses as idempotency key. Insert-or-ignore pattern: `INSERT INTO credit_ledger (..., idempotency_key) VALUES (...) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`. If RETURNING is empty, webhook was replay. | Test `webhook_replay_test.ts`: send same Stripe event twice → first inserts ledger entry, second no-ops, balance unchanged after second. |
| **EEO quarantine** | `user_eeo_data` table is separate from `profile_versions`. No foreign key from `profile_versions` to `user_eeo_data`. EEO data access is via separate DAO (`EeoDao`) never injected into AI-calling services. Form filler uses exact-match lookup: `SELECT data->>$optionText FROM user_eeo_data WHERE user_id=$1`, only fills if option text matches exactly. | Test `eeo_isolation_test.ts`: mock AI service receives profile data, assert `eeo` key never present. Integration test: form with "Gender" dropdown, user has gender='Male' in EEO → field filled. Form has "Gender" dropdown with different options ('M', 'F', 'Other') → field left blank for user review. Code review: grep for `user_eeo_data` access paths, verify all are in `EeoDao` and `EeoDao` is never imported in AI modules. |
| **Profile version pinning** | `applications.profile_version_id` references `profile_versions.id`. At application create, this captures the current published profile version. All document generation and form filling uses `SELECT data FROM profile_versions WHERE id = $profile_version_id`, never "latest" lookup. | Test `profile_pinning_test.ts`: create application, then publish new profile version, then generate documents → documents use original profile data, not new. |
| **Crash recovery: stuck jobs reclaimed** | Reaper query runs every 60 seconds: `UPDATE job_queue SET status='pending', claimed_by=NULL, claimed_at=NULL, attempts=attempts+1 WHERE status='processing' AND claimed_at < NOW() - INTERVAL '10 minutes'`. Jobs exceeding `max_attempts` move to `failed`. | Test `job_reclaim_test.ts`: insert job, set status='processing' with claimed_at 11 minutes ago, run reaper → job status is 'pending', attempts incremented. Test `max_attempts_test.ts`: job at attempts=max_attempts, run reaper → job status is 'failed'. |
| **Crash recovery: application state** | Worker writes `application_events` row before each major step (entering `preparing`, fetching form, generating documents, entering `submitting`, filling each field, submitting). On reclaim, worker reads events to determine resume point. If last event was `filling_field:employment_history` and form state is unknown, worker re-fetches form and re-fills (idempotent for browser; employer sees one submission). | Test `application_recovery_test.ts`: simulate crash by killing worker mid-prepare, restart worker, application reaches `ready`. Integration test: crash mid-submit, form is partially filled, restart → worker detects partial fill via form inspection, completes submission (if form accepts) or resets and re-fills (if form allows). |
| **OTP flow doesn't block resources** | When OTP requested: (1) application moves to `awaiting_otp`, (2) browser context is released to pool (session state saved via `browserContext.storageState({path})`), (3) `otp_requests` row created with 5-minute expiry. When user provides OTP: (4) job enqueued to resume submission. Worker acquires new context, restores session state, enters OTP, continues. If OTP expires: (5) application moves to `failed`, error message explains. | Test `otp_release_test.ts`: trigger OTP state, verify browser context count returns to max available. Test `otp_timeout_test.ts`: create OTP request, wait 6 minutes, run reaper → application status is 'failed'. Integration test: mock OTP flow end-to-end with real browser, verify session continuity. |

---

## 5. Failure-Mode Walkthrough

### 5.1 Machine Dies Mid-Submission

**Scenario:** Worker is executing `submit_application` job. It has filled the form and is about to click submit. The machine loses power.

**What happens:**
1. PostgreSQL connection drops. The open transaction (if any) rolls back. No data corruption.
2. The `applications` row remains at `status = 'submitting'` (this was committed in a prior transaction when the worker claimed the job).
3. The `job_queue` row remains at `status = 'processing'` with `claimed_at` timestamp.
4. The Docker container does not restart (machine is off).
5. On machine boot, Docker Compose restarts all containers.
6. Worker starts, but does not immediately reclaim jobs (it processes new claims first).
7. After 60 seconds, the reaper query runs: `UPDATE job_queue SET status='pending', claimed_by=NULL, claimed_at=NULL, attempts=attempts+1 WHERE status='processing' AND claimed_at < NOW() - INTERVAL '10 minutes'`.
8. If the job was stuck for >10 minutes (machine was down that long), it's reclaimed. If <10 minutes, it remains in `processing` until the next reaper run.
9. When reclaimed, worker picks up the job. It loads the application row and sees `status = 'submitting'`.
10. Worker fetches the application URL in a fresh browser context. It looks for "application received" confirmation (site-specific selectors in `board_config`).
11. **If confirmation found:** Application was actually submitted before crash. Worker marks `status = 'submitted'`, records `submission_proof_file_id` with screenshot.
12. **If no confirmation and form is blank:** Previous attempt didn't submit. Worker re-fills form from `form_answers_final` and submits. This is safe because `form_answers_final` is immutable post-review.
13. **If form shows partial data:** Worker compares visible values against `form_answers_final`. If match, continues to submit. If mismatch (form drift), blocks and sets `status = 'ready'` for user re-review.

**Evidence:** `application_events` shows: `status_change:submitting` at time T1, `job_reclaimed` at time T2 (T2 - T1 > 10 min), then either `submission_confirmed` or `refilled_and_submitted` or `form_drift_detected`. The browser trace file (recorded via `page.video.path()` if enabled in recovery mode) shows the recovery flow.

### 5.2 Same Job Runs Twice (Duplicate Claim)

**Scenario:** Due to a bug or race condition, two worker threads both try to claim the same `job_queue` row.

**What happens:**
1. Both workers execute: `SELECT * FROM job_queue WHERE status='pending' AND scheduled_for <= NOW() ORDER BY priority DESC, created_at LIMIT 1 FOR UPDATE SKIP LOCKED`.
2. `FOR UPDATE SKIP LOCKED` means the second worker's query skips any row locked by the first. The second worker either gets a different job or no job.
3. If somehow both got the same job ID (impossible with SKIP LOCKED, but for completeness):
4. The first worker executes: `UPDATE job_queue SET status='processing', claimed_by=$worker_id, claimed_at=NOW() WHERE id=$job_id AND status='pending'`. Returns 1 row affected.
5. The second worker executes the same UPDATE. Returns 0 rows affected (status is already 'processing'). Second worker sees 0 rows, logs "job already claimed", and moves on.

**Evidence:** No second submission occurs. Worker logs show "job already claimed by {other_worker_id}". `job_queue` row shows single `claimed_by` value.

### 5.3 LLM Returns Garbage or Times Out

**Scenario:** During document generation, the AI API call returns malformed JSON, refuses to answer, or exceeds the 30-second timeout.

**What happens:**
1. AI service wrapper has try-catch around `anthropic.messages.create()`. Timeout is set via Anthropic SDK's `timeout` option: 30,000 ms.
2. **Timeout:** `APIConnectionError` thrown. Caught, logged as `ai_timeout`. Job increments `attempts` and is rescheduled with exponential backoff: `scheduled_for = NOW() + (2^attempts * 30 seconds)`.
3. **Malformed JSON:** Response is run through `JSON.parse()`. If it throws, logged as `ai_malformed_response`. Same retry path.
4. **Refusal (LLM says "I can't help with that"):** Response parsed successfully but required fields missing. Validator function (`validateGeneratedResume()`) returns `{valid: false, errors: [...]}`. Logged as `ai_refusal`. Same retry path.
5. After 3 attempts, job moves to `failed`. Application status moves to `failed` with error message "Document generation failed after multiple attempts. Please try again or contact support."
6. Notification sent to user.
7. Credit hold is released (no charge).

**Evidence:** `job_queue` row shows `attempts=3`, `status='failed'`, `error_message="..."`. `application_events` shows 3 `ai_call_failed` events with timestamps and error types. `credit_ledger` shows `hold` entry followed by `release` entry.

### 5.4 Employer Form Changes After Review

**Scenario:** User reviews and approves their application. Before the worker submits, the employer updates their application form (adds a new required field).

**What happens:**
1. User submits review via `POST /applications/:id/submit`. This sets `form_answers_final` and enqueues `submit_application` job.
2. Worker claims job. It loads the application, including `form_snapshot.structure_hash` (captured at prepare time).
3. Worker navigates to employer form URL. It extracts current form structure and computes `current_hash = sha256(canonicalFormStructure)`.
4. Worker compares: `current_hash !== form_snapshot.structure_hash`.
5. Condition true → `FormDriftError` thrown.
6. Transaction: `UPDATE applications SET status='ready', error_message='The employer updated their application form. Please review your answers.', form_snapshot_id=NULL WHERE id=$id`.
7. New `form_snapshot` row created with current structure.
8. `application_events` row: `{event_type: 'form_drift_detected', event_data: {old_hash, new_hash, diff: [...]}}`.
9. Notification sent: "Application form changed. Please review your updated answers."
10. User must re-review. `form_answers_final` is cleared; they see new form with their previous answers pre-filled where fields match, blank where fields are new.

**Evidence:** `application_events` shows `form_drift_detected` with old/new hashes. Application status is `ready`. Two `form_snapshots` rows exist for this application with different `structure_hash` values.

### 5.5 Stripe Webhook Replayed

**Scenario:** Stripe sends the same `payment_intent.succeeded` webhook twice (network retry, or replay attack attempt).

**What happens:**
1. First webhook arrives. Handler extracts `event.id` (e.g., `evt_1234`).
2. Handler executes: `INSERT INTO credit_ledger (user_id, entry_type, amount, balance_after, stripe_payment_intent_id, idempotency_key) VALUES ($1, 'purchase', $2, $3, $4, $event_id) ON CONFLICT (idempotency_key) DO NOTHING RETURNING id`.
3. First time: insert succeeds, RETURNING returns the new row ID. Handler updates `user_balances`.
4. Second webhook arrives. Same `event.id`.
5. Handler executes same INSERT. `ON CONFLICT ... DO NOTHING` fires due to unique `idempotency_key`. RETURNING returns nothing (no row inserted).
6. Handler checks RETURNING result: empty. Logs "duplicate webhook ignored". Returns 200 to Stripe (so it doesn't keep retrying).
7. No balance change on second webhook.

**Evidence:** `credit_ledger` has exactly one row with `idempotency_key = 'evt_1234'`. User balance increased exactly once. Server logs show "duplicate webhook ignored" on second request.

### 5.6 Browser Hangs

**Scenario:** Playwright navigates to an employer's form. The page has a JavaScript infinite loop or never finishes loading.

**What happens:**
1. Worker calls `page.goto(url, {timeout: 30000})`. The 30-second timeout is set explicitly.
2. After 30 seconds, Playwright throws `TimeoutError`.
3. Worker catches error, logs `page_load_timeout`.
4. Worker calls `page.close()` to release resources.
5. Job retry logic: increment attempts, reschedule with backoff.
6. After 3 attempts, job fails.
7. Application status moves to `failed` with message "Could not load employer's application page. Their site may be experiencing issues."
8. Credit hold released.

**Evidence:** `job_queue.error_message` contains "TimeoutError: page.goto". `application_events` shows 3 `page_load_timeout` events. Browser context is released (pool size returns to max).

### 5.7 Worker OOMs During Complex Form

**Scenario:** An employer's form page is extremely JavaScript-heavy. The Chromium process exceeds memory limits.

**What happens:**
1. Docker's cgroup kills the Chromium process. Node.js worker process may survive or may die (depends on how the OOM manifests).
2. **If worker survives:** Playwright detects browser crash, throws `TargetClosedError`. Worker catches, logs `browser_crashed`. Worker must restart its browser context pool. Job retry logic applies.
3. **If worker dies:** Container restarts via Docker `restart: unless-stopped`. Worker's `processing` jobs are orphaned.
4. Reaper query reclaims after 10 minutes.
5. On retry, worker tries again with same form. If OOM is reproducible, job fails after max attempts.
6. Application marked `failed` with "The employer's form is too complex for our system. Please apply directly."
7. This is a known limitation; the failure message is honest.

**Evidence:** Docker logs show `OOMKilled` or container restart. `job_queue` shows `attempts` incrementing. After 3 attempts, `status='failed'` with appropriate message.

---

## 6. AI Strategy

### 6.1 Model Selection

| Task | Model | Reasoning |
|------|-------|-----------|
| Resume extraction (PDF/DOCX → JSON) | Claude Haiku 4.5 | High-volume, relatively simple structured output. Haiku is 10× cheaper than Sonnet and sufficient for extraction. |
| Job description parsing | Claude Haiku 4.5 | Bulk task, runs on every ingested job. Simple extraction. |
| Match scoring + explanation | Claude Sonnet 4 | Needs nuanced reasoning about fit, red flags, and gaps. Quality matters for user trust. |
| Resume tailoring | Claude Sonnet 4 | Core product differentiator. Must understand emphasis, ordering, relevance. |
| Cover letter generation | Claude Sonnet 4 | Needs strong writing quality. |
| Form field drafting | Claude Sonnet 4 | Needs to interpret ambiguous field labels, match profile data appropriately. |
| Confidence scoring | Rules-based + Claude Haiku 4.5 | Most confidence is deterministic (did we find an exact match?). Haiku confirms edge cases. |

### 6.2 Prompt Architecture

Each AI task has a prompt template with:
- **System prompt:** Role definition, output format, constraints (JSON schema for structured output, truthfulness rules)
- **Cached prefix:** Profile JSON + job description (reused across resume, cover letter, form filling)
- **Task-specific suffix:** What to generate this call

Example for resume tailoring:
```
System: You are a professional resume writer. Output JSON matching the ResumeSchema.
You MUST NOT include any experience, skill, or credential not present in the profile.
If asked to emphasize something the candidate lacks, acknowledge the gap honestly.

[CACHED PREFIX START]
PROFILE:
{profileJson}

JOB DESCRIPTION:
{jobDescription}
[CACHED PREFIX END]

Generate a tailored resume that emphasizes relevant experience for this position.
Output JSON only, no explanation.
```

### 6.3 Truthfulness Validation

The truthfulness audit has three layers:

**Layer 1: Prompt constraint.** System prompt explicitly forbids fabrication. This catches most cases but is not reliable alone.

**Layer 2: Structural validation.** Generated resume JSON must reference profile data by key. The generator outputs:
```json
{
  "experience": [
    {
      "company": "Acme Corp",
      "source_path": "profile.work_history[0].company",
      "dates": "2020-2023",
      "source_path_dates": "profile.work_history[0]"
    }
  ]
}
```
Validator function checks each `source_path` actually exists and matches.

**Layer 3: Semantic similarity.** For free-text fields (bullet points), we compare generated text against source text using embedding similarity. If generated bullet has <0.7 cosine similarity to any profile text, flag for review. Implementation: Claude's embedding model or sentence-transformers/all-MiniLM-L6-v2 locally. Flagged items are logged and reviewed in weekly quality audits; false positives tune the threshold.

### 6.4 Quality Measurement

**Ranking quality:**
- Golden set: 50 manually-rated profile-job pairs with expected scores (1-10) and explanation quality (acceptable/unacceptable).
- Nightly regression test computes model scores, measures MAE against golden set. Threshold: MAE < 1.5. Alert if exceeded.
- Monthly refresh: sample 10 new pairs from production, have me rate them, add to golden set.

**Document quality:**
- Truthfulness violation rate: count of Layer 2/3 failures per 1000 documents. Target: <1%. Measured weekly from production logs.
- User edit rate: percentage of users who edit generated resume before submission. Baseline established in first month; significant increase triggers investigation.

**Form filling quality:**
- Confidence calibration: of fields marked >90% confidence, what percentage did user change? Target: <5% change rate. If higher, confidence model is overconfident.
- Miss rate: required fields left blank that should have been fillable. Sampled manually from support tickets.

### 6.5 Cost Model

**Per-application cost breakdown (typical case):**

| Step | Model | Input tokens | Output tokens | Input cost | Output cost | Total |
|------|-------|-------------|---------------|------------|-------------|-------|
| Resume extraction | Haiku | 3,000 | 1,500 | $0.0003 | $0.0006 | $0.0009 |
| Job parsing (amortized) | Haiku | 2,000 | 500 | $0.0002 | $0.0002 | $0.0004 |
| Match score | Sonnet (cached) | 4,000 (90% cached) | 500 | $0.0006 | $0.0030 | $0.0036 |
| Resume tailoring | Sonnet (cached) | 5,000 (90% cached) | 2,000 | $0.00075 | $0.0120 | $0.0128 |
| Cover letter | Sonnet (cached) | 5,500 (90% cached) | 800 | $0.00083 | $0.0048 | $0.0056 |
| Form filling (15 fields) | Sonnet (cached) | 6,000 (90% cached) | 500 | $0.0009 | $0.0030 | $0.0039 |
| **Total** | | | | | | **$0.027** |

Prices used (Claude 3.5 Sonnet current pricing): Sonnet $3/$15 per 1M tokens input/output, Haiku $0.25/$1.25, cached input 90% discount.

**Worst case (long resume, complex job, 40 fields, one regeneration):**
~$0.06 per application. Still well under $0.10 budget.

**Cost cap enforcement:**
- `application_ai_cost` column tracks cumulative AI spend per application.
- Each AI call checks: `if (current_cost + estimated_call_cost > $0.15) throw CostLimitExceeded`.
- $0.15 is hard cap (50% buffer over worst case). Application fails with "This job's complexity exceeds our processing limits."

### 6.6 Caching Strategy

Anthropic prompt caching discounts input tokens when the prompt prefix repeats within 5 minutes. Strategy:
- Profile + job description is the cached prefix (stable across resume, cover letter, form filling calls).
- These calls happen within seconds of each other during preparation, so all share cache.
- Match scoring happens on-demand; cache hit depends on recency of last user view. Accepted.

Local caching:
- Job parsing results cached in `jobs.description_structured` (parse once per job, not per user).
- Match scores cached in `match_scores` table (recompute only when profile version or job changes).

---

## 7. Testing and Release Confidence

### 7.1 Test Pyramid

```
                    /\
                   /  \
                  / E2E \        5 tests
                 / (10 min) \
                /------------\
               /  Integration  \    30 tests
              /    (3 min)      \
             /--------------------\
            /     Unit tests       \   200+ tests
           /       (30 sec)         \
          /----------------------------\
```

### 7.2 Unit Tests

**What they cover:**
- All business logic functions in isolation
- State machine transitions (`ApplicationStateMachine.transition()`)
- Validators (`validateClaimsAgainstProfile()`, `validateFormAnswers()`)
- Credit ledger operations (hold, charge, release math)
- Serializers/deserializers for all API contracts
- Hash computation for form drift detection

**Framework:** Vitest (fast, TypeScript-native, built-in mocking).

**Every PR blocks on:** All unit tests pass. Coverage threshold: 80% line coverage on `src/domain/**`. Coverage measured by `vitest --coverage`.

### 7.3 Integration Tests

**What they cover:**
- Database operations (all DAOs against real Postgres in Docker)
- Job queue claim/complete/fail cycles
- API endpoint request/response contracts
- Stripe webhook handling with test mode webhooks
- AI service wrappers with mocked HTTP (recorded responses via `nock`)

**Environment:** Docker Compose spins up Postgres. Tests run against real schema.

**Every PR blocks on:** All integration tests pass.

### 7.4 E2E Tests

**What they cover:**
- Critical user journeys against real browser (Playwright Test)
- Signup → upload resume → view matches → queue application → review → cancel/submit
- Form filling against mock employer site (local HTML serving known form structures)
- Credit purchase flow with Stripe test mode

**Employer mock:** A local Express server serves Greenhouse-style forms from fixtures. Tests verify correct field population.

**Run frequency:** Nightly + before any production deploy. Not on every PR (too slow at 10+ minutes).

### 7.5 Invariant-Specific Tests

**No-double-submit (concurrent simulation):**
```typescript
// test/invariants/no-double-submit.test.ts
test('concurrent submit attempts result in exactly one submission', async () => {
  const app = await createApplicationInReadyState();
  const results = await Promise.all([
    submitApplicationAsWorker(app.id, 'worker-1'),
    submitApplicationAsWorker(app.id, 'worker-2'),
  ]);
  const successes = results.filter(r => r.status === 'submitted');
  expect(successes).toHaveLength(1);
  const failures = results.filter(r => r.status === 'already_submitting');
  expect(failures).toHaveLength(1);
});
```

**Crash recovery simulation:**
```typescript
// test/invariants/crash-recovery.test.ts
test('application stuck in submitting is recovered after reaper runs', async () => {
  const app = await createApplicationInSubmittingState();
  await setJobClaimedAt(app.jobId, minutesAgo(15));
  await runReaper();
  const job = await getJob(app.jobId);
  expect(job.status).toBe('pending');
  expect(job.attempts).toBe(1);
});

test('recovered submission detects already-submitted and completes', async () => {
  const app = await createApplicationInSubmittingState();
  mockEmployerSite.setConfirmationVisible(true); // "Application received"
  await runWorker();
  const updated = await getApplication(app.id);
  expect(updated.status).toBe('submitted');
});
```

**Credit atomicity:**
```typescript
// test/invariants/credit-atomicity.test.ts
test('hold and application create are atomic', async () => {
  const user = await createUserWithCredits(1);
  db.on('query', (q) => {
    if (q.includes('INSERT INTO applications')) {
      throw new Error('simulated crash');
    }
  });
  await expect(queueApplication(user.id, jobId)).rejects.toThrow();
  const balance = await getBalance(user.id);
  expect(balance.available).toBe(1);
  expect(balance.held).toBe(0);
});
```

### 7.6 Pre-Production Checklist

Before first user:
1. All unit tests pass (CI gate)
2. All integration tests pass (CI gate)
3. All E2E tests pass (manual trigger)
4. Crash recovery test passes: manually kill worker mid-submit, verify recovery
5. Double-submit test passes: trigger concurrent submits via test script
6. Form drift test passes: change mock form between review and submit
7. Credit race test passes: concurrent hold attempts on last credit
8. Restore test passes: drop database, restore from backup, verify data
9. Load test passes: 50 concurrent match-score requests complete in <5s p99

---

## 8. Delivery Phases

### Phase 0: Local Foundation

**What gets built:**
- Project scaffold: TypeScript, Hono, Docker Compose, Vitest
- PostgreSQL schema (all tables in §3)
- User auth: signup, login, session management
- Basic web UI shell (layout, navigation, no features yet)

**Exit criteria:**
- `docker-compose up` starts web server + postgres
- User can sign up with email/password
- User can log in and see empty dashboard
- Session persists across page refreshes
- All schema constraints verified by attempting invalid inserts

### Phase 1: Profile System

**What gets built:**
- Resume upload (PDF/DOCX accepted)
- AI extraction to profile draft
- Profile editing UI (all fields editable)
- Profile versioning (draft → publish)
- User preferences form
- EEO data collection (separate UI, separate storage)

**Exit criteria:**
- Upload a PDF resume; see extracted profile draft
- Edit any field in the draft
- Publish the draft; confirm `profile_versions` row with `is_draft=false`
- Publish again; confirm version_number incremented
- Query `profile_versions` directly: v1 and v2 both contain their original data
- EEO data saved; confirm it's in `user_eeo_data`, not in profile JSON

### Phase 2: Job Discovery

**What gets built:**
- Company board seeding (manual admin entry)
- Scraper process: navigate board, extract job URLs
- Job page parsing: extract structured data
- Job lifecycle: new/updated/closed tracking
- Admin UI: view boards, trigger scrape, see job counts

**Exit criteria:**
- Add a Greenhouse board URL in admin
- Trigger scrape; see jobs appear in database
- Re-scrape; existing jobs show `last_seen_at` updated
- Remove a job from the test board; re-scrape; job shows `closed_at`

### Phase 3: Matching and Feed

**What gets built:**
- Match scoring AI call
- Explanation generation
- Match score caching (`match_scores` table)
- User feed UI: ranked jobs with explanations
- Filters: remote, location, salary
- Dismiss/bookmark functionality
- Shareable filtered feed URL

**Exit criteria:**
- User with published profile sees ranked job feed
- Each job shows explanation of fit
- Apply filters; URL updates; reload shows same filters
- Dismiss a job; it disappears; visit dismiss list; restore it; it reappears
- Second user sees same job, no cross-user data leakage

### Phase 4: Document Generation

**What gets built:**
- Resume tailoring AI pipeline
- Cover letter generation
- Truthfulness validator
- PDF generation via Typst
- DOCX generation
- Download endpoints
- Regenerate functionality
- Async progress UI (SSE updates)

**Exit criteria:**
- From a job match, click "Generate Documents"
- See progress indicator; eventually see "Ready"
- Download PDF resume; it's professionally formatted, contains only profile data
- Download DOCX cover letter; opens in Word correctly
- Regenerate; new version produced; previous version still accessible
- Test with non-ASCII name (accents, CJK); renders correctly

### Phase 5: Application Preparation

**What gets built:**
- Queue application endpoint
- `prepare_application` worker job
- Form fetching via Playwright
- Form structure extraction
- Form snapshot storage
- Field-by-field AI drafting
- Confidence scoring
- Review UI: show form preview with answers
- Edit any field in review UI
- Auto-submit gating (confident fields + sensitive field detection)

**Exit criteria:**
- Queue an application for a test job
- See status move: queued → preparing → ready
- Review screen shows all fields with drafted answers
- Edit a field; save; the edit persists
- Required field left blank shows required indicator
- Form with "criminal history" question triggers sensitive-field flag

### Phase 6: Submission (First E2E)

**What gets built:**
- Submit endpoint (user authorization)
- `submit_application` worker job
- Form filling via Playwright
- Confirmation detection
- Submission proof screenshot
- Form drift detection
- Status: ready → submitting → submitted
- Failure handling and retry
- Cancel at any stage

**This is the earliest end-to-end run.**

**Why Phases 0-5 come first:**
- Phase 0: Can't build anything without scaffold
- Phase 1: Submission requires profile (source of truth)
- Phase 2: Submission requires job to apply to
- Phase 3: Submission requires knowing which job to apply to
- Phase 4: Submission requires tailored documents
- Phase 5: Submission requires prepared form answers

**Exit criteria:**
- Full flow: signup → upload → profile → match → queue → prepare → review → submit
- Test against mock employer form: application submitted, confirmation detected
- `application_events` shows full audit trail
- Screenshot proof stored in files table
- Cancel mid-prepare: status moves to canceled
- Cancel mid-submit: status moves to canceled (if possible), failed (if already submitted)
- Crash worker mid-submit, restart: application recovers

### Phase 7: OTP Flow

**What gets built:**
- OTP detection in employer flow
- Session parking (`browserContext.storageState`)
- `otp_requests` table and UI
- OTP entry UI
- Resume submission with OTP
- Timeout handling

**Exit criteria:**
- Test against mock employer that requires OTP
- Application pauses, user sees "Enter verification code"
- User enters code, submission continues
- Session continuity verified: employer form shows same state
- OTP expires after 5 minutes; application fails with clear message

### Phase 8: Credits and Billing

**What gets built:**
- Stripe integration (payment intents)
- Credit pack purchase UI
- Welcome bonus on signup
- Hold on queue, charge on success, release on failure
- Ledger UI (transaction history)
- Balance display
- Webhook endpoint with idempotency

**Exit criteria:**
- New user has welcome bonus (5 credits)
- User can purchase 10-credit pack via Stripe test mode
- Balance increases; ledger shows purchase
- Queue application: balance decreases (hold)
- Successful submit: held credit charged
- Failed application: held credit released
- Replay webhook: no duplicate credit

### Phase 9: Notifications and Polish

**What gets built:**
- Notification system (in-app)
- SSE for real-time updates
- Unread indicator
- Account settings
- Data export (JSON + files ZIP)
- Account deletion
- Rate limiting
- Error page polish

**Exit criteria:**
- Application ready for review triggers notification
- Notification appears without page refresh
- Export downloads ZIP with profile JSON and all documents
- Delete account: user row has `deleted_at`, files removed from disk
- `deletion_audit` row created
- Rapid requests (>100/min) return 429

### Phase 10: Operations and Launch

**What gets built:**
- Admin dashboard: queue depth, error rates, worker health
- Backup automation (pg_dump to encrypted offsite)
- Restore runbook (documented, tested)
- Monitoring: basic health endpoint + Uptime Robot
- Alert on: queue depth >50, error rate >5%, backup failure
- Rate limiting refinement
- Security audit (dependency scan, header review)

**Exit criteria:**
- Admin dashboard shows live stats
- Backup runs automatically; verify by checking offsite storage
- Restore runbook executed on a fresh machine; data loads
- Trigger deliberate error; alert fires within 5 minutes
- Dependency scan shows no critical vulnerabilities
- Security headers present: CSP, HSTS, X-Frame-Options

---

## 9. Security and Privacy

### 9.1 Authentication

- Passwords hashed with Argon2id (node:argon2 package, parameters: memoryCost 65536, timeCost 3, parallelism 4)
- Sessions stored in `sessions` table, 256-bit random token, SHA-256 hashed for storage
- Session cookie: `HttpOnly`, `Secure`, `SameSite=Strict`, 7-day expiry with rolling renewal
- CSRF protection: `SameSite=Strict` cookie + check `Origin` header on mutations
- No "remember me" checkbox; session simply expires

### 9.2 Authorization

- User can only access their own data: all queries include `WHERE user_id = $session_user_id`
- Admin role: boolean flag on `users` table. Checked via middleware `requireAdmin()`
- Admin can view any user's application (for support), cannot submit on their behalf
- Admin cannot access EEO data (no query path exists)

### 9.3 Tenant Isolation

- All tables have `user_id` column; composite indexes include it
- No query ever fetches cross-user data except: jobs (shared), company_boards (shared)
- Test: integration test attempts to access another user's application → 404

### 9.4 Secrets

- Database password: environment variable, not in code
- Stripe keys: environment variable
- AI API keys: environment variable
- All env vars loaded from `/app/.env` at container start
- `.env` file encrypted at rest (LUKS volume)
- Secrets never logged (application-level: strip from any logged request/response)

### 9.5 Encryption at Rest

- Volume mounted at `/data` is LUKS-encrypted
- Passphrase stored on separate USB key or entered at boot (operator choice)
- Without passphrase, disk contents are indistinguishable from random
- This covers: database files, uploaded resumes, generated documents, browser traces

### 9.6 Webhook Verification

- Stripe webhooks verified via `stripe.webhooks.constructEvent(payload, sig, webhookSecret)`
- Webhook endpoint rejects unsigned/invalid requests with 400
- Rate limited: 100 requests/minute per IP (legitimate Stripe traffic is <10/min)

### 9.7 Rate Limiting

| Endpoint category | Limit | Window | Scope |
|-------------------|-------|--------|-------|
| Login attempts | 5 | 15 min | IP + email |
| Password reset | 3 | 60 min | email |
| Resume upload | 10 | 60 min | user |
| Application queue | 20 | 60 min | user |
| AI generation | 30 | 60 min | user |
| General API | 200 | 60 min | user |
| Unauthenticated | 50 | 60 min | IP |

Implementation: in-memory sliding window (Map of timestamps). If process restarts, limits reset (acceptable; brief window of abuse possible).

### 9.8 Input Validation

- All user input validated at API boundary (Zod schemas)
- File uploads: MIME type checked, size limited (10 MB)
- SQL: all queries parameterized (pg driver's native parameterization)
- HTML: output escaped via Handlebars default escaping
- JSON API responses: no HTML, so no XSS concern

### 9.9 Dependency Security

- `npm audit` run on CI; fail build on critical/high
- Dependabot enabled for security updates
- Container base image: `node:22-slim` (minimal attack surface)
- No unnecessary system packages installed

---

## 10. Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|------------|--------|------------|
| 1 | Employer sites change their forms frequently, breaking automation | High | High | Form structure versioning; generic field-type handlers (text, dropdown, checkbox, file) rather than site-specific code; fallback to "form_drift" error with graceful user message. Phase 1 monitoring: track drift rate per board, deprioritize high-churn boards. |
| 2 | Bot detection by employer ATS blocks our applications | Medium | Critical | Playwright stealth plugin (default); realistic timing (randomized delays 100-500ms between actions); residential IP via proxy for high-security sites (Bright Data, ~$15/GB, used only when direct access fails). Escalation path documented for when stealth fails. |
| 3 | AI hallucinates credentials not in profile | Medium | Critical | Three-layer truthfulness validation (§6.3). Layer 2 (structural) catches most cases; Layer 3 (semantic) catches paraphrasing. Weekly audit of 10 random generated documents. First confirmed hallucination reaching production triggers incident review. |
| 4 | LLM costs spike due to prompt injection or abuse | Low | Medium | Per-application cost cap ($0.15). Per-user daily cap ($5). Prompt hardening: user-provided content wrapped in XML tags, system prompt instructs to ignore instructions in content. Alert on unusual spend patterns (>3σ from mean). |
| 5 | Worker OOM on complex employer forms | Medium | Low | Memory limit enforced (8 GB container). If OOM, application fails with honest message. Instrumentation: log peak memory per job; if consistently near limit, scale up hardware. |
| 6 | Stripe webhook mishandling causes credit discrepancies | Low | Medium | Idempotency via event ID (§5.5). Daily reconciliation job: sum ledger entries vs. balance table, alert on mismatch. Stripe dashboard as ground truth for disputes. |
| 7 | Data loss from disk failure | Low | Critical | Daily pg_dump to encrypted offsite (Backblaze B2, ~$0.50/month). Tested restore procedure quarterly. RAID would help but single Hetzner machine has single disk; mitigated by backup frequency. |
| 8 | Session hijacking via XSS | Low | High | HttpOnly cookies (no JS access). CSP header: `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'`. No inline scripts except htmx (included from own domain). |
| 9 | Employer reports our submissions as spam, damages reputation | Medium | High | Per-employer cooldown: 1 submission per user per employer per 24h. No bulk application features. If an employer complains: immediate board removal, apology email to user. |
| 10 | Single point of failure (one machine) | Medium | Medium | Accepted for v1. Daily backups mitigate data loss. Restore procedure takes ~2 hours (spin up new machine, restore, DNS update). Documented runbook. Revenue exceeding $1000/month triggers second-machine planning. |

---

## 11. Explicit Tradeoffs

### 11.1 No Real-Time Status Updates

**Brief implies:** "Live-feeling status updates"

**What we deliver:** SSE with 500ms polling interval for event queries. This means updates can lag by up to 500ms. True WebSocket push would be more immediate.

**Why:** WebSocket connection management adds complexity (reconnection, state sync, scaling concerns). At 200 WAU with <20 concurrent users, the 500ms lag is imperceptible. Revisit if user feedback indicates the latency is noticeable.

### 11.2 OTP Entry Has 5-Minute Window

**Brief implies:** "User enters the code within a few minutes"

**What we deliver:** Exactly 5-minute timeout. If user is slow, application fails.

**Why:** Holding application state while waiting for OTP complicates recovery. The browser session is serialized, but form state may change if we wait too long. 5 minutes is generous for "check email, copy code" workflow. Configurable per-user would add complexity without clear benefit.

### 11.3 Crash Recovery May Re-Fill Already-Filled Form

**Brief implies:** "Interrupted work must be recovered automatically"

**What we deliver:** On crash recovery, if form state is unclear, worker re-fills all fields (including already-filled ones) before submitting. If employer form doesn't allow overwriting, this could cause issues.

**Why:** Detecting partial fill state across arbitrary forms is unreliable. Re-filling with same data is idempotent for most forms. Risk is low; specific form issues can be addressed with per-board configuration.

### 11.4 Match Explanations Are AI-Generated, Not Rule-Based

**Brief implies:** Explanation of fit with accountability

**What we deliver:** LLM generates natural-language explanation. No formal rule engine.

**Why:** Rule-based explanations ("You have 3/5 required skills") require structured requirement extraction that's brittle. LLM explanations are more nuanced ("Your experience leading backend teams aligns well with their 'senior engineer' requirement, though they mention Rust expertise you don't list"). Quality is measured against golden set (§6.4).

### 11.5 No Subscription Billing, Only Credit Packs

**Brief mentions:** "Credit packs or a subscription"

**What we deliver:** Credit packs only at launch.

**Why:** Subscription adds recurring billing complexity (renewal failures, cancellation, proration). Credit packs are simpler and map directly to per-submission value. Subscription can be added later if users request it.

---

## 12. Where This Is Stronger Than Required

### 12.1 Profile Versioning Is Immutable, Not Just Traceable

**Brief requires:** "Which version of the profile was this based on, and what did it say?"

**What we deliver:** Profile versions are immutable (`profile_versions` rows never updated). Every historical version is retrievable forever.

**Why:** Immutability is simpler to implement than auditable mutation. It also future-proofs for potential requirements like "show me my profile as of 6 months ago" or "compare document to profile side-by-side."

### 12.2 Form Snapshot Includes Full Structure, Not Just Hash

**Brief requires:** Detect when form changes between review and submission

**What we deliver:** Full form structure stored in `form_snapshots.form_structure` (JSONB), plus hash for comparison.

**Why:** Storing full structure enables debugging ("which fields changed?"), supports form re-display in review UI without re-fetching, and enables future features like form change diff display.

### 12.3 EEO Data Has Zero AI Exposure by Architecture

**Brief requires:** "Never sent to any AI model"

**What we deliver:** EEO data is in a separate table with a separate DAO. The AI service modules have no import path to `EeoDao`. This is enforced architecturally, not just by policy.

**Why:** Policy can be violated by accident ("oops, I included user.eeo in the context"). Architecture cannot (without code change that would show in review).

### 12.4 Full Audit Trail of Automation Actions

**Brief requires:** "Full audit trail of what the automation did"

**What we deliver:** `application_events` captures every state transition, form field filled, browser navigation, and error. Browser sessions can optionally be recorded (`page.video.start()`).

**Why:** Support debugging needs detailed logs. When a user says "your system submitted wrong info," we can prove exactly what was sent. Video recording is optional (storage cost) but available for disputed cases.

---

## 13. Assumptions

| # | Assumption | Where It Appears | Rationale |
|---|------------|------------------|-----------|
| A1 | Greenhouse-style ATS forms are standard HTML forms without anti-bot protections beyond basic fingerprinting | §1.5, §5.1 | Greenhouse documentation shows standard form submission. Lever, Workday similar. If specific boards have CAPTCHAs, we deprioritize them (scrape job info but don't automate apply). |
| A2 | Users have email access to receive OTP codes within 2 minutes | §7 Phase 7, §11.2 | OTP flows assume user is present. If user is away from email, submission fails. This is acceptable; user was warned when they clicked "submit." |
| A3 | Resume files are <10 MB | §9.8 | Typical resume PDF is 100-500 KB. DOCX similar. 10 MB accommodates portfolios. Larger files rejected at upload. |
| A4 | Employer job pages remain stable for at least 4 hours between scrapes | §2.4 | Jobs don't typically appear and disappear in <4 hours. If they do, our users miss them; acceptable at launch scale. |
| A5 | Single-region hosting (EU or US) is acceptable; users are not globally distributed | §1.10 | Brief doesn't specify. Assumed US-centric launch market. Latency for EU users accessing US server is ~100ms, acceptable for this application. |
| A6 | Anthropic prompt caching is available and provides 90% discount | §6.1, §6.5 | Based on current Anthropic documentation. If caching changes, costs increase ~3×; still within budget but closer to limit. |
| A7 | The operator can enter a disk encryption passphrase at boot | §9.5 | Self-hosted security requires operator presence for cold boot. If automated boot is required, the passphrase would need key management (Vault/HSM), which contradicts the "no Vault" decision. |
| A8 | 200 WAU means <20 concurrent users at peak | §2.2, §11.1 | Standard web concurrency assumption: ~10% of weekly active concurrent at peak. 200 WAU × 10% = 20. If higher, web server scaling is straightforward. |
| A9 | Employers don't rate-limit applications from the same IP | §10 #9 | If they do, submission fails; we'd need residential proxies. Added to risk register but not implemented at launch. |
| A10 | PDF resumes are text-based, not scanned images | §1.7 | Scanned resumes require OCR, which is unreliable and not implemented. If uploaded resume has no extractable text, extraction fails with message "Please upload a text-based PDF or DOCX." |
| A11 | No internationalization at launch; UI is English only | Not explicit | Brief doesn't mention i18n. Building for English-speaking users first. Generated documents handle non-ASCII content; UI does not need translation. |
| A12 | Admin role is trusted; no need for admin action approval workflow | §9.2 | Brief says "narrow admin role." Assumed operator is the only admin and is trusted. |

---

*End of engineering plan.*
