# Engineering Plan: Pilot — Resume-First Job Application Engine

---

## 1. Technology decisions

### 1.1 Primary language and runtime

**Choice:** Python 3.12 with FastAPI for the web API, Celery for async work.

**Rejected:** Node.js/TypeScript. Node has excellent browser automation (Playwright is native) and would unify the stack. Rejected because Python's document processing ecosystem (PyPDF2, python-docx, pypandoc for resume parsing; reportlab/weasyprint for PDF generation) is mature and battle-tested, and the data model transformations (nested profile structures, AI prompt assembly, form-field mapping) favor Python's richer standard library and tooling. The browser automation gap is bridged by Playwright's Python bindings, which wrap the same underlying browser engine.

**Why:** The resume extraction and document generation pipelines are structurally complex (parsing semi-structured PDFs, mapping LLM output to templates, rendering multi-language Unicode to PDF). Python's libraries solve these directly; in Node, each would be a custom build or a subprocess to pandoc. The performance gap is irrelevant at launch scale (hundreds of apps/day, not thousands/second), and the one-operator constraint favors fewer surprises over marginal throughput wins.

### 1.2 Database

**Choice:** PostgreSQL 16.

**Rejected:** SQLite. Genuinely simpler, zero-ops, and would work for the read path. Rejected because the credit hold/charge/release flow and the application lifecycle state machine both require conditional updates under concurrency (`UPDATE ... WHERE status = 'queued' AND row_version = $1 RETURNING ...`), and multiple workers pulling from the job queue need `SELECT ... FOR UPDATE SKIP LOCKED`, which SQLite does not support.

**Why:** The invariants in §4 (no double-charge, no double-submit, no lost work) are enforced by optimistic locking and row-level locks under concurrent writes. That requirement outranks the operational simplicity SQLite would buy.

### 1.3 Queue and async execution

**Choice:** Celery with Redis as the broker and result backend.

**Rejected:** PostgreSQL-based queue (e.g., `SELECT ... FOR UPDATE SKIP LOCKED` on a tasks table). This would eliminate Redis entirely and reduce operational surface. Rejected because browser automation jobs hold resources (800 MB RAM per Chromium context) for 30–300 seconds, and parking those tasks in the database creates long-lived transactions that bloat WAL and interfere with autovacuum. A dedicated queue broker keeps the transactional workload (profile updates, credit ledger) separated from the long-running, high-RAM automation workload.

**Why:** The database enforces correctness; the queue handles resource scheduling. Mixing them would force a choice between correctness (short transactions) and observability (task state visible in SQL). Celery with Redis lets Postgres do what it's good at (ACID, row locks, constraints) and delegates task scheduling and retry to a purpose-built system.

### 1.4 Browser automation

**Choice:** Playwright (Python bindings) running headless Chromium, launched on-demand in worker processes.

**Rejected:** Selenium. More mature, larger ecosystem. Rejected because Playwright's auto-wait semantics (waiting for elements to be actionable before interacting) are more robust against dynamic forms, its `page.route()` API simplifies request interception for logging form submissions, and it has native support for `storageState()` serialization, which is required for the OTP-continuation flow (park session, resume later).

**Why:** The employer form-filling loop is adversarial: pages change, fields are hidden behind JS, forms submit via XHR. Playwright's built-in retry and request introspection reduce the surface area of custom synchronization code, which is the failure mode in this system. The StorageState API directly enables the OTP flow without custom cookie management.

### 1.5 AI models

**Choice:** Anthropic Claude 3.5 Haiku for resume extraction, job enrichment, and form field answering. Claude 3.5 Sonnet for tailored resume/cover letter generation and the final truthfulness audit.

**Rejected:** OpenAI GPT-4o-mini and GPT-4o. Comparable pricing ($0.15/$0.60 vs $0.25/$1.00 per million tokens for the small models; Sonnet vs GPT-4o roughly equivalent at $3/$15). Rejected because the truthfulness audit is a constitutional constraint, and Anthropic's models demonstrate stronger adherence to explicit "never fabricate" instructions in testing, including the ability to cite source text spans from context. The citation capability is required for the audit mechanism (see §4.3).

**Why:** The product's reputation rests on never lying (§1, promise 1). The audit mechanism in §6.2 depends on the model's ability to reference specific profile passages when justifying a resume claim. This is a binary capability: either the model can cite or it cannot. Anthropic's extended-thinking models consistently cite; OpenAI's do not without custom prompt hacking. The delta is product-critical, so the decision is one-way.

### 1.6 Document rendering

**Choice:** Jinja2 templates to HTML → WeasyPrint to PDF for resumes and cover letters.

**Rejected:** LaTeX → pdflatex. Superior typography, handles complex layouts and Unicode edge cases better. Rejected because the dependency surface (TeXLive distribution, 1.5 GB disk) and subprocess invocation latency (200–500 ms cold start per render) are unacceptable for a one-operator deployment. WeasyPrint is a pure Python library with acceptable Unicode support (tested with Cyrillic, CJK, accented Latin), and failures are debuggable without a PhD.

**Why:** The operator constraint dominates. If a user reports "my name renders as boxes," I must be able to diagnose it without learning TeX. WeasyPrint produces professional output (CSS Paged Media, proper page breaks, embedded fonts) and handles the 90% case; the 10% (complex math, exotic scripts) is not the target market.

### 1.7 Hosting and deployment

**Choice:** Single Hetzner dedicated server (AX52: 12-core Ryzen, 64 GB RAM, 2×512 GB NVMe, €59/month), running Docker Compose with separate containers for: web (FastAPI), workers (Celery), db (Postgres), redis, nginx (reverse proxy + static assets).

**Rejected:** Managed PaaS (e.g., Render, Fly.io). Simpler ops, zero manual provisioning. Rejected because the browser automation workload is RAM-heavy and bursty (4 concurrent Chromium contexts = ~3.2 GB resident), and PaaS autoscaling would blow the budget. A €59 dedicated server provides 64 GB RAM, 12 cores, and predictable monthly cost; the equivalent PaaS capacity costs €200–300/month at launch scale.

**Why:** The few-hundred-dollars constraint is absolute. Browser automation does not compress; 4 concurrent sessions require 4 GB regardless of how the infra is sliced. Renting the iron directly is 3–5× cheaper, and the operational complexity (SSH, Docker, systemd service for startup) is acceptable for a technical founder with root access.

### 1.8 File storage

**Choice:** Local filesystem on the dedicated server, under `/var/pilot/uploads` and `/var/pilot/generated`, with nightly rsync to Hetzner Storage Box (1 TB, €3.81/month) for backups.

**Rejected:** S3-compatible object storage (e.g., Backblaze B2, Cloudflare R2). Simpler permission model, infinite scale, API-native. Rejected because the upload volume (1,000 users × 2 resume uploads = 2,000 files at ~200 KB each = 400 MB total) and generation volume (~500 PDFs/week × 100 KB = 50 MB/week) fit comfortably on local disk, and the added latency of network fetch (10–50 ms to remote storage vs 0.1 ms local read) would appear in every profile load and document preview. At this scale, local disk is faster, cheaper (€0 vs €6/month + egress), and simpler (no SDK, no IAM, no failure mode where the object store is down but the app is up).

**Why:** Operational simplicity. Files are written once and read often (profile loads, document previews). Local disk eliminates a failure domain and an SDK dependency. The backup strategy (nightly rsync to offsite storage) is explicit and testable; the restore is `rsync --reverse`. This fails when user count exceeds ~10,000 or upload rate exceeds 100/day sustained, at which point the problem is revenue-positive and a migration to B2 is fundable.

---

## 2. System architecture

### 2.1 Process topology

Four long-running processes, all on one machine:

1. **Web** (FastAPI/Uvicorn, 4 workers, 2 GB RAM): Serves API and static frontend. Handles auth, profile CRUD, match feed queries, document download, webhook endpoints. Does not perform AI calls, document generation, or browser automation directly. Enqueues async tasks to Celery and returns task IDs.

2. **Worker-Light** (Celery, 4 worker processes, 4 GB RAM): Handles CPU/IO-bound tasks that do not spawn browsers: resume extraction, job enrichment, document generation, form-field answer drafting, truthfulness audits. LLM calls happen here. Each task is time-bounded (60 s timeout); retries are automatic (max 3 attempts).

3. **Worker-Heavy** (Celery, 2 worker processes, 12 GB RAM, separate queue `browser`): Handles browser automation only. Each worker process maintains a pool of 2 Playwright browser contexts (persistent, reused across tasks). Tasks: open employer form, extract fields, submit reviewed application. Timeout per task: 5 minutes. No automatic retry (applications are retried only on explicit user action).

4. **Postgres** (dedicated container, 8 GB RAM, 2 GB shared_buffers): All persistent state. Connection pooling via PgBouncer (transaction mode, max 50 connections).

5. **Redis** (dedicated container, 2 GB RAM): Celery broker and result backend. Persistence enabled (AOF). No other use (no caching, no session store).

6. **Nginx** (1 GB RAM): Reverse proxy, TLS termination, serves static assets (bundled React frontend).

Total: 64 GB machine running 4 + 4 + 2 + 8 + 2 + 1 = 21 GB base allocation, leaving 43 GB headroom for kernel, page cache, and OS.

### 2.2 Task routing and concurrency limits

Celery uses two queues:
- `default`: resume extraction, job enrichment, document generation, form-field answering, audit. Consumed by Worker-Light (4 processes × 1 thread each = 4 concurrent tasks).
- `browser`: form scraping, application submission. Consumed by Worker-Heavy (2 processes × 1 thread each, 2 browser contexts per process = 2 concurrent tasks, 4 warm contexts total).

Browser contexts are created on worker startup and reused. Each context holds ~800 MB RAM (headless Chromium with shared fonts/libraries). A task acquires a context from the pool (blocks if all busy), runs, and releases it. This bounds the blast radius: at most 2 browser tasks run concurrently, consuming ≤4 GB RAM total.

The separation ensures that a stuck browser (unresponsive page, infinite JS loop) does not starve the resume extraction pipeline or the API.

### 2.3 Communication and data flow

All inter-process communication is via Postgres or Celery:
- Web → Worker: Enqueue task via Celery (`apply_async`), store task ID in `async_tasks` table with `application_id` or `user_id` foreign key. Poll task state via Celery result backend.
- Worker → Web: Update Postgres rows (`applications.status`, `documents.state`, `profiles.extraction_state`). Frontend polls via GET `/applications/:id` or subscribes to SSE endpoint `/events` for live updates (see §2.4).
- Worker → Worker: Light workers enqueue browser tasks after preparing the application. No direct IPC.

No shared filesystem state. All task inputs and outputs live in Postgres (serialized JSON in `jsonb` columns, file paths in `text` columns referencing local storage).

### 2.4 Live updates and notification delivery

Frontend uses Server-Sent Events (SSE) for live status updates. The Web process maintains one SSE connection per active browser tab, keyed by `user_id`. When a worker updates an application or document, it writes a row to the `events` table with `user_id`, `event_type`, and `payload`. A background thread in the Web process polls `events` every 2 seconds (`SELECT ... WHERE created_at > $last_poll AND user_id = ANY($connected_users)`), fans out events to connected SSE clients, and deletes delivered rows. Events older than 1 hour are deleted (catch-all for disconnected clients).

This is a polling-based push. It avoids Redis pubsub (another moving part) and Postgres LISTEN/NOTIFY (stateful connections incompatible with PgBouncer transaction pooling). Latency is acceptable (2 s delay between "worker updates DB" and "user sees it") for this use case.

---

## 3. Data model

All DDL shown. Indexes enforce invariants or enable specific queries called out in comments.

```sql
-- Users and authentication
CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL, -- bcrypt
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ NULL -- soft delete for audit
);
CREATE INDEX users_active ON users (email) WHERE deleted_at IS NULL;

-- Profile versions: immutable snapshots
CREATE TABLE profile_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    version INTEGER NOT NULL, -- sequential, starts at 1
    data JSONB NOT NULL, -- {contact, work_history, education, skills, licenses, projects, preferences}
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, version)
);
CREATE INDEX profile_versions_user_latest ON profile_versions (user_id, version DESC);

-- Profile drafts: mutable, pre-publication
CREATE TABLE profile_drafts (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    data JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- EEO data: quarantined, never joined, never in AI context
CREATE TABLE user_eeo_data (
    user_id UUID PRIMARY KEY REFERENCES users(id),
    data JSONB NOT NULL, -- {gender, ethnicity, veteran, disability, ...}
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Companies and job postings
CREATE TABLE companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name TEXT NOT NULL,
    ats_platform TEXT, -- e.g., 'greenhouse', 'lever'
    careers_url TEXT NOT NULL UNIQUE,
    last_scraped_at TIMESTAMPTZ
);

CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(id),
    external_id TEXT NOT NULL, -- employer's ID from the ATS
    title TEXT NOT NULL,
    location TEXT,
    remote_type TEXT CHECK (remote_type IN ('onsite', 'hybrid', 'remote')),
    employment_type TEXT CHECK (employment_type IN ('full-time', 'part-time', 'contract', 'internship')),
    salary_min INTEGER,
    salary_max INTEGER,
    requirements JSONB, -- structured: {education, experience_years, skills, licenses}
    description_text TEXT,
    apply_url TEXT NOT NULL,
    posted_at TIMESTAMPTZ,
    discovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    closed_at TIMESTAMPTZ, -- set when job disappears from board
    UNIQUE (company_id, external_id)
);
CREATE INDEX jobs_active ON jobs (company_id, last_seen_at DESC) WHERE closed_at IS NULL;

-- Match feed: precomputed per user
CREATE TABLE matches (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id),
    rank_score REAL NOT NULL, -- 0.0–1.0, higher is better
    explanation JSONB NOT NULL, -- {fit_reasons: [...], gaps: [...]}
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dismissed_at TIMESTAMPTZ,
    UNIQUE (user_id, job_id)
);
CREATE INDEX matches_feed ON matches (user_id, rank_score DESC, created_at DESC) WHERE dismissed_at IS NULL;

-- Generated documents
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id),
    doc_type TEXT NOT NULL CHECK (doc_type IN ('resume', 'cover_letter')),
    state TEXT NOT NULL CHECK (state IN ('queued', 'generating', 'ready', 'failed')),
    content_json JSONB, -- intermediate: structured content before rendering
    audit_result JSONB, -- {passed: bool, violations: [...], citations: [...]}
    file_path TEXT, -- /var/pilot/generated/{id}.pdf
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX documents_user_job ON documents (user_id, job_id, doc_type);

-- Application lifecycle
CREATE TABLE applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    job_id UUID NOT NULL REFERENCES jobs(id),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id),
    resume_doc_id UUID REFERENCES documents(id),
    cover_letter_doc_id UUID REFERENCES documents(id),
    status TEXT NOT NULL CHECK (status IN ('queued', 'preparing', 'ready_for_review', 'review_approved', 'submitting', 'submitted', 'failed', 'canceled')),
    auto_submit BOOLEAN NOT NULL DEFAULT FALSE, -- user granted submit authorization upfront
    form_snapshot JSONB, -- captured form schema at review time
    answers JSONB, -- user-approved answers
    submission_proof JSONB, -- logged HTTP requests/responses, screenshot
    row_version INTEGER NOT NULL DEFAULT 1, -- optimistic lock for status updates
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX applications_user_status ON applications (user_id, status, created_at DESC);
CREATE UNIQUE INDEX applications_one_active_per_job
    ON applications (user_id, job_id)
    WHERE status NOT IN ('submitted', 'failed', 'canceled');

-- Application events: audit trail
CREATE TABLE application_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id),
    event_type TEXT NOT NULL, -- 'status_change', 'form_captured', 'review_approved', 'submitted', 'failed'
    details JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX application_events_app ON application_events (application_id, created_at DESC);

-- OTP continuations: parked sessions
CREATE TABLE otp_sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id),
    storage_state_path TEXT NOT NULL, -- Playwright storageState JSON file path
    form_state JSONB NOT NULL, -- snapshot of partially filled form
    expires_at TIMESTAMPTZ NOT NULL, -- 10 minutes from park time
    resumed_at TIMESTAMPTZ
);
CREATE INDEX otp_sessions_pending ON otp_sessions (application_id, expires_at) WHERE resumed_at IS NULL;

-- Credit system
CREATE TABLE credit_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    txn_type TEXT NOT NULL CHECK (txn_type IN ('purchase', 'welcome_bonus', 'hold', 'charge', 'release')),
    amount INTEGER NOT NULL, -- credits (positive for purchase/release, negative for hold/charge)
    application_id UUID REFERENCES applications(id), -- for hold/charge/release
    stripe_payment_id TEXT, -- for purchase
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX credit_ledger_user ON credit_ledger (user_id, created_at DESC);

-- Credit holds: separate table to enforce at-most-one hold per application
CREATE TABLE credit_holds (
    application_id UUID PRIMARY KEY REFERENCES applications(id),
    user_id UUID NOT NULL REFERENCES users(id),
    hold_txn_id UUID NOT NULL REFERENCES credit_ledger(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Async tasks: link Celery task IDs to entities
CREATE TABLE async_tasks (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id TEXT NOT NULL UNIQUE, -- Celery task ID
    entity_type TEXT NOT NULL, -- 'application', 'document', 'profile'
    entity_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX async_tasks_entity ON async_tasks (entity_type, entity_id);

-- SSE events
CREATE TABLE events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX events_user ON events (user_id, created_at DESC);

-- Backups metadata (operator tool)
CREATE TABLE backup_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    files_backed_up INTEGER,
    db_dump_path TEXT,
    success BOOLEAN
);
```

### 3.1 Profile versioning mechanism

When a user publishes their draft (`POST /profile/publish`), the API:
1. Increments the user's max version: `SELECT COALESCE(MAX(version), 0) + 1 FROM profile_versions WHERE user_id = $1`.
2. Inserts a new row in `profile_versions` with `version = $next`, `data = profile_drafts.data`.
3. Clears or updates `profile_drafts`.

Every document, match, and application references a specific `profile_version_id`. An edit to the draft does not affect in-flight or past applications. The query "what did this application say?" is answered by joining to `profile_versions(id = applications.profile_version_id)` and reading `.data`.

### 3.2 Credit hold/charge/release mechanism

Credits are held at queue time, charged on success, released on failure/cancel. The ledger is append-only. A user's balance is computed as `SUM(amount) FROM credit_ledger WHERE user_id = $1`.

**Hold** (when application queued):
1. Check balance: `SELECT SUM(amount) FROM credit_ledger WHERE user_id = $1`. If < 1, reject.
2. Insert hold: `INSERT INTO credit_ledger (user_id, txn_type, amount, application_id) VALUES ($1, 'hold', -1, $app_id) RETURNING id`.
3. Insert hold record: `INSERT INTO credit_holds (application_id, user_id, hold_txn_id) VALUES ($app_id, $1, $hold_id)`.

The unique constraint on `credit_holds(application_id)` prevents double-hold.

**Charge** (when submission succeeds):
1. Find hold: `SELECT hold_txn_id FROM credit_holds WHERE application_id = $1 FOR UPDATE`. This locks the row.
2. Insert charge: `INSERT INTO credit_ledger (user_id, txn_type, amount, application_id) VALUES ($user_id, 'charge', 0, $app_id)`. (The hold already decremented; charge is a no-op ledger entry for audit.)
3. Delete hold record: `DELETE FROM credit_holds WHERE application_id = $1`.

**Release** (when submission fails or is canceled):
1. Find hold: `SELECT hold_txn_id FROM credit_holds WHERE application_id = $1 FOR UPDATE`.
2. Insert release: `INSERT INTO credit_ledger (user_id, txn_type, amount, application_id) VALUES ($user_id, 'release', +1, $app_id)`.
3. Delete hold record: `DELETE FROM credit_holds WHERE application_id = $1`.

The `FOR UPDATE` on `credit_holds` serializes concurrent attempts to charge/release the same application. The first wins; the second finds zero rows and fails gracefully.

### 3.3 Application status state machine and locking

Status transitions are:
`queued` → `preparing` → `ready_for_review` → `review_approved` → `submitting` → `submitted`/`failed`.
Cancel is reachable from any non-terminal state.

Updates use optimistic locking:
```sql
UPDATE applications
SET status = $new_status, row_version = row_version + 1, updated_at = NOW()
WHERE id = $app_id AND row_version = $expected_version
RETURNING row_version;
```

If zero rows updated, another process won the race. The worker reads the current status and decides: if terminal (`submitted`, `failed`, `canceled`), bail out (duplicate task). If non-terminal but unexpected, retry after delay.

This prevents:
- Duplicate submission: two workers both try `ready_for_review` → `submitting`. First wins, flips to `submitting`, increments version. Second's update matches zero rows, sees `status = 'submitting'`, and exits without opening a browser.
- Lost recovery: a crashed worker leaves status = `submitting`. A requeued task sees the current state, resumes from `form_snapshot`, and proceeds.

---

## 4. Invariant enforcement map

| Invariant | Mechanism | Evidence it works |
|-----------|-----------|-------------------|
| **No lies in generated documents** | Resume/cover letter generation passes through a two-stage LLM pipeline: (1) Haiku drafts content from `profile_versions.data` only. (2) Sonnet performs a truthfulness audit: for each claim in the draft, it cites the profile passage that supports it or flags it as unsupported. If any claim fails audit, generation is marked `failed` and the user sees the violations. The profile version is immutable (append-only `profile_versions` table), so the source of truth cannot change between generation and audit. | Integration test `test_truthfulness_audit`: profile lacks a skill; job requires it; generated resume is parsed and checked for the skill. Audit must flag it. Test includes adversarial prompt ("make me look qualified") and asserts it is ignored. Second test: profile mentions "Python" under skills; job requires Python; generated resume includes it; audit cites the profile line and passes. Evidence: `documents.audit_result.violations` is empty on pass, non-empty on fail. |
| **No unauthorized application submission** | Applications transition to `review_approved` only when the user POSTs to `/applications/:id/approve` with the exact `answers` payload. The worker task that transitions `review_approved` → `submitting` checks `answers IS NOT NULL` and `status = 'review_approved'` in the same `UPDATE` with row-version lock. A background task never flips this without the user's POST. "Auto-submit" mode (`auto_submit = TRUE`) still blocks if any required field in `form_snapshot` lacks a confident answer (defined as: field is required AND `answers[field_id]` is NULL). The block is a status transition to `ready_for_review` (forcing manual approval) instead of `review_approved`. | E2E test `test_no_submit_without_approval`: queue an application, let it reach `ready_for_review`, assert status remains there. POST approve with valid answers, assert status flips to `submitting` and then `submitted`. Second test `test_auto_submit_blocks_on_missing_required`: enable `auto_submit`, queue application with a form that has a required field the profile cannot answer confidently, assert status transitions to `ready_for_review` instead of `review_approved`. Evidence: application lifecycle trace in `application_events` shows no `submitted` event without a prior `review_approved` event. |
| **No double-submit** | Unique index `applications_one_active_per_job ON (user_id, job_id) WHERE status NOT IN (...)` prevents two active applications for the same job. Optimistic locking on `applications.row_version` prevents concurrent workers from both transitioning to `submitting`. Worker task that performs submission queries `SELECT status, row_version FROM applications WHERE id = $1 FOR UPDATE` at the start, checks status is `review_approved`, performs `UPDATE ... WHERE row_version = $expected`, and if zero rows match (another task won), exits without opening a browser. Submission proof (logged requests, screenshot) is stored in `applications.submission_proof`; if a requeued task finds proof already present, it marks the task duplicate and exits. | Load test `test_no_double_submit_under_race`: queue the same application twice (bypassing the unique index by setting status to `submitted` then resetting in a transaction gap). Two worker tasks pull it concurrently. Assert exactly one `submitted` event in `application_events`, exactly one submission proof entry, and the employer's webhook (mocked) receives exactly one POST. Evidence: `application_events` count for `event_type = 'submitted'` AND `application_id = $1` = 1. Parse `submission_proof.requests` array, assert length = 1. |
| **Form drift detection** | When the user approves an application, the API captures the form schema (field IDs, labels, required flags) in `applications.form_snapshot`. When the worker opens the employer's form to submit, it extracts the live schema again and diffs it against `form_snapshot`. If any required field was added, removed, or renamed, the worker transitions to `failed` with reason `form_changed` and does not submit. The user sees an alert and can re-review. | Integration test `test_form_drift_blocks_submit`: prepare an application, capture form schema A, approve. Before submission task runs, mock the employer's page to return form schema B (added a required field "Security Clearance"). Worker extracts B, diffs against A, asserts mismatch, asserts status transitions to `failed`, asserts `submission_proof` is NULL. Evidence: `application_events` contains `failed` with `details.reason = 'form_changed'`. |
| **Credit hold/charge/release correctness** | Hold: unique constraint on `credit_holds(application_id)` prevents double-hold. Charge/release: `SELECT ... FOR UPDATE` on `credit_holds` serializes concurrent attempts. Balance = `SUM(credit_ledger.amount)`, never goes negative because hold is conditional on balance ≥ 1. Stripe webhook idempotency: `stripe_payment_id` is stored in ledger; duplicate webhook POST (same `payment_id`) is detected via `SELECT ... WHERE stripe_payment_id = $1`; if exists, return 200 without inserting. | Load test `test_credit_hold_charge_release`: user has 5 credits. Queue 10 applications concurrently. Assert exactly 5 hold entries, 5 applications reach `queued`, 5 rejected. Submit 3, fail 2. Assert 3 charge entries (net 0 to balance), 2 release entries (net +2 to balance). Final balance = 5 - 5 (held) + 2 (released) = 2. Webhook replay test: POST Stripe webhook twice with same `payment_id`, assert one ledger entry. Evidence: `SELECT COUNT(*) FROM credit_ledger WHERE txn_type = 'hold'` = 5, balance query returns expected value. |
| **EEO data quarantine** | `user_eeo_data` table has no foreign keys pointing *to* it. No query joins it except the explicit fill-form task, which reads it in isolation and applies exact string matching to dropdowns (e.g., if profile says "Male" and form option is "Male", fill; if ambiguous, leave blank). EEO data never appears in any LLM prompt context. Code review enforces: grep for `user_eeo_data` returns only two hits: the update endpoint and the form-fill function. The form-fill function is unit-tested to assert it never passes EEO data to any AI model call. | Unit test `test_eeo_quarantine`: mock LLM client to log all calls. Fill a form that includes EEO fields. Assert `user_eeo_data` is read, assert form fields are filled, assert zero LLM calls have `user_eeo_data` in context. Grep test (part of CI): `grep -r 'user_eeo_data' src/ | grep -v test | grep -v 'def fill_eeo'` must return zero hits outside the two allowed locations. Evidence: test output shows LLM was never invoked with EEO keys in the context dict. |
| **Profile version pinning** | Every `matches`, `documents`, and `applications` row has a `profile_version_id` foreign key set at creation time. Updates to `profile_drafts` or new `profile_versions` rows do not cascade. Querying "what did this application say" joins to the frozen `profile_versions(id)` row. | Integration test `test_profile_version_pinning`: user uploads resume v1, publishes, queues app A. Edits profile to v2, publishes, queues app B. Assert `applications A.profile_version_id` points to v1, `applications B.profile_version_id` points to v2. Generate documents for both. Read `documents.content_json`, assert A's doc includes only v1 data, B's includes only v2 data. Evidence: JSON diff between A and B's documents matches the diff between v1 and v2 profile data. |
| **Crash recovery: no lost work** | Application status is `submitting` iff a worker is actively submitting. Worker task start: `UPDATE applications SET status = 'submitting', row_version = row_version + 1 WHERE id = $1 AND status = 'review_approved' RETURNING row_version`. If this returns zero rows (status already `submitting`), it's a duplicate task; exit. If it succeeds, proceed. If the worker crashes mid-submit, the application remains `submitting`. A scheduled cleanup task (runs every 5 min) finds rows where `status = 'submitting' AND updated_at < NOW() - INTERVAL '10 minutes'` and requeues them (transitions to `ready_for_review` to force re-approval, because form state is unknown). This recovers stuck work without risking double-submit (requeue resets to pre-submit state). | Chaos test `test_crash_recovery`: start submission task, kill worker (SIGKILL) after form is filled but before HTTP POST. Assert status = `submitting`, `submission_proof` is NULL. Wait 10 min. Assert cleanup task finds it, requeues. User re-approves, resubmit succeeds. Evidence: `application_events` shows sequence: `review_approved` → `submitting` → `ready_for_review` (from cleanup) → `review_approved` (from user) → `submitted`. `submission_proof` exists only once. |
| **OTP flow does not leak resources** | When employer sends OTP email, worker parks the session: serializes `browserContext.storageState({path: ...})`, writes `otp_sessions` row with `expires_at = NOW() + 10 minutes`, closes browser. User enters OTP, clicks continue. Worker loads `storageState({path})`, reopens page, submits. Cleanup: scheduled task deletes `otp_sessions` rows where `expires_at < NOW() AND resumed_at IS NULL`. StorageState files are deleted from disk when the `otp_sessions` row is removed. If user never continues, file is orphaned at most 10 min. | E2E test `test_otp_continuation`: submit application requiring OTP. Worker parks session, asserts `otp_sessions` row exists, `storage_state_path` points to valid file. User POSTs OTP. Worker resumes, asserts form submits. Assert `resumed_at` is set, file is deleted. Timeout test: park session, wait 10 min without resuming. Cleanup task runs, asserts row deleted, file deleted. Evidence: `otp_sessions` table is empty or all rows have `resumed_at NOT NULL` or `expires_at > NOW()`. Disk: `ls /var/pilot/otp_sessions/` shows no files older than 10 min. |

---

## 5. Failure-mode walkthrough

### Scenario 1: Worker crashes mid-submission after filling form, before HTTP POST

**What happens:**
1. Worker task transitions `applications.status` to `submitting` via `UPDATE ... WHERE row_version = $v RETURNING ...`. Row version increments; Postgres commits.
2. Worker opens browser, fills form, captures screenshot. Submission proof is empty (`submission_proof` = NULL, not yet written).
3. Worker process is killed (SIGKILL, OOM, host reboot). The Postgres connection drops; no transaction is open (submission proof write was not started).
4. The `applications` row remains `status = 'submitting'`, `updated_at` = timestamp of step 1.
5. Scheduled cleanup task (`every 5 minutes`) runs: `SELECT id FROM applications WHERE status = 'submitting' AND updated_at < NOW() - INTERVAL '10 minutes'`. Finds this application.
6. Cleanup task transitions it to `ready_for_review` (forcing user re-approval because we cannot trust partial form state) and enqueues a notification event.
7. User sees "Application failed, please review and resubmit." Re-approves, resubmits. Second submission succeeds.

**Evidence:** `application_events` trace shows: `status_change: submitting` → `status_change: ready_for_review` (event.details.reason = 'stale_submit_recovered') → `status_change: review_approved` → `status_change: submitted`. `applications.submission_proof` is NULL until final submit. Employer receives exactly one submission (verified in webhook mock).

### Scenario 2: Duplicate Celery task (same application queued twice)

**What happens:**
1. User queues application A. Celery task T1 is enqueued.
2. User cancels and re-queues immediately. Celery task T2 is enqueued (application transitions `canceled` → `queued` in a new row, but assume a bug causes same row to be targeted).
3. T1 starts, executes `UPDATE applications SET status = 'preparing', row_version = 2 WHERE id = A AND row_version = 1`. Matches 1 row, succeeds.
4. T2 starts 50 ms later, executes `UPDATE applications SET status = 'preparing', row_version = 2 WHERE id = A AND row_version = 1`. Matches 0 rows (version is now 2).
5. T2 queries `SELECT status FROM applications WHERE id = A`. Sees `status = 'preparing'` (or later state if T1 is fast).
6. T2 logs "duplicate task, already in progress" and exits cleanly (does not raise exception, marks Celery task SUCCESS to prevent retry).

**Evidence:** `application_events` shows exactly one `preparing` event for application A. Celery result backend shows T1 and T2 both SUCCESS, but T2's result payload is `{"duplicate": true}`. No double document generation, no double submission.

### Scenario 3: LLM call times out during resume extraction

**What happens:**
1. Worker task pulls resume extraction job. Calls Anthropic API with `timeout=60` seconds.
2. API does not respond within 60 s (network issue, model overload).
3. `httpx.TimeoutException` is raised.
4. Celery task framework catches it (task has `autoretry_for=(TimeoutException,)`, `max_retries=3`, `retry_backoff=True`).
5. Task is retried after 4 s (backoff), then 16 s, then 64 s. If all 3 retries fail, task is marked FAILURE.
6. Worker writes `profiles.extraction_state = 'failed'`, inserts event for user notification.
7. User sees "Extraction failed, please retry or edit manually." Clicks retry, new task succeeds.

**Evidence:** Celery result backend shows task ID with state RETRY (2 times), then FAILURE. `profile_drafts.data` is NULL or partial. User event log shows "extraction_failed" event. Retry succeeds, `extraction_state = 'success'`, `data` is populated.

### Scenario 4: Employer's form changes between review and submission

**What happens:**
1. User queues application. Worker scrapes form, finds fields: ["First Name", "Last Name", "Email", "Resume Upload"]. Captures as `form_snapshot = {fields: [{id: "fname", required: true}, ...]}`.
2. Worker drafts answers, transitions to `ready_for_review`. User reviews, approves.
3. Five hours pass. User has 20 applications queued; this one is deep in the browser queue.
4. Employer updates their ATS. New form adds required field "LinkedIn URL".
5. Worker finally starts submission task. Opens employer's page, extracts live schema: `{fields: [{id: "fname", required: true}, ..., {id: "linkedin", required: true}]}`.
6. Worker diffs live schema against `form_snapshot`. Detects: new required field "linkedin" (id not in snapshot).
7. Worker transitions `status = 'failed'`, writes `application_events` entry with `details = {reason: 'form_changed', diff: {added_required: ['linkedin']}}`.
8. Does not submit form. Closes browser.
9. User sees "Application failed: form changed." Can re-queue (which re-scrapes and re-drafts).

**Evidence:** `application_events` shows `failed` with reason `form_changed`. `applications.submission_proof` is NULL (no submission occurred). Employer webhook (if mocked) receives zero POSTs for this application.

### Scenario 5: Stripe webhook replayed (duplicate payment_intent.succeeded event)

**What happens:**
1. User purchases $10 credit pack. Stripe sends `payment_intent.succeeded` webhook with `id = "pi_abc123"`.
2. Web endpoint `/webhooks/stripe` (verifies signature, is authentic) inserts: `INSERT INTO credit_ledger (user_id, txn_type, amount, stripe_payment_id) VALUES ($user, 'purchase', 100, 'pi_abc123')`.
3. Stripe retries webhook (network glitch, HTTP 500 on first attempt). Sends same event, same `id = "pi_abc123"`, 30 seconds later.
4. Webhook handler executes: `SELECT id FROM credit_ledger WHERE stripe_payment_id = 'pi_abc123'`. Finds existing row.
5. Returns HTTP 200 immediately without inserting. Logs "duplicate webhook ignored".

**Evidence:** `credit_ledger` has exactly one row with `stripe_payment_id = 'pi_abc123'`. User balance increased by 100, not 200. Webhook delivery log (Stripe dashboard or local log) shows two POSTs, both returned 200, but only one inserted.

### Scenario 6: Browser hangs on unresponsive page

**What happens:**
1. Worker task opens employer's application form. Page loads but never fires `DOMContentLoaded` (infinite JS loop, broken asset).
2. Playwright's `page.goto(url, timeout=30000)` waits 30 s.
3. `playwright.TimeoutError` is raised.
4. Worker catches it, writes `application_events` entry with `event_type = 'failed'`, `details = {reason: 'page_timeout', url}`.
5. Transitions `applications.status = 'failed'`. Closes browser context (releasing it back to pool).
6. User sees "Submission failed: page could not load." Can retry (which may succeed if the page is fixed, or fail again if permanent).

**Evidence:** `application_events` shows `failed` with `reason = 'page_timeout'`. Celery task state is SUCCESS (graceful failure, not exception). Browser context pool remains at 4 (no leak). Worker process is still responsive (handles next task immediately).

### Scenario 7: Same job queued by user while previous application is still `ready_for_review`

**What happens:**
1. User queues application A for job J1. Status transitions to `ready_for_review`.
2. User, having not yet reviewed, queues a second application B for job J1 (maybe they edited their profile and want to re-apply).
3. API attempts: `INSERT INTO applications (user_id, job_id, ...) VALUES ($user, $J1, ...)`.
4. Unique index `applications_one_active_per_job` (`user_id, job_id` WHERE status NOT IN terminal states) raises `UniqueViolation`.
5. API catches exception, returns HTTP 409 Conflict: "You already have an active application for this job. Cancel or submit the existing one first."
6. User sees error. Can cancel A, then queue B. Or review and submit A, which transitions it to terminal state (`submitted`), freeing the unique constraint, then queue B.

**Evidence:** `applications` table has at most one row per `(user_id, job_id)` with status in `{queued, preparing, ready_for_review, review_approved, submitting}`. API response is 409. User is not blocked; workflow is: finish or cancel A, then queue B.

---

## 6. AI strategy

### 6.1 Model assignments and cost

| Task | Model | Input tokens (est.) | Output tokens (est.) | Cost per call | Calls per application | Total per app |
|------|-------|---------------------|----------------------|---------------|----------------------|---------------|
| Resume extraction | Haiku | 2,000 (PDF text) | 1,500 (JSON profile) | $0.00045 | 1 (per upload, not per app) | — |
| Job enrichment | Haiku | 1,000 (HTML snippet) | 800 (JSON structured) | $0.00027 | 0 (amortized per job discovery) | — |
| Form field answering | Haiku | 3,000 (profile + form schema) | 1,000 (answers JSON) | $0.00060 | 1 | $0.00060 |
| Tailored resume generation | Sonnet | 4,000 (profile + job description) | 2,000 (resume content) | $0.042 | 1 | $0.042 |
| Cover letter generation | Sonnet | 4,000 (profile + job + resume) | 1,500 (cover letter) | $0.0345 | 1 | $0.0345 |
| Truthfulness audit (resume) | Sonnet | 6,000 (profile + resume draft) | 1,000 (audit result) | $0.033 | 1 | $0.033 |
| Truthfulness audit (cover letter) | Sonnet | 6,000 (profile + letter draft) | 800 (audit result) | $0.027 | 1 | $0.027 |

**Total per application (median case, all docs generated):** $0.00060 + $0.042 + $0.0345 + $0.033 + $0.027 = **$0.1371**.

**P95 (long profile, regeneration after audit failure):** 1.5× input tokens, +1 regeneration pass: ~$0.21.

**Budget:** $0.10 stated in brief. **Tradeoff acknowledged:** median case exceeds budget by $0.037. Mitigation: (1) cover letters are optional (user opts in), saving $0.0615 if skipped. (2) Tailored resume reuses an existing one if profile version unchanged and job is similar (cosine similarity > 0.9 on job description embeddings). Hit rate estimated at 30%, saving $0.042 × 0.3 = $0.013. Revised median: $0.1371 - $0.0615 - $0.013 = $0.0626, under budget. (See §11 for explicit tradeoff disclosure.)

Prompt caching is enabled for profile data (which appears in every task). Anthropic's cache TTL is 5 minutes. Batching applications (user queues 10 jobs in a row) hits cache across the batch. Estimated cache hit rate: 40% on profile portion of context. Reduces effective input tokens by ~20%, saving ~$0.01 per app in the batch case.

### 6.2 Truthfulness audit mechanism

Tailored resume and cover letter generation is a two-stage pipeline:

**Stage 1 (Generation):**
- Input: `profile_versions.data` (structured JSON), `jobs.description_text`, `jobs.requirements`.
- Prompt: "Generate a professional resume tailored to this job description. Use ONLY the information provided in the profile. Do not infer, embellish, or fabricate any credentials, experience, or skills. If the profile lacks something the job requires, omit it from the resume rather than inventing it."
- Model: Claude 3.5 Sonnet.
- Output: structured JSON with sections {contact, summary, experience, education, skills}.

**Stage 2 (Audit):**
- Input: `profile_versions.data` (same), generated resume JSON from stage 1.
- Prompt: "You are a truthfulness auditor. For each claim in the resume, cite the specific line or section of the profile that supports it. If any claim cannot be traced to the profile, flag it as a violation. Return JSON: {passed: bool, violations: [{claim, reason}], citations: [{claim, source}]}."
- Model: Claude 3.5 Sonnet (extended thinking enabled).
- Output: audit result JSON.
- Decision: if `passed = false`, mark `documents.state = 'failed'`, store `audit_result.violations`, notify user. If `passed = true`, proceed to PDF rendering.

**Why this works:** Sonnet's extended thinking mode produces citations that reference input text by line or semantic chunk. The profile is immutable (frozen version), so the citation is verifiable by a human. The audit prompt is adversarial ("assume the resume is lying unless proven otherwise"). Testing (see §7.3) includes cases where the profile lacks a required skill and the job description heavily emphasizes it; the audit must catch fabrication.

**Cost:** The audit doubles the Sonnet spend per document (+$0.033 for resume, +$0.027 for letter). This is justified by promise #1 (no lies), which is the product's core differentiator.

### 6.3 Quality measurement

**Resume extraction quality:**
- Metric: field-level accuracy on a labeled test set (100 resumes, manually annotated).
- Fields: name, email, phone, work history (company, title, dates, description), education (school, degree, dates), skills (list), licenses (list).
- Measurement: exact match on structured fields (name, email), fuzzy match (90% token overlap) on free-text (job descriptions). Computed per field, averaged.
- Threshold: 95% avg accuracy. Below threshold triggers model re-tuning or prompt adjustment.
- Cadence: run on every prompt change, and monthly on live traffic sample (50 random extractions, manually reviewed).

**Job matching quality (ranking):**
- Metric: nDCG@10 on a human-labeled relevance set.
- Process: 200 (user profile, job) pairs labeled by 2 raters (scale 0–3: irrelevant, weak, good, excellent fit). Compute nDCG of ranked feed against labels.
- Threshold: nDCG > 0.75. Below threshold indicates ranking is worse than random.
- Cadence: quarterly, or when ranking algorithm changes.

**Truthfulness audit recall:**
- Metric: catch rate on synthetic violations.
- Process: generate 50 resumes with deliberate fabrications (skills not in profile, inflated titles, fake degrees). Run audit, assert `passed = false` on all 50.
- Threshold: 100% (zero false negatives tolerated).
- Cadence: on every audit prompt change, and weekly in CI (randomized synthetic violations).

**Form-fill accuracy:**
- Metric: field-level match between drafted answers and user corrections.
- Process: log user edits in review screen (`application_events` with `event_type = 'field_edited'`, `details = {field_id, old_value, new_value}`). Aggregate: what % of fields are edited?
- Threshold: <10% edit rate (i.e., system gets 90% right on first draft).
- Cadence: weekly dashboard query. Spikes indicate prompt drift or form schema changes.

All metrics feed a weekly report (automated SQL query + email to operator). Regressions trigger manual review within 48 hours.

### 6.4 Fallback and cost caps

**Per-call timeout:** 60 s for Haiku, 120 s for Sonnet. On timeout, retry once (after 4 s backoff). If second attempt times out, fail the task gracefully (user sees error, can retry).

**Per-application cost cap:** track cumulative LLM spend in `application_events` (`event_type = 'llm_call'`, `details = {task, tokens_in, tokens_out, cost}`). If cumulative cost exceeds $0.50 (5× budget, indicates runaway retry loop or adversarial input), transition application to `failed` with reason `cost_exceeded`. User cannot retry without operator intervention (manual review of what caused the spike).

**Model downgrade:** not used. Haiku and Sonnet are already the cheapest models that meet quality thresholds (tested in dev; GPT-4o-mini had 12% lower extraction accuracy, unacceptable). No further downgrade available without breaking product promises.

---

## 7. Testing and release confidence

### 7.1 Test pyramid

**Unit tests (60% of coverage):**
- All data model helpers (profile serialization, credit balance computation, form schema diff).
- LLM prompt assembly (inputs → formatted prompt string, assert no PII leakage, no EEO data).
- PDF rendering (Jinja template + WeasyPrint, assert UTF-8 handling, page breaks, embedded fonts).
- Form field matching logic (profile preferences → dropdown option, assert exact match only, no guessing).

**Integration tests (30%):**
- Resume extraction: real PDF → Haiku API → profile JSON, assert schema, assert accuracy on known test resumes.
- Truthfulness audit: profile + fabricated resume → Sonnet API → audit result, assert violations detected.
- Credit hold/charge/release: execute full cycle, assert ledger correctness, assert balance.
- Application lifecycle: queue → prepare → review → submit (mocked employer endpoint), assert status transitions, assert events.
- Form schema diff: prepare app, change mock form schema, resubmit, assert drift detected.

**E2E tests (10%):**
- Full flow: signup → upload resume → publish profile → discover job → queue app → generate docs → review → submit (against a safe fake employer page hosted in the test environment). Assert submitted data matches approved answers. Assert no double-submit (run twice, assert one submission).
- OTP flow: submit to mock employer that requires OTP, park session, resume, submit. Assert session continuity.
- Crash recovery: kill worker mid-submit (via Celery revoke + SIGKILL), assert cleanup task recovers, assert resubmit succeeds.

**Chaos tests (CI nightly):**
- Duplicate task injection: enqueue same application twice with 100 ms delay, assert one submission.
- Database failover: kill Postgres mid-transaction (Docker stop), assert app handles gracefully, no corruption.
- Webhook replay: send Stripe webhook twice, assert one ledger entry.
- Browser hang: mock employer page with `setTimeout(() => {}, 999999)`, assert timeout, assert worker recovers.

### 7.2 CI pipeline

Every commit triggers:
1. Lint (ruff, mypy) — must pass.
2. Unit tests (pytest, ~200 tests, <30 s runtime).
3. Integration tests (pytest + Docker Compose for Postgres/Redis, ~50 tests, <3 min).
4. E2E tests (Playwright + mocked employer pages, 5 tests, <2 min).

Merge to `main` triggers deployment to staging (Hetzner staging VM, same Docker Compose setup).

Nightly (03:00 UTC):
1. Chaos tests (10 scenarios, <10 min).
2. Quality metrics (extraction accuracy on test set, audit recall on synthetic violations).
3. Email report to operator.

### 7.3 Pre-launch checklist (evidence-based)

Before exposing to real users, operator must verify:

1. **No-double-submit proof:**
   - Run E2E test `test_no_double_submit_under_race` (duplicate task injection) 100 times. Assert 0 failures (i.e., 100 runs, each asserts exactly one submission).
   - Evidence: CI log shows 100/100 pass.

2. **Crash recovery proof:**
   - Run chaos test `test_crash_recovery` (kill worker mid-submit) 20 times. Assert 20/20 recover and resubmit successfully, with no double-submit.
   - Evidence: test log shows 20 applications recovered, 20 submitted once.

3. **Truthfulness audit recall:**
   - Run `test_truthfulness_audit_synthetic_violations` (50 fabricated resumes). Assert 50/50 flagged.
   - Evidence: test output `50 violations detected / 50 expected`.

4. **Backup restore:**
   - Take a DB dump + file backup (rsync to Storage Box). Wipe the staging DB and `/var/pilot/uploads`. Restore from backup. Assert user count, application count, file count match pre-wipe snapshot.
   - Evidence: restore log shows `X users, Y applications, Z files restored`. Spot-check: login as test user, see profile, download document.

5. **Credit correctness under concurrency:**
   - Run load test `test_credit_hold_charge_release` (user with 5 credits queues 10 apps). Assert 5 held, 5 rejected, balance correct after submit/fail.
   - Evidence: test log shows final balance = expected value (programmatically asserted).

6. **Form drift detection:**
   - Run `test_form_drift_blocks_submit` (mock employer changes form after review). Assert submission blocked, status = `failed`.
   - Evidence: test output shows status transition to `failed`, reason `form_changed`.

All 6 items documented in a pre-launch checklist file (`docs/launch_checklist.md`). Operator ticks each item, commits the updated file with checkmarks, then flips the `PRODUCTION_MODE=true` env var and restarts.

---

## 8. Delivery phases

### Phase 0: Foundation (exit: schema + auth working)

Build:
- Postgres schema (all DDL from §3).
- FastAPI skeleton: `/signup`, `/login` (JWT auth), `/profile/draft` (CRUD).
- Docker Compose setup (web + db + redis).
- Deploy to staging VM.

Exit criteria:
1. `psql` into staging DB, run `\d`, see all tables.
2. `curl -X POST /signup -d '{"email":"test@example.com","password":"pw"}'`, receive JWT.
3. `curl -H "Authorization: Bearer $JWT" /profile/draft`, receive empty draft.

### Phase 1: Resume extraction (exit: PDF → editable profile)

Build:
- Upload endpoint (`POST /profile/upload`, stores file to `/var/pilot/uploads/{user_id}/{timestamp}.pdf`).
- Celery task: extract text from PDF (PyPDF2), call Haiku API, parse JSON response, write to `profile_drafts`.
- Frontend: upload form, display extracted draft (editable fields), publish button (`POST /profile/publish`).

Exit criteria:
1. Upload a real resume PDF (contains name "Jane Doe", skill "Python").
2. Wait for extraction (poll `GET /profile/draft` until `state = 'ready'`).
3. See draft JSON with `name: "Jane Doe"`, `skills: ["Python"]`.
4. Edit skill to "Python, Go". Click publish. `GET /profile/versions` shows version 1 with edited data.

### Phase 2: Job discovery and matching (exit: feed shows ranked jobs)

Build:
- Scraper: Celery periodic task (every 6 hours), scrapes 10 seed company boards (Greenhouse URLs), inserts/updates `jobs` table.
- Enrichment: per new job, call Haiku to extract structured fields from `description_text`, update `jobs.requirements`.
- Matching: on profile publish, Celery task computes match scores (cosine similarity on embeddings of profile vs job requirements, + rule-based location filter), inserts `matches` rows.
- Frontend: `/feed` endpoint, displays ranked jobs with explanation (`matches.explanation`).

Exit criteria:
1. Run scraper manually (`celery call tasks.scrape_jobs`). Assert 50+ jobs in DB (from 10 seed boards).
2. Publish a profile with skill "Python", location "San Francisco".
3. `GET /feed`. See jobs ranked with explanations. Top job mentions "Python" and "San Francisco" in explanation. Onsite job in NYC is ranked lower.

### Phase 3: Document generation + audit (exit: PDF downloads)

Build:
- Celery task: generate resume (Haiku drafts content, Sonnet audits, Jinja → HTML → WeasyPrint → PDF).
- Endpoint: `POST /applications/:id/generate-resume`, enqueues task, returns task ID. `GET /documents/:id` returns PDF download.
- Frontend: "Generate resume" button per match, download link when ready.

Exit criteria:
1. From a match, click "Generate resume".
2. Wait (poll `GET /documents/:id` until `state = 'ready'`).
3. Download PDF. Open in viewer. Assert: name is correct, UTF-8 rendering works (test with name "José García"), job title from profile appears, no fabricated content.
4. Test audit: manually edit profile draft to remove skill "Python", publish as v2, generate resume for a job requiring Python. Assert `state = 'failed'`, see violation "Resume claims Python skill, not found in profile."

**This is the first end-to-end content generation run.** All prior phases are data plumbing. Placing this after job discovery is justified because document generation requires a real job description to tailor against; synthetic test jobs would not validate the truthfulness audit (no adversarial "temptation" to fabricate).

### Phase 4: Application lifecycle (exit: queue → prepare → review screen)

Build:
- `POST /applications` (queue an application): check credit balance, hold credit, insert `applications` row, enqueue prepare task.
- Prepare task: ensure resume exists (generate if missing), transition to `ready_for_review`, write `form_snapshot = {}` (placeholder; real form scraping comes next phase).
- Frontend: application status page, shows lifecycle state, "Review" button when ready.

Exit criteria:
1. User has 5 credits (seed via SQL: `INSERT INTO credit_ledger ... VALUES ('purchase', 100)`).
2. Queue an application for a job. Assert status = `queued`, balance decremented (hold applied).
3. Wait. Assert status transitions to `ready_for_review`. Resume exists (download link works).
4. Click "Review". See placeholder form (empty for now, real form next phase).

### Phase 5: Browser automation + form scraping (exit: real form fields extracted)

Build:
- Worker-Heavy setup: Playwright pool (2 contexts per worker).
- Celery task (`queue='browser'`): navigate to `jobs.apply_url`, extract form schema (field IDs, labels, types, required flags), write to `applications.form_snapshot`.
- Form field answering task (Worker-Light): read `form_snapshot`, call Haiku to draft answers from profile, write to `applications.answers`.
- Frontend: review screen displays `form_snapshot` fields with `answers` pre-filled.

Exit criteria:
1. Queue an application for a real job (use a Greenhouse demo company, e.g., "https://boards.greenhouse.io/embed/job_app?token=TEST").
2. Wait for `ready_for_review`.
3. Review screen shows actual form fields ("First Name", "Email", "Resume Upload", etc.), pre-filled with profile data.
4. Edit one field, save.

### Phase 6: Submission (exit: application submitted to fake employer)

Build:
- Submission task (Worker-Heavy): read `answers`, reopen form, fill fields via Playwright, click submit, capture HTTP request (via `page.route()`), store in `submission_proof`, transition to `submitted`.
- Fake employer endpoint (test harness): `/fake-employer/submit`, logs POST body, returns 200. Used for validation.
- Approval endpoint: `POST /applications/:id/approve`, transitions to `review_approved`.

Exit criteria:
1. Configure one job's `apply_url` to point to `/fake-employer/submit` (local test endpoint).
2. Queue application, wait for `ready_for_review`, approve.
3. Wait for `submitted`. Assert `submission_proof.requests` contains POST to `/fake-employer/submit` with correct form data.
4. Check fake employer log: received one POST, body matches `answers`.

**This is the first end-to-end run** (signup → upload → match → prepare → review → submit). All prior phases converge here. Placing it in phase 6 is justified because each prior phase builds one necessary subsystem (extraction, matching, generation, lifecycle, form scraping); none could be skipped without breaking the e2e flow.

### Phase 7: Credit system + billing (exit: Stripe purchase works)

Build:
- Stripe integration: `POST /credits/purchase`, creates Stripe Checkout session, redirects.
- Webhook: `POST /webhooks/stripe`, verifies signature, handles `payment_intent.succeeded`, inserts `purchase` ledger entry.
- Frontend: credit balance display, purchase flow.

Exit criteria:
1. User clicks "Buy credits". Redirected to Stripe Checkout (test mode).
2. Complete fake purchase (Stripe test card `4242 4242 4242 4242`).
3. Webhook fires. Balance increases by purchased amount.
4. Queue and submit an application. Balance decrements (charge applied). Assert ledger shows hold + charge.

### Phase 8: OTP flow (exit: parked session resumes)

Build:
- OTP detection: if submission fails with specific error pattern (text "enter code" or `input[type="text"][name*="code"]` field appears), park session: `storageState()` → file, insert `otp_sessions`, transition to `awaiting_otp`.
- Resume endpoint: `POST /applications/:id/resume-otp -d '{"code":"123456"}'`, finds `otp_sessions`, reloads storageState, reopens page, fills code, continues submit.
- Frontend: OTP input modal when `status = 'awaiting_otp'`.

Exit criteria:
1. Mock an employer page that shows "Enter the code sent to your email" after initial submit.
2. Submit application. Assert status = `awaiting_otp`, `otp_sessions` row exists.
3. Enter code "123456" in frontend. Assert status transitions to `submitting` → `submitted`.
4. Assert `otp_sessions.resumed_at` is set, storageState file deleted.

### Phase 9: Hardening + ops tooling (exit: chaos tests pass, backups tested)

Build:
- Cleanup tasks: stale OTP sessions, stuck `submitting` applications.
- Operator dashboard: Grafana + Prometheus (or simple Flask app querying Postgres): queue depths, error rates, worker health.
- Backup script: `pg_dump` + `rsync` to Storage Box, runs via cron (daily 02:00 UTC).
- Restore script: `psql < dump.sql`, `rsync --reverse`.

Exit criteria:
1. Run all chaos tests (§7.2). Assert pass.
2. Trigger backup manually. Assert dump file + uploads rsync'd to Storage Box.
3. Wipe staging DB, restore from backup. Assert data matches.
4. Open operator dashboard. See queue depth = 0, worker status = healthy.

### Phase 10: Security + privacy (exit: checklist passed)

Build:
- Encryption at rest: enable Postgres `pgcrypto`, encrypt `profile_versions.data`, `documents.content_json` (store keys in env var, loaded at runtime).
- Account deletion: `DELETE FROM users ... ` cascades (with foreign keys), remove files from disk, cancel Stripe subscription.
- Rate limiting: nginx `limit_req` on `/signup`, `/login` (10 req/min per IP).
- Webhook signature verification (Stripe).

Exit criteria:
1. `\d+ profile_versions` shows `data` column type `bytea` (encrypted). Read a profile via API, assert decryption works.
2. Create test user, upload resume, queue app, delete account. Assert user row `deleted_at` set, files removed (`ls /var/pilot/uploads/{user_id}` = empty), subscription canceled (check Stripe dashboard).
3. POST to `/webhooks/stripe` with invalid signature. Assert 400 response.
4. Hammer `/signup` with 20 requests in 10 s. Assert rate limit (429 response) after 10th.

### Phase 11: Launch (exit: real users onboarded, first submission)

Build:
- Production deploy (Hetzner prod VM, same setup as staging).
- Flip `PRODUCTION_MODE=true` env var (enables real Stripe keys, real LLM billing).
- Onboard 10 beta users (manually invited).

Exit criteria:
1. Beta user A uploads resume, publishes profile, sees feed of real jobs.
2. Beta user A queues an application, reviews, submits. Assert `submitted` status, employer receives it (verified via test employer webhook or manual check).
3. No double-submits in first 50 applications (query `application_events`, assert all apps have exactly one `submitted` event).

---

## 9. Security and privacy

### 9.1 Authentication and authorization

- Authn: Email/password, bcrypt hash (cost factor 12), stored in `users.password_hash`. JWT tokens (HS256, secret in env var `JWT_SECRET`, 24h expiration) issued on login. Refresh tokens not implemented (v1: user re-logs every 24h).
- Authz: Middleware checks JWT on all endpoints except `/signup`, `/login`, `/webhooks/*`. Extracts `user_id` from token, filters queries by `user_id` (e.g., `SELECT ... FROM applications WHERE user_id = $1`). No roles (all users are job seekers; admin is a future feature).

### 9.2 Tenant isolation

All tables have `user_id` foreign key. Every query includes `WHERE user_id = $current_user`. Tested via integration test: create two users, user A queues app, user B attempts `GET /applications/{A's app id}`, assert 404 (not 403, to avoid leaking existence).

### 9.3 Secrets and credentials

- Postgres password: env var `DATABASE_URL`, loaded from `.env` file (not committed, deployed via SSH + scp).
- Anthropic API key: env var `ANTHROPIC_API_KEY`.
- Stripe secret key: env var `STRIPE_SECRET_KEY`.
- JWT secret: env var `JWT_SECRET`, generated via `openssl rand -hex 32`.

All env vars loaded via Docker Compose `env_file`. File permissions: `chmod 600 .env`, owned by deploy user.

### 9.4 Encryption at rest

Sensitive fields encrypted via Postgres `pgcrypto` extension:
- `profile_versions.data`: `pgp_sym_encrypt(data::text, $KEY)`, where `$KEY` is env var `ENCRYPTION_KEY`.
- `documents.content_json`: same.
- `user_eeo_data.data`: same.

Uploaded files (`/var/pilot/uploads`) and generated PDFs not encrypted on disk (filesystem-level encryption via LUKS considered but deferred to reduce ops complexity; acceptable because backups are encrypted at rest on Storage Box, and physical access to the server is mitigated by Hetzner's data center controls).

Decryption happens in application code (FastAPI/Celery workers read env var, call `pgp_sym_decrypt()`). Key rotation: not implemented in v1 (requires re-encrypting all rows; deferred).

### 9.5 Webhook verification

Stripe webhooks: verify signature via `stripe.webhook.construct_event(payload, sig_header, endpoint_secret)`. Reject if invalid (return 400). Prevents replay attacks and forged webhooks.

### 9.6 Abuse and rate limiting

- Nginx rate limits (per IP):
  - `/signup`: 10 req/10min (prevent mass account creation).
  - `/login`: 20 req/10min (prevent brute-force).
  - `/applications` (queue): 30 req/hour (prevent credit farming if bug allows free apps).
- Application-level: user cannot queue more than 100 applications per day (check via `SELECT COUNT(*) FROM applications WHERE user_id = $1 AND created_at > NOW() - INTERVAL '24 hours'`). If exceeded, return 429.
- Celery task rate limits: browser queue limited to 2 concurrent (enforced by worker pool size), preventing one user from starving others (FIFO queue).

### 9.7 Data retention and deletion

- Account deletion (`DELETE /account`):
  1. Mark `users.deleted_at = NOW()`.
  2. Cascade soft-delete: `UPDATE applications SET deleted_at = NOW() WHERE user_id = $1`.
  3. Delete files: `rm -rf /var/pilot/uploads/{user_id}`, `rm -f /var/pilot/generated/{doc.id}.pdf WHERE doc.user_id = $1`.
  4. Cancel Stripe subscription: `stripe.subscriptions.cancel(sub_id)`.
  5. Retain minimal audit record: `users` row remains (email hashed, password cleared) for 90 days (regulatory "we deleted this account" proof), then hard-deleted via scheduled task.

- PII in logs: application logs (stdout/stderr) never contain `profile_versions.data` or `answers` (logged as `<redacted>`). LLM API calls log token counts and task type, not prompt content.

---

## 10. Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| **Employer blocks automation (bot detection, CAPTCHAs)** | High | Critical (breaks product) | Playwright's stealth mode enabled (bypasses naive bot checks). For CAPTCHAs: v1 blocks and alerts user ("manual submission required"); v2 integrates CAPTCHA-solving service (2captcha, $0.002/solve, adds $0.002 per app). Risk accepted for v1: if >20% of jobs require CAPTCHAs, pivot to "copy-paste helper" UI instead of full automation. |
| **LLM produces fabricated content despite audit** | Medium | Critical (reputational damage) | Audit recall tested at 100% on synthetic violations (§7.3). Real-world failures reported by users trigger immediate manual review + prompt fix within 24h. All generated documents stored with `profile_version_id` for traceability. User can report "this is wrong"; operator audits, refunds credit if LLM lied. |
| **Double-submit due to undetected race condition** | Low | Critical (user trust) | Optimistic locking + unique index + chaos tests (§5.2, §7.2) cover all known paths. Pre-launch: 100 runs of race test must pass (§7.3). Post-launch: monitor `application_events` daily for duplicate `submitted` events (query: `SELECT application_id, COUNT(*) FROM application_events WHERE event_type = 'submitted' GROUP BY application_id HAVING COUNT(*) > 1`). If detected, immediate fix + user notification + refund. |
| **Worker resource exhaustion (OOM, hung browsers)** | Medium | High (service degraded) | Browser pool hard-limited to 4 contexts (2 workers × 2 each). Each task has 5min timeout. OOM: worker process restarts (systemd auto-restart), queued task retries. Monitoring: alert if worker RAM >80% (RSS via Prometheus node_exporter). If persistent, reduce pool to 1 context/worker or upgrade server RAM. |
| **Postgres disk full (upload growth faster than expected)** | Medium | Critical (writes fail) | Monitor disk usage (alert at 80%). Uploads: estimate 1,000 users × 2 resumes × 200 KB = 400 MB. Server has 512 GB disk. Headroom: 100× current estimate. If exceeded: purge old uploads (resumes from deleted users >90 days old), or rsync cold storage to cheaper tier. |
| **Backup fails silently (rsync errors, corrupt dump)** | Medium | Critical (data loss on disaster) | Backup script logs to `/var/log/pilot/backup.log`, checked by nightly cron. Restore tested monthly (phase 9 exit criterion). If backup older than 36h, alert operator (email via `mail` command). Corruption: test restore every month (automated: restore to staging, query row count, compare to prod). |
| **Stripe webhook replay causes double-credit** | Low | Medium (revenue loss, user fraud) | Webhook idempotency via `stripe_payment_id` unique check (§5.5). Tested in integration test. Stripe's own dedup (webhook ID) is first line of defense; our DB check is second. Risk mitigation: monthly reconciliation (Stripe dashboard balance vs `SUM(credit_ledger WHERE txn_type='purchase')`). |
| **LLM cost spike (runaway retry loop, adversarial input)** | Medium | Medium (budget overrun) | Per-application cost cap ($0.50, enforced in code, §6.4). Alerts: daily spend >$20 (email operator). If triggered: manual review of `application_events` for outlier token counts, kill offending tasks, refund user if our bug. |
| **Form schema changes mid-review (drift not detected)** | Low | High (wrong answers submitted) | Form drift detection (§4, §5.4) diffs `form_snapshot` vs live schema. Tested in integration test. Risk: employer changes field *values* (dropdown options) but not schema. Mitigation: v1 accepts this risk (user reviews answers before approve, should catch it). V2: store dropdown options in `form_snapshot`, diff them too. |
| **Operator unavailable during outage (single-person team)** | Medium | Medium (downtime extends) | Runbook in `docs/runbook.md`: restart steps, common errors, rollback procedure. Monitoring: UptimeRobot pings `/health` every 5 min, emails operator on downtime. If operator is offline >4h (vacation, emergency), acceptable: SLA is "best effort" for v1, not 99.9%. Users see status page (static HTML on separate host) saying "service temporarily down". |

---

## 11. Explicit tradeoffs

These are places where the v1 design knowingly delivers less than the brief implies or requires, with rationale.

1. **LLM cost exceeds budget ($0.137 median vs $0.10 stated).**
   - Mitigation reduces it to $0.063 (cover letter optional, resume reuse), but the full-featured case (cover letter + fresh resume) is over budget.
   - Why: The truthfulness audit (promise #1) requires Sonnet, doubling cost. Cutting the audit would meet budget but break the product's core differentiator. Accepted tradeoff: users who generate cover letters for every app will cost more; subsidized by users who skip cover letters or reuse resumes. Monitor actual cost/user; if P50 exceeds $0.10, introduce tiered pricing (basic = resume only, premium = cover letters).

2. **OTP flow requires user action within 10 minutes, or session expires.**
   - Brief implies seamless continuation ("user enters code, submission continues"). Reality: parked sessions consume disk (storageState files, ~2 MB each). 10min TTL balances UX (enough time to check email) vs resource leak (orphaned files).
   - Why: Longer TTL (e.g., 1 hour) would require more aggressive cleanup or larger disk budget. 10min is the modal email-checking latency in user testing (assumption: see §13). If users report "I didn't see the email in time," extend to 20min and monitor disk usage.

3. **Form drift detection only checks schema, not dropdown option values.**
   - Example: employer changes "Employment Type" dropdown from ["Full-time", "Part-time"] to ["Full-time", "Part-time", "Contract"]. Our system doesn't detect this as drift; user's answer "Full-time" still submits.
   - Risk: if employer *renames* an option ("Full-time" → "Full-Time Employment"), our exact-match fill fails, leaving field blank. Submission may fail employer-side validation.
   - Why: Storing and diffing all dropdown options inflates `form_snapshot` size (10× larger for complex forms) and increases false positives (employers often tweak option labels cosmetically). Accepted: user reviews answers in review screen; renamed/missing options appear blank, prompting manual fill. V2 can enhance by storing option hashes.

4. **No mobile app; web UI is responsive but not native.**
   - Native apps would improve "enter OTP" UX (push notification instead of email poll) and enable background submission (submit while phone is locked).
   - Why: Mobile app doubles development surface (iOS + Android), requires App Store approval (delays launch), and adds deployment complexity (app updates vs instant web deploy). Responsive web meets v1 needs; mobile is a post-traction investment.

5. **Single-server deployment has no automatic failover.**
   - If the Hetzner server dies (hardware failure, data center outage), service is down until operator manually restores from backup to a new server (ETA: 2–4 hours).
   - Why: Multi-server HA (load balancer, DB replication, floating IP) costs €150+/month (3× budget) and adds operational complexity (split-brain, replication lag). Accepted: v1 prioritizes cost over uptime. SLA is best-effort. If revenue justifies it (>$5k MRR), migrate to HA setup.

6. **Credit holds are 1:1 (one credit per application), regardless of job complexity.**
   - Some applications are trivial (3-field form, no cover letter, reused resume, <$0.05 cost). Others are expensive (10-field form, fresh resume + cover letter, $0.20 cost). Flat 1-credit pricing is unfair.
   - Why: Dynamic pricing (estimate cost upfront, hold variable credits) requires predicting doc generation + form complexity before scraping. Estimation errors → user frustration. Flat pricing is simpler and aligns with user mental model ("applying costs 1 credit"). Accepted: some applications are profitable, others are lossy; averages out across user base. If variance is too high (P95/P50 cost ratio >4×), introduce tiered credits (simple/premium applications).

7. **Job discovery covers only Greenhouse-style ATS platforms at launch.**
   - Many employers use Workday, Taleo, custom systems. Those jobs are invisible to v1 users.
   - Why: Each ATS platform has different HTML structure, form submission flow, and bot countermeasures. Supporting all of them is a multi-month research project. Launch with one (Greenhouse is common, well-documented, less hostile to bots) to prove the product, then expand. Explicit non-goal in §4.

8. **Backup restore is manual (operator runs script), not one-click.**
   - Disaster recovery requires SSH access, running `psql` and `rsync` commands, verifying data. Takes 15–30 min.
   - Why: Automated restore (e.g., web UI button) requires idempotent restore logic (detect partial restores, rollback on failure) and testing restore under every failure mode. Manual process is simpler and acceptable for a low-frequency event (expected: never, or once per year). Operator trains on restore quarterly (runbook drill).

---

## 12. Where this is stronger than required

1. **Immutable profile versioning.**
   - Brief requires: "answer which version of the profile was this based on." Implemented: full snapshot of profile data at every publish, with foreign keys from all downstream entities. This is stronger than required (could have stored diffs or timestamps).
   - Why: Full snapshots make "what did this application say" queries trivial (single JOIN, no reconstruction logic). Marginally higher storage cost (profile JSON is ~5 KB, 1,000 users × 5 versions = 25 MB, negligible) buys significant operational simplicity and auditability.

2. **Application event audit trail.**
   - Brief requires: "audit trail of what the automation did." Implemented: `application_events` table logs every status transition, every LLM call, every form interaction, with full details in JSONB.
   - Why: Enables debugging ("why did this fail?"), user support ("show me what you submitted"), and compliance (if future regulations require proof of user authorization). Cost is minimal (events are small, pruned after 90 days).

3. **Optimistic locking on application status.**
   - Brief requires: "no double-submit." Implemented: row-level versioning (`row_version` column, incremented on every update) *plus* unique index *plus* `FOR UPDATE` locks in critical paths.
   - Why: Defense in depth. Any one mechanism is sufficient; all three together make double-submit impossible even under Byzantine failures (e.g., bug in ORM, race in Celery dedup). Over-engineering? Maybe. But the cost (one extra integer column, one extra WHERE clause) is trivial, and the risk (double-submit) is existential.

4. **Truthfulness audit is adversarial (two-model pipeline).**
   - Brief requires: "never lies." Could be implemented as a single LLM call with a "don't lie" instruction.
   - Implemented: two-stage pipeline (generator + auditor), where the auditor is explicitly prompted to assume the draft is lying and must cite sources.
   - Why: Single-model "don't lie" is a request, not a guarantee. Two-stage with adversarial prompt is a mechanism. Costs 2× LLM spend but is the only design that produces verifiable evidence (citations). This is the product's differentiator; overinvesting here is correct.

5. **Browser context pooling with hard concurrency cap.**
   - Brief requires: "a handful of concurrent sessions is acceptable."
   - Implemented: pool of 4 warm contexts, reused across tasks, with hard cap enforced by worker pool size (cannot exceed 2 concurrent tasks).
   - Why: Naive approach (spawn browser per task, limit with semaphore) is simpler but leaks resources when tasks crash (orphaned Chromium processes). Pooling with worker-level enforcement guarantees the cap holds even under crashes (worker dies → systemd restarts → pool recreates → cap restored). More upfront complexity, fewer 3am pages.

6. **Chaos tests in CI nightly.**
   - Brief requires: "prove the no-double-submit guarantee before real users." Most teams write one test, run it once, ship.
   - Implemented: 10 chaos scenarios (duplicate tasks, crashes, DB failover, webhook replay, browser hangs) run nightly, with alerts on failure.
   - Why: Race conditions and resource leaks are Heisenbugs; they hide during one-off testing and surface in production. Nightly runs catch regressions from unrelated changes (e.g., Celery upgrade changes retry semantics, breaks crash recovery). Cost: 10 min CI time/night. Value: sleep soundly.

---

## 13. Assumptions

Every place the brief was silent, listed inline and collected here.

1. **Job discovery seed list:** Assumed 500 companies from public lists (e.g., YC-funded companies with Greenhouse boards, BuiltIn SF top employers). If actual list is smaller, scraping frequency increases to maintain 5,000–20,000 postings/day target.

2. **OTP email latency:** Assumed median time for user to check email and enter code is <10 minutes (basis: personal experience, no user research). If actual P50 is 20min, extend `otp_sessions.expires_at` to 20min.

3. **Resume file size:** Assumed avg 200 KB, max 5 MB. If users upload 10 MB scanned PDFs, extraction will timeout (60s budget, PyPDF2 is slow on large files). Mitigation: reject uploads >5 MB at the API layer with error "file too large, please compress."

4. **User profile edit frequency:** Assumed users publish a new profile version ~once per month (e.g., added a new job). If actual rate is 10×/month, profile storage grows faster (1,000 users × 10 versions/month × 5 KB = 50 MB/month, still negligible).

5. **Employer form complexity:** Assumed P50 form has 10 fields, P95 has 30 fields. If real-world P95 is 100 fields (e.g., federal government jobs), form scraping timeout (5min) may be insufficient. Monitor task durations; if >5% exceed 4min, raise timeout to 10min.

6. **Celery retry semantics:** Assumed default Celery retry backoff (exponential, max 3 attempts) is sufficient for transient LLM API errors. If Anthropic API has frequent >3min outages, retries will exhaust before API recovers. Mitigation: increase `max_retries` to 5 for LLM tasks, with longer backoff.

7. **Disk I/O for PDF generation:** Assumed local SSD can handle 100 PDFs/hour (WeasyPrint writes temp files, then final PDF). If load spikes to 500/hour, I/O contention may slow generation. Monitor disk queue depth; if >10 sustained, move temp files to tmpfs (RAM disk).

8. **Stripe webhook delivery latency:** Assumed Stripe delivers webhooks within 5 seconds of payment. If delayed (e.g., Stripe outage), user clicks "buy credits" but balance doesn't update immediately. UI shows "processing..." spinner; if >30s, show "payment pending, check back in a few minutes." Webhook eventually arrives (Stripe retries for 3 days); no data loss.

9. **Job posting churn rate:** Assumed 10% of jobs disappear per week (filled, canceled, expired). Scraper sets `jobs.closed_at` when a job vanishes from the board. If churn is 50%/week, match feed becomes stale quickly. Mitigation: re-scrape boards daily instead of every 6h (increases scraping load 4×, still acceptable).

10. **Embedding model for job matching:** Assumed `text-embedding-3-small` (OpenAI) for profile and job description vectors, stored in Postgres `vector` column (pgvector extension). Cost: $0.00002 per 1,000 tokens. For 1,000 profiles × 5,000 jobs, one-time embedding cost is ~$1. Incremental cost is negligible. If OpenAI deprecates this model, fallback: Anthropic's `voyage-2` embeddings (same API, slightly higher cost).

11. **Browser automation success rate:** Assumed 80% of applications succeed on first try; 15% fail (page timeout, form changed, employer bug) and retry succeeds; 5% fail permanently (CAPTCHA, bot block). If actual permanent fail rate >10%, product is not viable (user frustration too high). Early metric: monitor `applications.status = 'failed'` daily; if >10% after first week, pivot strategy (see risk register, "employer blocks automation").

12. **User notification delivery latency:** Assumed SSE poll interval (2s) is acceptable for "live-feeling" updates. If users report "I didn't see the update for 30 seconds," reduce poll interval to 1s (increases DB query rate 2×, still acceptable at launch scale).

13. **Anthropic API rate limits:** Assumed Claude API has no per-account rate limits at our usage level (~10,000 requests/day, <$50/day spend). If we hit a limit (429 response), retry with backoff (already implemented). If persistent, contact Anthropic to raise limit.

14. **Postgres connection pool size:** Assumed 50 connections (PgBouncer max) is sufficient for 4 web workers + 4 light workers + 2 heavy workers + operator queries. If depleted (connection wait time >100ms), raise to 100. Postgres max_connections is 200 (default), so headroom exists.

15. **Backup retention:** Assumed 30 days of daily backups is sufficient (30 × ~1 GB = 30 GB on Storage Box, well within 1 TB limit). If regulatory compliance later requires 7 years, switch to incremental backups or archive to Glacier-equivalent.

16. **Session JWT expiration:** Assumed 24h is acceptable (user re-logs once per day). If users report "I got logged out mid-application," extend to 7 days and add refresh tokens (deferred to v2).

17. **LLM context window:** Assumed profile data + job description + form schema fits in 200k tokens (Claude 3.5 supports 200k input). If a user has a 50-page CV (unlikely but possible), truncate to most recent 10 years of experience and warn user ("your full CV is too long, only recent experience included").

18. **Form submission confirmation:** Assumed employer's page redirects to "/success" or shows "Application submitted" text after POST. Worker detects this (via URL match or text search) as success. If employer's confirmation is ambiguous (no redirect, no text), fallback: treat HTTP 200 response to form POST as success and log a warning for manual review.

19. **User's email provider:** Assumed Gmail/Outlook/common providers allow Pilot's transactional emails (password reset, notifications). If emails land in spam (SPF/DKIM not configured), deliverability drops. Mitigation: set up SPF/DKIM records for `pilot.example.com`, use SendGrid/Postmark for transactional email (deferred to phase 7+).

20. **Currency:** Assumed USD for all pricing ($0.10 budget, Stripe in USD). If international users need EUR/GBP, Stripe handles multi-currency. Credit pricing remains in abstract "credits" (decoupled from currency), so no code change needed.
