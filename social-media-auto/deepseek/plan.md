# ToolBox Poster — Engineering Plan

Source of truth: `product.md`. This plan is self-contained: every technical decision is
made, no question is asked, no `TBD` remains. Where the brief was silent, an assumption
was made and is collected in §13. Mechanisms are named concretely; numbers are derived,
not asserted.

---

## 1. Technology decisions

Each decision names the choice, the strongest rejected alternative, and the reason.

### 1.1 Language and runtime
**Decision:** TypeScript 5.x (strict) on Node.js 22 LTS for every runtime component
(web API, workers, acquisition service, tooling), in a pnpm monorepo with one shared
`packages/domain` for types, transition tables, and validation schemas (zod).
**Rejected:** Go 1.24 — better raw concurrency and single-binary deploys, but it splits
the frontend/backend type contract into two implementations. The correctness-critical
parts of this product (state machines, uniqueness, ordering) are enforced in Postgres,
not in the language; what the language does carry is domain types shared with the UI.
**Rejected:** Python — media tooling is fine but type safety for a 20-state queue/attempt
FSM and shared frontend contracts is weaker.

### 1.2 Monorepo and packages
**Decision:** pnpm workspaces with `apps/web`, `apps/worker-media`,
`apps/worker-publish`, `apps/worker-schedule`, `apps/acquisition`, `packages/domain`,
`packages/db` (migrations), `packages/ig` (Meta Graph API client), `packages/mock-ig`
(fake Meta Graph API for tests and drills).
**Rejected:** One monolithic app — lanes (publish vs media vs browser work) must be
separate processes to guarantee starvation isolation (§2). Microservices over HTTP — the
extra network surface buys nothing at 2 nodes.

### 1.3 Web framework
**Decision:** Hono 4 on Node, zod-openapi schema-first routes, serving REST, SSE, and
webhook endpoints from one process. **Rejected:** NestJS (DI ceremony at a 1-engineer
team), Express 5 (weak TS), tRPC (internal-only; we need public webhook endpoints and
signed URLs, so an HTTP-first contract is required anyway).

### 1.4 Frontend
**Decision:** React 18 + Vite 6 + TanStack Query 5 + dnd-kit (drag reorder) + Tailwind 4
+ Radix primitives (accessibility) + vitest/RTL. An SPA behind auth; the public marketing
pages and legal pages are prerendered static files served from the same process.
**Rejected:** Next.js 15 — SSR value is near zero for a fully-authenticated product, and
it adds a server runtime plus cache-invalidation hazards. Svelte 5 — smaller a11y/dnd
ecosystem.

### 1.5 Database
**Decision:** PostgreSQL 16, single primary, self-hosted on the main VPS. Extensions:
`pgcrypto`, `btree_gist`, `citext`. Row-Level Security (RLS) enabled on every
tenant-scoped table (§3.3); `graphile_worker` runs its jobs tables in a separate schema.
**Rejected:** MySQL 8 — no partial unique indexes (needed for "one active publish attempt
per item", "one connected IG account globally"), no exclusion constraints. DynamoDB /
any NoSQL — cannot express the queue FSM, uniqueness, and counting atomically. Managed
Postgres (Neon etc.) — $50+/mo at our size and another vendor's incident process; a
self-hosted PG with WAL-G to R2 (§1.11) plus rehearsed restores is cheaper and its
failure mode is understood (stated RTO tradeoff in §11).

### 1.6 Job queue and scheduler
**Decision:** `graphile-worker` (Postgres-backed): transactional job enqueue inside
domain transactions, retry with exponential backoff, per-task concurrency pools, cron
schedules for planner ticks. **Rejected:** BullMQ/Redis — a second system of record and
enqueue is not atomic with the business write. Temporal — correct but heavy; revisit if
volume ×100. A custom queue — reinventing retries, DLQs, and scheduling.

### 1.7 Live updates
**Decision:** Postgres `LISTEN/NOTIFY` on per-workspace channel `ws:<id>` and
per-account channel `acct:<id>` → the web process → Server-Sent Events per browser
session, with `event_id` dedupe on the client. **Rejected:** WebSockets — connection
state and reconnection semantics are real work at 1 engineer; SSE over HTTP/2 is enough
for one-directional status. Pusher/Ably — cost, and a third party inside the tenant
isolation boundary.

### 1.8 Payments
**Decision:** Stripe: Checkout Sessions (`client_reference_id = workspace_id`),
Customer Portal, webhooks (signature-verified, event-idempotent per §3). Cards never
touch our product. **Rejected:** Paddle — merchant-of-record is tempting but entitlement
events and invoicing transparency are weaker and it is vendor-locking. LemonSqueezy —
immature webhook and seat primitives.

### 1.9 Instagram integration
**Decision:** Official Meta Graph API only for all customer-visible flows and all
**publishing**: Facebook Login for Business → account picker → Instagram Graph API
(media containers, `creation_id` idempotency, Insights, deletion to Recently Deleted).
Restricted sourcing uses Graph API hashtag/reels-search endpoints where they exist, plus
an isolated Playwright browser pool (on a separate VM, §2) for the residual flows with
no official endpoint. The browser pool **never** performs publishing; publishing always
goes through the official API so receipts are clean and the "no password" promise holds.
**Rejected:** Unofficial scraping SaaS as a dependency — unlicensed, unauditable, and a
reliability black box. Pure-browser publishing — violates the password promise and
receipt requirements.

### 1.10 Media processing
**Decision:** ffmpeg 7 (static build) + sharp, invoked as child processes from
`worker-media` under a cgroup CPU/memory limit; 2 worker processes × 2 lanes × 2 cores
(§2.4 derivation). **Rejected:** Cloudinary/Transloadit — per-unit cost exceeds the VPS
at 2,000 items/day and adds a vendor to the private-media chain. Lambda — cold starts
and cost at burst; a 2-minute transcode is cheaper on owned CPU.

### 1.11 Object storage and backups
**Decision:** Cloudflare R2 (S3-compatible) for all media and backups: private buckets,
presigned URLs for access (10-minute expiry, §9). Backups: WAL-G streaming WAL every
15 minutes + daily full base backups to R2; daily `pg_dump` for convenience; 7-day
daily, 14-day PITR retention. **Rejected:** AWS S3 — $0.09/GB egress would dominate
cost when serving customer media back. Backblaze B2 — fine, but R2's zero egress wins
outright. pgBackRest — more machinery than WAL-G for one node.

### 1.12 Email
**Decision:** Resend (magic links, invites, account-recovery email, transactional
notices; notification emails are off by default). **Rejected:** SES (deliverability
setup burden at 1 engineer), Postmark (fine; Resend has the simpler DX and same price
band).

### 1.13 Deployment
**Decision:** Hetzner, one region (FSN1, Germany): CPX41 (8 vCPU / 16 GB / 360 GB NVMe)
running web + all workers + PostgreSQL 16 via Docker Compose + systemd; a second CX22
(2 vCPU / 4 GB) dedicated to the acquisition/browser service with its own egress IP.
Caddy 2 for TLS/HTTP2. CI/CD: GitHub Actions → Docker images → compose pull/up with
healthcheck-gated rollback. **Rejected:** Kubernetes — ~100× the operational surface for
2 machines. Fly.io/Render — long-running worker and egress economics favor Hetzner at
this scale. Managed Postgres — cost (§1.5).

### 1.14 Authentication
**Decision:** Customers: email magic links — opaque 256-bit token, SHA-256 stored,
10-minute TTL, single-use, 5 per email per hour, 3 failed attempts invalidate. Sessions:
opaque 256-bit id, 30-day sliding expiry. Operators: separate realm with WebAuthn +
TOTP, IP allowlist, 15-minute idle timeout, per-action audit (§9).
**Rejected:** Passwords — breach surface and reset flows buy nothing for a tool whose
differentiator is not touching secrets. Full SSO — no enterprise customers at launch.

### 1.15 Observability
**Decision:** Structured JSON logs with a centralized redaction filter (§9.8) shipped to
Loki (self-hosted single container); Prometheus metrics + Grafana dashboards; Sentry for
frontend/backend errors. Correlation via `trace_id` and `event_id` fields in logs — no
distributed tracing backend in v1 (stated tradeoff, §11). Alarms (Prometheus
Alertmanager → Resend email to operator):
- any active account with a schedule rule has **0 ready queue items** ("silent queue
  death") — page
- publish lane age p95 > 15 min — page
- unknown-outcome (reconcile) backlog > 20 items — page
- media prep failure rate > 5%/hour — warn
- any worker process restarts > 5/hour — warn
- WAL archiving lag > 30 min or R2 5xx rate > 1% — page
- disk > 80% on VPS — warn
- Stripe webhook last-delivery age > 15 min — warn
**Rejected:** Datadog — ~$100+/mo at our size. Self-hosted Tempo — ops weight with no
current customer pain behind it.

### 1.16 Secrets and encryption at rest
**Decision:** SOPS + age for repo/CI secrets. Instagram tokens at rest: envelope
encryption — per-row random data key, AES-256-GCM, data key wrapped by a master key that
exists only in the worker/web process environment. Browser sessions at rest: same
envelope scheme, keys only in the acquisition service environment. **Rejected:**
HashiCorp Vault — a server to run, patch, and back up, for one consumer. Cloud KMS — we
are on a VPS; stated tradeoff in §11.

### 1.17 State machines
**Decision:** Hand-written typed transition tables in `packages/domain` (exhaustive
union matching, compile-time complete) mirrored 1:1 by Postgres CHECK constraints and
BEFORE UPDATE transition-guard triggers (§3.6). A CI test diffs the SQL table against
the TS table to keep them in parity. **Rejected:** XState in the backend — runtime
serialization cost and the SQL/TS mirror gets harder to keep honest.

### 1.18 Operating numbers derived from the brief

| Parameter | Value | Derivation |
|---|---|---|
| Registered users | 1,000 | brief |
| WAU | 200 | brief |
| Connected IG accounts | ≤ 300 | brief ("several hundred") |
| Peak items/day | 2,000 | brief upper bound |
| Peak publish load | 4.4/min | assume 40% of daily publishes in the 19:00–22:00 local window → 800 items / 3h |
| Publish throughput target | 12/min sustained | 4 concurrent API streams × 60s / (p95 send 15s + 5s overhead); clears 2,000 in ≤ 2.8h |
| Publish send timeout | 30 s | covers p95 container create; pre-send failure retry ×3 backoff 15s/60s/240s |
| Unknown-outcome poll | every 60 s, ≤ 20 min | then `needs_review` + notification; never auto-retry (§3.4, §4) |
| Media prep timeout | 10 min hard, retry ×2 (5s/30s) | per item; poisoned input → `failed` with reason |
| Media throughput | 1,200 videos × 90 s = 30 core-h/day worst case | 2 workers × 2 lanes × 2 cores → ~7.5 h; backlog is prepared ahead of slots, not at slot time |
| IG publishes/account/day | hard cap 25 (IG documented API limit); product default `daily_allowance = 4`, customer-set 1–25 | enforced by `usage_daily` counter (§3.4) |
| Min interval between publishes | 60 min default, per-account setting 0–1440 min | cooldown, product-level |
| Insights calls | IG allows 240/h/token; we budget ≤ 120/h, 1 refresh per account per hour, 4 lanes | manual refresh rate-limited: 1 per account per 10 min |
| Upload limits | image ≤ 30 MB, video ≤ 2 GB, 10 MB chunks, 4 concurrent uploads/workspace | matches IG media constraints |
| Browser pool | 2 concurrent sessions total, 1 per workspace, 30 s step timeout, 3 min page timeout, 20 min idle kill | isolation-first |
| Storage growth | avg item = 60 MB original (images 8 MB, reels 120 MB, 60/40 mix) + 45 MB prepared; 400 items/day steady → ~1.5 TB originals + 1.1 TB prepared at month 12 | backlog of weeks means originals accumulate |
| Monthly cost at month 12 | ≈ $107 (table below) | within "few hundred dollars" |

**Monthly cost (USD):**

| Item | Cost |
|---|---|
| Hetzner CPX41 | ~40 |
| Hetzner CX22 (browser pool) | ~9 |
| R2 storage 2.6 TB × $0.015 | ~39 |
| R2 Class A/B ops (2M/mo) | ~10 |
| Resend ~6,000 emails | ~10 |
| Domain | ~1 |
| Sentry / Grafana stack | 0 (free tier / self-hosted) |
| **Total** | **≈ 109** |

Year-1 start ≈ $70/month, month-12 ≈ $109/month.

**Retention:**
- Analytics raw snapshots 90 days; daily rollups 730 days; weekly rollups indefinite.
- Item events + audit log 7 years (hash-chained, append-only).
- Deletion proofs 7 years (minimal, non-identifying).
- R2 originals: until explicit deletion. Prepared media: kept (receipt evidence).
- Completed jobs pruned 14 days; failed jobs retained 30 days for inspection.
- Notifications 180 days. Stripe raw events 7 years (immutable).
- Magic-link tokens and session rows deleted on expiry (30-day sweep).

## 2. System architecture

### 2.1 Topology

```
                        [Caddy: TLS, static site, SSE]

  customers ────►  apps/web (Hono) ◄───────────── Stripe webhooks
   (browser)        │ REST + SSE + admin API        (signature verified)
                    │ NOTIFY listener per ws:/acct:
                    │
                    ▼                          ┌──────────────────────────────┐
            ┌───────────────┐                  │ PostgreSQL 16 (single source │
            │ apps/worker-  │                  │ of truth)                    │
            │ media  (2×2)  │◄─────────────────┤  - tenant data + RLS         │
            │ ffmpeg/sharp  │                  │  - graphile_worker jobs      │
            └───────────────┘                  │  - NOTIFY events             │
                                               └──────────────┬───────────────┘
            ┌───────────────┐                                 │
            │ apps/worker-  │   ┌───────────────┐   ┌─────────┴────────┐
            │ publish (4)   │   │ apps/worker-  │   │ apps/worker-     │
            │ reconcile (1) │   │ schedule      │   │ analytics (4)    │
            │ cleanup  (1)  │   │ planner tick  │   │ insights snapshots│
            │ autofill (1)  │   │ 60s, 14d look │   │ rollups           │
            └──────┬────────┘   └───────┬───────┘   └─────────┬────────┘
                   │                    │                     │
                   ▼                    ▼                     ▼
           Meta Graph API        R2 (private buckets)   Meta Insights API
   (publish, delete, health,     (media, backups)      (metrics snapshots)
    sourcing read endpoints)

  ┌────────────────────────────────────────────────────────────┐
  │ Separate VM (CX22): apps/acquisition                       │
  │  - source verification/collection jobs (graphile, own pool)│
  │  - Playwright pool (2 sessions, per-workspace ≤ 1)         │
  │  - encrypted session store; NOTHING session-related egress │
  │  - internal HTTP API consumed only by worker-schedule via  │
  │    a shared secret; no customer-facing endpoint            │
  └────────────────────────────────────────────────────────────┘
```

### 2.2 Components and responsibilities

- **apps/web** — auth (magic links, sessions), REST API for the SPA, SSE gateway
  (LISTEN/NOTIFY fan-out), Stripe webhook endpoint, operator admin API, presigned-URL
  issuance. Stateless; all writes are domain transactions that enqueue jobs.
- **apps/worker-schedule** — the planner: every 60 s it (a) regenerates
  `schedule_occurrences` for accounts whose rules/timezone changed, (b) claims due
  slots → enqueues `publish.item` jobs, (c) triggers `analytics.refresh` jobs, (d) runs
  `autofill` and `source.check` scheduling, (e) escalates overdue unknowns. All planner
  writes are idempotent: slot claims are transactional `INSERT ... ON CONFLICT DO
  NOTHING` on `schedule_occurrences.claimed_by_item_id` (§3.4).
- **apps/worker-publish** — lanes:
  - `publish.item` × 4 global concurrency, per-account serialized via
    `pg_advisory_xact_lock('publish', account_id)`; executes the frozen attempt (§3.4).
  - `publish.reconcile` × 1: polls container status for `unknown` attempts (60 s
    interval, ≤ 20 min), then resolves or escalates to `needs_review`.
  - `cleanup.execute` × 1: entitled destructive cleanup (§3.5), one item at a time per
    account.
  - `autofill` × 1: moves eligible backlog items into the queue up to `target_depth`.
- **apps/worker-media** — `media.prepare` × 4 (2 procs × 2 lanes): validates input,
  transcodes/normalizes (aspect fit, metadata strip, overlay), writes prepared +
  thumbnail to R2, flips `media_assets.state`. Rejected inputs recorded with a
  customer-facing reason.
- **apps/worker-analytics** — `analytics.refresh` × 4 lanes: Insights fetch per account
  (≤ 1/h/account, jittered), snapshot + rollup upserts. Never touches publish lanes.
- **apps/acquisition** (separate VM) — restricted sourcing only (§3.5): verifies
  sources, collects candidates into `source_posts`/`backlog_items`, runs the browser
  pool for flows without official endpoints. Refuses work for any workspace without an
  active `restricted_sourcing` entitlement (server-side check, not UI).
- **Postgres** — the only system of record. Jobs, locks, uniqueness, transitions,
  audit, and usage counters all live here so a process crash can never corrupt the
  domain state.
- **R2** — private buckets `media-originals`, `media-prepared`, `exports`,
  `backups`; presigned access only.

### 2.3 How work moves (happy path)

1. Upload: browser requests presigned multipart URLs → chunks land in R2 → web inserts
   `media_assets` (state `preparing`) + enqueues `media.prepare` **in one transaction**.
2. Prep: worker-media processes → writes prepared artifacts → state `ready` → NOTIFY →
   SSE updates the uploader's page (they may have left; state is durable regardless).
3. Queue: user selects account, caption, rights checkbox → web inserts `queue_items`
   with `frozen` snapshot + `rights_acceptance_id`, position at tail, state `ready`.
4. Planner: at each due occurrence, claims slot → enqueues `publish.item`.
   "Publish now" enqueues the same job with an immediate slot.
5. Publish: worker-publish in a transaction: lock item row, assert transition
   `ready→publishing`, bump `usage_daily`, create `publish_attempts` (state `claimed`,
   `frozen_copy`, `creation_id`), commit. Then external: POST media container with
   `creation_id = tbx_<queue_item>_<sha256(frozen)>`; 30 s timeout.
   - Success → `accepted` → poll container status ≤ 20 min → publish → `resolved_published`,
     insert `receipts`, NOTIFY, SSE.
   - Pre-send failure (4xx/5xx/timeout before any send succeeded) → retry ×3 → else
     `failed` with `error_class`.
   - Timeout after send (unknown) → state `unknown` → `publish.reconcile` investigates;
     never auto-retried (§3.4, §5).
6. Analytics: hourly refresh snapshots metrics for published media ids → rollups →
   library view reads snapshots/rollups only.

### 2.4 Starvation and endangerment isolation (numbers)

- Lanes are **separate processes** with fixed pools (graphile `--concurrency` per task
  identifier). Heavy media work consumes only the media pool; a flood of Reels
  transcodes cannot delay the 4 publish streams or the planner tick.
- Publish jobs carry deadlines: a job older than 60 s at pickup is re-planned to the
  next slot rather than posting late (a late "publish now" is better than a surprise
  "publish later").
- The acquisition VM shares nothing but the DB with the main host: browser hangs,
  memory leaks, or IP reputation issues there cannot starve publishing.
- DB admission control: `worker-publish` wraps every attempt in
  `pg_advisory_xact_lock` (per-account) and uses `SELECT ... FOR UPDATE SKIP LOCKED` on
  jobs; a stuck transaction blocks one account, never the queue.
- Reconcile lane (1) is a different task from publish (4), so an ambiguity storm (e.g.,
  Meta 500s mid-accept) cannot consume publish capacity.
- Autofill and cleanup both run at concurrency 1 and both are capped per account
  (`target_depth`, one-cleanup-active-at-a-time), so background work cannot flood the
  queue or the destructive boundary.

## 3. Data model

Executable PostgreSQL 16 DDL. Schema `app` for domain, `graphile_worker` for jobs.
Two conventions apply everywhere:

- Every tenant-scoped table carries `workspace_id bigint not null` and an RLS policy
  of the form `using (workspace_id = app.ws())`. `app.ws()` reads a GUC
  (`app.workspace_id`) set by the web process after authorization, and by workers from
  the job payload. RLS is defense-in-depth: the application layer must also filter by
  workspace in every query (CI lint, §4).
- Mutation of state columns goes through transition-guard triggers (§3.6) so the
  database, not the caller, is the final authority on allowed transitions.

```sql
create schema app;
create extension if not exists pgcrypto;
create extension if not exists btree_gist;
create extension if not exists citext;

-- tenant context helper for RLS
create function app.ws() returns bigint language sql stable as $$
  select nullif(current_setting('app.workspace_id', true), '')::bigint
$$;

-- authenticated user helper for RLS (set alongside app.workspace_id)
create function app.uid() returns bigint language sql stable as $$
  select nullif(current_setting('app.user_id', true), '')::bigint
$$;

-- roles: app_web (RLS-enforced), app_worker (RLS-enforced, per-job GUC), app_svc (BYPASSRLS, backup/ops only)
create role app_web login;
create role app_worker login;
create role app_svc bypassrls login;

-- ============================================================
-- Identity, workspaces, invites
-- ============================================================

create table app.users (
  id            bigint generated always as identity primary key,
  email         citext not null unique,
  name          text,
  state         text not null default 'active'
                check (state in ('active','suspended','deleted')),
  created_at    timestamptz not null default now(),
  last_login_at timestamptz
);
alter table app.users enable row level security;
create policy users_sel on app.users for select using (
  id = app.uid() or exists (
    select 1 from app.workspace_memberships m
    where m.user_id = app.users.id and m.workspace_id = app.ws()));

create table app.workspaces (
  id            bigint generated always as identity primary key,
  name          text not null,
  slug          citext not null unique,
  state         text not null default 'active'
                check (state in ('active','suspended')),
  created_at    timestamptz not null default now()
);
alter table app.workspaces enable row level security;
create policy ws_sel on app.workspaces for select using (id = app.ws());
create policy ws_upd on app.workspaces for update using (id = app.ws());

create table app.workspace_memberships (
  workspace_id bigint not null references app.workspaces(id),
  user_id      bigint not null references app.users(id),
  role         text not null check (role in ('owner','editor','viewer')),
  state        text not null default 'active'
               check (state in ('active','suspended','left')),
  created_at   timestamptz not null default now(),
  primary key (workspace_id, user_id)
);
alter table app.workspace_memberships enable row level security;
create policy mem_sel on app.workspace_memberships for select using (workspace_id = app.ws());
create policy mem_ins on app.workspace_memberships for insert with check (workspace_id = app.ws());
create policy mem_upd on app.workspace_memberships for update using (workspace_id = app.ws());
-- launch rule: exactly one active owner per workspace (transfer is a 2-step flow)
create unique index uq_one_owner on app.workspace_memberships (workspace_id)
  where role = 'owner' and state = 'active';

create table app.login_links (
  token_hash text primary key,            -- sha256(token); token never stored
  email      citext not null,
  used_at    timestamptz,
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);
create index on app.login_links (email, created_at);

create table app.sessions (
  id         text primary key,            -- 256-bit opaque id
  user_id    bigint not null references app.users(id),
  expires_at timestamptz not null,
  revoked_at timestamptz,
  created_at timestamptz not null default now()
);
create index on app.sessions (user_id);

create table app.invites (
  id           bigint generated always as identity primary key,
  workspace_id bigint not null references app.workspaces(id),
  email        citext not null,
  role         text not null default 'editor' check (role in ('editor','viewer','owner')),
  token_hash   text not null unique,
  created_by   bigint not null references app.users(id),
  created_at   timestamptz not null default now(),
  expires_at   timestamptz not null,      -- 7 days, single-use
  consumed_by  bigint references app.users(id),
  consumed_at  timestamptz,
  revoked_at   timestamptz
);

create table app.waitlist_entries (
  id         bigint generated always as identity primary key,
  email      citext not null unique,
  state      text not null default 'waiting'
             check (state in ('waiting','invited','rejected','converted')),
  note       text,
  created_at timestamptz not null default now()
);

create table app.account_requests (
  id                 bigint generated always as identity primary key,
  workspace_id       bigint not null references app.workspaces(id),
  requested_by       bigint not null references app.users(id),
  ig_username        text not null,
  state              text not null default 'pending'
                     check (state in ('pending','approved','declined','invited')),
  operator_decision  text,          -- customer-visible reason
  internal_note      text,          -- never returned by any customer-facing query
  decided_by         bigint,        -- operator id (app.operator_users)
  decided_at         timestamptz,
  created_at         timestamptz not null default now(),
  unique (workspace_id, ig_username)
);

-- ============================================================
-- Instagram accounts, schedule
-- ============================================================

create table app.instagram_accounts (
  id                 bigint generated always as identity primary key,
  workspace_id       bigint not null references app.workspaces(id),
  ig_user_id         text not null,
  username           text not null,
  profile_image_key  text,
  state              text not null default 'connected'
                     check (state in ('connected','disconnected','revoked','suspended')),
  access_token_enc   bytea not null,        -- AES-256-GCM envelope (§9)
  token_expires_at   timestamptz,
  scopes             jsonb not null,
  connected_at       timestamptz not null default now(),
  disconnected_at    timestamptz,
  last_health_check  timestamptz,
  last_health_error  text,
  daily_allowance    int not null default 4 check (daily_allowance between 1 and 25),
  min_interval_min   int not null default 60 check (min_interval_min between 0 and 1440),
  publish_state      text not null default 'active'
                     check (publish_state in ('active','paused','held')),
  hold_reason        text check (hold_reason in
                     ('paused','disconnected','suspended','over_plan','over_quota','reauth_needed')),
  timezone           text not null default 'UTC',
  created_at         timestamptz not null default now()
);
-- the same IG account can never be connected to two workspaces simultaneously;
-- disconnect/reconnect to the *same* workspace is allowed because the index is partial
create unique index uq_ig_connected on app.instagram_accounts (ig_user_id)
  where state = 'connected';
alter table app.instagram_accounts enable row level security;
create policy iga_sel on app.instagram_accounts for select using (workspace_id = app.ws());
create policy iga_upd on app.instagram_accounts for update using (workspace_id = app.ws());
create policy iga_del on app.instagram_accounts for delete using (workspace_id = app.ws());

create table app.schedule_rules (
  id               bigint generated always as identity primary key,
  ig_account_id    bigint not null references app.instagram_accounts(id),
  kind             text not null check (kind in ('fixed','interval')),
  days             int[] not null,          -- ISO 1..7 for fixed
  local_time       time,                    -- fixed
  window_start     time,                    -- interval
  window_end       time,
  interval_minutes int,
  timezone         text not null,           -- IANA zone for this rule
  active           boolean not null default true,
  created_at       timestamptz not null default now(),
  updated_at       timestamptz not null default now(),
  check (kind <> 'fixed' or (local_time is not null)),
  check (kind <> 'interval' or (window_start is not null and window_end is not null
                                and interval_minutes between 5 and 720))
);

-- Materialized upcoming runs, regenerated by the planner on rule/timezone edits.
create table app.schedule_occurrences (
  id                  bigint generated always as identity primary key,
  ig_account_id       bigint not null references app.instagram_accounts(id),
  rule_id             bigint references app.schedule_rules(id) on delete set null,
  slot_utc            timestamptz not null,
  generation          int not null default 1,   -- bumped on every regen; old gen rows deleted
  claimed_by_item_id  bigint,                   -- set atomically by the planner
  claimed_at          timestamptz,
  unique (ig_account_id, slot_utc, generation)
);
create unique index uq_slot_claim on app.schedule_occurrences (claimed_by_item_id)
  where claimed_by_item_id is not null;
create index on app.schedule_occurrences (ig_account_id, slot_utc) where claimed_by_item_id is null;

-- ============================================================
-- Media, rights, queue
-- ============================================================

create table app.media_assets (
  id               bigint generated always as identity primary key,
  workspace_id     bigint not null references app.workspaces(id),
  kind             text not null check (kind in ('image','video')),
  storage_key      text not null unique,     -- originals/<ws>/<uuid>/raw
  prepared_key     text,                     -- prepared/<ws>/<uuid>/out
  thumbnail_key    text,
  mime             text not null,
  size_bytes       bigint not null,
  sha256           text not null,
  width            int, height int, duration_ms int,
  state            text not null default 'preparing'
                   check (state in ('preparing','ready','failed','purged')),
  fail_reason      text,
  prepared_frozen  jsonb,                    -- what preparation was applied (aspect, overlay, strip)
  created_by       bigint not null references app.users(id),
  created_at       timestamptz not null default now(),
  unique (workspace_id, sha256)              -- identical uploads dedupe storage
);
alter table app.media_assets enable row level security;
create policy med_sel on app.media_assets for select using (workspace_id = app.ws());
create policy med_ins on app.media_assets for insert with check (workspace_id = app.ws());
create policy med_upd on app.media_assets for update using (workspace_id = app.ws());

create table app.rights_acceptances (
  id           bigint generated always as identity primary key,
  workspace_id bigint not null references app.workspaces(id),
  user_id      bigint not null references app.users(id),
  version      int not null,                 -- the rights text version accepted
  context      jsonb not null,
  accepted_at  timestamptz not null default now()
);
alter table app.rights_acceptances enable row level security;
create policy ra_sel on app.rights_acceptances for select using (workspace_id = app.ws());
create policy ra_ins on app.rights_acceptances for insert with check (workspace_id = app.ws());

create table app.queue_items (
  id                   bigint generated always as identity primary key,
  workspace_id         bigint not null references app.workspaces(id),
  ig_account_id        bigint not null references app.instagram_accounts(id),
  position             bigint not null,      -- order within account (renumbered in tx)
  state                text not null default 'preparing'
                       check (state in ('preparing','ready','publishing','published',
                                        'failed','needs_review','hidden','removed','canceled')),
  frozen               jsonb not null,       -- caption, dest, prep choices, attribution (immutable once set)
  media_asset_id       bigint not null references app.media_assets(id),
  source_id            bigint,               -- provenance (restricted sourcing)
  dedupe_key           text,                 -- external dedupe (usually the source post id)
  rights_acceptance_id bigint not null references app.rights_acceptances(id),
  created_by           bigint not null references app.users(id),
  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  published_at         timestamptz,
  constraint uq_qpos unique (ig_account_id, position) deferrable initially deferred,
  constraint dedupe_in_queue unique (ig_account_id, dedupe_key)
    -- NULLs don't collide; 'removed' rows keep the key reserved against re-import
);
alter table app.queue_items enable row level security;
create policy qi_sel on app.queue_items for select using (workspace_id = app.ws());
create policy qi_ins on app.queue_items for insert with check (workspace_id = app.ws());
create policy qi_upd on app.queue_items for update using (workspace_id = app.ws());
create index on app.queue_items (ig_account_id, state) where state in ('ready','publishing');

create table app.publish_attempts (
  id             bigint generated always as identity primary key,
  queue_item_id  bigint not null references app.queue_items(id),
  attempt_no     int not null default 1,
  slot_id        bigint references app.schedule_occurrences(id),
  state          text not null default 'claimed'
                 check (state in ('claimed','sending','sent','accepted','rejected',
                                  'unknown','reconciling','resolved_published',
                                  'resolved_failed','canceled')),
  creation_id    text not null unique,      -- tbx_<item>_<sha256(frozen)>; sent to IG as creation_id
  container_id   text,
  media_id       text,
  permalink      text,
  frozen_copy    jsonb not null,            -- immutable snapshot of frozen at claim time
  error_class    text check (error_class in
                 ('customer_fixable','outside_temporary','perms_exhausted',
                  'account_limit','invalid_media','unknown_outcome')),
  error_detail   jsonb,
  evidence       jsonb,                     -- request/response digests, timestamps; redacted (§9)
  started_at     timestamptz,               -- first external send
  sent_at        timestamptz,
  resolved_at    timestamptz,
  claimed_by     text,                      -- worker id
  leased_until   timestamptz,
  unique (queue_item_id, attempt_no)
);
-- the "only one active publication" rule, enforced by the database
create unique index uq_active_attempt on app.publish_attempts (queue_item_id)
  where state in ('claimed','sending','sent','unknown','reconciling');
create index on app.publish_attempts (state)
  where state in ('claimed','sending','unknown','reconciling');

create table app.usage_daily (
  ig_account_id bigint not null references app.instagram_accounts(id),
  day_local     date not null,              -- account-local calendar day
  publishes     int not null default 0,
  primary key (ig_account_id, day_local),
  constraint usage_cap check (publishes <= 25)
);

create table app.receipts (
  queue_item_id bigint primary key references app.queue_items(id),
  attempt_id    bigint not null references app.publish_attempts(id),
  workspace_id  bigint not null references app.workspaces(id),
  ig_account_id bigint not null references app.instagram_accounts(id),
  frozen_copy   jsonb not null,
  media_id      text not null,
  permalink     text not null,
  published_at  timestamptz not null,
  evidence      jsonb not null,             -- attempt digests, IG status history
  created_at    timestamptz not null default now()
);
alter table app.receipts enable row level security;
create policy rec_sel on app.receipts for select using (workspace_id = app.ws());

create table app.item_events (
  id            bigint generated always as identity primary key,
  workspace_id  bigint not null,
  queue_item_id bigint not null references app.queue_items(id),
  kind          text not null,              -- 'state_change' | 'attempt' | 'schedule' | ...
  from_state    text,
  to_state      text,
  actor         text not null,              -- 'user:<id>' | 'system' | 'operator:<id>'
  reason        text,
  created_at    timestamptz not null default now()
);
create index on app.item_events (queue_item_id, id);

create table app.notifications (
  id           bigint generated always as identity primary key,
  workspace_id bigint not null references app.workspaces(id),
  user_id      bigint not null references app.users(id),
  kind         text not null,               -- connection|publish|uncertain|cleanup|source|limit|billing|attention
  severity     text not null default 'info' check (severity in ('info','warning','critical')),
  title        text not null,
  body         text not null,
  link         text,                        -- deep link to account/item
  dedupe_key   text,
  read_at      timestamptz,
  dismissed_at timestamptz,
  created_at   timestamptz not null default now(),
  unique (user_id, dedupe_key)
);
alter table app.notifications enable row level security;
create policy notif_sel on app.notifications for select using (workspace_id = app.ws());
create policy notif_upd on app.notifications for update using (workspace_id = app.ws());

-- ============================================================
-- Analytics
-- ============================================================

create table app.analytics_snapshots (
  id            bigint generated always as identity primary key,
  workspace_id  bigint not null,
  ig_account_id bigint not null references app.instagram_accounts(id),
  media_id      text not null,
  metrics       jsonb not null,             -- raw IG insights payload
  captured_at   timestamptz not null,
  unique (media_id, captured_at)
);

create table app.analytics_rollups (
  workspace_id        bigint not null,
  ig_account_id       bigint not null,
  media_id            text not null,
  bucket              text not null check (bucket in ('day','week')),
  bucket_start        date not null,
  metrics             jsonb not null,       -- aggregated (reach, plays, likes, comments, saves, shares)
  source_snapshot_ids bigint[] not null,    -- which snapshots fed this rollup (idempotent rebuild)
  primary key (ig_account_id, media_id, bucket, bucket_start)
);

-- ============================================================
-- Library, cleanup
-- ============================================================

create table app.protected_posts (
  workspace_id bigint not null,
  media_id     text not null,
  protected_by bigint not null references app.users(id),
  created_at   timestamptz not null default now(),
  primary key (workspace_id, media_id)
);

create view app.library_posts with (security_barrier = true) as
select r.workspace_id, r.ig_account_id, r.media_id, r.permalink,
       r.frozen_copy->>'caption' as caption,
       q.id as queue_item_id, q.media_asset_id, q.source_id,
       r.published_at,
       sp.external_post_id as source_post_id, sp.original_url,
       (pp.media_id is not null) as protected
from app.receipts r
join app.queue_items q on q.id = r.queue_item_id
left join app.source_posts sp on sp.id = q.source_id
left join app.protected_posts pp
  on pp.media_id = r.media_id and pp.workspace_id = r.workspace_id
where r.workspace_id = app.ws();

create table app.cleanup_rules (
  id                     bigint generated always as identity primary key,
  ig_account_id          bigint not null references app.instagram_accounts(id),
  name                   text not null,
  media_kind             text not null check (media_kind in ('image','reel','any')),
  min_age_days           int not null check (min_age_days between 1 and 365),
  conditions             jsonb not null,    -- [{metric, op, value}]; metric in likes|comments|plays|reach|saves|shares
  analytics_max_age_hours int not null default 24 check (analytics_max_age_hours between 1 and 168),
  schedule               text not null default 'manual' check (schedule in ('manual','daily','weekly')),
  local_time             time,
  day_of_week            int,
  active                 boolean not null default true,
  created_by             bigint not null references app.users(id),
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now()
);

create table app.cleanup_runs (
  id            bigint generated always as identity primary key,
  ig_account_id bigint not null references app.instagram_accounts(id),
  rule_id       bigint references app.cleanup_rules(id) on delete set null,
  trigger       text not null check (trigger in ('manual','scheduled')),
  state         text not null default 'preview'
                check (state in ('preview','confirmed','running','paused_reconcile',
                                 'stopped','completed','invalidated')),
  frozen_rule   jsonb not null,             -- rule snapshot at preview time
  selection     jsonb not null,             -- [{media_id, metrics_used, metrics_age, reason}]
  selection_hash text not null,             -- sha256(canonical(selection))
  confirmed_by  bigint references app.users(id),
  confirmed_at  timestamptz,
  started_at    timestamptz,
  finished_at   timestamptz,
  created_at    timestamptz not null default now()
);
-- one active cleanup per account, period (hard rule 4)
create unique index uq_active_cleanup on app.cleanup_runs (ig_account_id)
  where state in ('confirmed','running','paused_reconcile');

create table app.cleanup_items (
  id           bigint generated always as identity primary key,
  run_id       bigint not null references app.cleanup_runs(id),
  media_id     text not null,
  media_kind   text not null check (media_kind in ('image','reel')),
  position     int not null,                -- execution order; stop between items
  state        text not null default 'pending'
               check (state in ('pending','executing','done','reconciling','skipped','failed','canceled')),
  action       text not null check (action in ('archive','delete_recently')),
  metrics_used jsonb not null,
  evidence     jsonb,                       -- redacted; never session material (§9)
  started_at   timestamptz,
  finished_at  timestamptz,
  unique (run_id, media_id),
  unique (run_id, position)
);

-- ============================================================
-- Restricted sourcing (operator-entitled only)
-- ============================================================

create table app.sources (
  id             bigint generated always as identity primary key,
  workspace_id   bigint not null references app.workspaces(id),
  ig_account_id  bigint not null references app.instagram_accounts(id),
  kind           text not null check (kind in ('account','hashtag','reels_feed')),
  target         text not null,
  state          text not null default 'pending_verification'
                 check (state in ('pending_verification','active','paused','retrying','blocked','removed')),
  filters        jsonb not null,            -- {media_types, max_age_days, min_likes, min_comments,
                                            --  min_plays, exclude_caption_words, candidates_per_run,
                                            --  check_interval_minutes}
  last_checked_at timestamptz,
  last_error     text,
  created_by     bigint not null references app.users(id),
  created_at     timestamptz not null default now()
);

create table app.source_posts (             -- dedupe ledger: identity + provenance
  id               bigint generated always as identity primary key,
  source_id        bigint not null references app.sources(id),
  external_post_id text not null,
  author_handle    text,
  caption          text,
  media_type       text not null,
  original_url     text not null,
  metrics          jsonb not null,          -- engagement observed at discovery
  discovered_at    timestamptz not null,
  state            text not null default 'new'
                   check (state in ('new','held','imported','rejected','expired')),
  unique (source_id, external_post_id)
);

create table app.backlog_items (
  id              bigint generated always as identity primary key,
  workspace_id    bigint not null,
  ig_account_id   bigint not null references app.instagram_accounts(id),
  source_post_id  bigint not null references app.source_posts(id),
  state           text not null default 'held'
                  check (state in ('held','queued','discarded','removed')),
  hold_reason     text,                     -- which filter held it back, vs ready-to-fill
  media_asset_id  bigint references app.media_assets(id),
  created_at      timestamptz not null default now(),
  unique (ig_account_id, source_post_id)
);

create table app.autofill_config (
  ig_account_id bigint primary key references app.instagram_accounts(id),
  enabled       boolean not null default false,
  target_depth  int not null default 7 check (target_depth between 1 and 30),
  max_per_run   int not null default 5 check (max_per_run between 1 and 10),
  updated_at    timestamptz not null default now()
);

-- ============================================================
-- Entitlements, billing
-- ============================================================

create table app.entitlements (             -- the only table entitlement checks read
  id           bigint generated always as identity primary key,
  workspace_id bigint not null references app.workspaces(id),
  capability   text not null,               -- e.g. 'accounts:10','cleanup','restricted_sourcing',
                                            --      'storage:100gb','seats:5','beta:standard'
  source       text not null check (source in ('plan','operator','beta')),
  granted_at   timestamptz not null default now(),
  expires_at   timestamptz,
  revoked_at   timestamptz,
  granted_by   text not null,               -- 'stripe' | 'operator:<id>'
  unique (workspace_id, capability, source)
);
create index on app.entitlements (workspace_id)
  where revoked_at is null and (expires_at is null or expires_at > now());

create table app.subscriptions (
  workspace_id           bigint primary key references app.workspaces(id),
  stripe_customer_id     text unique,
  stripe_subscription_id text,
  status                 text not null default 'none'
                         check (status in ('none','trialing','active','past_due',
                                           'canceled','unpaid','incomplete')),
  plan_id                text not null,
  interval               text not null default 'monthly' check (interval in ('monthly','annual')),
  current_period_end     timestamptz,
  cancel_at_period_end   boolean not null default false,
  seats                  int not null default 1,
  last_processed_stripe_ts timestamptz,     -- replay watermark (out-of-order guard)
  updated_at             timestamptz not null default now()
);

create table app.stripe_events (
  id           text primary key,            -- Stripe event id = idempotency key
  type         text not null,
  payload      jsonb not null,
  created_ts   timestamptz not null,
  processed_at timestamptz,
  attempts     int not null default 0,
  last_error   text
);
-- ============================================================
-- Audit, operators, export, deletion, idempotency
-- ============================================================

create table app.audit_log (                -- append-only, hash-chained
  id           bigint generated always as identity primary key,
  workspace_id bigint,                      -- null for platform-level actions
  actor        text not null,               -- 'user:<id>' | 'operator:<id>' | 'system:<name>'
  action       text not null,               -- e.g. 'publish.resolve','invite.revoke','entitlement.grant'
  target_type  text,
  target_id    text,
  outcome      text not null check (outcome in ('success','failure','uncertain')),
  evidence     jsonb not null,
  prev_hash    text not null,
  hash         text not null,               -- sha256(prev_hash || id || canonical(rest))
  created_at   timestamptz not null default now()
);
create index on app.audit_log (workspace_id, created_at);
create index on app.audit_log (target_type, target_id);

create table app.operator_users (
  id             bigint generated always as identity primary key,
  email          citext not null unique,
  webauthn_cred  jsonb not null,
  totp_secret_enc bytea,
  created_at     timestamptz not null default now()
);

create table app.operator_sessions (
  id           text primary key,
  operator_id  bigint not null references app.operator_users(id),
  expires_at   timestamptz not null,        -- 15 min idle, 8h absolute
  last_seen_at timestamptz not null default now(),
  created_at   timestamptz not null default now()
);

create table app.export_jobs (
  id           bigint generated always as identity primary key,
  workspace_id bigint not null references app.workspaces(id),
  kind         text not null check (kind in ('workspace','account')),
  state        text not null default 'queued'
               check (state in ('queued','building','ready','failed','expired')),
  storage_key  text,
  requested_by bigint not null references app.users(id),
  created_at   timestamptz not null default now(),
  ready_at     timestamptz,
  expires_at   timestamptz                  -- presigned link lifetime (24h)
);

create table app.deletion_requests (
  id           bigint generated always as identity primary key,
  kind         text not null check (kind in ('user','workspace')),
  ref_id       bigint not null,             -- user id or workspace id
  requested_by bigint not null,
  state        text not null default 'scheduled'
               check (state in ('scheduled','revoking','purging','done','failed')),
  steps        jsonb not null,              -- [{step, state, evidence}] idempotent ledger
  created_at   timestamptz not null default now(),
  completed_at timestamptz
);

create table app.deletion_proofs (          -- the only retained record, non-identifying
  request_id  bigint primary key references app.deletion_requests(id),
  proof       jsonb not null,               -- {counts, sha256(email), dates, completion time}
  created_at  timestamptz not null default now()
);

create table app.idempotency_keys (         -- generic external side-effect fence
  key         text primary key,             -- uuid v4 or derived (creation_id style)
  workspace_id bigint,
  scope       text not null,                -- 'stripe_checkout' | 'ig_publish' | ...
  state       text not null default 'open'
              check (state in ('open','locked','done','failed')),
  result      jsonb,
  created_at  timestamptz not null default now(),
  expires_at  timestamptz not null          -- 30 days; only open rows are queried
);
create index on app.idempotency_keys (workspace_id, scope);

### 3.6 Transition guards (DB as final authority)

Every state column is mutated only through a BEFORE UPDATE trigger that validates the
edge against the same transition table defined in `packages/domain`. The TS table is
exported as a JSON snapshot; a CI test diffs it against the SQL function source so the
two can never drift silently.

```sql
create function app.guard_queue_item_transition() returns trigger as $$
begin
  if new.state <> old.state then
    if not (new.state = 'ready'     and old.state in ('preparing','failed','needs_review')
         or new.state = 'publishing' and old.state = 'ready'
         or new.state = 'published'  and old.state = 'publishing'
         or new.state = 'needs_review' and old.state = 'publishing'
         or new.state = 'failed'     and old.state in ('publishing','needs_review')
         or new.state = 'hidden'     and old.state in ('ready','failed','needs_review')
         or new.state = 'canceled'   and old.state = 'publishing'   -- cancel while safe
         or new.state = 'removed'    and old.state <> 'published') then
      raise exception 'illegal queue_item transition % -> %', old.state, new.state;
    end if;
  end if;
  return new;
end $$ language plpgsql;
create trigger qi_transition before update of state on app.queue_items
  for each row execute function app.guard_queue_item_transition();

create function app.guard_attempt_transition() returns trigger as $$
begin
  if new.state <> old.state then
    -- ambiguous states may ONLY move to reconciled states; never back to 'claimed'
    if old.state = 'unknown' and new.state not in ('reconciling','resolved_published',
                                                   'resolved_failed') then
      raise exception 'ambiguous attempt may only be reconciled';
    end if;
    if old.state = 'reconciling' and new.state not in ('resolved_published','resolved_failed') then
      raise exception 'reconciling attempt must resolve';
    end if;
    if new.state = 'claimed' and old.state <> 'claimed' then
      raise exception 'attempts cannot be re-claimed';
    end if;
  end if;
  return new;
end $$ language plpgsql;
create trigger pa_transition before update of state on app.publish_attempts
  for each row execute function app.guard_attempt_transition();
```

### 3.7 Semantics the schema encodes (how the brief's behaviors are realized)

- **Queue order vs schedule:** `queue_items.position` (unique per account, deferrable —
  reorders are one transaction: renumber + swap) decides *what* is next.
  `schedule_occurrences.slot_utc` decides *when*. The planner claims a slot by setting
  `claimed_by_item_id` for the front-of-queue `ready` item; `uq_slot_claim` makes a
  double-claim impossible.
- **Freezing:** `queue_items.frozen` is written once at queue time and treated as
  immutable by the API (no update path). `publish_attempts.frozen_copy` re-freezes it
  at claim, so even a bug elsewhere cannot alter what is sent. `cleanup_runs` freezes
  rule + selection + `selection_hash` at preview; execution only touches rows whose
  hash still matches (§5.11).
- **Idempotency at the boundary:** `publish_attempts.creation_id` is unique and is sent
  to Instagram as the API's own `creation_id` parameter — Meta deduplicates by it on
  their side; our side deduplicates by `uq_active_attempt`. Two fences, two systems.
- **Daily usage survives restarts:** `usage_daily` is incremented in the same
  transaction that transitions `ready→publishing`, under the per-account advisory lock.
  No cache is involved; a restart cannot forget a publish (§5.6).
- **DST/timezone:** occurrences are generated by expanding each rule against its IANA
  zone for the next 14 days, one wall-clock instant per local date. Spring-forward
  gaps produce no occurrence; fall-back duplicated wall times collapse to one UTC
  instant (the first), so duplicated local times cannot double-post. Generation bump +
  unclaimed-row delete makes rule edits take effect for exactly the not-yet-claimed
  future.
- **Billing replay:** `stripe_events.id` is the idempotency key. Subscription state is
  a projection: each event handler is idempotent by event id, events are applied in
  `created_ts` order, `last_processed_stripe_ts` is the watermark, and late/duplicate
  events either re-apply harmlessly or are skipped. Entitlements derived from the
  subscription are mirrored into `app.entitlements` rows (source `plan`) so every
  capability check is a single indexed query.
- **Deletion:** `deletion_requests.steps` is an idempotent ledger; the purge worker
  resumes from the first incomplete step, so an interrupted deletion continues, never
  re-sends side effects (billing cancel via Stripe is keyed by request id), and ends by
  writing only `deletion_proofs`. Workspace rows are deleted with FK cascades only
  after R2 objects are purged (R2 first, DB second — nothing in the DB can reference
  a missing object).
- **RLS hygiene:** a CI job runs `SELECT * FROM pg_policies` and fails the build if any
  table in the tenant list has no policy, and a static lint (pg-query-parser based)
  fails any application SQL touching a tenant table without a `workspace_id` filter.
  The `app_svc` role bypasses RLS but is used only by backup and restore tooling.

## 4. Invariant enforcement map

Each row names the concrete mechanism (not an assurance) and the specific test that
proves it. Test names refer to suites in §7: `ci:` runs on every change, `nightly:`
runs in the full suite, `drill:` is part of the rehearsed drills.

| # | Invariant | Mechanism | Proving test |
|---|---|---|---|
| P1a | Correct workspace + account | RLS `workspace_id = app.ws()` on every tenant table + app-layer WHERE clause lint; publish worker resolves the account id strictly inside the item's workspace | `ci: tenant_scrub` — workspace A requests workspace B's queue item id → 404; `ci: rls_matrix` — every table × {select,insert,update,delete} × cross-tenant id fails |
| P1b | Reviewed media/caption is what publishes | `queue_items.frozen` written once at queue time (no update API); `publish_attempts.frozen_copy` snapshotted at claim; attempt sends only from `frozen_copy` | `ci: frozen_immutable` — caption edited after queueing; published payload still contains the queued caption |
| P1c | Never twice | `uq_active_attempt` (one non-final attempt per item) + `creation_id` unique + Meta-side `creation_id` dedupe | `nightly: race_publish` — 2 workers claim same item; exactly one attempt survives, other gets 23P01; `drill: creation_id_replay` — same creation_id submitted twice to mock-ig → one container |
| P2 | Queue tells the truth | Single state machine on `queue_items` + append-only `item_events`; UI renders exclusively from state/events; no derived "optimistic" status | `ci: unknown_visible` — attempt goes `unknown` → UI shows "needs review", item is not published anywhere; `nightly: event_trail` — every transition has an `item_events` row with actor + reason |
| P3 | Uncertainty never repeats | Guard trigger: `unknown` → only `reconciling`/resolved states; no path back to `claimed`; reconcile poll ≤ 20 min then `needs_review`; republish only after operator/`resolved_failed` | `ci: no_auto_retry` — mock-ig accepts then times out; assert exactly 1 send request over 30 min and state ends `needs_review` |
| P4 | Hold, never destroy | No FK cascade from account/workspace/suspend paths; `publish_state='held'` + `hold_reason`; `removed` is the only destructive item op and requires explicit user intent + audit row | `ci: hold_semantics` — disconnect, suspend, over-plan, quota, reauth: each sets hold with queue intact; reauthorize → resume from same queue |
| P5 | Private access stays private | No password column exists anywhere; IG tokens envelope-encrypted (AES-256-GCM, data key per row); log redaction filter; browser sessions confined to acquisition VM | `ci: no_secret_surface` — grep of schema/API surface for password fields fails; `ci: redaction` — fixtures with fake tokens/sessions logged at all levels → output contains no plaintext |
| R1 | Tenant isolation on every action incl. background | RLS + per-worker `app.workspace_id` GUC from job payload + media via presigned, per-object URLs signed for one workspace context; acquisition service refuses non-entitled workspace ids | `nightly: cross_tenant_matrix` — media link, SSE stream, notification fetch, analytics query, operator item lookup: cross-tenant all fail |
| R2 | Repeat-safe publish/charge/archive/delete/invite/access | Idempotency keys (`app.idempotency_keys`), unique constraints (`stripe_events.id`, `invites.token_hash`, `publish_attempts.creation_id`), transition guards | `ci: double_click` — same "publish now" POST twice → one attempt; `ci: stripe_replay` — same webhook twice → one projection; `ci: invite_reuse` — consumed invite cannot be used again |
| R3 | One active publication per item | `uq_active_attempt` partial unique index + `pg_advisory_xact_lock('publish', account_id)` serializing per-account sends | `nightly: concurrent_workers` — 4 workers, 1 item → 1 send (covered by P1c, restated for the hard rule) |
| R4 | One cleanup per account at the boundary | `uq_active_cleanup` partial unique index + advisory lock `pg_advisory_xact_lock('cleanup', account_id)` held per item | `ci: cleanup_serial` — two manual runs started concurrently → second is rejected before preview |
| R5 | Known failures retry; ambiguous don't | Error taxonomy: pre-send failures (DNS/conn refused/4xx validation) → retry ×3; post-send ambiguity → `unknown` state, reconcile only | `ci: retry_taxonomy` — mock-ig: conn-refused → 3 retries then `failed`; 500-after-accept → 0 retries, `unknown` |
| R6 | Frozen approval for every action | `frozen` (queue), `frozen_copy` (attempt), `frozen_rule` + `selection_hash` (cleanup), Stripe Checkout `metadata` (billing), `rights_acceptance_id` in frozen | `ci: cleanup_freeze` — rule edited after preview → run executes old rule; `ci: stripe_metadata` — checkout session metadata equals the plan/seat snapshot at start |
| R7 | Held, not destroyed | Hold reasons enumerable in schema; no code path deletes `queue_items`/`receipts` except explicit deletion requests | `ci: hold_matrix` — all 6 hold reasons → rows retained, counts identical before/after |
| R8 | Daily usage survives restart/cache loss | `usage_daily` counter in DB, incremented in the same tx as `ready→publishing` under the account lock | `nightly: crash_counter` — SIGKILL the publish worker between counter update and send; restart → counter still counts the attempt (usage is by claim, not by success) |
| R9 | No duplicate source/upload into same queue | `source_posts.unique(source_id, external_post_id)` + `queue_items.dedupe_in_queue` partial unique on `dedupe_key` + autofill claims with `SELECT ... FOR UPDATE SKIP LOCKED` | `nightly: double_collect` — two collection jobs for the same source run concurrently → each post appears once in `source_posts` and once max in `queue_items` |
| R10 | Restricted capabilities unreachable without entitlement | Single `app.entitlements` table; capability check middleware in web (404, not 403, for unentitled routes) and in acquisition service; cleanup/sourcing worker refuses jobs without entitlement rows | `ci: hidden_means_missing` — unentitled workspace calls sourcing/cleanup endpoints → 404; direct graphile job injection without entitlement → job rejects |
| R11 | Media private by default | R2 buckets private; presigned GET/PUT URLs 10-min expiry; no public URL ever persisted (schema stores keys only) | `ci: signed_url_ttl` — fetch after 11 min → 403; `ci: cross_tenant_media` — workspace B's URL for workspace A's key → 403 |
| R12 | Grants/sessions never leave the server | No API route reads `access_token_enc`; browser sessions live only in acquisition VM memory/encrypted store; `evidence`/`receipts`/logs redaction filter strips header names | `ci: receipt_scrub` — receipts for browser-assisted items contain no cookie/token material; `ci: log_scrub` — grep all captured logs |
| R13 | Rights acceptance attributable + versioned | `rights_acceptances(user_id, version, accepted_at)` FK from every `queue_items.frozen` | `ci: rights_versioned` — item queued under rights v3 remains attributable to v3 after v4 rolls out |
| R14 | Repeat/out-of-order external events safe | `stripe_events` PK + created_ts-ordered replay + watermark; `deletion_requests.steps` idempotent ledger; Meta callbacks keyed by `creation_id` | `nightly: stripe_out_of_order` — deliver events [B then A] → projection identical to [A then B]; `ci: deletion_resume` — interrupt purge at step 3 → resume finishes, no double side effects |
| R15 | Every final claim has evidence | `receipts.evidence` (digests + status history), `cleanup_items.evidence` (redacted), `deletion_proofs`, hash-chained `audit_log` with nightly chain verification | `nightly: evidence_complete` — for every `published`/`done`/`charged`/`deleted` row, the corresponding evidence row exists and validates; `nightly: audit_chain` — recompute chain hashes, zero breaks |

Mechanism summary: Postgres uniqueness (partial indexes, deferrable constraints), advisory
locks, transition-guard triggers, RLS, one-way state machines, frozen snapshots, an
external idempotency token (`creation_id`), a replay-by-created_ts billing projection,
an idempotent step ledger for deletion, envelope encryption, presigned URLs, and an
append-only hash-chained audit log. No row above relies on "the code is careful".

## 5. Failure-mode walkthroughs

Every walkthrough ends with the evidence an operator can inspect (admin work-item
view, §9.12; never a developer console).

### 5.1 Crash before publish
1. Planner claims slot 18:00, enqueues `publish.item` for queue item #481.
2. Worker starts job, opens tx: locks item row, `ready→publishing`, bumps `usage_daily`,
   creates attempt A1 (`claimed`), commits. Job is now in the work queue with the DB
   already consistent.
3. Worker process is SIGKILLed **before** any external request. graphile marks the job
   abandoned on its 30-min lease expiry and re-runs it.
4. Re-run sees attempt A1 in `claimed` (not final) with no `sent_at`: it owns the
   attempt (lease take via `leased_until`), moves it `sending`, and sends. If the crash
   happens during step 4's send: A1 has `sent_at` unset → this was a **pre-send**
   failure → retry path (§5.3 rules). No duplicate: `uq_active_attempt` and the
   advisory lock admit exactly one actor.
**Evidence:** `item_events` for #481 (claimed → sending → …), `publish_attempts` A1 row
with timestamps, audit row for the claim, `usage_daily` for the local day.

### 5.2 Crash after the publish may have been accepted
1. Worker sends the media container POST; Meta's TCP layer accepts and the connection
   drops before a response arrives (30 s client timeout).
2. Worker cannot know the outcome → attempt A1: `sending → unknown`, `error_class =
   unknown_outcome`, evidence records the full request digest (uri, creation_id,
   content hash, timestamps). Item: `publishing → needs_review`. NOTIFY → user
   notification "outcome uncertain, we are checking". **No automatic retry** — the
   transition guard forbids `unknown → claimed`.
3. `publish.reconcile` picks it up within 60 s: GET container status by container id
   (unknown at first, container may be `IN_PROGRESS`) every 60 s, up to 20 min.
4a. Container completes → GET media id → `unknown → reconciling → resolved_published`;
    `receipts` row written; item `publishing → published`; NOTIFY → "published".
4b. Still unknown after 20 min → remains `needs_review`; escalation notification to
    operator; operator inspects `evidence` and, only on positive proof the publish did
    not happen (e.g., Meta's response shows no container ever created), sets
    `resolved_failed` → item returns to `ready`, retry allowed.
**Evidence:** attempt row with full state history + evidence digest, `item_events`
trail, reconcile job log events, audit rows for operator resolution.

### 5.3 Duplicate publish work
1. Two publish jobs for item #482 exist (double "publish now" click raced past
   dedupe — both API calls got the same idempotency key).
2. Worker X takes the advisory lock, transitions `ready→publishing`, creates A1.
3. Worker Y's tx fails on `uq_active_attempt` (item already has a non-final attempt)
   and the item's state guard (`ready→publishing` again is illegal). Y's API response
   returns the existing attempt's status (idempotent response), the second button click
   produces no second job.
4. Even if Y somehow got past the DB (it can't), the identical `creation_id` is
   deduplicated by Meta. Belt, suspenders, and a second belt.
**Evidence:** exactly one attempt row; `audit_log` shows Y's rejected claim; mock-ig
test replays the same creation_id and asserts one container (§4 P1c).

### 5.4 Media preparation failure
1. User uploads a video with a corrupt moov atom. `media_assets` row exists
   (`preparing`), original is safely in R2.
2. `media.prepare` fails validation/transcode → retry ×2 (5 s / 30 s backoff) → state
   `failed`, `fail_reason = "container metadata corrupt; re-export and re-upload"`,
   stored on the row. NOTIFY → uploader sees a specific, actionable reason even if
   they left the page.
3. User re-uploads a fixed file: it is a **new** `media_assets` row (new sha256), the
   queue item keeps pointing at the old asset until the user attaches the new one —
   no phantom duplication, no zombie queue item. If the queue item referenced the
   failed asset, it shows `needs attention`, not silently `ready`.
**Evidence:** media asset row state + reason; item event showing the user's re-attach.

### 5.5 Account revocation mid-flight
1. Meta revokes the token at 17:59; slot at 18:00.
2. Health check (hourly) or the publish itself sees OAuth error →
   `instagram_accounts.state = 'revoked'`, `publish_state = 'held'`, `hold_reason =
   'reauth_needed'`; attempt ends `failed`/`rejected` with `error_class =
   perms_exhausted`; **queue is untouched**.
3. User gets a critical notification with the recovery path (reconnect via Meta
   login). While revoked, the planner skips slots for the account (held-state check in
   the claim query).
4. User reconnects; ownership verified by `ig_user_id`; the **same**
   `instagram_accounts` row is reused (partial unique index allows it after
   `disconnected`), so queue, schedule, and history are preserved; `publish_state →
   active`; planner resumes at the next slot.
**Evidence:** account state history, hold_reason timeline, notification record, queue
count before/after identical.

### 5.6 Quota exhaustion
1. Account reaches its `daily_allowance` (4) at 16:00; next slot 17:00.
2. Planner's claim tx: `usage_daily.publishes = 4`, CHECK/application guard → slot not
   claimed; item stays `ready`; UI shows "next eligible tomorrow" with the account
   usage bar; nothing fails, nothing is lost.
3. Also enforced against Meta's own 25/24h limit: even if the customer set
   `daily_allowance = 25`, a Meta `4` error (`limit reached`) ends the attempt with
   `error_class = account_limit`; the item returns to `ready` (pre-send, known
   failure → retryable) and the account enters `hold_reason = 'over_quota'` for the
   local day's remainder, auto-cleared by the planner at midnight account-local.
4. Counter lives in Postgres only — a restart at 16:05 cannot forget publishes.
**Evidence:** `usage_daily` rows, hold transitions with timestamps, attempt with
`account_limit` classification.

### 5.7 Schedule edits near a slot
1. It is 17:58; a rule says 18:00; the user edits the rule to 18:30 and saves at
   17:58:30.
2. The planner tick at 17:59 regenerates occurrences (generation +1): the unclaimed
   18:00 occurrence is deleted, an 18:30 one appears; the UI preview shows the same.
3. Worst-case race — the planner's claim tx for 18:00 commits at 17:58:20, before the
   edit: the 18:00 slot is *claimed* (row exists, `claimed_by_item_id` set). The edit
   deletes only **unclaimed** future rows; the 18:00 publish proceeds exactly once.
   The 18:30 occurrence is also generated; nothing double-posts because `uq_slot_claim`
   allows one claim per slot and one item publishes per slot.
4. Timezone edit (e.g., Europe/Berlin → America/New_York): same regen path; all future
   wall times re-map to new UTC instants; claimed ones stay.
**Evidence:** occurrence table before/after (generation column), claim rows, and the
preview API output; a `nightly:` DST test expands a fixed rule over the 2025–2026
spring-forward/fall-back weekends and asserts exactly one occurrence per intended
local time.

### 5.8 Duplicate source collection
1. Two `source.check` jobs for the same source overlap (retry of a slow run).
2. Both upsert candidates into `source_posts` with
   `ON CONFLICT (source_id, external_post_id) DO NOTHING` → each external post exists
   once.
3. Autofill pulls from `backlog_items` with `SELECT … FOR UPDATE SKIP LOCKED` and
   transitions `held → queued`; the second job finds no eligible rows.
4. When queued, `queue_items.dedupe_key = source_post_id` + the partial unique
   `dedupe_in_queue` makes a repeat import impossible even if an earlier import's item
   still exists (including `removed` items, whose dedupe key stays reserved).
**Evidence:** distinct counts in `source_posts` vs API pages fetched; `dedupe_key`
values; nightly double-collect test (§4 R9).

### 5.9 Browser hang during cleanup
1. Cleanup item C7 (feed photo, archive action) is executing in the acquisition pool;
   the browser tab hangs mid-navigation (3 min page timeout will fire).
2. `cleanup_items` row for C7 is `executing`; the run holds the account's cleanup
   advisory lock.
3. Browser step times out → the worker marks C7 `reconciling`; the run transitions
   `running → paused_reconcile`; **nothing is retried automatically** and no later
   cleanup for this account can start (`uq_active_cleanup`).
4. Reconciliation: operator (or the run's owner via UI) inspects C7's evidence —
   a read-only status query to IG (API, no browser) determines whether the archive
   crossed the boundary. Positive proof of no side effect → C7 `failed` (retryable
   item, new attempt) or `skipped`; ambiguous → stays `reconciling` until resolved.
   Later scheduled runs for the account remain ordered behind this run (they check
   for an active run before preview).
**Evidence:** run + item state history with timestamps, the read-only status query
result in `evidence`, audit rows for the resolution decision.

### 5.10 Changed cleanup selection between confirm and run
1. User previews run: 12 items selected; `selection_hash = H(12 items)`; user confirms
   (recorded `confirmed_by`, `confirmed_at`).
2. Between confirmation and start, analytics refresh moves 2 items above threshold and
   the user protects 1 more.
3. At `confirmed → running`, the worker recomputes the selection from the frozen rule
   and compares hashes: mismatch → run `invalidated`, user is shown the new preview
   and must re-confirm. Selection changes are the default hazard; re-confirmation is
   the default behavior, not a corner case.
4. After start, each item is re-checked individually before its own execution (fresh
   protection check + metrics age ≤ `analytics_max_age_hours`); stale ones are
   `skipped` with reason, never silently acted on.
**Evidence:** `selection_hash` before/after, run state history, per-item `skipped`
reasons, audit rows for both confirmations.

### 5.11 Repeated or out-of-order billing events
1. Stripe delivers `invoice.paid` twice (retry) and `customer.subscription.updated`
   arriving *before* the `checkout.session.completed` it logically follows.
2. Each webhook: signature verified → `stripe_events` INSERT with id PK (duplicate →
   already processed, 200 to Stripe immediately, stops the retry loop).
3. The projection applies events ordered by `created_ts`; `last_processed_stripe_ts`
   watermark prevents stale events from mutating a newer state (e.g., a late
   "incomplete" cannot overwrite "active").
4. Entitlement mirror updates exactly once per effective change (diff on apply).
**Evidence:** `stripe_events` rows with created/processed timestamps, subscription
state history, entitlements rows with `granted_by = 'stripe'`.

### 5.12 Storage failure
1. R2 returns 5xx during upload: client chunk retries (3×/chunk); on persistent
   failure the UI says the upload is paused — **no** `media_assets` row exists yet
   (row is inserted only after all chunks are complete), so no zombie rows.
2. R2 down during media prep: `media.prepare` retries ×2 with backoff, then stays
   `preparing` (not `failed` — nothing is wrong with the input), re-enqueued by a
   sweeper every 10 min until R2 returns; alarm fires at >5% failure/hour.
3. R2 down during publish: pre-send failure → standard retry path; slot may be missed
   — item stays `ready` for the next slot. Never a silent loss.
4. Local disk fills on the VPS (transcode temp): cgroup limit + disk alarm at 80%;
   workers shed oldest temp files; transcode fails retryably.
**Evidence:** asset state timeline, job retry counts, R2 5xx metric dashboard, alarm
history.

### 5.13 Deletion interrupted halfway
1. Workspace deletion requested: `deletion_requests` row with step ledger —
   ① revoke Stripe sub (keyed by request id), ② mark workspace suspended (holds all
   activity immediately), ③ revoke IG tokens + mark accounts disconnected, ④ purge R2
   prefixes (originals/prepared/exports), ⑤ delete DB rows via FK cascade, ⑥ write
   `deletion_proofs`.
2. Crash during ④. On resume the worker reads the ledger, sees steps ①–③ done, ④
   partial (R2 list prefixes are idempotent by design — listing remaining keys and
   deleting them again is safe), completes ④–⑥.
3. No step re-sends an external side effect: Stripe cancellation is once (idempotency
   key), IG token revocation is a one-way call whose retry is harmless (already
   revoked → same result).
4. `deletion_proofs` records counts, hashed identifiers, and completion time; the
   customer keeps the request reference; nothing identifying survives.
**Evidence:** step ledger with per-step state + timestamps, `deletion_proofs` row,
Stripe webhook history showing one cancellation.

## 6. AI strategy

**Decision: no AI anywhere in v1.** Not for captions, not for scheduling, not for
support triage, not for moderation.

Why deterministic behavior is sufficient:

- The five product promises are determinism problems. Every one of them is solved by
  state machines, uniqueness constraints, frozen snapshots, and evidence — none of
  which an LLM improves and all of which it could undermine if it sat on a critical
  path (a generated caption is another thing to review; a generated schedule is
  another thing to trust).
- The highest-risk parts of this product (restricted sourcing, managed cleanup) carry
  legal and platform-policy exposure. A provenance-clean, deterministic pipeline is a
  much simpler posture to defend than an AI-assisted one.
- Cost at scale: even cheap caption suggestions (≈ $0.003/item at 2,000 items/day)
  is ≈ $180/month and buys nothing the brief requires.
- The operator is non-technical; every AI surface becomes a support surface.

Conditions that would justify revisiting post-v1 (evaluated, not promised): caption
*drafting suggestions* (never autonomous publishing) behind an entitlement, human
confirm before queueing, model-pinned, logged prompt/response pairs, per-workspace
opt-in — and only after the deterministic core has shipped through Phase 5 and real
usage data exists to measure whether users even want it.

## 7. Testing and release confidence

**On every change (CI, must stay < 25 min):**
- `pnpm -r typecheck` + eslint + dependency audit.
- Unit tests (vitest): transition tables, occurrence expansion (incl. DST fixtures for
  Europe/Berlin, America/New_York, Australia/Lord_Howe), frozen-payload builders,
  redaction filter, idempotency-key logic.
- Integration tests against Testcontainers Postgres: migrations up/down from scratch
  and from a seeded fixture; the full invariant matrix of §4 (tenant scrub, RLS
  matrix, transition guards, partial unique indexes); billing projection replay.
- RLS policy lint + workspace-filter SQL lint (fail the build).
- Playwright smoke of the three most dangerous UI flows (queue item, confirm cleanup,
  checkout) on the staging stack.

**Requires real supporting services (staging, per-change for affected modules):**
- Stripe test mode: webhook signature tests, checkout → portal round-trip, card-failure
  drills with test cards.
- Meta Graph API sandbox: token exchange, container lifecycle, creation_id dedupe.
- Resend sandbox: magic link delivery + expiry.

**Nightly:**
- Full integration suite + `fast-check` property tests over the queue/attempt FSM
  (500 runs: any sequence of legal transitions must never produce two active attempts).
- Chaos drills against `mock-ig` (a local fake of the Graph API with programmable
  latency, timeouts, and injected 500s): SIGKILL publish worker at each of 6
  instants of the attempt lifecycle (before claim, after claim, mid-send, after
  accept, mid-poll, mid-resolve); SIGKILL during cleanup item execution; kill
  PostgreSQL mid-transaction; duplicate job injection; assert final state is exactly
  one of the allowed outcomes with intact evidence.
- Backup verification: WAL-G restore to a scratch VM from last night's backup; verify
  row counts and 100 sampled sha256 checksums of media metadata.
- Audit chain verification: recompute all `hash` links, expect zero breaks.

**Weekly against safe real Instagram accounts (business test accounts we own):**
- Connect → upload → prepare → queue → publish image → publish Reel → receipt →
  insights fetch → library view, end to end.
- Archive one feed photo and move one Reel to Recently Deleted via API; verify the
  UI states and that we never claim auto-restore.
- Token revocation drill: revoke from Meta side → held state, queue intact, recovery
  path works.
- creation_id replay: publish the same creation_id twice → one media.
- Manual refresh rate-limit behavior.

**Drills rehearsed before launch (each has a runbook and an operator-facing checklist):**
crash-before-publish, crash-after-accept, race, quota exhaustion, timezone/DST edits,
restore-from-backup (full production-shaped restore to scratch), ambiguous-outcome
reconciliation, deletion interruption (§5.13), storage outage.

**Release confidence gate:** a release is shippable when CI + nightly + the weekly
real-IG run are green *and* the two most recent restore drills succeeded. The
operator never needs a developer console for any drill in §5.

## 8. Delivery phases

Each phase ships a vertical slice and exits only on objective evidence.

**Phase 0 — Foundation (2 people-weeks equivalent).** Monorepo, CI, Docker Compose,
Caddy, Postgres + RLS scaffold, graphile, WAL-G to R2, mock-ig server, Loki/Grafana/
Sentry, redaction filter.
*Exit:* seeded two-workspace fixture passes the RLS matrix; a production-shaped backup
restores to a scratch VM and passes checksum verification; kill-9 drill on a dummy job
shows retry semantics.

**Phase 1 — Identity, waitlist, invites, workspaces, roles, onboarding.** Magic links,
sessions, waitlist page + operator review UI, invites (expiry, single-use, revoke),
roles owner/editor/viewer, onboarding flow, notifications scaffold, SSE plumbing.
*Exit:* invite lifecycle e2e (issue → accept → join → revoke); cross-tenant isolation
tests green; a11y audit of these pages (keyboard-only walkthrough, empty/loading/
error states).

**Phase 2 — Instagram connect.** Account requests (operator approve/decline with
customer-visible reason), Meta login → picker, cross-workspace uniqueness, health
checks, disconnect/reconnect preserving queue, revocation → held path.
*Exit:* connect/disconnect/revoke drills against mock-ig **and** one real test
account; reconnect restores the same account row (queue/history intact).

**Phase 3 — Upload and preparation.** Chunked upload (resumable), rights acceptance
(versioned), ffmpeg/sharp pipeline, previews, failure reasons, retry-without-duplicate.
*Exit:* 2 GB upload survives network interruption and resumes; corrupt file rejected
with reason; retry creates no second queue item; transcoding a 1 h backlog batch
completes within the throughput budget of §1.18.

**Phase 4 — Queue and schedule.** Drag reorder, hide/restore/remove, bulk actions,
rules (fixed + interval) with preview, occurrence generation + planner, pause/resume,
holds, daily allowance + cooldown enforcement, "publish now".
*Exit:* DST test matrix green; edit-rule-near-slot walkthrough (§5.7) passes
automatically; pause/resume preserves queue exactly.

**Phase 5 — Publish, receipts, reconciliation, first analytics. ← earliest complete
end-to-end.** Attempt FSM + advisory locks + creation_id idempotency, retry taxonomy,
reconcile lane, receipts, needs-review flow, insights snapshot fetch + library view,
uncertain-outcome notifications.
*Exit:* **the full chain connect → upload → prepare → queue → publish → receipt →
analytics runs against a safe real Instagram account, and all §5 publish-related
walkthroughs (5.1, 5.2, 5.3, 5.6) pass in drills.** Every earlier phase existed to
make this phase a small, provable step: 0 (prove restore before data exists), 1
(ownership to attach accounts to), 2 (the account), 3 (the media), 4 (the schedule
that decides when), and only then the irreversible boundary.

**Phase 6 — Analytics completion.** Rollups, trends, best/worst, source-level
performance, honest staleness labels, manual refresh with limits.
*Exit:* rollup rebuild from snapshots is idempotent (same inputs → same outputs);
comparison UI labels stale/missing measures.

**Phase 7 — Plans, billing, entitlements.** Stripe Checkout + Portal + webhooks,
entitlement mirror, plan caps (accounts, seats, storage), downgrade/payment-failure
holds, billing pages.
*Exit:* §5.11 out-of-order drill green; card-failure test holds activity without
destroying data; seat/account caps enforced server-side with 404s beyond limits.

**Phase 8 — Feedback, settings, export, deletion, legal.** Feedback with page context,
workspace/personal settings, leave workspace, export job, deletion flow, public
legal/privacy pages.
*Exit:* §5.13 interrupted-deletion drill green; export of a full workspace zip
verifies; legal pages live.

**Phase 9 — Operator console.** Waitlist/invites/requests/users/workspaces/
suspensions/entitlements/feedback management, health screen, work-item inspector
(§9.12), safe actions with attribution, WebAuthn+TOTP realm.
*Exit:* every §5 walkthrough is resolvable through the console by the non-technical
operator; every privileged action produces an attributable audit row.

**Phase 10 — Restricted sourcing (entitled).** Sources, verification, filters,
backlog, autofill, one-time Reels sample, acquisition VM + browser pool, legal/abuse
controls (§9.10).
*Exit:* entitled workspace only (unentitled → 404); duplicate-collection drill (§5.8)
green; removal stops acquisition without touching accepted work; sourcing failure
states visible.

**Phase 11 — Managed cleanup (entitled).** Protection, rules, preview + hash-verified
confirmation, per-item execution with stop-between, scheduled runs, reconciliation,
cleanup history.
*Exit:* §5.9/§5.10 drills green on mock-ig **and** one real test account; no two
cleanup boundaries ever overlap; history shows frozen rule + redacted evidence.

Invite-only beta opens after Phase 9; Phases 10–11 ship as entitled features during
the beta, invisible to everyone else.

## 9. Security and privacy

**Identity.** Magic links (10-min TTL, single-use, 5/email/h, 3 attempts then
invalidate). Sessions: 256-bit opaque ids, 30-day sliding expiry, revoke on role
change, password change (n/a), or suspension. Suspended users keep data but cannot
issue new sessions.

**Authorization.** Two realms with no shared session type:
- *Customer realm:* workspace-scoped roles owner (billing + destructive admin),
  editor (day-to-day publishing), viewer (read-only). Capability checks are
  server-side; the UI merely hides what the API refuses.
- *Operator realm:* separate login (WebAuthn + TOTP, IP allowlist, 15-min idle),
  separate sessions table, never issuable from the customer API, and a
  `operator:<id>` actor string that appears in every audit row it produces.
Entitlement checks read exactly one table (`app.entitlements`) via one middleware;
unentitled capability routes return 404 (indistinguishable from nonexistent).

**Workspace isolation.** RLS + GUC per request/job + SQL lint + the §4 test matrix.
Media links are presigned per object and validate the requesting workspace context;
SSE channels are per-workspace and join only after session check; notifications and
analytics queries are workspace-filtered at the DB.

**Secrets.** No Instagram password ever exists in the system (no column, no API
field, no log pattern). IG tokens: envelope encryption — per-row AES-256-GCM data
key, wrapped by a master key present only in worker/web environments; decryption
happens only inside the publish worker for the duration of one attempt (never in
web processes, never in queries, never in exports). Stripe keys: SOPS+age in repo
CI, env-injected at runtime; webhook signing secret rotated quarterly. Operator
credentials: WebAuthn credential store, TOTP secrets encrypted.

**Private media.** R2 buckets are private; the only egress is presigned URLs
(10 min, single object, GET for preview / PUT for chunked upload with
size+type constraints). Original uploads are retrievable by the owner only; receipts
store keys, not URLs.

**Restricted sessions.** Browser automation lives only on the acquisition VM: 2
concurrent sessions, 1 per workspace, sessions encrypted at rest with keys confined
to that host, memory-only decryption, 20-min idle kill, no route that could ship
session state to the customer browser. Logs from that VM pass the same redaction
filter plus a cookie/header-name blocklist; receipts and `cleanup_items.evidence`
have no columns that could hold session material (schema-level impossibility, §4
R12).

**Billing callbacks.** Stripe webhook: signature verification before any processing,
`stripe_events` PK idempotency, replay-by-created_ts projection, watermark. The
billing pages read only our projection (never live Stripe state), so a Stripe outage
degrades freshness, not correctness.

**Abuse controls.** Turnstile on the waitlist; invite issuance 20/workspace/week;
magic-link rate limits; per-IP login throttle (10/10 min); upload size/type
validation before presigning; per-plan storage/account caps enforced server-side;
IG account requests are operator-reviewed (never auto-approved); restricted sourcing
requires entitlement **and** a verified source before collection starts;
`removed`-state dedupe keys stay reserved so deleted imports cannot be silently
re-imported by a runaway source.

**Audit.** Append-only hash-chained `audit_log` (chain verified nightly): every
publish resolution, cleanup action, entitlement change, invite issue/revoke,
suspension, operator action, and deletion step. Actors are attributable strings,
never anonymous.

**Retention.** §1.18 table. Exports expire after 24 h. Deletion proofs (minimal,
non-identifying) retained 7 years.

**Export.** Owner-only; builds a zip (originals + receipts CSV + settings) into the
`exports` bucket; presigned link, 24 h; export job itself audited.

**Deletion.** §3.7 ledger semantics + §5.13 drill: holds first, then external
revocations (billing, IG tokens), then R2 purge, then DB cascade, then proof. The
request id is given to the customer as the status reference.

**Support access.** The operator console can inspect a work item's full trail
(events, attempts, evidence) without ever seeing decrypted tokens. "View as
workspace" requires a customer-consented, time-boxed (30 min) support grant that is
itself audited and revoked; there is no path to edit production data directly —
reconciliation and retry go through named, guarded actions (§9 operator realm).
Production DB access by the founder uses a personal role; `app_svc` is
backup/restore-only.

**Privacy posture.** Single region (EU), GDPR-first: data-export and deletion are
built features (Phase 8), public privacy/terms/copyright/security-contact/
data-deletion pages ship before any real customer onboard.

## 10. Risk register

| # | Risk | Early warning signal | Mitigation already in the plan |
|---|---|---|---|
| 1 | Meta platform enforcement: API changes, limits tighten, or accounts restricted (kills the core loop) | % of publishes ending `perms_exhausted`/`account_limit` rising; Meta changelog | Publish exclusively via official API with `creation_id`; limits modeled as holds not failures; weekly real-IG drills; feature flags to degrade gracefully (queue-only mode) |
| 2 | Restricted sourcing draws legal/policy blowback (accounts banned, legal demand) | Operator complaints; Meta enforcement notices on test accounts | Entitled-only, separated VM/IP, provenance records on every import, no public marketing of the capability, removal path that preserves accepted work, kill-switch flag |
| 3 | A publish double-fires despite the fences (reputation killer) | `creation_id` collisions at Meta; mock-ig race tests failing | Dual idempotency (DB partial unique + Meta creation_id), advisory locks, nightly chaos drills, reconciliation-only-for-unknowns |
| 4 | Silent queue death (schedule says active, nothing posts, nobody notices) | Alarm: account with active rule and 0 ready items | "Silent queue death" page-alarm (§1.15); health screen shows per-account next-slot |
| 5 | Billing breaks trust (wrong charges, phantom subscription states) | Stripe webhook delivery failures; projection drift vs Stripe dashboard | Event-sourced replay projection, signature + idempotency, downgrade-holds-not-destroys, test-mode drills monthly |
| 6 | Storage cost overrun (week-long backlogs × video) | R2 growth dashboard vs the §1.18 curve | Per-plan storage entitlements enforced server-side; dedupe by sha256 per workspace; lifecycle: prepared versions regenerable, originals single-copy |
| 7 | One-person bus factor (founder unavailable during incident) | — (single point by definition) | Automated restore drills + runbooks for every §5 walkthrough, operator console requiring no console access, backups rehearsed weekly |
| 8 | Token/session leak (workspace B sees A's content) | RLS lint failures; Sentry alerts from cross-tenant tests | RLS + GUC + lint + presigned URLs + weekly matrix tests; envelope encryption limits blast radius of any DB read |
| 9 | Browser pool compromise (session theft → IG account hijack) | Suspicious logins from new IPs on test accounts; pool egress anomalies | VM isolation, encrypted-at-rest sessions, memory-only keys, redaction, no session data in receipts/logs, pool caps, per-workspace 1 session |
| 10 | Monetization/usage mismatch (free users dominate infra cost) | Free:paid WAU ratio, R2 per-free-user cost | Invite-only gate controls inflow; storage caps on the free plan; plan caps enforced server-side so cost can't be unbounded by a bug |

## 11. Explicit tradeoffs

Every place the plan knowingly provides weaker behavior than the brief, and why that
trade is acceptable for v1:

1. **Feed-photo "archive" is implemented as move-to-Recently-Deleted.** Instagram's
   official API has no archive endpoint; true archiving would require browser
   automation with the customer's password, which the brief forbids. We apply the API
   delete (→ Recently Deleted, recoverable in the IG app) for both kinds and the UI
   explains the difference honestly. Protection is the defensive alternative. This is
   the largest conscious deviation and is called out here rather than buried.
2. **Single region, single primary DB: recovery takes 2–4 h, not seconds.** At 200
   WAU the cost of multi-AZ/HA exceeds the revenue risk; RTO is bounded by rehearsed
   WAL-G restores (weekly drills).
3. **No distributed tracing backend.** Correlation via trace_id/event_id in logs
   covers the operator console and §5 evidence needs; add Tempo only if a real
   incident shows the gap.
4. **Magic-links-only auth** — email delivery is a hard dependency; a mail outage
   blocks logins (not publishing). Acceptable because publishing continues without
   the user.
5. **Analytics is limited to what the IG API returns**; no third-party enrichment, no
   cross-platform comparison. Brief's non-goals agree.
6. **Queue reordering uses full renumbering** (not fractional keys): O(n) per
   reorder at ≤ few thousand items is milliseconds; no exotic indexes.
7. **Restricted sourcing browser pool is capped** (2 sessions, 1/workspace): refill
   is slower than it could be — deliberate isolation over throughput.
8. **No SMS/email notification fan-out by default** (in-app only, email on critical
   events) — saves deliverability work; revisit if customers ask.
9. **Cleanup selection uses snapshots ≤ 24 h old**, not live metrics at confirm time
   — bounded by `analytics_max_age_hours`, labeled in the UI.
10. **Self-hosted Postgres** (vs managed): an incident during the founder's vacation
    is the cost of saving ~$100/mo; mitigated by backups + runbooks, and it can be
    migrated later without app changes.
11. **No native apps, no other networks, no marketplace, no AI** — per brief
    non-goals, restated so nothing sneaks back in.
12. **Operator support access cannot edit data directly** — reconciliation only via
    guarded named actions; slightly slower support, much smaller blast radius.

## 12. Where this is stronger than required

1. **Dual-boundary idempotency**: DB partial-unique fence *and* Meta's own
   `creation_id` dedupe. The brief demands "never twice"; this makes it true even if
   one of the two systems misbehaves.
2. **Hash-chained append-only audit log** with nightly chain verification — evidence
   for customer disputes that survives even a compromised DB session.
3. **Envelope-encrypted IG tokens** (per-row data keys): a DB dump is useless
   without the master key.
4. **Occurrence materialization** (not just rules): "upcoming runs" preview is exact,
   and slot claims are DB-unique — DST and edit semantics are testable artifacts, not
   code paths.
5. **Rights acceptance as a versioned, attributable FK on every queued item**, not a
   checkbox string.
6. **Staged mock-ig + chaos drills before any real-account test** — failure drills
   (§5) run on every release without risking Meta accounts.
7. **Cross-workspace IG uniqueness** via partial index: "the same account must not
   belong to two workspaces" is a database fact.
8. **Autofill target-depth design** prevents queue flooding by construction (fills
   to N, stops before N+1).
9. **Weekly real-Instagram drills** including revocation and cleanup — the operator
   proves the outside world still behaves as modeled.
10. **Support-grant "view as" with customer consent** instead of free-form operator
    impersonation.

## 13. Assumptions

Collected decisions where the brief was silent:

1. Single region: EU (Hetzner FSN1, Germany); UI in English; GDPR-first because the
   operator is EU-based.
2. Currency USD; Stripe available in-region.
3. IG API documented limits hold at launch: 25 API-published posts/24h/account,
   240 Insights calls/h/token, business/creator accounts only.
4. All customer publishing goes through the official Graph API; browser automation
   is used **only** for entitled sourcing collection, never publishing.
5. "Publish now" publishes the front-of-queue item; it never jumps the queue.
6. The queue's daily allowance default is 4 (customer-set 1–25); min interval
   default 60 min.
7. Pricing: Free (1 account, 2 seats, 10 GB, no cleanup/sourcing), Pro $12/mo or
   $115/yr (3 accounts, 3 seats, 100 GB), Studio $49/mo or $470/yr (10 accounts,
   5 seats, 500 GB); cleanup and restricted sourcing are operator-granted
   entitlements, not sold publicly at launch.
8. Beta ~6 weeks invite-only; waitlist is operator-managed, no self-serve invites.
9. "Reconnect preserves relationship" is by `ig_user_id` match to a previously
   disconnected row in the same workspace.
10. Timezone is per-account (not per-rule), but rules carry their own zone field for
    future multi-zone workspaces; occurrence generation uses the account zone.
11. Cleanup "scheduled occurrences recheck current protection" — protection is
    per-media-id, so re-protecting the same IG media through another product path is
    out of scope.
12. Analytics "best/worst" uses reach/plays as the primary comparison with honest
    staleness labels; engagement-rate derivations use IG-delivered counts only.
13. Storage cap enforcement is soft (upload blocks above cap) — never deletes media.
14. Support contact is email only at launch (Resend inbox); no chat widget.
15. The founder is the sole operator at launch; operator console supports one
    operator but is schema-ready for several (audit attribution already per-actor).
16. Reels publishing via the official API (REELS container) is within Meta policy
    for business accounts; the weekly drill verifies it end to end.
17. Deletion proof retention (7 years) and audit retention (7 years) satisfy the
    brief's "minimum non-identifying evidence".
18. Queue items are per-account; a workspace with 10 accounts has 10 queues, each
    with its own schedule and allowance.
19. Hiding an item removes it from publishing consideration but keeps its position
    semantics (restore returns it to its previous relative slot).
20. Duplicate uploads are deduplicated per workspace by sha256 (storage, not
    visibility — two users can still queue the same file twice deliberately).
21. Email deliverability (Resend) is sufficient for magic links; no SMS fallback in
    v1 (tradeoff §11).
22. Node 22 + Postgres 16 + ffmpeg 7 are pinned; upgrades are scheduled, not
    opportunistic.
23. The browser pool's target IP reputation is maintained by never using it for
    publishing or high-volume scraping; 2 sessions keeps it below heuristic
    thresholds.
24. The mock-ig server is maintained as a first-class test dependency (it encodes
    Meta's contract), updated on any observed API behavior change.

---

*End of plan. The first delivery phase (Phase 0) is specified precisely enough to
start today: monorepo scaffold, Postgres+RLS, graphile, WAL-G backups, mock-ig, and
the CI invariant matrix of §4.*


