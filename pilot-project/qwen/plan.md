# Pilot — Engineering Plan

Complete engineering plan for the product defined in `product.md`, written to
`engineering-plan-standard.md`. Section numbers and titles follow the standard. Every
mechanism claim names its artifact (table, constraint, transaction, ordering, or named
function). Every quantity carries a value and a derivation. Where the brief was silent,
the decision is recorded inline as (A#) and collected in §13.

---

## 1. Technology decisions

### 1.1 Language and runtime

**Choice:** TypeScript 5.x on Node.js 22 LTS. One language across web API, workers, and
browser automation.

**Rejected:** Python 3.12 + FastAPI. Python's ML-ecosystem advantage does not apply: every
AI interaction in this product is a REST API call, not model training or local inference.

**Why:** Playwright's Node API is first-class — `chromium.launch()`,
`browser.newContext()`, `context.storageState({path})`, and `locator.fill()` with
actionability waits — and a single language removes the split-brain surface between the
truthfulness-audit logic and the automation logic. Optimizing for: one reviewable codebase
for a one-person team.

### 1.2 Web framework

**Choice:** Fastify 5.

**Rejected:** Next.js 15 full-stack. Next.js couples server-side rendering into the web
process and tempts co-locating browser workers in the same runtime.

**Why:** the invariant set in §4 requires worker isolation (§2); Fastify's JSON-Schema
route validation gives per-route input validation with zero additional dependencies.
Optimizing for: process isolation with minimal dependency surface.

### 1.3 Frontend

**Choice:** React 18 + Vite single-page app, served as static files by Fastify;
server-to-client push over Server-Sent Events.

**Rejected:** server-rendered React (Next.js). The application is fully authenticated —
there is no search-engine-optimization value — and SSR re-couples rendering into the
API process.

**Why:** `EventSource` gives server-to-client push with native browser auto-reconnect,
and the client never sends anything over the push channel, so SSE's one-directionality is
a non-impediment. Optimizing for: push notifications without a second protocol stack.

### 1.4 Datastore

**Choice:** PostgreSQL 16.

**Rejected:** SQLite. Genuinely simpler and zero-ops, and it would work for the read path.

**Why:** concurrent writers from the web process and two worker pools across two machines
need row-level locking; the credit and submission invariants in §4 are enforced by
conditional updates and `SELECT … FOR UPDATE SKIP LOCKED`, which have no SQLite
equivalent. Optimizing for: invariant enforcement under concurrency.

### 1.5 Job queue / async approach

**Choice:** a Postgres `jobs` table plus a worker claim loop using
`SELECT … FOR UPDATE SKIP LOCKED` with lease tokens and expiry timestamps (§3, §2).

**Rejected:** BullMQ/Redis. The strongest managed-queue alternative, mature and convenient.

**Why:** a second datastore with opaque crash semantics (RDB/AOF snapshots) sits adjacent
to the transaction boundaries the credit and submission invariants depend on. With the
`jobs` table, every claim, lease, retry count, and failure reason is inspectable in the
same `psql` session as the application rows. Optimizing for: auditable async state.

### 1.6 Browser automation

**Choice:** Playwright (Node) driving Chromium.

**Rejected:** Puppeteer. The strongest alternative Node automation library.

**Why:** Playwright provides `browser.newContext()` isolation (one employer session per
context), `context.storageState({path})` persistence, and the `locator` API whose
`locator.fill()` auto-waits for actionability. Puppeteer lacks first-class context
isolation and `storageState`. Optimizing for: per-employer session isolation and
actionability-gated field operations.

### 1.7 Document formats (extraction and rendering)

**Choice:** resume text extraction via `pdfjs-dist` (`getDocument().then(pdf =>
pdf.getPage(n)).then(page => page.getTextContent())`) for PDF and `mammoth`
(`mammoth.extractRawText({arrayBuffer})`) for DOCX; DOCX output via the `docx`
(dolanmiu/docx) library built from the structured document model; PDF output via
Playwright `page.pdf({path})` rendering a print stylesheet from the same structured model.

**Rejected:** pandoc for both directions. Pandoc forces HTML as the intermediary between
two formats that must share one source of truth (the structured document model), and its
DOCX styling control is coarse.

**Why:** the truthfulness audit (§4) maps generated lines to profile fields through a
structured representation; an HTML intermediary loses that provenance structure. Both
output formats compile from one structured model, so a document cannot differ between
PDF and DOCX. Optimizing for: single-source-of-truth documents.

### 1.8 AI access and model tiers

**Choice:** OpenAI-compatible REST API. Two pinned tiers: cheap tier for generation
tasks, strong tier for escalation and judging. Model versions pinned in config; any
re-pinning passes the promotion gates in §6 before it ships.

**Rejected:** self-hosted open-weight models. At the brief's scale (tens to low hundreds
of applications/day, ≤$0.10 each), self-hosting requires GPU spend of ~$500–2,000/month.

**Why:** the brief itself frames commodity API pricing ("cheap models with quality
fallbacks are fine"); API tiers keep infrastructure spend inside the few-hundred-dollar
budget while the promotion gates in §6 keep quality enforced rather than asserted.
Optimizing for: budget compliance with measured quality.

### 1.9 Hosting / deployment shape

**Choice:** two rented VMs. Machine A (8 vCPU / 16 GB): PostgreSQL 16, `web`,
`worker-core`, `watchdog`, backup timer. Machine B (4 vCPU / 8 GB): `worker-browser`
only. Single-region deployment (A8). Cost: ~$60/month + ~$40/month ≈ $100/month,
inside the few-hundred-dollar budget.

**Rejected:** a single VM with cgroup partitioning of browser workloads.

**Why:** the no-double-submit invariant requires that the crash domain of the submission
path never include the database. A Chromium OOM or kernel panic on Machine A takes down
PostgreSQL with it; on Machine B it takes down nothing but its own contexts. Optimizing
for: crash-domain separation of the submission path.

### 1.10 Secrets management

**Choice:** environment-variable injection from a root-owned `0600` secrets file loaded
at process start; quarterly rotation via a documented runbook. Total secret count: 8
(Postgres password, Stripe secret key, Stripe webhook endpoint secret, LLM API key,
Resend API key, file-encryption master key, B2 credentials, admin bootstrap token).

**Rejected:** HashiCorp Vault. Adds a stateful long-running service to operate and patch.

**Why:** at two machines and 8 secrets, environment injection has the same exposure
window with zero additional failure modes. Optimizing for: ops surface minimization.

### 1.11 Email

**Choice:** Resend transactional API.

**Rejected:** self-hosted SMTP relay. Deliverability to major providers requires managed
sending (SPF/DKIM/DMARC at scale, sender reputation).

**Why:** Resend's entry tier covers the observed volume (~500 messages/day: signup
codes, application alerts, OTP alerts, daily operator digest). Optimizing for:
deliverability without a mail-infrastructure ops burden.

### 1.12 Payments

**Choice:** Stripe Checkout sessions plus signed webhooks.

**Rejected:** Stripe PaymentElement (card fields rendered on our origin).

**Why:** Checkout offsites the card UI entirely — card data never touches our servers,
which the brief requires — and reduces our integration surface to webhook signature
verification (`stripe.webhooks.constructEvent`). Optimizing for: minimal PCI-adjacent
surface.

### 1.13 Backup and restore

**Choice:** nightly `pg_dump --format=custom` pushed to Backblaze B2 via `rclone`;
retention 14 daily + 8 weekly; weekly automated restore drill into a scratch Postgres
instance with schema-diff and row-count-diff report.

**Rejected:** logical replication to a second VM. Doubles cost and adds failover
promotion decisions the brief does not require.

**Why:** nightly RPO with a repeatedly *tested* restore path meets the brief's launch
requirement ("tested, documented restore path") at ~$0.05/month storage. Optimizing for:
proven-restorability, not replication complexity.

### 1.14 User push channel

**Choice:** SSE sourced from PostgreSQL `pg_notify`/`LISTEN` (§2).

**Rejected:** WebSocket.

**Why:** the client never sends over the channel; SSE rides plain HTTP, survives
corporate proxies, and `EventSource` auto-reconnects natively. Optimizing for: push
delivery with the smallest protocol surface.

### 1.15 Password hashing

**Choice:** Argon2id via `@node-rs/argon2` (`argon2idHash`), parameters m = 19 MiB,
t = 2, p = 1.

**Rejected:** bcrypt cost 12.

**Why:** Argon2id is memory-hard (resists GPU/ASIC mining) at the OWASP-recommended
minimum parameters; bcrypt's 72-byte input cap is a latent truncation hazard for long
passwords. Optimizing for: hash strength without input-cap hazards.

---

## 2. System architecture

### 2.1 Processes and responsibilities

| Process | Machine | Responsibility | Concurrency |
|---|---|---|---|
| `web` | A | Fastify API, static SPA, SSE fan-out (`LISTEN app_events`), Stripe webhook endpoint, signup/rate-limit enforcement | 1 process, Node default worker model; Postgres pool 10 connections |
| `worker-core` | A | Claims jobs of kinds `extraction`, `scoring`, `document_gen`, `render`, `reconcile`, `export`, `deletion` | 8 concurrent jobs (all I/O-bound on LLM/HTTP; CPU-light) |
| `worker-browser` | B | Claims jobs of kind `form_fill`; runs Playwright Chromium | 4 concurrent Chromium contexts (8 GB usable RAM ÷ ~800 MB per context (A6), leaving ~4 GB headroom) |
| `watchdog` | A | Lease-expiry sweeps, worker-liveness checks, alarm evaluation, daily digest | 1 process, sweep interval 30 s |
| `backup` | A | systemd timer, 02:30 UTC: `pg_dump` → `rclone` to B2; weekly Sunday: restore drill | 1 process, non-overlapping |

PostgreSQL 16 runs on Machine A only. Machine B holds no user data; its only state is
in-memory browser contexts and transient trace files (retained 7 days, then purged).

### 2.2 Communication

- **Shared state:** PostgreSQL is the only shared state. Job claims are conditional
  `UPDATE … WHERE status = 'pending'` leases obtained through
  `SELECT … FOR UPDATE SKIP LOCKED` on `jobs` (§3). A claim writes
  `lease_token` (UUID) and `lease_expires_at`; a worker acts only while its token matches.
- **User push:** worker transactions that change user-visible state emit
  `SELECT pg_notify('app_events', jsonb)` in the same transaction. `web` runs
  `LISTEN app_events` and fans each payload out to the matching user's SSE stream,
  filtered by `user_id`. Delivery latency: sub-second (Postgres notify is in-process on
  Machine A).
- **Machine B → A:** nothing except Postgres. Employer sites are reached only from
  Machine B. LLM APIs and Resend are reached only from Machine A. Stripe webhooks arrive
  only at `web`.

### 2.3 Keeping browser-heavy work from starving the rest

1. **Machine separation:** browser work runs only on Machine B; Machine A's CPU, RAM,
   and Postgres are unreachable by Chromium faults.
2. **Job-kind separation:** `form_fill` jobs are claimed only by `worker-browser`;
   `worker-core` never claims them and vice versa.
3. **Capacity semaphore:** the `worker-browser` claim loop claims at most 4 concurrent
   `form_fill` jobs (the context limit above). OTP waits hold a slot until TTL (§5.8).
4. **Priority ordering:** user-facing jobs (`extraction`, `document_gen`, `form_fill`,
   `render`) priority 10; `scoring` priority 1; housekeeping (`reconcile`, `export`,
   `deletion`) priority 0. The claim query orders by `(priority DESC, created_at ASC)`,
   so scoring backlogs never delay a user-facing job.
5. **Timeouts:** navigation 30 s (`page.goto({timeout: 30000})`); field operation
   10 s (Playwright `actionTimeout`); OTP wait 300 s; whole-job lease: `form_fill`
   600 s, all other kinds 300 s (extraction/document_gen: LLM timeout 60 s × 2 attempts
   + validation headroom).

### 2.4 Watchdog sweeps (every 30 s)

- Applications with `status = 'submitting' AND lease_expires_at < NOW()` →
  `failed`/`lease_expired` + credit-hold release in one transaction (§5.1).
- Jobs with expired leases → re-queued within `max_attempts`, else failed (§5.3).
- Worker heartbeats older than 60 s (heartbeat interval 10 s) → alarm row + daily
  digest entry (A10).
- Alarm evaluation: user-facing job queue depth > 50; job failure rate > 5% over the
  trailing hour; last successful backup older than 26 h. Alarms are written to
  `alarms` and included in the 08:00 UTC digest email; they do not gate anything —
  prevention lives in the mechanisms of §4.

---

## 3. Data model

Executable PostgreSQL 16 DDL. `gen_random_uuid()` is built in since PG13. Constraints that
cannot be expressed in DDL are named with their enforcing code path immediately after the
table.

```sql
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE users (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email CITEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at TIMESTAMPTZ
);

CREATE TABLE sessions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,          -- SHA-256 of the 256-bit bearer token
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    expires_at TIMESTAMPTZ NOT NULL           -- sliding 30-day lifetime
);
CREATE INDEX sessions_user_idx ON sessions (user_id);
CREATE INDEX sessions_expiry_idx ON sessions (expires_at);

CREATE TABLE resumes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL CHECK (content_type IN (
        'application/pdf',
        'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
    size_bytes BIGINT NOT NULL CHECK (size_bytes BETWEEN 1 AND 10485760),
    sha256 TEXT NOT NULL,
    encrypted_bytes BYTEA NOT NULL,           -- AES-256-GCM, §9
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX resumes_user_idx ON resumes (user_id);

-- Immutable published snapshots. No UPDATE/DELETE path exists against this table;
-- the migration-lint test `migrations_no_profile_version_mutation` greps every
-- migration and fails CI if one appears. Supersession is by insertion, never mutation.
CREATE TABLE profile_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    version INTEGER NOT NULL CHECK (version >= 1),
    source_resume_id UUID REFERENCES resumes(id),
    snapshot JSONB NOT NULL,                  -- contact, work history, education,
                                              -- skills, licenses, certifications,
                                              -- projects, preferences (work
                                              -- authorization, relocation, notice
                                              -- period, salary, consent answers)
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, version)
);

-- User-correctable drafts. Nothing downstream reads this table until publish.
CREATE TABLE profile_drafts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    source_resume_id UUID NOT NULL REFERENCES resumes(id),
    status TEXT NOT NULL DEFAULT 'editing' CHECK (status IN ('editing','published','superseded')),
    snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    published_version_id UUID REFERENCES profile_versions(id)
);

-- EEO self-identification. Physically separate table; never copied into any snapshot.
-- Read only by the demographic-section fill path (§4 row 6).
CREATE TABLE demographics (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    gender TEXT,
    ethnicity TEXT,
    veteran_status TEXT,
    disability TEXT,
    other JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

```sql
CREATE TABLE companies (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain TEXT NOT NULL UNIQUE,
    board_url TEXT NOT NULL,
    ats_platform TEXT NOT NULL DEFAULT 'greenhouse',
    discovered_by TEXT NOT NULL CHECK (discovered_by IN ('seed','discovered')),
    poll_interval_minutes INTEGER NOT NULL DEFAULT 240 CHECK (poll_interval_minutes >= 30),
    next_poll_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    otp_ttl_seconds INTEGER NOT NULL DEFAULT 300 CHECK (otp_ttl_seconds BETWEEN 60 AND 900),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE postings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    locations JSONB NOT NULL DEFAULT '[]'::jsonb,       -- [{name, country, lat, lng}]
    work_arrangement TEXT NOT NULL DEFAULT 'unknown' CHECK (work_arrangement IN
        ('remote','hybrid','onsite','unknown')),
    employment_type TEXT NOT NULL DEFAULT 'unknown' CHECK (employment_type IN
        ('full_time','part_time','contract','intern','unknown')),
    salary_raw TEXT,
    requirements JSONB NOT NULL DEFAULT '{"hard":[],"soft":[],"skills":[]}'::jsonb,
        -- shape-validated at ingest by zod schema `requirementsSchema`;
        -- hard/soft: requirement strings; skills: normalized skill strings
    description_text TEXT NOT NULL DEFAULT '',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    changed_at TIMESTAMPTZ,
    closed_at TIMESTAMPTZ,
    UNIQUE (company_id, external_id)
);
CREATE INDEX postings_open_idx ON postings (company_id) WHERE closed_at IS NULL;
CREATE INDEX postings_changed_idx ON postings (changed_at) WHERE closed_at IS NULL;

-- Precomputed deterministic match scores (§6.4). Refreshed incrementally on posting
-- change and fully on profile publish. Feed queries read this table only.
CREATE TABLE match_scores (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    score NUMERIC(6,4) NOT NULL CHECK (score BETWEEN 0 AND 1),
    factors JSONB NOT NULL,               -- {skills, location, arrangement, employment, salary}
    explanation_text TEXT NOT NULL,       -- deterministic template, §6.4
    scored_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, job_id)
);

CREATE TABLE feed_dismissals (
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    restored_at TIMESTAMPTZ,
    PRIMARY KEY (user_id, job_id)
);

-- Shareable/bookmarkable filtered views: /feed?v=<id>.
CREATE TABLE feed_views (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    filters JSONB NOT NULL DEFAULT '{}'::jsonb,   -- {remote?, location?, salary_min?, employment?}
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

```sql
-- Immutable per-generation document rows. Regeneration inserts a new row
-- (generation + 1); earlier generations remain resolvable for audit.
CREATE TABLE documents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN ('resume','cover_letter')),
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation >= 1),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id),  -- pinned basis
    preferences_snapshot JSONB NOT NULL,                                -- pinned at generation
    content JSONB NOT NULL,          -- structured document model; every line carries
                                     -- source_refs into profile_version fields
    audit_report JSONB NOT NULL,     -- auditDocument() output: per-line pass/fail + drops
    status TEXT NOT NULL DEFAULT 'generating' CHECK (status IN ('generating','ready','failed')),
    approved_at TIMESTAMPTZ,         -- user approval gates submission
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, job_id, kind, generation)
);
CREATE INDEX documents_latest_idx ON documents (user_id, job_id, kind, generation DESC);

CREATE TABLE applications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES postings(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued','preparing','ready','submitting','submitted','failed','canceled')),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id),   -- pinned basis
    preferences_snapshot JSONB NOT NULL,                                 -- pinned at queue time
    resume_document_id UUID REFERENCES documents(id),                    -- approved resume
    reviewed_answers JSONB,              -- frozen at review_finalization; what gets submitted
    reviewed_form_fingerprint TEXT,      -- SHA-256 of canonicalized form structure
    otp_expires_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,        -- submission-path lease, checked by watchdog
    failure_reason TEXT CHECK (failure_reason IN
        ('fill_interrupted','form_drift','otp_timeout','automation_timeout',
         'lease_expired','cost_cap','user_canceled','other')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- At most one active application per user per job. Second queue attempt gets SQLSTATE
-- 23505, mapped by the API to HTTP 409 with the existing application id.
CREATE UNIQUE INDEX applications_one_active_per_job
    ON applications (user_id, job_id)
    WHERE status IN ('queued','preparing','ready','submitting');

-- Append-only audit trail. seq is allocated by
--   INSERT … SELECT COALESCE(MAX(seq),0)+1 … inside the same transaction as the event.
CREATE TABLE application_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL CHECK (seq >= 1),
    type TEXT NOT NULL,   -- queued|preparing|review_finalized|submit_authorized|
                          -- fill_started|otp_requested|otp_resolved|receipt_obtained|
                          -- submitted|failed|canceled|lease_expired|hold_released
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (application_id, seq)
);

-- Inserted before the employer submit control is clicked; UNIQUE makes a second
-- intent for the same application impossible.
CREATE TABLE submission_intents (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL UNIQUE REFERENCES applications(id) ON DELETE CASCADE,
    intent_token UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Employer-side evidence of completion. Inserted in the same transaction as the
-- status flip to 'submitted'; UNIQUE makes a second receipt impossible.
CREATE TABLE submission_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL UNIQUE REFERENCES applications(id) ON DELETE CASCADE,
    evidence JSONB NOT NULL,   -- {success_url, confirmation_text_hash, dom_hash, received_at}
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- One active OTP wait per application; UNIQUE(application_id) while unresolved.
CREATE TABLE otp_waits (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL UNIQUE REFERENCES applications(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL,
    code_hash TEXT,            -- SHA-256 of entered code; NULL until entered
    resolved_at TIMESTAMPTZ
);
```

```sql
CREATE TABLE credit_accounts (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    frozen_at TIMESTAMPTZ,       -- set by reconcile on ledger mismatch; blocks charges
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Append-only ledger. The partial unique indexes are the double-charge barrier:
-- at most one hold, and at most one of {charge, release}, per application.
CREATE TABLE credit_ledger (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    application_id UUID REFERENCES applications(id),
    type TEXT NOT NULL CHECK (type IN
        ('welcome','purchase','hold','charge','release','adjustment')),
    amount INTEGER NOT NULL CHECK (amount > 0),
    balance_after INTEGER NOT NULL CHECK (balance_after >= 0),
    reference TEXT,              -- purchase id / adjustment reason
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX ledger_one_hold_per_app
    ON credit_ledger (application_id) WHERE type = 'hold';
CREATE UNIQUE INDEX ledger_one_terminal_per_app
    ON credit_ledger (application_id) WHERE type IN ('charge','release');
CREATE INDEX ledger_user_idx ON credit_ledger (user_id, created_at DESC);

CREATE TABLE purchases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stripe_session_id TEXT NOT NULL UNIQUE,
    credits INTEGER NOT NULL CHECK (credits > 0),
    amount_cents INTEGER NOT NULL CHECK (amount_cents > 0),
    currency TEXT NOT NULL CHECK (char_length(currency) = 3),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','paid','failed','refunded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    paid_at TIMESTAMPTZ
);

-- Webhook idempotency: stripe_event_id UNIQUE; processing guarded by
--   UPDATE stripe_webhook_events SET processed_at = NOW()
--   WHERE stripe_event_id = :id AND processed_at IS NULL
CREATE TABLE stripe_webhook_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    stripe_event_id TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMPTZ
);

-- The job queue. Claim:
--   UPDATE jobs SET lease_token = :t, lease_expires_at = NOW() + :lease,
--                claimed_at = NOW(), status = 'running'
--   WHERE id = (SELECT id FROM jobs WHERE status = 'pending'
--               ORDER BY priority DESC, created_at ASC LIMIT 1
--               FOR UPDATE SKIP LOCKED)
--   RETURNING *;
CREATE TABLE jobs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL CHECK (kind IN
        ('extraction','scoring','document_gen','form_fill','render',
         'reconcile','export','deletion')),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority IN (0,1,10)),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN
        ('pending','running','done','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);
CREATE INDEX jobs_claim_idx ON jobs (priority DESC, created_at ASC) WHERE status = 'pending';
CREATE INDEX jobs_lease_idx ON jobs (lease_expires_at) WHERE status = 'running';

CREATE TABLE worker_heartbeats (
    worker_id TEXT PRIMARY KEY,        -- e.g. 'worker-browser-1'
    kind TEXT NOT NULL,
    host TEXT NOT NULL,
    last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE notifications (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL CHECK (kind IN
        ('review_ready','submitted','failed','otp_requested','credits_low','other')),
    application_id UUID REFERENCES applications(id),
    read_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX notifications_user_idx
    ON notifications (user_id, created_at DESC) WHERE read_at IS NULL;

CREATE TABLE alarms (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL CHECK (kind IN
        ('queue_depth','job_error_rate','backup_stale','worker_stale','ledger_mismatch','other')),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    cleared_at TIMESTAMPTZ
);

CREATE TABLE rate_limits (
    key TEXT NOT NULL,                 -- ip: or user:
    kind TEXT NOT NULL CHECK (kind IN
        ('signup','document_gen','application_queue','purchase','api')),
    bucket_date DATE NOT NULL,
    count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0),
    PRIMARY KEY (key, kind, bucket_date)
);

CREATE TABLE backups_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind TEXT NOT NULL CHECK (kind IN ('pg_dump','restore_drill')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running','ok','failed')),
    artifact_uri TEXT,
    size_bytes BIGINT,
    diff_report JSONB                  -- restore drills: schema diff + row-count diff
);

-- Minimal anonymized record of account deletion (no personal data).
CREATE TABLE deletion_records (
    user_id UUID PRIMARY KEY,
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    counts JSONB NOT NULL              -- {resumes, documents, applications, ledger_entries}
);

-- Per-task AI cost ledger; feeds the per-application cost cap check in the task
-- dispatcher (§6.6). cost_micros: integer micro-dollars.
CREATE TABLE ai_costs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_kind TEXT NOT NULL,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    application_id UUID REFERENCES applications(id),
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    cost_micros INTEGER NOT NULL CHECK (cost_micros >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX ai_costs_app_idx ON ai_costs (application_id);
```

**Constraints not expressible in DDL** (enforcing code path named):

- *Profile-version immutability* — enforced by absence: no UPDATE/DELETE statement
  exists against `profile_versions`; the migration-lint test
  `migrations_no_profile_version_mutation` fails CI if one appears.
- *Ledger `balance_after` consistency* — enforced by `reconcileCreditLedger()`
  (src/credits/reconcile.ts), run nightly and after each purchase/charge/release burst:
  recomputes each balance from ledger entries ordered by `(created_at, id)` and freezes
  the account (`credit_accounts.frozen_at`) plus raises an `alarms` row of kind
  `ledger_mismatch` on any divergence.
- *`application_events.seq` gap-freedom* — allocated inside the event's transaction via
  `INSERT … SELECT COALESCE(MAX(seq), 0) + 1`; the `UNIQUE (application_id, seq)` index
  makes concurrent misallocation fail loudly.

---

## 4. Invariant enforcement map

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| **Truthfulness of generated documents** — every claim traceable to the profile; missing credentials never papered over | Generated lines are structured JSON `{text, source_refs[]}`; `auditDocument()` (src/audit/audit.ts) verifies each `source_ref` resolves against the pinned `profile_versions.snapshot` and that every named skill, credential, date, or number in the line appears verbatim (case/whitespace/accent-normalized) in the referenced fields; failing lines are dropped; a job whose required lines fail is failed, not published | `audit_mutation.property.ts`: mutates each field value of 20 golden profiles, regenerates, asserts ≥1 line flagged per mutation. `audit_golden.unit.ts`: asserts zero dropped lines on 20 clean (profile, job) pairs. `credential_gap.unit.ts`: job requiring a license absent from the profile ⇒ document omits it and the review screen's gap panel lists it |
| **Review gate** — no submission without explicit per-application authorization | Status transition to `submitting` occurs only inside `submitApplication()` (src/apply/submit.ts), which requires the review-authorization token minted by the user's explicit submit click (`POST /applications/:id/submit`, session-authenticated). No worker path writes `status = 'submitting'` without that token | `lifecycle_gate.integration.ts`: seeds `ready` rows, runs the worker pool for 60 s with no authorization tokens; asserts zero rows reach `submitting` |
| **No double submit** — under crashes, restarts, duplicate jobs | (a) `submitApplication()`'s transition is a conditional `UPDATE … WHERE status = 'ready'` — a racing worker's update matches zero rows; (b) `submission_intents` (`UNIQUE (application_id)`) inserted before the employer submit control is clicked; (c) the flip to `submitted` occurs only inside the transaction that inserts the `submission_receipts` row (`UNIQUE (application_id)`) | `submit_race.integration.ts`: 16 parallel workers against one application and a fake employer that counts receipts; asserts exactly 1 receipt. `chaos_kill.e2e.ts`: ≥100 runs with SIGKILL at random points across the submit window; asserts zero duplicate employer-side receipts per application |
| **Form-drift block** — reviewed form = submitted form | `reviewed_form_fingerprint` = SHA-256 of `canonicalizeForm()` (src/apply/canonicalize.ts): ordered tuples of (control type, normalized label, required flag, sorted option-set hash), dynamic tokens excluded; recomputed from the live DOM before any fill at submit time; mismatch ⇒ `failed`/`form_drift` before field writes | `drift.e2e.ts`: mutates the fake employer's form between review and submit (field added / removed / renamed / option added); asserts zero field-write events at the employer and `failure_reason = 'form_drift'` |
| **Credit safety** — no negative balances, no double charge, incl. webhook retries and concurrency | Conditional `UPDATE credit_accounts SET balance = balance - :amt WHERE user_id = :u AND balance >= :amt` (zero rows ⇒ abort); `credit_ledger` partial uniques `ledger_one_hold_per_app` and `ledger_one_terminal_per_app`; `CHECK (balance >= 0)`; `reconcileCreditLedger()` recomputation with account freeze on divergence | `credit_interleaving.property.ts`: 1,000 randomized interleavings of hold/charge/release across concurrent transactions; asserts balance never negative and exactly one of {charge, release} per application. `webhook_replay.integration.ts`: replays one `charge.succeeded` 50×; asserts exactly one ledger entry and balance delta equal to the pack size |
| **EEO quarantine** — never inferred, never sent to AI, never used in matching; exact-match fill only | `demographics` is a physically separate table, never copied into any snapshot; prompt builders (src/ai/prompts.ts) serialize only the allowlisted keys of `profile_versions.snapshot`; demographic-section fill performs exact normalized option-label matching and leaves ambiguous/absent matches blank; the scorer's input signature is `(profileVersion, job)` — no demographic parameter exists | `eeo_quarantine.property.ts`: runs the scorer with demographics present/absent; asserts identical scores. `prompt_allowlist.unit.ts`: asserts no demographics column value appears in any serialized prompt across the golden task corpus. `eeo_fill.unit.ts`: ambiguous option sets are left blank and flagged for the user |
| **Profile version pinning** — today's edits never change yesterday's basis | `documents.profile_version_id` and `applications.profile_version_id` are FK-pinned at creation; snapshots are immutable (row above); document `audit_report` records the snapshot hash it was audited against | `version_pin.integration.ts`: publishes v1, generates a document, publishes v2 with edited values; asserts the stored document content and audit report reference v1 values and are unchanged by v2 |
| **Crash recovery** — no application stuck in `submitting` | `applications.lease_expires_at` written with the transition to `submitting`; the watchdog sweep (30 s) flips `status = 'submitting' AND lease_expires_at < NOW()` to `failed`/`lease_expired` inside the transaction that inserts the credit-hold `release` entry | `lease_expiry.e2e.ts`: SIGKILLs `worker-browser` during submit; asserts `failed`/`lease_expired` within one watchdog cycle (≤30 s) and exactly one `release` entry in the ledger |
| **OTP challenge waits** — a challenge wait cannot exhaust capacity or hang | `otp_waits` row (`UNIQUE (application_id)` while unresolved, `expires_at = now() + 300 s` (A1)) created before the challenge wait; the wait holds one of the 4 context slots until resolved or expired; expiry ⇒ `failed`/`otp_timeout` + hold release + context close | `otp_expiry.e2e.ts`: fake employer challenges; no code entered; asserts failure within ≤300 s + one sweep, the slot freed (a second queued job claims within one cycle), and one `release` entry |
| **Webhook idempotency** — replayed events mutate state once | `stripe.webhooks.constructEvent()` signature verification before any DB write; `stripe_webhook_events.stripe_event_id` UNIQUE; processing guarded by `UPDATE … SET processed_at = NOW() WHERE stripe_event_id = :id AND processed_at IS NULL` | `webhook_replay.integration.ts` (above): 50× replay ⇒ one ledger entry. `webhook_reprocess.integration.ts`: kills the processor mid-event; asserts the unprocessed row is re-driven exactly once |
| **One active application per user per job** | Partial unique index `applications_one_active_per_job` | `double_queue.integration.ts`: concurrent queue attempts for the same job; asserts exactly one row and HTTP 409 (SQLSTATE 23505) carrying the existing application id on the second |
| **Reviewed answers are the submitted answers** | Fill values at submit time are read from `applications.reviewed_answers` (frozen at the `review_finalized` event), never regenerated; the drift check gates before any fill | `review_fidelity.e2e.ts`: user edits an answer at review; asserts the value received by the fake employer equals the edited value |
| **Notifications without manual refresh** | Worker transactions emit `SELECT pg_notify('app_events', jsonb)` in-transaction; `web` `LISTEN`s and fans out to the user's SSE stream filtered by `user_id` | `sse_push.e2e.ts`: state change appears on a connected `EventSource` within ≤2 s; kill-`web`-and-reconnect ⇒ trailing-50 notifications fetched from `notifications` on reconnect |
| **Tenant isolation** — a user's queries never see another tenant's rows | Every user-scoped query passes through `scopedQuery()` (src/db/scoped.ts), which injects `user_id = :sessionUser` into the WHERE clause; admin endpoints additionally assert `role = 'admin'`; the lint test `no_unscoped_user_table_access` fails CI on direct access outside `scopedQuery`/admin paths | `tenant_isolation.property.ts`: two-tenant seeded corpus; randomized session pairs; asserts every feed/document/ledger/notifications query returns only own-tenant rows |

---

## 5. Failure-mode walkthrough

**Scenario 1: Machine B dies mid-submission (submit clicked, receipt undetected).**
**What happens:**
1. Machine B power loss; `worker-browser` dies; its open Postgres transactions roll back on connection drop.
2. The `applications` row remains `status = 'submitting'` with `lease_expires_at = T + 600 s` — committed when the transition occurred, before the click.
3. The watchdog sweep (Machine A, every 30 s) matches `status = 'submitting' AND lease_expires_at < NOW()` and flips the row to `failed`/`lease_expired` inside the transaction that inserts the credit-hold `release` entry (`ledger_one_terminal_per_app` admits exactly one).
4. A `notifications` row of kind `failed` lands in-transaction; `pg_notify` fans out to the user's SSE stream: "submission interrupted; retry available".
5. Retry requires explicit user action. The retry path runs `dedupCheck()` (src/apply/dedup.ts) first: navigates to the employer's confirmation/status page; if employer-side receipt evidence is present, an explicit confirm dialog gates any further fill.
**Evidence:** `application_events` shows `lease_expired` at ≤ T + 630 s; `credit_ledger` shows exactly one terminal entry; the fake employer's receipt count is unchanged; a retry with detectable prior receipt surfaces the confirm dialog (`dedup.e2e.ts`).

**Scenario 2: The same job is queued twice.**
**What happens:**
1. Second `POST /applications` arrives while the first application is active.
2. The `INSERT` violates `applications_one_active_per_job` (SQLSTATE 23505).
3. The API maps the violation to HTTP 409 carrying the existing application id; the UI links to it.
**Evidence:** `double_queue.integration.ts`: concurrent queue attempts ⇒ exactly one row, one 409.

**Scenario 3: `worker-browser` crashes mid-fill (before submit).**
**What happens:**
1. Process dies; open transactions roll back. No status change occurred — filling precedes the `submitting` transition.
2. The job's lease (600 s) expires. `form_fill` jobs carry `max_attempts = 1`: no silent requeue. The watchdog marks the job `failed` and the application `failed`/`fill_interrupted`, releasing the credit hold in one transaction.
3. User notified; retry starts a fresh Chromium context (`browser.newContext()`). No state is reused across attempts — `context.storageState({path})` is not used to resume fills, because it persists cookies/localStorage only, not DOM
state (A2).
**Evidence:** `application_events` shows `fill_interrupted`; the fake employer counts zero partial form posts; `credit_ledger` shows one `release`. (`fill_crash.e2e.ts`.)

**Scenario 4: The employer's form changes after review.**
**What happens:**
1. User reviews answers; the `review_finalized` event freezes `reviewed_answers` and `reviewed_form_fingerprint`.
2. Employer deploys a changed form version.
3. At submit, the worker recomputes the fingerprint from the live DOM via `canonicalizeForm()` before any field write.
4. SHA-256 mismatch ⇒ worker writes `failed`/`form_drift`, releases the hold, and notifies with a re-review link. No fill occurs.
**Evidence:** `drift.e2e.ts`: employer-side fill-event log empty after drift; `failure_reason = 'form_drift'`; one `release` entry.

**Scenario 5: A Stripe webhook is replayed.**
**What happens:**
1. Stripe retries `charge.succeeded` after a timed-out ack.
2. `POST /webhooks/stripe`: `stripe.webhooks.constructEvent(payload, sig, endpointSecret)` verifies the signature; invalid ⇒ HTTP 400 before any DB write.
3. `INSERT INTO stripe_webhook_events` hits `UNIQUE (stripe_event_id)` ⇒ duplicate detected.
4. Duplicate path reads `processed_at`: non-NULL ⇒ HTTP 200 ack, no ledger writes. NULL ⇒ the guarded `UPDATE … WHERE processed_at IS NULL` admits exactly one processor.
5. Credit mutation occurs only inside that processing transaction; the ledger partial uniques make a second insert impossible even if two processors raced.
**Evidence:** `webhook_replay.integration.ts`: 50× replay ⇒ exactly one ledger entry; account balance delta equals the pack size.

**Scenario 6: The LLM returns garbage or times out.**
**What happens:**
1. Every AI task call enforces a 60 s timeout (`AbortSignal.timeout(60000)`) and validates the response against the task's zod schema.
2. Timeout/5xx ⇒ exactly one retry after 5 s backoff on the same tier. Schema-invalid output ⇒ exactly one retry with the validator error appended to the prompt. Still invalid ⇒ one escalation to the strong tier (task-scoped). Still invalid ⇒ job `failed` with `last_error`; user-facing explanation; explicit retry available.
3. Document tasks additionally pass `auditDocument()`; audit failure ⇒ one regeneration, then job `failed`. No partial document is ever published.
4. Every attempt's tokens and cost land in `ai_costs`; the dispatcher checks cumulative per-application cost against the $0.08 block / $0.10 hard-stop before each dispatch (§6.6).
**Evidence:** `llm_failure.unit.ts` asserts the exact retry/escalation counts per failure class; golden-run reports record escalation counts; `ai_costs` rows exist per attempt (`ai_costs_audit.unit.ts`).

**Scenario 7: The browser hangs (employer page never resolves).**
**What happens:**
1. Every navigation and locator operation carries an explicit timeout: `page.goto({timeout: 30000})`, Playwright `actionTimeout` 10 s.
2. Timeout throws `TimeoutError`; the worker marks the job `failed` with `last_error = 'timeout:<operation>'`.
3. The whole-job lease (600 s) backstops any path that escapes operation timeouts: watchdog expiry ⇒ application `failed`/`automation_timeout` + hold release.
4. `context.close()` runs in a `finally`; Machine B restarts daily at 03:00 UTC (systemd timer) to bound memory growth.
**Evidence:** the chaos suite includes a hung-employer fixture (accepts connections, never responds); asserts failure within ≤30 s of the hang and one hold `release`. (`hang.e2e.ts`.)

**Scenario 8: The OTP TTL expires with no code entered.**
**What happens:**
1. Worker detects the challenge (code control appears after the submit click); inserts the `otp_waits` row (`expires_at = now() + 300 s`, per-employer override `companies.otp_ttl_seconds` (A1)); the context slot is held; `status` remains `submitting` with `lease_expires_at` extended to cover the wait.
2. User alerted via SSE (`otp_requested`); enters the code; worker validates format (6–8 digits), stores `code_hash`, fills via `locator.fill()`, completes the submission, writes the receipt.
3. If `expires_at` passes without a code: the watchdog sweep flips `failed`/`otp_timeout`, releases the hold, closes the context, frees the slot.
4. Retry is explicit; it re-derives answers from `reviewed_answers` (no regeneration) and re-enters the flow.
**Evidence:** `otp_expiry.e2e.ts`: failure within ≤300 s + one sweep; slot freed (second queued job claims within one cycle); one `release` entry. `otp_success.e2e.ts`: code entry within TTL completes submission with exactly one receipt.

**Scenario 9: PostgreSQL restarts with in-flight transactions.**
**What happens:**
1. Open transactions roll back; committed state is unchanged. Ledger integrity holds because every credit mutation is atomic with its ledger entry.
2. Worker claim loops see connection errors; backoff schedule 5 s → 15 s → 45 s (cap), infinite retries with jitter; systemd `Restart=always` restarts crashed processes.
3. Leases written before the restart expire on schedule; the watchdog (separate process) resumes sweeps; expired leases resolve per Scenarios 1 and 3.
4. SSE streams drop; `EventSource` auto-reconnects; the UI fetches the trailing-50 `notifications` rows on reconnect, so no notification is lost.
**Evidence:** the chaos suite includes a Postgres restart mid-transaction; asserts `reconcileCreditLedger()` reports zero divergence afterwards and recovery within ≤2 watchdog cycles (≤60 s). (`pg_restart.e2e.ts`.)

---

## 6. AI strategy

### 6.1 Task-to-model map

| Task | Tier | Median tokens (in/out) | Notes |
|---|---|---|---|
| Resume extraction | cheap | 5K / 4K | Output: structured fields with per-field source spans; resumes extracting >20K tokens are rejected at upload with an explanation (A7) |
| Tailored resume | cheap, escalate strong | 5K / 2.5K | Output: structured document model, every line `{text, source_refs[]}` |
| Cover letter | cheap, escalate strong | 4.5K / 1.2K | Same constraint |
| Form-field answering | cheap, escalate strong | 6K / 1.5K per chunk | Chunked ≤8 fields per call; confidence score per answer; low-confidence required answers self-block to review |
| Judge/eval scoring | strong | rubric prompts | Golden-set scoring and shadow scoring only; never on the production hot path |
| Explanation polishing | none | — | Feed explanations are deterministic templates (§6.4) |

Model versions are pinned in config (`ai/tiers.json`). A re-pin ships only through the
promotion gate (§6.3) with the golden-run report attached.

### 6.2 Golden sets (quality corpora)

- **Extraction:** 12 resumes with hand-verified expected structures; checks are field-level
  exact match on employers, dates, skills, credentials.
- **Document generation:** 20 (profile, job) pairs; deterministic checks: schema validity,
  `auditDocument()` pass rate = 100% (zero dropped lines), required-credential omission
  check; judge rubric (strong tier): truthfulness, relevance, professionalism, each 1–5.
- **Form answering:** 15 captured real forms (anonymized) with hand-authored expected
  answers; checks: required-field coverage, exact-value match where answerable,
  blank-where-unanswerable.

### 6.3 Promotion gate (any model or prompt change)

A candidate ships only if, on the full golden corpora: deterministic-check pass rate ≥95%,
judge mean ≥4.2/5 with no rubric dimension <3.5, and cost-per-task ≤1.5× the incumbent.
Otherwise the incumbent stays pinned. Nightly, the pinned model re-runs the corpora
(provider-side drift); alarm on deterministic pass rate <100% or judge-mean drop >0.3.
Weekly, 2% of production document outputs are shadow-scored by the strong tier; alarm on
mean drop >0.3 below the golden baseline.

### 6.4 Ranking: deterministic, measured, regression-tested

Scoring is a weighted factor function over structured data only — no LLM in the ranking
path. Factors (weights): skills 0.50 (weighted Jaccard of normalized skill sets), location
0.25, work arrangement 0.15, employment type 0.05, salary overlap 0.05. Location rules:
remote jobs compatible with any user location; hybrid/onsite require distance ≤ the
user's stated radius (default 50 km) or relocation preference = yes; missing/ambiguous
location data scores the location factor neutral (0.5) and keeps the job visible with a
"location unclear" flag — weak inference never hides a job. Explanations are template
strings assembled from factor deltas ("strong match on 5/6 required skills; falls short:
onsite requirement in city X").

Quality measurement: (a) **offline** — 30 golden (profile, job-set) pairs with
hand-validated expected orderings; metric: pairwise inversions on mandatory pairs (gate:
0) and Kendall τ vs the incumbent scorer version (gate: no regression); runs nightly and
on every scorer change. (b) **online** — implicit labels: application queued = +1,
dismissed = −1; weekly precision@10 report of applied-vs-dismissed; alarm on drop >10
points week-over-week (dashboard + digest).

### 6.5 Output constraint and checking

- Every task response is schema-validated (zod) before use; schema-invalid ⇒ retry/
  escalation per Scenario 6.
- Document lines carry `source_refs`; `auditDocument()` is deterministic and in-process;
  it runs on every generation before the document reaches `ready`.
- Form answers carry a per-field confidence score; required fields below the confidence
  threshold (0.8, calibrated on the form-answering golden set) are left blank and surfaced
  at review — never guessed.

### 6.6 Cost arithmetic and caps

Unit prices (current commodity pricing, pinned at build time (A4)): cheap tier
$0.40/M input, $1.60/M output; strong tier $2.00/M input, $8.00/M output.

Per-task cost at median tokens:

| Task | Arithmetic | Cost |
|---|---|---|
| Extraction | 5K×$0.40/M + 4K×$1.60/M | $0.0020 + $0.0064 = **$0.0084** (per user, amortized) |
| Tailored resume | 5K×$0.40/M + 2.5K×$1.60/M | $0.0020 + $0.0040 = **$0.0060** |
| Cover letter | 4.5K×$0.40/M + 1.2K×$1.60/M | $0.0018 + $0.0019 = **$0.0037** |
| Form answering (median 40 fields = 5 chunks) | 5 × (6K×$0.40/M + 1.5K×$1.60/M) | 5 × ($0.0024 + $0.0024) = **$0.0240** |
| One escalated chunk (strong tier) | 6K×$2.00/M + 1.5K×$8.00/M | $0.0120 + $0.0120 = **$0.0240** |

**Median per prepared application** (documents once + form answering):
$0.0060 + $0.0037 + $0.0240 = **$0.0337**.
**P95** (long resume, 60-field form = 8 chunks, one document regeneration, one
escalation): $0.0197 + $0.0384 + $0.0240 = **$0.0821**.
**Worst realistic** (two document regenerations, three escalated chunks):
$0.0296 + $0.0384 + 3×$0.0240 = **$0.1104** — exceeds budget, which is why the cap
exists.

**Cap mechanism:** the task dispatcher sums `ai_costs.cost_micros` per application before
each dispatch. At cumulative ≥$0.08, further regenerations and escalations are blocked and
the user sees why (the current draft stands). At ≥$0.10, remaining tasks abort and the
application fails with `failure_reason = 'cost_cap'`, releasing the hold.

**Monthly LLM spend at brief scale** (~300 prepared applications/week): median mix
300×$0.0337/week ≈ $10/week ≈ **$43/month**; P95-weighted mix ≈ **$70/month**. Inside
the budget with ≥3× headroom at median.

---

## 7. Testing and release confidence

### 7.1 Pyramid and schedule

| Level | Contents | Runs |
|---|---|---|
| Unit | Deterministic functions: scorer factors, `canonicalizeForm()`, `auditDocument()`, credit math, prompt builders, `dedupCheck()` classification | every change (CI), target <90 s |
| Property | `audit_mutation`, `credit_interleaving` (1,000 randomized interleavings), `tenant_isolation`, `eeo_quarantine`, `prompt_allowlist`, `version_pin` | every change (CI) |
| Integration | Lifecycle gates under concurrency (16-worker races), webhook replay storms, drift/OTP fixtures against the embedded fake employer (in-process HTTP server) | every change (CI) |
| Golden-set AI evals | Per-task corpora (§6.2): deterministic checks + judge scoring; promotion gates apply | nightly |
| Ranking regression | 30 golden ordering pairs; mandatory-pair inversions = 0; Kendall τ ≥ incumbent | nightly + every scorer change |
| Chaos suite | Full loop against the fake employer with toggles (drift, OTP, hang, partial-state); chaos driver: SIGKILL of `worker-browser`/Postgres at random points across the submit window; Postgres restart mid-transaction | nightly; full ≥100-run gate before launch |
| Restore drill | Latest `pg_dump` restored into scratch Postgres; schema diff + row-count diff; report archived to `backups_log` | weekly |

CI: GitHub Actions (free tier) with a Postgres 16 service container; nightly jobs via
scheduled workflows. The embedded fake employer implements a Greenhouse-style form:
standard controls, a drift toggle, an OTP-challenge toggle, a hang toggle, and a
submission counter used as ground truth.

### 7.2 Pre-launch proofs (explicit)

**No-double-submit proof.** Chaos gate, ≥100 runs with random kill points across the
submit window. Assertions per run: employer-side receipt count ≤1 per application; zero
orphan credit holds (`reconcileCreditLedger()` clean); zero applications in `submitting`
beyond lease expiry + one watchdog sweep. All assertions must hold in every run.

**Crash-recovery proof.** Same suite. Assertions: every interrupted application reaches a
terminal state within ≤2 watchdog cycles (≤60 s); retry paths exist at every terminal
state; `dedupCheck()` surfaces the confirm dialog whenever employer-side evidence is
present.

**Ledger-safety proof.** `credit_interleaving.property.ts` (1,000 interleavings) plus the
50× webhook replay storm: balance never negative; exactly one of {charge, release} per
application; replayed events mutate state once.

---

## 8. Delivery phases

Phases close on verifiable evidence, never on dates. **Phase 4 is the first end-to-end
run** (signup → upload → match → prepare → review → submit) against the embedded fake
employer. Everything placed before it is a prerequisite of that loop: extraction and
versioning (Phase 1) feed matching; matching (Phase 2) feeds the feed; documents
(Phase 3) gate submission. No pre-Phase-4 item is optional.

**Phase 0 — Foundations.** Repo/CI, schema migrations, secrets file, backup timer +
restore drill, worker heartbeats, admin health skeleton. Justification: the brief makes
backups and a tested restore path launch requirements; standing them up before user data
lands makes the restore drill meaningful and cheap.
*Exit criteria:* schema migrated on staging; a nightly `pg_dump` artifact present in B2;
restore drill completed with zero schema diff and zero row-count diff; worker heartbeats
visible in the admin dashboard; a watchdog sweep observed in the admin event log.

**Phase 1 — Identity + profile.** Authn (Argon2id, sessions), resume upload/extraction
(pdfjs-dist/mammoth), draft-edit UI with per-field source spans, publish → immutable
version.
*Exit criteria:* upload a resume and see an extracted draft; edit any field; publish and
confirm a new immutable `profile_versions` row exists; re-upload and confirm v1 and v2
both resolve to their original content; a scanned-PDF fixture is rejected with an
explanation.

**Phase 2 — Discovery + matching.** Board ingestion (500 seeded companies (A3)),
enrichment,
deterministic scorer, feed with filters, dismissal/restore, share-URL views.
*Exit criteria:* a 24 h soak ingests ≥95% of live postings on seeded boards (measured
against board counts); feed ranks the golden ordering sets with zero mandatory-pair
inversions; dismissal/restore round-trips; a shared-URL view reproduces its filters; a
location-ambiguous fixture stays visible with its flag.

**Phase 3 — Documents.** Generation with provenance, `auditDocument()`, PDF/DOCX
rendering, approval.
*Exit criteria:* generated resumes pass audit with zero dropped lines on clean pairs;
the mutation property test flags ≥1 line per mutated field; PDF/DOCX golden fixtures
render non-Latin scripts (CJK, Arabic, accented Latin (A11)) without missing glyphs
(visual
diff against reference renders); regeneration history preserves earlier generations.

**Phase 4 — First end-to-end run (fake employer).** Full loop; credits
hold/charge/release; SSE notifications.
*Exit criteria:* the full loop completes against the fake employer; the audit trail is
complete (every lifecycle event present in `application_events`); the ledger shows
hold→charge with consistent `balance_after`; cancellation at every stage leaves no
orphan hold.

**Phase 5 — Hardening.** Drift/OTP paths, webhook idempotency, chaos suite, crash-
recovery proofs.
*Exit criteria:* the chaos gate passes ≥100 runs with every §7.2 assertion; the drift
toggle blocks with `form_drift` and zero employer-side field writes; the OTP flow
completes on code entry and expires on silence; the 50× webhook replay storm yields
exactly one ledger entry.

**Phase 6 — Billing + account operations.** Stripe Checkout + webhooks, credit packs,
SSE notifications incl. credits-low, data export, account deletion.
*Exit criteria:* the purchase→hold→charge→release cycle completes under the replay
storm; deletion removes user rows and files (verified by direct SQL) leaving only the
anonymized `deletion_records` row; the export archive round-trips (re-import dry-run
parses).

**Phase 7 — Launch readiness.** Admin hardening, cost/quality gates live, runbooks,
2× peak scale test.
*Exit criteria:* the runbook executes once end-to-end (deploy, backup, restore, scale at
2× peak: 400 simulated concurrent users against staging); the cost dashboard shows
per-application spend within caps across the scale run; seeded faults (queue-depth,
stale-heartbeat) raise the corresponding alarm rows.

---

## 9. Security and privacy

- **Authn:** Argon2id (m = 19 MiB, t = 2, p = 1) via `@node-rs/argon2`. Sessions: 256-bit
  random bearer tokens, stored SHA-256-hashed in `sessions.token_hash`; sliding lifetime
  30 days; revoked on password change and account deletion. Signup emails carry a 6-digit
  code valid 15 min, single-use (consumed by conditional update).
- **Authz:** session `user_id` scopes every user query through `scopedQuery()`;
  `role = 'admin'` gates `/admin/*`; application submission requires the explicit review-
  authorization token (§4 review gate).
- **Tenant isolation:** single-schema Postgres with `user_id` FKs everywhere;
  `scopedQuery()` (src/db/scoped.ts) is the only path to user-scoped tables; the lint test
  `no_unscoped_user_table_access` fails CI on direct access outside `scopedQuery`/admin
  paths.
- **Secrets:** environment injection from a root-owned `0600` file (§1.10); 8 secrets;
  quarterly rotation runbook.
- **Encryption at rest:** LUKS2 full-disk encryption on both VMs; stored user files
  (resume originals, rendered documents) additionally AES-256-GCM encrypted
  application-side (`node:crypto` `aes-256-gcm`, random 96-bit nonce per row) under the
  file-encryption master key; quarterly re-encryption job rewrites rows under the new key
  with a dual-read window.
- **Webhook verification:** `stripe.webhooks.constructEvent(payload, sig, endpointSecret)`
  before any DB write; invalid signature ⇒ HTTP 400. Idempotency per §4.
- **Rate limits** (Postgres `rate_limits` daily/hourly buckets): signup 5/hour/IP;
  document generation 20/day/user; application queueing 10/day/user; purchases
  5/day/user; authenticated API 60 req/min/user.
- **Transport:** TLS on every endpoint (Let's Encrypt certificates, auto-renew via
  systemd timer); HSTS `max-age=63072000`.
- **Data export/deletion:** export = JSON archive of profile versions, documents,
  applications + events, ledger entries, notifications. Deletion = transactional cascade
  deleting user rows and file bytes, inserting the anonymized `deletion_records` row
  (id, timestamp, row counts — no personal data). External subscriptions canceled via
  Stripe before the cascade.
- **Retention:** closed postings and their scores purged after 90 days (A12); webhook
  payloads blanked after 30 days (event ids retained); `worker_heartbeats` truncated
  daily; Machine B trace files purged after 7 days.

---

## 10. Risk register

| # | Risk | Planned mitigation (mechanism) |
|---|---|---|
| 1 | Employer form diversity beyond Greenhouse-style (unusual controls) leaves fill coverage gaps | Generic control reading over standard HTML inputs/selects/textareas only; required fields without a confident answer self-block to review (§6.5 threshold 0.8); per-employer coverage metric in admin; uncovered employers are excluded from full-auto while manual review remains available |
| 2 | Bot detection (Cloudflare-style challenges) blocks automation | Real (headless) Chromium with default user agent (A5); per-employer allowlist; failures surfaced with explanation; `form_fill` `max_attempts = 1` — never blind-retried |
| 3 | Employer ATS persists partial submissions server-side across sessions | Fresh context per attempt (no state reuse); drift fingerprint compares structure, not values; retry re-reads live field values into the draft; the review gate re-asserts before submit |
| 4 | LLM provider pricing/quality drift shifts cost or quality | Pinned versions; nightly golden re-runs with drift alarms; promotion gates gate every change; the $0.08/$0.10 cost caps bound exposure per application |
| 5 | Chromium memory growth on Machine B | Context recycled per job; daily restart 03:00 UTC; capacity alarm at ≥3 simultaneously occupied contexts; Machine B sized 8 GB with ~4 GB headroom over the 4-context limit |
| 6 | Postgres single point of failure (Machine A) | Nightly backups + weekly restore drills; systemd `Restart=always` on every process; restore-promotion runbook with RTO measured by the drills (target <4 h) |
| 7 | Stripe account/region limits block billing | Standard provider in the launch region (A9); test-mode gates in CI; webhook idempotency bounds the blast radius of provider retries |
| 8 | Employer OTP TTL variance (shorter validity than assumed) | Default TTL 300 s with per-employer override `companies.otp_ttl_seconds`; expiry never auto-resubmits; retry path explicit; overrides set from observed employer behavior |
| 9 | Resume extraction failure (scanned PDFs, exotic layouts) breaks onboarding | Text-length threshold (<200 extracted chars ⇒ reject with explanation); per-field extraction confidence displayed in the draft; manual-entry fallback for every field |
| 10 | Single-operator bus factor during incidents | Runbooks for every operational action (deploy, restore, rotate, scale); daily 08:00 UTC digest with actionable alarm text; the chaos suite doubles as incident rehearsal |

---

## 11. Explicit tradeoffs

Weaker behavior knowingly delivered, each with rationale:

1. **Scanned/image-only resumes are rejected at upload** (no OCR in v1). The brief permits
   PDF/DOCX upload; this accepts text-based files only. Rationale: OCR quality variance
   undermines extraction trust; rejection with an explanation is the honest behavior, and
   the manual-entry fallback covers the user.
2. **Client-side death after employer-side receipt** marks the application `failed` with a
   dedup-guarded retry requiring explicit user confirmation. The brief's no-double-submit
   holds (no auto-resubmit), but the user experiences failed-then-confirm instead of a
   clean success when employer-side confirmation is detectable, and failed-then-retry
   when it is not. Rationale: certainty of non-duplication outranks submission
   convenience.
3. **Employer-side confirmation emails are not consumed** (no mailbox access). Dedup relies
   on form-revisit evidence where the employer exposes a confirmation/status page;
   employers without one rely on explicit user confirmation at retry.
4. **Partially filled forms are abandoned, never resumed** (fresh context per attempt). If
   an employer persists partial state, a retry may surface pre-filled fields; the drift
   check compares structure, so a structure-identical form passes and the user re-reviews
   values before submit — the review gate holds.
5. **Ranking explanations are deterministic templates**, not LLM prose. Truthful and
   auditable, less fluent. Rationale: explanation claims are product promises; template
   assembly from factor deltas cannot assert what the scorer did not compute.
6. **Single-region deployment** — no multi-region failover. RTO is via restore, target
   <4 h, measured by weekly drills. The brief specifies single region.

## 12. Where this is stronger than required

1. **Application-level AES-256-GCM atop LUKS2** for stored user files (the brief's
   "protect them at rest" is satisfied by LUKS alone). Adds key rotation and per-row
   isolation; cost: negligible at this data volume.
2. **`pg_notify`-sourced instant notification fan-out** (the brief requires updates
   without manual refresh; polling suffices). Push delivery with zero added
   infrastructure.
3. **Weekly automated restore drills with diff reports** (the brief requires a tested,
   documented restore path). Recurring proof replaces a one-time test.
4. **Per-application cost caps enforced at dispatch** (the brief states the budget as a
   goal). The caps convert the goal into a constraint with defined over-budget behavior.
5. **Weekly 2% shadow scoring** (the brief requires quality measurement). Drift detection
   beyond golden-set re-runs.
6. **Submission receipts retaining employer-side evidence** (success URL + confirmation-
   text hash; the brief requires proof of what was submitted). Receipts additionally power
   `dedupCheck()` retries.

## 13. Assumptions

Every place the brief was silent, decided inline above and collected here:

1. **(A1)** Employer OTP code validity ≥300 s; TTL set accordingly, with per-employer
   overrides where observed otherwise.
2. **(A2)** Employer ATS forms do not persist partial submissions server-side across
   sessions; the fresh-context design assumes it, and drift + re-review bound the
   violation case.
3. **(A3)** Greenhouse-style boards expose public JSON endpoints or parseable HTML;
   ingestion uses JSON where present, HTML parsing otherwise.
4. **(A4)** Commodity pricing at the stated unit prices holds for the pinned models;
   re-pinning is gated by the promotion gates.
5. **(A5)** Employer sites tolerate real (headless) Chromium without aggressive bot
   detection at launch volumes (tens to low hundreds of submissions/day).
6. **(A6)** ~800 MB per Chromium context on the target hardware class (4 vCPU / 8 GB);
   the 4-context limit derives from it.
7. **(A7)** Real resumes extract to <5K tokens; the 20K-token extraction cap rejects
   outliers at upload with an explanation.
8. **(A8)** Single-region latency is acceptable (<150 ms to employer sites).
9. **(A9)** Stripe and Resend are available in the launch region at entry-tier pricing.
10. **(A10)** All timestamps UTC; operator digest at 08:00 UTC.
11. **(A11)** User base is English-primary but Unicode-safe end to end (the brief
    requires any-language content handling).
12. **(A12)** Closed-posting retention of 90 days is acceptable (the brief is silent on
    retention).
