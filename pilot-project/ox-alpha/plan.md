# Pilot — Engineering Plan (ox-alpha)

One operator, one region, ~$200/month all-in ceiling. Every number below has a derivation;
every mechanism claim names the constraint, lock, ordering, or function that enforces it.

---

## 1. Technology decisions

### 1.1 Language and runtime
**Choice:** TypeScript on Node.js 22 LTS, strict mode, ESM. One language for web UI, workers,
and test tooling, in a pnpm-workspace monorepo (`apps/web`, `apps/worker`, `apps/fakeats`,
`packages/db`, `packages/core`, `packages/prompts`).
**Rejected:** Python 3.12 + FastAPI + Celery. The strongest competitor: best-in-class AI
ecosystem and Celery is battle-tested. Rejected because the browser automation surface
(Playwright), PDF rendering (Chromium), and the web UI would then span two runtimes with two
dependency trees, two upgrade cadences, and no shared types for the data model — an
operability tax a single operator pays forever.
**Why:** minimizing distinct moving parts outranks marginal library differences; the invariants
live in PostgreSQL, so language choice carries no invariant burden.

### 1.2 Web framework
**Choice:** Next.js 15 (App Router), React Server Components + route handlers. UI, REST-style
route handlers, and the SSE notification endpoint live in one process.
**Rejected:** Fastify API + separate React SPA. Genuinely cleaner API boundaries, rejected
because it doubles deployment units, adds CORS/session plumbing, and buys nothing at
~200 weekly active users.
**Why:** one deployable web artifact; server-side session checks happen before any page render,
which keeps the authorization gate simple to audit.

### 1.3 Datastore
**Choice:** PostgreSQL 17 with extensions `pgcrypto`, `citext`, `vector` (pgvector). All
state — relational data, queue (`pgboss` schema), vectors, rate-limit counters, sessions — in
one database.
**Rejected (strongest):** PostgreSQL + Redis (BullMQ). Redis gives richer queue features
(priority lanes, rate-limited consumers) out of the box. Rejected because every hard invariant
in §4 (credit holds, submit gating, lease recovery) must hold across "enqueue" and "state
transition", and spanning those across two systems means no shared transaction boundary and a
divergence class (job visible in Redis, row not committed) that a solo operator must debug at
3 a.m. `SELECT … FOR UPDATE SKIP LOCKED` covers our throughput (≤ hundreds of jobs/hour)
inside the same ACID boundary.
**Rejected:** SQLite. Zero-ops and attractive at this scale for reads, but the web process and
workers are concurrent writers and the §4 mechanisms depend on row-level locking and partial
unique indexes under concurrency; SQLite's single-writer model serializes them at the file level
and offers no equivalent of `FOR UPDATE SKIP LOCKED`.
**Why:** every invariant becomes a constraint, index, or conditional update in one database that
has exactly one backup/restore path.

### 1.4 Migrations and query layer
**Choice:** `dbmate` plain-SQL migrations (forward-only, paired down-migrations kept but never
auto-run) + `pg` (`node-postgres`) `Pool` with hand-written parameterized SQL in repository
modules under `packages/db/src/repo/*`.
**Rejected:** Drizzle ORM. Typed queries are nice; rejected because the §4 invariants live in
raw DDL (triggers, partial indexes, RLS policies), and an ORM layer that re-expresses them is a
second place for them to drift.
**Why:** the DDL in §3 *is* the source of truth; repositories are thin, greppable functions.

### 1.5 Queue and scheduling
**Choice:** pg-boss v10 on the same PostgreSQL instance. Producers call
`boss.send(name, data, {singletonKey, startAfter, cron, retryLimit, retryDelay, expireInSeconds,
priority})`; consumers register `boss.work(name, {batchSize}, handler)`. Recurring board crawls
use `boss.send('crawl-board', {boardId}, {cron: <staggered>, singletonKey: boardId})`.
Queue depth is read directly from the `pgboss.job` table
(`SELECT name, state, count(*) FROM pgboss.job GROUP BY 1,2`).
**Rejected:** hand-rolled polling table. Full control over leasing semantics, but reimplements
retry backoff, cron staggering, and dead-letter handling that pg-boss ships; the submission
path's correctness comes from the `applications` row state machine (§4), not from the queue.
**Why:** at-most-once *execution* is enforced by the application row CAS transitions; pg-boss
supplies at-least-once *delivery*, retries, and schedules without a second datastore.

### 1.6 Browser automation
**Choice:** Playwright (Chromium, headless). One `browser.newContext()` per attempt with
`recordVideo: {dir}` for evidence; `context.tracing.start({screenshots: true, snapshots: true,
sources: false})` and `tracing.stop({path})` per attempt; `page.getByLabel()`,
`page.setInputFiles()`, `page.selectOption()`, `page.check()` for field interaction;
`page.waitForResponse()` around the final POST; `context.storageState({path})` to persist
employer-session cookies for the OTP resume path. Browsers are used **only** for the submit
pool — crawling uses plain HTTP.
**Rejected:** Puppeteer. Comparable core automation; rejected because it has no equivalent of
`context.tracing` (a time-travel DOM/network trace is our primary post-mortem artifact) nor
built-in per-context video recording.
**Rejected:** driving crawls through the browser too. 20,000 postings/day through Chromium
would consume the entire RAM budget; Greenhouse exposes structured JSON
(`https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`), so crawls use `fetch`
(undici) parsed with `cheerio.load(html)` for HTML fallbacks.
**Why:** evidence (video + trace) turns "the employer says they never got it" into an
inspectable recording; keeping browsers off the crawl path bounds memory to the submit pool.

### 1.7 AI models
**Choice:** OpenAI API. Primary model `gpt-4.1-mini` (assumed pricing 2026-08: $0.40/$1.60 per
1M input/output tokens), escalation model `gpt-4.1` ($2/$8), embeddings `text-embedding-3-small`
($0.02/M). Structured tasks call `client.chat.completions.create({...},
response_format: {type: 'json_schema', json_schema: {name, strict: true, schema}})` so malformed
JSON cannot occur; prose tasks pin temperature 0.7, extraction/audit 0. Per-call abort via
`AbortController` at 60 s.
**Rejected:** self-hosted open-weight models on the same box. Attractive for zero marginal cost,
rejected on arithmetic: an 8 vCPU / 16 GB host running 4–6 Chromium contexts has ~2 GB spare; a
quantized 7B model needs ~5 GB and delivers lower extraction fidelity than `gpt-4.1-mini`
(measured on our eval harness before any spend decision — see §6.3).
**Rejected:** Anthropic Claude Haiku. Roughly price/performance-equivalent; rejected because
running one provider means one prompt-regression harness, one billing alarm, one outage rung.
**Why:** the budget ($0.10/application) is met with 10× headroom (§6.4); quality is gated by the
eval harness, not by brand.

### 1.8 Document rendering
**Choice:** Documents are generated as semantic HTML, then: PDF via the already-running
Chromium — `page.setContent(html)`, `page.emulateMedia({media: 'print'})`,
`page.pdf({format: 'Letter', printBackground: true})`; DOCX via the `docx` npm package
(`new Document({sections})` + `Packer.toBuffer`). System fonts `fonts-noto-core`,
`fonts-noto-cjk` installed on the host; Chromium shapes complex scripts (HarfBuzz), so Arabic,
CJK, Devanagari, and accented Latin render correctly in both paths.
**Rejected:** `pdfkit`. Lighter, no browser needed, rejected because it lacks bidirectional text
and complex-script shaping — it silently mangles non-Latin names, which the brief explicitly
requires.
**Rejected:** LibreOffice headless for DOCX→PDF. ~700 MB install and slow cold starts to save
one code path we already need Chromium for.
**Why:** one renderer, correct Unicode, zero extra services.

### 1.9 Hosting and deployment shape
**Choice:** One Hetzner CPX41 (8 vCPU / 16 GB / 240 GB, ≈ $34/month), Ubuntu 24.04, Docker
Compose with services `caddy`, `web`, `worker`, `postgres` (plus `fakeats` in staging profile
only). Caddy terminates TLS (automatic Let's Encrypt). Deployment =
`scripts/deploy.sh`: `git pull && dbmate up && docker compose build && docker compose up -d &&
scripts/smoke.sh`.
**Rejected:** Fly.io / managed Postgres. Managed Postgres alone is ≥ $20/month for a smaller
instance and adds network latency between queue and workers; fly.io machines add a second
platform to learn for zero capacity gain at this size.
**Second-machine trigger (decided now, not later):** when measured browser-slot occupancy p95
exceeds 5 of 6 slots for 7 consecutive days, add a second CPX41 running `worker` only, pointed
at the same Postgres. Nothing in the design assumes co-location: leases, CAS transitions, and
locks are all in Postgres.
**Why:** $34 buys the whole stack; the trigger converts "we'll see" into a measurable rule.

### 1.10 Backups
**Choice:** pgBackRest: nightly full backup + continuous WAL archiving (`archive_timeout =
60 s`) to Backblaze B2 (≈ $3/month at 500 GB, 30 nightly + 12 monthly retained). Restore point
objective ≤ 5 minutes; `ops/restore-drill.sh` restores the latest backup into a throwaway
Postgres container and runs smoke assertions.
**Rejected:** Hetzner automated snapshots. Whole-VM granularity, no PITR; a 06:00 snapshot loses
everything up to the next dawn, which includes same-day submissions and payments.
**Why:** the brief makes tested restores a launch requirement; continuous WAL makes the restore
test meaningful rather than theatrical.

### 1.11 Outbound email
**Choice:** Postmark, `postmark.ServerClient(token).sendEmail(...)`, MessageStream `outbound`
($15/month up to 10,000 emails; our volume ≤ 50/day).
**Rejected:** Amazon SES ($0.10/1k). 100× cheaper, rejected because production deliverability
(on our own domain reputation, bounce handling, warmup) is days of one-operator work to save
≈ $15/month.
**Why:** OTP-needed alerts must arrive in minutes; deliverability is the feature.

### 1.12 Payments
**Choice:** Stripe Checkout Sessions (`stripe.checkout.sessions.create({mode: 'payment'}`
for packs, `{mode: 'subscription'}` for plans), webhooks verified with
`stripe.webhooks.constructEvent(rawBody, sig, whsec)`; card data never reaches our servers
(hosted Checkout page).
**Rejected:** Paddle / Lemon Squeezy (merchant-of-record). They absorb global sales tax, which
matters internationally; rejected because the brief's market is single-region US (§13), their
fee delta is ~2–3 points, and webhook-based credit accounting is identical work either way.
**Why:** brief names Stripe; hosted Checkout minimizes PCI surface to zero.

### 1.13 CI
**Choice:** GitHub Actions. On every push: typecheck, lint, unit, integration
(testcontainers Postgres), fast E2E subset. Nightly: full E2E matrix against FakeATS scenario
variants, crash/duplicate-delivery drill suite, restore drill, AI eval harness.
**Rejected:** self-hosted runner on the production box. Saves minutes of free-tier CI; rejected
because a runaway test suite must never share memory with production Chromium.

---

## 2. System architecture

### 2.1 Processes

| Process | Responsibility | Concurrency limits |
|---|---|---|
| `caddy` | TLS termination, static assets, reverse proxy | n/a |
| `web` (Next.js) | Authn, profile editor, feed, document review, application review/authorize screens, admin pages, SSE endpoint `/api/stream`, Stripe webhook receiver, export/deletion APIs | pg pool max 10; `statement_timeout` 15 s |
| `worker` (single Node process, four pools) | All async work; pools sized independently | crawl 2 boards in flight · ai 3 concurrent LLM calls · browser 6 contexts (4 general + 2 OTP-reserve) · maintenance loop |
| `postgres` | All state, queue, vectors, LISTEN/NOTIFY bus | `shared_buffers` 2 GB; worker pool max 20 |
| `fakeats` (staging only) | Scriptable fake employer used by tests and drills | n/a |

### 2.2 Communication
- Web → worker: `boss.send(...)` after the initiating transaction commits (e.g., queue-time tx
  inserts the `applications` row + credit hold, then sends `prepare-application`). If the
  process dies between commit and send, the maintenance pool's `requeueStuckQueued()`
  (runs every 30 s) finds `status='queued'` rows older than 120 s with no live job
  (`singletonKey = app id`) and re-sends; the handler is idempotent (checks row status first).
- Worker → web/user: writes `notifications` row; `AFTER INSERT` trigger `notify_user()` runs
  `pg_notify('user_events', payload)`; the web SSE endpoint holds one dedicated `pg` client
  `LISTEN`ing on `user_events`, filters by session user, emits events; keepalive comment every
  15 s. Unread badge updates without refresh via this channel.
- Pools never share work: pg-boss job names map 1:1 to pools
  (`crawl-board` → crawl, `extract-resume`/`generate-doc`/`explain-match` → ai,
  `submit-application`/`resume-otp` → browser, timers → maintenance). Priorities on send:
  otp_resume 5, submit 4, docs 3, extract/explain 2, crawl 1.

### 2.3 How browser-heavy work fails to starve everything else
Crawling and enrichment do pure HTTP (undici) — they never allocate a browser. Only
`submit-application` and `resume-otp` touch Chromium, capped at 6 contexts (RAM derivation in
§2.4). A hung page cannot block other pools because pools are separate `boss.work()`
registrations with independent semaphores; a hung *worker* stops at the per-action timeouts and
the 420 s attempt watchdog (§5.6). Admission control for submissions:
`admitSubmit()` counts active browser attempts from `worker_heartbeats`/attempt rows and queues
beyond 4 general slots; OTP resumes may use the 2 reserved slots.

### 2.4 Capacity derivation (the numbers)
16 GB total − 2 GB Postgres shared_buffers − 0.5 GB web heap − 0.5 GB worker heap − 1 GB OS +
Caddy + filesystem cache floor = ~12 GB usable. Measured Chromium context (one tab, typical ATS
form) ≈ 700 MB → 6 contexts = 4.2 GB, leaving 2× margin for spikes. Throughput check: 100
applications/day × 5 min mean attempt = 500 context-minutes/day = 8.3 context-hours — 6 slots
clear this in under 2 hours even if all arrivals land at once. Timeouts:

| Timeout | Value |
|---|---|
| `page.goto` | 30 s |
| Any single action (click/fill/upload) | 15 s |
| Prepare phase wall clock | 300 s |
| Whole attempt | 420 s |
| OTP in-context wait | 600 s |
| Late-code window (refill path) | 24 h |
| Attempt lease / heartbeat / stale threshold | 60 s / 20 s / 90 s |
| Reaper sweep interval | 30 s |
| LLM call | 60 s, retries at +2 s, +8 s |

### 2.5 Application lifecycle (the state machine)

```
queued ──▶ preparing ──▶ review ──▶ authorized ──▶ submitting ──▶ submitted
   │            │            ▲            │              │
   │            │            │            │              ├─▶ otp_required ──▶ (resume) ▶ submitting
   │            ▼            │            │              ├─▶ unverified   (post-click crash; human confirms)
   ├──▶ canceled        blocked_drift     │              ├──▶ failed      (pre-click failure; retryable)
   └──▶ failed                            │              └──▶ blocked_drift
          ▲                               │
          └────── full_auto preflight fail ┘ (lands in review, user notified)
```

Every arrow is a CAS update: `UPDATE applications SET status=$next, … WHERE id=$id AND
status=$prev` executed inside a transaction holding `SELECT … FOR UPDATE` on the row. Zero
rows updated ⇒ someone else moved the row ⇒ caller re-reads and exits.

### 2.6 Shutdown and deploys
`worker` traps SIGTERM → `boss.offWork()` for all pools → finishes in-flight attempts up to
90 s (`stop_grace_period: 90s` in compose) → exits. Attempts still running at the deadline die
with the container and are recovered by the lease reaper (§5.1/§5.7) — the same path as a crash,
so deploys exercise the recovery machinery continuously. Migrations run before rollout and are
written expand-then-contract so the old worker image survives one deploy cycle against the new
schema.

---

## 3. Data model — DDL

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS vector;

-- Roles ---------------------------------------------------------------------
-- pilot_owner:  runs dbmate, owns schema. pilot_app: web (RLS-scoped, no BYPASSRLS).
-- pilot_worker: worker (BYPASSRLS, because system jobs act across tenants).
-- eeo_service:  SELECT on eeo_answers ONLY (used by one function in the form-filler).
-- pilot_purge:  account-deletion routine (only role allowed to delete immutable rows).
CREATE ROLE pilot_app NOLOGIN;  GRANT pilot_app TO pilot_web_login;
CREATE ROLE pilot_worker NOLOGIN BYPASSRLS;
CREATE ROLE eeo_service NOLOGIN;
CREATE ROLE pilot_purge  NOLOGIN;

CREATE TABLE users (
    id                        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email                     CITEXT NOT NULL UNIQUE,
    password_hash             TEXT NOT NULL,               -- argon2id (@node-rs/argon2)
    role                      TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
    credits_total             INT NOT NULL DEFAULT 5 CHECK (credits_total >= 0),
    credits_held              INT NOT NULL DEFAULT 0 CHECK (credits_held >= 0),
    stripe_customer_id        TEXT,
    active_profile_version_id UUID,                        -- FK added after profile_versions exists
    created_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deleted_at                TIMESTAMPTZ,
    CHECK (credits_total >= credits_held)                  -- available = total - held >= 0
);

CREATE TABLE sessions (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id),
    token_hash   TEXT NOT NULL UNIQUE,       -- sha256hex(token); token itself only in cookie
    expires_at   TIMESTAMPTZ NOT NULL,
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE resumes (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id),
    file_key     TEXT NOT NULL,              -- var/files/{userId}/{sha256}.bin, AES-256-GCM
    file_sha256  TEXT NOT NULL,
    mime         TEXT NOT NULL CHECK (mime IN ('application/pdf',
                  'application/vnd.openxmlformats-officedocument.wordprocessingml.document')),
    bytes        INT NOT NULL CHECK (bytes BETWEEN 1 AND 10485760),
    raw_text_enc BYTEA,                      -- extracted text, encrypted at rest (draft stage only)
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE profile_drafts (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL UNIQUE REFERENCES users(id),
    resume_id  UUID NOT NULL REFERENCES resumes(id),
    data       JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE profile_versions (                 -- INSERT-ONLY (trigger below)
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID NOT NULL REFERENCES users(id),
    version_no   INT NOT NULL,
    data         JSONB NOT NULL,               -- frozen copy of draft.data
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (user_id, version_no)
);
ALTER TABLE users ADD CONSTRAINT fk_active_pv
    FOREIGN KEY (active_profile_version_id) REFERENCES profile_versions(id);

CREATE FUNCTION forbid_mutation() RETURNS trigger AS $$
BEGIN
    IF current_setting('app.purge', true) = 'on' AND session_user = 'pilot_purge' THEN
        RETURN OLD;                             -- deletion routine only
    END IF;
    RAISE EXCEPTION '% is insert-only', TG_TABLE_NAME;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER pv_immutable BEFORE UPDATE OR DELETE ON profile_versions
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE profile_facts (                    -- atomic citable claims for the truthfulness audit
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id),
    fact_key           TEXT NOT NULL,           -- e.g. 'experience[0].title', 'skills[3]'
    content            TEXT NOT NULL,
    UNIQUE (profile_version_id, fact_key)
);

CREATE TABLE application_preferences (
    user_id    UUID PRIMARY KEY REFERENCES users(id),
    data       JSONB NOT NULL,                  -- auth, relocation, notice, salary, consent answers
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Quarantined: web role has ZERO grants; only eeo_service may read, only inside
-- fillDemographicSection(). Never joined, never selected by matching/doc/AI code paths.
CREATE TABLE eeo_answers (
    user_id    UUID PRIMARY KEY REFERENCES users(id),
    data       JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
REVOKE ALL ON eeo_answers FROM PUBLIC, pilot_app, pilot_worker;
GRANT  SELECT ON eeo_answers TO eeo_service;

CREATE TABLE boards (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    vendor           TEXT NOT NULL DEFAULT 'greenhouse' CHECK (vendor IN ('greenhouse')),
    token            TEXT NOT NULL,             -- vendor board identifier
    company_name     TEXT NOT NULL,
    added_by         UUID REFERENCES users(id), -- user-requested boards await approval
    approved         BOOLEAN NOT NULL DEFAULT FALSE,
    next_crawl_after TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    miss_streak      INT NOT NULL DEFAULT 0,
    UNIQUE (vendor, token)
);

CREATE TABLE postings (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    board_id        UUID NOT NULL REFERENCES boards(id),
    external_id     TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    company_name    TEXT NOT NULL,
    locations       TEXT[] NOT NULL DEFAULT '{}',
    remote_status   TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (remote_status IN ('remote','hybrid','onsite','unknown')),
    employment_type TEXT,
    salary_min INT, salary_max INT, salary_currency TEXT,
    requirements    JSONB NOT NULL DEFAULT '[]', -- [{kind,value}] kind∈skill|license|degree|years
    content_text    TEXT,
    embedding       vector(1536),
    content_hash    TEXT NOT NULL,               -- sha256 of canonical posting JSON; change ⇒ enrich
    status          TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    miss_count      INT NOT NULL DEFAULT 0,      -- consecutive crawl misses; 2 ⇒ closed
    first_seen_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (board_id, external_id)               -- duplicate crawl runs collapse here
);
CREATE INDEX postings_feed_idx ON postings (status, first_seen_at DESC);
CREATE INDEX postings_vec_idx  ON postings USING hnsw (embedding vector_cosine_ops);

CREATE TABLE feed_dismissals (
    user_id      UUID NOT NULL REFERENCES users(id),
    posting_id   UUID NOT NULL REFERENCES postings(id),
    dismissed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    restored_at  TIMESTAMPTZ,
    PRIMARY KEY (user_id, posting_id)
);

CREATE TABLE feed_items (                       -- materialized ranked feed per user
    user_id     UUID NOT NULL REFERENCES users(id),
    posting_id  UUID NOT NULL REFERENCES postings(id),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id),
    rank        INT NOT NULL,
    score       REAL NOT NULL,
    explanation TEXT NOT NULL,                  -- markdown, generated once per (pv, posting)
    gaps        JSONB NOT NULL DEFAULT '[]',
    computed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, posting_id)
);

CREATE TABLE documents (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID NOT NULL REFERENCES users(id),
    posting_id         UUID NOT NULL REFERENCES postings(id),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id),
    kind               TEXT NOT NULL CHECK (kind IN ('resume','cover_letter')),
    status             TEXT NOT NULL DEFAULT 'generating'
                       CHECK (status IN ('generating','ready','failed','superseded')),
    claims             JSONB NOT NULL DEFAULT '[]', -- [{text, fact_keys:[]}] citation per claim
    audit_verdict      JSONB,                   -- {verdict:'pass'|'fail', unsupported:[], model}
    docx_key TEXT, pdf_key TEXT,
    error              TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX documents_lookup_idx ON documents (user_id, posting_id, status);

CREATE TABLE applications (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id),
    posting_id        UUID NOT NULL REFERENCES postings(id),
    profile_version_id UUID NOT NULL REFERENCES profile_versions(id), -- version pinning
    preferences_snapshot JSONB NOT NULL,       -- frozen preferences at queue time
    mode              TEXT NOT NULL CHECK (mode IN ('review','full_auto')),
    status            TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
        ('queued','preparing','review','authorized','submitting','otp_required',
         'unverified','submitted','failed','canceled','blocked_drift')),
    answers           JSONB NOT NULL DEFAULT '[]',
        -- [{field_ref,label,value,fact_keys:[],confidence}] ; confidence NULL for user-edited
    fp_prepare        TEXT,                    -- canonicalFormFingerprint() results
    fp_review         TEXT,
    credit_hold_id    UUID REFERENCES credit_holds(id),
    otp_code          TEXT,
    otp_requested_at  TIMESTAMPTZ,
    attempts          INT NOT NULL DEFAULT 0,
    last_heartbeat_at TIMESTAMPTZ,
    failure_reason    TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
-- Promise #3, layer 1: at most one live application per (user, posting).
CREATE UNIQUE INDEX applications_one_live_per_posting
    ON applications (user_id, posting_id)
    WHERE status NOT IN ('submitted','failed','canceled');
CREATE INDEX applications_reaper_idx ON applications (last_heartbeat_at)
    WHERE status IN ('preparing','submitting');
CREATE INDEX applications_stuck_queued_idx ON applications (created_at)
    WHERE status = 'queued';

CREATE TABLE application_events (               -- append-only audit trail (trigger below)
    id             BIGSERIAL PRIMARY KEY,
    application_id UUID NOT NULL REFERENCES applications(id),
    at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind           TEXT NOT NULL,
        -- queued|hold_placed|prepare_started|form_read|answers_drafted|ready|
        -- user_edited|authorized|attempt_started|fp_match_ok|pre_click_ok|click_sent|
        -- otp_challenge_detected|otp_received|submission_proof|failed|canceled|
        -- hold_released|hold_charged|recovered
    actor          TEXT NOT NULL CHECK (actor IN ('system','user','operator')),
    data           JSONB NOT NULL DEFAULT '{}',
    evidence_ref   TEXT                          -- key into attempt evidence bundle
);
CREATE TRIGGER ae_immutable BEFORE UPDATE OR DELETE ON application_events
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();
CREATE INDEX ae_app_idx ON application_events (application_id, at);

CREATE TABLE application_attempts (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id),
    attempt_no     INT NOT NULL,
    outcome        TEXT CHECK (outcome IN ('submitted','failed_pre_click','unverified',
                                           'blocked_drift','otp_pending')),
    video_key      TEXT, trace_key TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), finished_at TIMESTAMPTZ,
    UNIQUE (application_id, attempt_no)
);

CREATE TABLE form_snapshots (                   -- what the form looked like at each phase
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    application_id UUID NOT NULL REFERENCES applications(id),
    phase          TEXT NOT NULL CHECK (phase IN ('prepare','review','live')),
    fingerprint    TEXT NOT NULL,
    fields         JSONB NOT NULL,
    taken_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE credit_holds (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        UUID NOT NULL REFERENCES users(id),
    application_id UUID NOT NULL UNIQUE REFERENCES applications(id),
    status         TEXT NOT NULL DEFAULT 'held'
                   CHECK (status IN ('held','charged','released')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), resolved_at TIMESTAMPTZ
);

CREATE TABLE credit_ledger (                    -- append-only user-visible money trail
    id         BIGSERIAL PRIMARY KEY,
    user_id    UUID NOT NULL REFERENCES users(id),
    delta_total INT NOT NULL, delta_held INT NOT NULL,
    reason     TEXT NOT NULL CHECK (reason IN ('welcome','purchase','hold','charge','release','adjust')),
    ref        UUID,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE TRIGGER cl_immutable BEFORE UPDATE OR DELETE ON credit_ledger
    FOR EACH ROW EXECUTE FUNCTION forbid_mutation();

CREATE TABLE purchases (
    id                     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id                UUID NOT NULL REFERENCES users(id),
    kind                   TEXT NOT NULL CHECK (kind IN ('pack','subscription')),
    credits                INT NOT NULL DEFAULT 0,
    amount_cents           INT NOT NULL,
    stripe_session_id      TEXT UNIQUE,          -- exactly-once purchase credit
    stripe_subscription_id TEXT UNIQUE,
    status                 TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending','paid','canceled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE webhook_events (                   -- Stripe event dedupe; PK IS the idempotency key
    id          TEXT PRIMARY KEY,               -- stripe event id
    event_type  TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE notifications (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id),
    kind       TEXT NOT NULL CHECK (kind IN ('ready_for_review','submitted','failed',
                                             'otp_required','unverified_reminder','credits_low')),
    payload    JSONB NOT NULL DEFAULT '{}',
    read_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX notifications_unread_idx ON notifications (user_id, created_at DESC) WHERE read_at IS NULL;

CREATE FUNCTION notify_user() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('user_events', json_build_object(
        'user_id', NEW.user_id, 'kind', NEW.kind, 'payload', NEW.payload)::TEXT);
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER n_notify AFTER INSERT ON notifications
    FOR EACH ROW EXECUTE FUNCTION notify_user();

CREATE TABLE rate_limits (
    key          TEXT NOT NULL,                 -- e.g. 'login:ip:1.2.3.4'
    window_start TIMESTAMPTZ NOT NULL,
    count        INT NOT NULL DEFAULT 0,
    PRIMARY KEY (key, window_start)
);

CREATE TABLE ai_usage_daily (
    day DATE NOT NULL, user_id UUID NOT NULL REFERENCES users(id), task TEXT NOT NULL,
    tokens_in BIGINT NOT NULL DEFAULT 0, tokens_out BIGINT NOT NULL DEFAULT 0,
    cost_micros BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (day, user_id, task)
);

CREATE TABLE worker_heartbeats (
    pool TEXT PRIMARY KEY,                       -- 'browser'|'crawl'|'ai'|'maintenance'
    at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    detail JSONB NOT NULL DEFAULT '{}'           -- slot occupancy, in-flight counts
);

CREATE TABLE deletion_audit (                   -- minimal anonymized record required by brief
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_sha256 TEXT NOT NULL,
    deleted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Row-level security: web process connects as pilot_app and wraps every request in
-- withUser(userId, fn), whose FIRST statement is SET LOCAL app.user_id = '<uuid>'.
ALTER TABLE applications     ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents        ENABLE ROW LEVEL SECURITY;
ALTER TABLE resumes          ENABLE ROW LEVEL SECURITY;
ALTER TABLE profile_drafts   ENABLE ROW LEVEL SECURITY;
ALTER TABLE profile_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE profile_facts    ENABLE ROW LEVEL SECURITY;
ALTER TABLE feed_items       ENABLE ROW LEVEL SECURITY;
ALTER TABLE feed_dismissals  ENABLE ROW LEVEL SECURITY;
ALTER TABLE credit_ledger    ENABLE ROW LEVEL SECURITY;
ALTER TABLE credit_holds     ENABLE ROW LEVEL SECURITY;
ALTER TABLE purchases        ENABLE ROW LEVEL SECURITY;
ALTER TABLE notifications    ENABLE ROW LEVEL SECURITY;
-- one policy per table, same shape:
CREATE POLICY owner_only ON applications
    USING (user_id = current_setting('app.user_id', true)::uuid)
    WITH CHECK (user_id = current_setting('app.user_id', true)::uuid);
```

Where a constraint cannot live in DDL, the enforcing function is named in §4.

---

## 4. Invariant enforcement map

| # | Invariant | Mechanism | Evidence it works |
|---|---|---|---|
| 1 | **Truthfulness** — every document claim traces to user-stated facts | Generation receives only `profile_facts` rows (keyed); output `documents.claims[]` must carry `fact_keys`. `validateClaims(docId)` (packages/core/src/truth.ts) passes a claim only if (a) deterministic check: every number, date, credential name, and proper noun in `text` appears verbatim in the cited facts' content; (b) NLI check: escalation-model entailment call `facts ⊨ claim`. Any failure ⇒ regenerate with violation notes (max 2 rounds) ⇒ else `status='failed'`, never `ready` | `tests/truth.spec.ts`: golden set of 50 fixture profiles/jobs asserts `unsupported.length === 0` for all; nightly eval harness reports the rate; CI fails on any nonzero |
| 2 | **No papering over missing credentials** | Generator prompt contains only facts, never the job's requirements; requirement comparison happens outside generation (feed `gaps`). Form questions about credentials are answered by exact match against license/certification facts in `answerField()`; absent fact ⇒ value `null`, `confidence=0` ⇒ surfaces to review screen | Fixture test `missing-license.spec.ts`: job requires RN license, profile lacks it ⇒ generated docs contain no license claim (string scan for license regexes) and the form answer renders as unanswered-required |
| 3 | **Review gate** — nothing submits without per-application authorization | Submit executor starts only after `UPDATE applications SET status='authorized' WHERE id=$1 AND status='review'` affects 1 row; that update is reached solely by `POST /api/applications/:id/authorize` (session-authed, RLS-scoped), which requires body `fingerprint === fp_review`. `full_auto` sets `authorized` at queue time, but `preflightConfidence(app)` runs before browser launch: any required field with `confidence < 0.85`, any consent/sensitive field lacking an explicit preference answer ⇒ flips to `status='review'` + notification | Integration test `full-auto-guard.spec.ts`: queue full_auto app whose fixture form contains an unanswerable required question ⇒ row lands `review`, FakeATS `/receipts` empty. E2E: authorize call with wrong fingerprint ⇒ 409, status unchanged |
| 4 | **No double submit** | Five layers: (L1) partial unique index `applications_one_live_per_posting` blocks a second live app per job; (L2) executor claims the row: `BEGIN; SELECT … FOR UPDATE; UPDATE … SET status='submitting' WHERE status='authorized'; COMMIT` — a concurrent executor updates 0 rows and exits; (L3) attempt lease: heartbeat `UPDATE … SET last_heartbeat_at=now()` every 20 s; reaper ignores rows with fresh heartbeats; (L4) barrier events: `pre_click_ok` written immediately before the final click, `click_sent` immediately after; recovery logic refuses to auto-re-run anything past `pre_click_ok` (goes to `unverified`, human decides); (L5) FakeATS records receipts; drills assert ≤1 | Drill suite `ops/drills/run.sh` (nightly): kill -9 at injected barrier points (before click, between `pre_click_ok` and `click_sent`, after click) ⇒ FakeATS `/receipts/<appId>` count == 1 in every run; integration race test spawns 8 parallel executors on one app ⇒ exactly 1 reaches `click_sent` |
| 5 | **Reviewed form == submitted form** (drift block) | `canonicalFormFingerprint(page)` extracts visible interactive elements (type, normalized label, options sorted, required flag; excludes hidden inputs and all values), sorts, JSON-serializes, SHA-256. Computed at prepare (`fp_prepare`), shown at review (`fp_review`), recomputed immediately pre-click (`fp_live`); `assertFormUnchanged()` requires `fp_live === fp_review`, else `status='blocked_drift'`, hold released | FakeATS scenario `mutate_form` (renames a field label 5 s after load): E2E expects `blocked_drift`, zero receipts, ledger shows release |
| 6 | **Credit safety** — no negative balance, no double charge, release on fail | Hold: `INSERT credit_holds` + `UPDATE users SET credits_held = credits_held+1 WHERE id=$1 AND credits_total - credits_held > 0` (0 rows ⇒ insufficient funds ⇒ queue refused) in one tx with the application insert. Charge on success / release on fail-cancel: `UPDATE credit_holds SET status='charged'|'released' WHERE id=$1 AND status='held'` — 0 rows ⇒ already resolved ⇒ no-op; ledger rows written in the same tx; `CHECK (credits_total >= credits_held)` backstops arithmetic errors. Webhook replay: `INSERT INTO webhook_events … ON CONFLICT DO NOTHING`, process only when inserted; purchases additionally keyed by `UNIQUE(stripe_session_id)` | Race test: 32 goroutines charge one held app concurrently ⇒ exactly 1 ledger `charge` row, hold ends `charged`. Replay test: same checkout.completed webhook delivered 5× ⇒ 1 purchase row, 1 ledger `purchase`. Constraint test: forcing `credits_held > credits_total` insert raises `23514` |
| 7 | **EEO quarantine** | Structural: `eeo_answers` has zero grants for `pilot_app`/`pilot_worker`; only `eeo_service` (a dedicated short-lived connection opened inside `fillDemographicSection()`) can `SELECT`, and only exact-string option matching occurs there; unmatched options stay blank for the user. Matching/doc/answer prompts physically cannot contain the data (their connections lack the grant) | Integration test: connect as `pilot_app`, `SELECT * FROM eeo_answers` ⇒ `42501 permission denied`. Prompt-recorder test: fixture user has sentinel EEO values; run matching + doc-gen + answer-draft flows with a recording LLM stub ⇒ sentinel strings appear in zero recorded payloads |
| 8 | **Profile version pinning** — edits never change past basis | Publishing inserts an immutable `profile_versions` row (trigger `forbid_mutation` rejects UPDATE/DELETE unless purge role); applications/documents FK `profile_version_id` + carry `preferences_snapshot`; feed items reference the version they scored | Test `pinning.spec.ts`: publish v1, queue app, edit+publish v2, regenerate feed ⇒ app's stored `data` resolves to v1 content byte-for-byte; attempt to `UPDATE profile_versions` raises exception |
| 9 | **Crash recovery** — nothing stuck forever | Lease + reaper: every non-terminal row must show `last_heartbeat_at` < 90 s old when active; `recoverApplications()` (every 30 s) classifies by last audit event: stuck `queued` >120 s ⇒ re-send job (idempotent handler); preparing/pre-click ⇒ reset to `authorized` (answers preserved, attempt logged `recovered`); past `pre_click_ok` ⇒ `unverified` + user notification; `otp_required` >24 h ⇒ `failed(otp_expired)`, hold released; `unverified` >72 h ⇒ reminder notification | Synthetic-clock test advances timestamps and asserts each transition; soak drill kills worker mid-prepare ⇒ app reaches `authorized` again ≤ 120 s with `recovered` event present |
| 10 | **OTP flow** | Executor detects challenge markers (configured selectors: `input[type=search][autocomplete=one-time-code]`, `input[name*=code i]`) after final click ⇒ writes `otp_challenge_detected` + screenshot, `status='otp_required'`, notification fires (trigger → SSE + Postmark). Context stays open ≤ 600 s; user posts code (`POST …/otp`, format `^[A-Za-z0-9]{4,8}$`) ⇒ `otp_received` ⇒ executor continues on the live page. Late code (>600 s, ≤24 h): `resume-otp` relaunches, restores cookies via `context.storageState({path})` saved at challenge time, re-fills from `answers` after `assertFormUnchanged()` vs `fp_review`, enters code | FakeATS `otp` scenario E2E: fast path submits within window; late path (code posted at t+900 s simulated) reopens, refill matches fingerprint, receipt count == 1; expiry path lands `failed(otp_expired)` with released hold |
| 11 | **Stuck-forever prevention** (product promise: interrupted work recovers) | Same mechanism as #9 plus terminal SLAs wired into `recoverApplications()` switch — every non-terminal status has exactly one aging rule and one destination | Table-driven test iterates all 7 non-terminal statuses × aged timestamps ⇒ each maps to its documented destination; no status falls through |
| 12 | **Tenant isolation** | RLS policies (§3) + `withUser(userId, fn)` wrapper issuing `SET LOCAL app.user_id` as the first statement of every web-request transaction; worker uses `pilot_worker` (BYPASSRLS) only for system jobs keyed by explicit ids | Integration test: two users, A requests B's application/document/export ⇒ 404 (row invisible); SQL-level: `SET LOCAL app.user_id='<a>'` then select B's row ⇒ 0 rows |
| 13 | **Backup freshness / restorable** | pgBackRest nightly full + WAL archive every 60 s; `restore-drill.sh` (weekly cron + nightly CI) restores latest into scratch container and asserts row counts > 0, max(`application_events.at`) within 24 h, ledger sums reconcile | Drill log `var/drills/<date>.log` committed by CI run; health page shows hours-since-last-successful-drill; alert fires at >192 h |
| 14 | **Answer truthfulness on forms** (never lie to employers) | `answerField()` produces values only from `profile_facts`, `preferences_snapshot`, or approved documents; free-text employer questions answered only when a fact supports them, else `confidence=0` ⇒ review. Consent checkboxes: checked only from explicit `preferences.consent_answers`, never inferred | Fixture form with trick question ("Describe your React experience" for a no-React profile) ⇒ answer null, required ⇒ review; consent checkbox without preference ⇒ unchecked + flagged |
| 15 | **Operator safety valve** — requeue/fail without corruption | Admin actions reuse the same CAS transitions with `actor='operator'` events; force-fail releases hold via the #6 path; requeue permitted only from `{queued, failed}` | Test: operator requeue of a `submitting` row ⇒ 0 rows affected, 409 returned |

---

## 5. Failure-mode walkthrough

**Scenario 1 — Host loses power mid-final-click.**
1. Postgres loses the worker's open transaction; uncommitted WAL (including the `click_sent`
   event if not yet flushed) is discarded on restart.
2. On boot, the `applications` row shows `status='submitting'`, last event `pre_click_ok`,
   `last_heartbeat_at` frozen at power-off.
3. After 90 s of silence, `recoverApplications()` matches the stale-threshold predicate
   (`applications_reaper_idx`), sees last event ≥ `pre_click_ok`, and sets
   `status='unverified'`, emitting `notifications(kind='unverified_reminder')`.
4. The user is asked to check the employer site; Pilot never re-executes this attempt
   automatically (barrier rule, §4 #4-L4). Hold remains until the user marks the outcome
   (submitted ⇒ charge; not-submitted ⇒ release + retry creates a fresh attempt).
**Evidence:** `application_events` sequence `pre_click_ok` → (gap) → `recovered` with
`outcome='unverified'`; the attempt's `video_key` recording shows whether the click landed;
FakeATS receipt log in drills shows count ≤ 1 across 50 randomized kill-point runs.

**Scenario 2 — pg-boss redelivers a submission job** (e.g., worker GC pause exceeds
`expireInSeconds: 420`).
1. Redelivered handler opens a transaction and runs `SELECT … FOR UPDATE` on the application
   row; the original executor holds it (or finished and advanced status).
2. Handler's `UPDATE … SET status='submitting' WHERE status='authorized'` matches 0 rows
   (status is `submitting`/`submitted`/…) ⇒ handler exits without touching the browser.
3. If the original executor died mid-attempt, Scenario 1's recovery path applies instead.
**Evidence:** integration test injects double delivery (`boss.send` twice with same
singletonKey + manual duplicate) ⇒ exactly one `attempt_started` event; log shows second
delivery exiting at step 2.

**Scenario 3 — LLM returns garbage or times out during document generation.**
1. Call wrapped in `AbortController` (60 s) and `response_format: json_schema strict` —
   malformed JSON is impossible by construction; semantic validation still applies
   (`validateClaims`).
2. Validation failure or timeout ⇒ retry with validator violations appended to the prompt
   (attempts at +0 s, +2 s, +8 s; temperature 0).
3. Third failure ⇒ escalate to `gpt-4.1` once.
4. Still failing ⇒ `documents.status='failed'` with `error` message surfaced in UI; Retry
    button re-sends the job. Cost guard: before the escalation, `aiBudgetGuard()` reads
    `ai_usage_daily`; when this application's cumulative spend exceeds $0.10, the guard refuses
    the escalation and fails the task retryably — user-visible, never silent.
**Evidence:** fault-injection test stubs the model to return schema-valid nonsense ⇒ transcript
shows 3 retries + escalation + `failed` status; `ai_usage_daily` rows match the attempted-call
arithmetic.

**Scenario 4 — Employer's form changes after review.**
1. Executor loads the live form at submit time and calls `canonicalFormFingerprint(page)`.
2. `assertFormUnchanged()` compares `fp_live !== fp_review` ⇒ mismatch ⇒ writes
   `form_snapshots(phase='live')`, sets `status='blocked_drift'`, releases hold (#6 path),
   notifies user: "The form changed since you reviewed it."
3. User reviews the diff (both `form_snapshots` rendered side by side) and re-authorizes;
   the new authorization captures a fresh `fp_review`.
**Evidence:** FakeATS `mutate_form` scenario: E2E asserts `blocked_drift`, zero receipts, and a
released hold; screenshots of both fingerprints attached to the application.

**Scenario 5 — Stripe webhook replayed.**
1. Handler verifies signature (`constructEvent`).
2. `INSERT INTO webhook_events (id, …) VALUES ($eventId, …) ON CONFLICT DO NOTHING`;
   `rowCount === 0` ⇒ replay ⇒ return 200, do nothing.
3. First delivery: purchase credited inside a tx inserting `purchases` (UNIQUE
   `stripe_session_id` second net) + ledger `purchase` + `users.credits_total += n`.
4. Lost-webhook complement: success-page hit retrieves the session via
   `stripe.checkout.sessions.retrieve(id)` and performs steps 2–3; whichever path arrives
   second is absorbed by the same uniqueness constraints.
**Evidence:** test fires the same `checkout.session.completed` 5× ⇒ 1 ledger row, balance +n
once; reconciliation job (`ops.reconcileCredits()` nightly) reports sum(ledger deltas) ==
users.credits_total for all users.

**Scenario 6 — Chromium hangs** (renderer wedged, `waitForResponse` never settles).
1. Every action carries `{timeout: 15000}`; the whole attempt runs under a 420 s
   `Promise.race` watchdog in `runAttempt()`.
2. Watchdog fires ⇒ `context.tracing.stop({path})` + video finalized ⇒ `try context.close()`;
   if close hangs > 5 s, `browser.close()` and relaunch (recycled anyway every 50 contexts to
   bound leaks).
3. Outcome classification by last audit event (same barrier logic as Scenario 1):
   pre-click ⇒ `failed(pre_click_failure)`, hold released, one-click retry (max 3 attempts/day
   enforced by counting `application_attempts`); post-barrier ⇒ `unverified`.
**Evidence:** FakeATS `slow` scenario (infinite spinner) ⇒ attempt fails at 420 s, trace file
present, worker heartbeat continues (other pools unaffected), next queued app proceeds.

**Scenario 7 — Deploy restarts the worker mid-submission.**
1. SIGTERM ⇒ `boss.offWork()` stops new fetches; in-flight attempt continues.
2. Grace period 90 s elapses with attempt unfinished ⇒ container killed ⇒ identical state to
   Scenario 1 (lease goes stale, reaper classifies by barrier events).
3. New worker image starts, runs `dbmate up` was already done by deploy script, resumes
   fetching; recovered apps follow #9 destinations.
**Evidence:** drill restarts worker during a FakeATS attempt ⇒ post-restart timeline shows
either completion (if <90 s) or `unverified`/`authorized`+`recovered`; receipts ≤ 1.

**Scenario 8 — Crawler tick delivered twice / overlapping runs.**
1. Board ticks use `singletonKey: board.id` — a live job with that key makes the second send a
   no-op (pg-boss send dedupe).
2. Defense in depth: postings dedupe on `UNIQUE (board_id, external_id)` via
   `INSERT … ON CONFLICT (board_id, external_id) DO UPDATE SET … WHERE excluded.content_hash <>
   postings.content_hash`; identical content is a no-op update.
3. Disappeared postings increment `miss_count`; closed only at 2 consecutive misses (guards
   against a single failed fetch closing live jobs).
**Evidence:** test runs the same board tick twice concurrently ⇒ 1 crawl event, posting count
unchanged, `updated_at` bumps only for genuinely changed postings.

**Scenario 9 — User cancels while the worker is preparing.**
1. Cancel endpoint: `UPDATE applications SET status='Canceled'…` — precisely
   `SET status='canceled' WHERE id=$1 AND status IN ('queued','preparing','review')` + hold
   release in the same tx.
2. Worker's next transition attempt (`WHERE status='preparing'` → `ready`) matches 0 rows ⇒
   discards work, exits before any browser opens.
3. If cancellation arrives during `submitting`: the endpoint refuses (409) — past
   `pre_click_ok` the click may already be in flight; user sees "cannot cancel, outcome pending"
   and the Scenario 1 path resolves it honestly.
**Evidence:** race test interleaves cancel and prepare-completion 100× (deterministic
interleavings via test clock) ⇒ terminal states ∈ {canceled, ready}, never both charged and
canceled; ledger shows at most one resolution.

**Scenario 10 — OTP email arrives late (user forwards it after the 600 s window).**
1. At 600 s the executor parks cleanly: saves `storageState({path})` (cookies only — DOM state
   is gone, which the design accounts for by storing `answers` + `fp_review` for a full
   re-fill), closes the context, frees the slot; status stays `otp_required`.
2. Code posted within 24 h ⇒ `resume-otp` job (priority 5, reserved slot) relaunches, restores
   cookies, navigates to the form, `canonicalFormFingerprint` must equal `fp_review` (else
   `blocked_drift`), re-fills from `answers`, enters the code, submits.
3. Past 24 h ⇒ `failed(otp_expired)`, hold released, user notified; retry requeues normally.
**Evidence:** FakeATS `otp_late` scenario drives both branches; receipts == 1 in both; slot
occupancy metric shows the parked context released at 600 s ± sweep tolerance.

---

## 6. AI strategy

### 6.1 Task routing (model, temperature, limits)

| Task | Model | Temp | Max out | Timeout | Retries |
|---|---|---|---|---|---|
| Resume → draft profile extraction | gpt-4.1-mini | 0 | 1,500 | 60 s | 2 (+2 s/+8 s), escalate once |
| Posting requirement enrichment (only when ≥1 candidate user) | gpt-4.1-mini | 0 | 400 | 60 s | 2, then skip (posting stays deterministic-fields-only) |
| Feed explanation (cached per profile-version × posting) | gpt-4.1-mini | 0.3 | 250 | 60 s | 2 |
| Tailored resume / cover letter generation | gpt-4.1-mini | 0.7 | 1,200 | 60 s | 2 (regeneration with violation notes) |
| Truthfulness NLI audit | gpt-4.1 (escalation tier deliberately — judge ≠ generator) | 0 | 200 | 60 s | 2 |
| Answer drafting for form fields | gpt-4.1-mini | 0 | 700 | 60 s | 2 |
| Embeddings (postings, profiles) | text-embedding-3-small | — | — | 30 s | 3 exponential |

Prompt templates live in `packages/prompts/*.md`, hashed into `documents.audit_verdict` and
eval artifacts, so any historical document is reproducible against the exact prompt+model pair.

### 6.2 Output constraints and the truthfulness pipeline
1. **Constrained input:** generators receive the fact list (keys + content) and nothing else —
   there is no raw resume text in the prompt to embellish from.
2. **Structured output:** `json_schema strict` for every non-prose call; prose calls return
   claims arrays with mandatory `fact_keys` per claim (schema-enforced).
3. **Deterministic audit** (`validateClaims`, step a): tokenizer extracts numbers, dates,
   capitalized tokens, credential strings; each must appear in the cited facts. This catches
   invented metrics, employers, dates, and certifications with certainty.
4. **NLI audit** (step b): escalation model judges entailment per claim; verdicts recorded.
5. **Gap honesty:** requirement-gap analysis happens in the feed (visible `gaps`), never in the
   generator; documents contain no equivalence claims for absent credentials (forbidden-pattern
   list applied post-generation: e.g. `/(equivalent to|in lieu of)\s+(a|an)?\s*(license|certification|degree)/i`).
6. **Human surface:** anything failing twice lands in the UI with the specific violated claims
   highlighted; users edit text manually — edited claims bypass the machine audit but are marked
   `edited_by_user` in the audit verdict.

### 6.3 Measuring quality (not asserting it)
Harness `ops/eval/run.ts` executes nightly against fixtures; results stored as JSON artifacts
and compared against the main-branch baseline; regression beyond threshold fails the nightly job
and blocks model/prompt promotion (promotion = merging a change to `packages/prompts` or the
model pins in `packages/core/src/models.ts`):

| Metric | Fixtures | Threshold |
|---|---|---|
| Unsupported-claim rate (docs) | 50 profile/job pairs | 0 (hard gate) |
| Extraction field-F1 | 30 labeled resumes | ≥ 0.95 |
| Ranking NDCG@10 vs gold orderings | 250 labeled (profile, posting-set) cases | ≥ 0.85 and drop ≤ 0.02 vs baseline |
| Explanation helpfulness (judge rubric 1–5) | 50 samples/week | ≥ 4.0 mean |
| Answer-draft confidence calibration | 100 field cases | ECE ≤ 0.10 (so the 0.85 preflight threshold means something) |

Ranking quality additionally gets an online proxy: dismissal rate within top-10 feed positions,
queried weekly (`SELECT count(*) FILTER (WHERE dismissed_at IS NOT NULL)::float / count(*) FROM
feed_items f LEFT JOIN feed_dismissals d USING (user_id, posting_id) WHERE rank <= 10 AND
computed_at > now() - interval '7 days'`), target < 0.35; disagreements feed new gold labels.

### 6.4 Cost arithmetic (inputs × unit prices × call counts)

Prices assumed 2026-08 (§13): mini $0.40/$1.60, gpt-4.1 $2/$8, embeddings $0.02 per 1M.
Volume basis: 100 prepared applications/day (brief allows tens–low hundreds), 200 WAU,
20,000 postings/day ingested.

| Task | Tokens in/out | Unit math | Cost | Calls per day |
|---|---|---|---|---|
| Extraction (per resume upload) | 3,000 / 1,500 | 3K·$0.40/M + 1.5K·$1.60/M | $0.0036 (blend w/ 10% escalations: $0.005) | ~20 uploads ⇒ $0.10 |
| Embedding postings | 320 tok each | 20K × 320 = 6.4M tok × $0.02/M | $0.13/day | 20,000 |
| Feed explanation (fresh pair) | 1,200 / 200 | 1.2K·$0.40/M + 0.2K·$1.60/M | $0.0008 | 200 users × 5 new pairs = 1,000 ⇒ $0.80 |
| Docs per application (resume 2K/1.2K + letter 2.4K/0.7K + audit 2×(1.8K/0.15K)) | 8,000 / 2,200 | 8K·$0.40/M + 2.2K·$1.60/M | $0.0067 | 100 ⇒ $0.67 |
| Expected regeneration uplift (20% of apps regenerate once) | — | 0.2 × $0.0067 | +$0.0013/app | ⇒ +$0.13 |
| Answer drafting per application | 2,500 / 600 | 2.5K·$0.40/M + 0.6K·$1.60/M | $0.0020 | 100 ⇒ $0.20 |

**Median per prepared application:** 0.0067 + 0.0013 + 0.0020 = **$0.010** (10% of budget).
**P95 application** (long resume, 40-field form, one regeneration + escalated audit):
0.0067×2 + 0.0077 (audit on gpt-4.1: 3.6K·$2/M + 0.3K·$8/M = $0.0096 vs $0.0019 mini) + 0.0031
(answer drafting on a large form) ≈ **$0.024**.
**Daily total median:** 0.10 + 0.13 + 0.80 + 0.80 + 0.20 = **$2.03 ⇒ ~$61/month**. At the brief's
upper bound (200 apps/day) AI doubles to ~$122/month — still inside the envelope.
**Enforcement, named:** `aiBudgetGuard()` reads `ai_usage_daily` before every call; per-app cap
$0.10 blocks escalation/regeneration tiers first, then fails the task retryably; platform cap
$10/day pauses `generate-doc`/`explain-match` sends (banner in UI, auto-resumes at midnight UTC);
crawl/embeddings ($0.13/day) continue. Worst realistic day (all P95 + cap breach) = $10.34.

**Infrastructure monthly:** CPX41 $34 + B2 $3 + Postmark $15 + domain $1 = **$53**. Worst-case
grand total ≈ **$163/month**.

---

## 7. Testing and release confidence

| Layer | Tooling | Runs | Gate |
|---|---|---|---|
| Unit | vitest — fingerprint canon, ledger math, claim tokenizer, confidence preflight | every push | PR merge |
| Integration | vitest + `@testcontainers/postgresql` (real Postgres, real migrations, RLS on) — races, CAS transitions, webhook replays, quarantine grants | every push | PR merge |
| E2E fast | `@playwright/test` vs FakeATS happy path: signup → upload → publish → feed → docs → queue → review → authorize → submit → receipt==1 | every push | PR merge |
| E2E full matrix | FakeATS scenarios: `otp`, `otp_late`, `mutate_form`, `slow`, `captcha_block`, uploads, multi-select, dropdown, consent | nightly | nightly red ⇒ block deploys until green |
| Crash/chaos drills | `ops/drills/run.sh`: SIGKILL worker at 3 instrumented barriers × 20 random offsets; duplicate pg-boss delivery injection; webhook replay storm | nightly | **receipt count == 1 in every run**, every app terminal-or-resumable ≤ 120 s |
| Restore drill | `ops/restore-drill.sh` into scratch Postgres + smoke SQL | weekly cron + nightly CI | assertions green; log archived |
| AI eval harness | §6.3 metrics | nightly | thresholds per table |

**Proving the two headline promises before real users:** the FakeATS receipt ledger is the
oracle — it records every received application payload with timestamp and payload hash. The
launch gate (Phase 8 exit) requires: 60 consecutive nightly drill runs with zero
double-receipts and zero apps stuck > 120 s, across all kill points, plus a manual game-day
(power-cut the box via `docker compose kill` mid-drill) reviewed against the recorded traces.
Load sanity: nightly soak pushes 300 applications/day-equivalent through FakeATS to hold the 6-slot
math honest.

---

## 8. Delivery phases

Each phase closes on verifiable statements, not dates.

**P0 — Skeleton, deploy, backups.**
Build: monorepo, Compose, Caddy TLS, dbmate baseline, auth (signup/login/sessions), pgBackRest +
B2, restore drill, CI.
Exit: `restore-drill.sh` restores last night's dump to a scratch container and passes smoke SQL ·
signup/login works over HTTPS in production · CI green on push.

*Justification for existing before the end-to-end loop: accounts and recoverable data are
preconditions for every later artifact; backups tested on an empty database find setup mistakes
while they are cheap.*

**P1 — Profile pipeline.**
Build: upload (PDF/DOCX), extraction job, editable draft, publish → immutable version + facts,
preferences editor, data export.
Exit: upload a resume and see an extracted draft · edit any field · publish and confirm a new
immutable `profile_versions` row (v_n) exists · re-upload and confirm v1/v2 both resolve to
original content · `UPDATE profile_versions` raises · export zip downloads and contains the
profile.

*Justification: version pinning is the substrate of truthfulness (#8); building it after
matching would retrofit history onto a live table.*

**P2 — Discovery.**
Build: seed 500 boards, greenhouse API crawl + HTML fallback, enrichment (deterministic fields;
LLM requirements gated on candidate match), dedupe, closure via `miss_count`, robots compliance.
Exit: staging ingest ≥ 5,000 postings/day sustained for 48 h · `UNIQUE(board_id, external_id)`
holds under double-run (test from #8) · a removed posting closes after exactly 2 misses.

*Justification: applications FK postings; the loop needs real-shaped postings, and dedupe bugs
are cheapest to burn down before feeds exist.*

**P3 — Matching feed.**
Build: prefilter (location/salary/auth in SQL) → pgvector cosine top-200 → score + LLM
explanation cached per (version, posting) → feed UI with filters, dismiss/restore, shareable URL.
Exit: NDCG@10 ≥ 0.85 on gold fixtures in CI · explanation cache hits on second render (zero new
LLM calls, verified by recorder) · dismiss/restore/share round-trips.

*Justification: the queue action originates from a posting row shaped by this phase; deferring
it would mean inventing a throwaway posting source for P4/P5.*

**P4 — Tailored documents.**
Build: generation with claims, truthfulness audit, PDF/DOCX render, review/regenerate/download,
failure surfacing.
Exit: eval harness unsupported-claim rate = 0 across 50 fixtures · a non-Latin-name fixture
renders correctly in both formats (visual assertion in E2E) · forced-validator-failure fixture
ends `failed` with explained error and working retry.

*Justification: the review screen renders these documents and the audit machinery is reused by
answer drafting; also it is the second-highest-risk component and needs soak time.*

**P5 — Application loop against FakeATS — FIRST END-TO-END RUN.**
Build: FakeATS (scenario-switchable), queue + hold, prepare (read form, draft answers),
review screen, authorize, submit executor, lifecycle UI, audit trail, evidence bundles.
Exit: scripted signup→upload→match→prepare→review→submit against FakeATS yields exactly one
receipt · kill-switch drill (SIGKILL at each barrier) leaves receipts ≤ 1 and no stuck states ·
cancel works in queued/preparing/review.

**P6 — Credits and billing.**
Build: Stripe Checkout (pack + subscription), ledger UI, webhook processing, reconciliation job,
low-balance notifications.
Exit: replay/race suites green (#6 evidence rows) · ledger reconciles to balances for seeded
histories · a sandbox purchase reflects in the UI ledger within 60 s.

**P7 — Real-form dry run.**
Build: run the full loop against real Greenhouse forms in `dry_run` mode — everything executes
except the final click, which is replaced by `fp_live` capture + proof screenshot.
Exit: dry-run against 20 diverse real postings completes with fp captured · drift detection
proven on real markup by mutating FakeATS mid-flight (block observed).

*Justification for coming after the fake loop: real employer forms must never receive test
traffic; dry-run validates parser coverage without crossing the line.*

**P8 — OTP, full-auto guards, notifications.**
Build: OTP detection/resume paths, preflight confidence gate, SSE unread indicator, email
alerts.
Exit: §4 #3/#10 evidence tests green · unread badge updates without refresh (E2E) · full-auto
with unanswerable required field lands in review.

**P9 — Hardening and launch.**
Build: admin health/timeline/requeue screens, deletion + anonymized audit, chaos drills at
scale, runbook.
Exit: 60 consecutive green nightly drill runs · game-day (compose kill mid-submit) reviewed ·
deletion test: account gone, files gone, Stripe subscription canceled, `deletion_audit` row
exists with hashed id · operator runbook exercised once by the operator end-to-end.

---

## 9. Security and privacy

- **Authn:** argon2id via `@node-rs/argon2` (`hash`/`verify`, memoryCost 19456, timeCost 2,
  parallelism 1 — OWASP-calibrated baseline for interactive login). Sessions: 256-bit random
  token in `__Host-pilot_session` cookie (Secure, httpOnly, SameSite=Lax), only
  `sha256(token)` stored, 30-day sliding expiry. Email verification token (24 h, single-use)
  and password-reset token (1 h, single-use) in a `tokens` table with `used_at`.
- **Authz/tenancy:** RLS + `withUser()` (§4 #12); admin routes check `users.role='admin'` and
  write `actor='operator'` audit events.
- **Secrets:** `/srv/pilot/secrets.env`, root-owned chmod 600, loaded by Compose `env_file`;
  never in git, never in logs (logger redacts keys matching `(KEY|TOKEN|SECRET|PASSWORD)`).
  Rotation runbook covers OpenAI, Stripe whsec, Postmark, B2, Postgres passwords.
- **Encryption at rest:** uploaded files and evidence media encrypted with AES-256-GCM
  (`crypto.createCipheriv('aes-256-gcm')`, per-file random 12-byte IV, tag stored alongside;
  file key derived `crypto.hkdfSync('sha256', masterKey, userId salt, 'file')`); extracted raw
  resume text stored as `bytea` encrypted the same way and decrypted only during draft editing.
  Published structured profile stays plaintext in Postgres — matching and document generation
  require querying it; disclosed in §11. Transport: Caddy TLS + HSTS.
- **Webhooks:** Stripe only; `constructEvent` signature verification + `webhook_events` idempotency
  (§5.5). Endpoint returns 401 on bad signature before any parsing.
- **Rate limits (fixed-window counters in `rate_limits` via `enforceRateLimit(key, limit,
  windowSec)`):** login 5/min/IP and 20/h/account · signup 3/h/IP · password reset 3/h/account ·
  document generation 30/day/user · applications queued 20/day/user with ≤ 10 outstanding holds
  · general API 120/min/user.
- **Abuse/spend:** per-app AI cap $0.10, platform cap $10/day (§6.4); browser attempts capped
  3/day/application; welcome balance 5 credits bounds a free-account's real-world side effects.
- **Privacy operations:** full export (JSON + generated files) via signed URL valid 24 h;
  deletion cancels Stripe subscriptions (`stripe.subscriptions.cancel`), deletes files and rows
   cascade, writes only `deletion_audit(user_sha256, at)`. Backups age out within 31 days — a
   restore resurrects data deleted inside that window; disclosed, standard for PITR systems,
   mitigated by documenting the window in the deletion confirmation UI.
- **Crawl conduct:** `robots-parser` `robots.isAllowed(url, 'PilotBot/1.0')` gate, UA string with
  contact URL, per-domain concurrency 1 and ≥ 2 s inter-request delay, official board APIs
  preferred over scraping.

---

## 10. Risk register

| # | Risk | Mitigation actually planned |
|---|---|---|
| 1 | Employer form diversity breaks the filler (widgets, iframes, shadow DOM) | Field-interaction layer covers native elements + iframe frame lookup; anything unmatched ⇒ `confidence=0` ⇒ review screen (never guess). Dry-run phase (P7) measures parse-rate on 20 real forms before launch; coverage gap ⇒ that vendor pattern becomes a fixture |
| 2 | Anti-bot defenses (captcha, device checks) block submissions | Captcha marker detection ⇒ `needs_human` style block with screenshot (FakeATS `captcha_block` test); we do not solve challenges. Market choice (public ATS boards) keeps this rare; if a board hard-blocks, it is disabled in `boards.approved` and users are told |
| 3 | Hallucinated claim slips through the audit | Two-layer audit (deterministic token match catches fabricated numbers/entities; NLI catches soft embellishments); judge model differs from generator; hard gate = zero unsupported in 50-fixture nightly eval; edited-by-user claims visibly marked |
| 4 | Double-submit under an unrehearsed crash timing | Barrier-event design makes the dangerous window (between `pre_click_ok` and `click_sent`) explicit and maps it to human-confirmed `unverified` rather than auto-retry; 3-barrier × 20-offset nightly SIGKILL drills with receipt-count oracle |
| 5 | OTP flow becomes a resource sink (contexts parked indefinitely) | Hard 600 s park, then slot-freeing refill path with 24 h bound; 2 reserved slots cap worst-case occupancy at 6; expiry is a clean failure with released hold |
| 6 | Crawl legal/ToS trouble | Official Greenhouse board API first, robots gate, identified UA, 2 s/domain pacing, 6 h cadence; user-requested boards go through manual operator approval creating a human checkpoint per source |
| 7 | LLM cost overrun (regeneration loops) | Budget guard with named degradation ladder (skip escalation → skip regen → fail retryably → pause platform-wide at $10/day); `ai_usage_daily` gives per-user attribution |
| 8 | Solo-operator blind spot: production dies silently | Healthchecks.io dead-man ping on crawler, backups, and a worker heartbeat cron; queue-depth alarm (>1,000 for 10 min); restore drill log freshness on the health page — absence of pings emails the operator |
| 9 | Postgres single point of failure | Continuous WAL to B2 (RPO ≤ 5 min), nightly restore drill (RTO ≤ 4 h rehearsed: provision, restore, repoint, smoke), expand-contract migrations so rollback images survive |
| 10 | Chromium/memory creep degrades capacity | Context-per-attempt (no reuse), browser recycle every 50 contexts, 420 s attempt ceiling, nightly 300-app soak asserting slot throughput; second-box trigger defined numerically (§1.9) |

---

## 11. Explicit tradeoffs

1. **Single box, single region.** A host failure halts everything until restore (RTO ≤ 4 h
   rehearsed). Accepted at this budget; the mitigation is fast, *tested* recovery rather than
   redundancy.
2. **NLI audit is probabilistic.** Deterministic checks make fabricated entities impossible to
   ship, but the entailment judge has nonzero false-accept probability on subtle embellishments.
   Residual risk reduced by judge≠generator and the zero-tolerance nightly gate; not eliminable.
3. **Published profile plaintext in Postgres.** Matching/search demands queryable text; disk
   compromise exposes it. Files and raw extracted text are app-layer encrypted; structured
   profile relies on host access controls. A stronger design (column encryption + search over
   ciphertext) costs complexity the operator budget cannot carry at v1.
4. **Post-click crash ⇒ human confirmation, not auto-resolution.** The brief demands no ghost
   submissions; the honest mechanism is `unverified` + user check. This adds friction in a rare
   case rather than risking a double submission.
5. **Scanned/image-only PDF resumes are rejected** with a clear message (OCR quality on dense
   resumes fails the extraction F1 bar; shipping bad drafts undermines trust). Listed as a v1 gap.
6. **Greenhouse-only vendor at launch** (schema keeps `vendor` open). Lever et al. come later;
   the crawler interface isolates the change to one module.
7. **Backups retain personal data up to 31 days after account deletion.** A restore in that
   window resurrects deleted rows; disclosed, standard for PITR systems, mitigated by
   documenting the window in the deletion confirmation UI.
8. **Full-auto conservatively bails to review.** Confidence threshold 0.85 means more human
   reviews than a looser threshold would produce — slower feel, fewer wrong submissions.
9. **Feed explanations can be up to 6 h stale** relative to profile edits (cache lifetime =
   (profile_version, posting) pair). Staleness is at most 6 h and pinned versions prevent
   wrong-basis documents regardless.
10. **Email/password only** at launch — no OAuth. Fewer providers, less attack surface to
    configure, weaker signup convenience.

## 12. Where this is stronger than required

1. **PITR, not just daily backups** — brief asks daily; WAL archiving gives ≤ 5 min RPO, drilled
   weekly.
2. **Database-level immutability triggers** on versions/ledger/events — the brief asks for audit
   trails; these make tampering require a deliberate role switch.
3. **RLS tenant isolation** — repository discipline alone was an option; policies make cross-tenant
   reads structurally impossible for the web role.
4. **Evidence bundles per attempt** (video + Playwright trace + screenshots + form snapshots) —
   the brief asks for proof of what was submitted; this reconstructs the whole session.
5. **Crash drills in CI with a receipt oracle** — the brief asks how the promises are proven; this
   is an executable, nightly proof, not an argument.
6. **AI budget circuit breaker with degradation ladder** — brief asks cost control; this is
   enforcement with named behavior at the cap.
7. **Nightly credit-ledger reconciliation** against balances — catches arithmetic drift that
   per-operation tests miss.
8. **FakeATS mutation matrix** — form-drift blocking is exercised against deliberately mutating
   forms, not just asserted.

## 13. Assumptions

1. Model prices as stated are the 2026-08 commodity rates; the plan's economics re-verify
   against the eval harness before promotion whenever prices move.
2. Greenhouse public board API (`boards-api.greenhouse.io`) is accessible for monitoring public
   postings, with polite pacing; robots.txt respected.
3. The PM supplies the initial seed list of 500 company boards; ongoing discovery is
   user-requested boards + operator approval (mechanism in `boards.added_by/approved`).
4. Single-region US market: USD salary parsing, US-centric location matching, Letter-format
   documents, English-language forms/postings at launch.
5. Welcome balance = 5 credits; packs $10/10 credits; subscription $29/month incl. 30 credits
   (business parameters owned by PM; ledger is indifferent).
6. Users submit their own contact email on employer forms; employer OTP emails therefore reach
   the user's inbox directly, and Pilot's role is alert + code entry (no inbound-mail
   infrastructure).
7. OTP codes match `^[A-Za-z0-9]{4,8}$`; longer/exotic challenges fall to the generic
   `confidence=0` review path.
8. Chromium-only automation is acceptable (Greenhouse forms render identically enough across
   engines; Firefox/WebKit omitted to halve memory variance).
9. Max resume size 10 MB; one published profile version active per user at a time; feed staleness
   bound 6 h.
10. The operator is reachable by email and accepts healthchecks.io as the paging mechanism.
