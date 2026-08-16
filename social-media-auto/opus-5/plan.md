# ToolBox Poster — Engineering Plan

Author: founding engineer. Scope: from empty repository to live invite-only product.
Region: EU (Hetzner Falkenstein, `eu-central`). Currency: EUR for infrastructure, USD for
SaaS vendors; both shown. Prices are vendor list prices at authoring time; a ±20% drift
does not change any decision below.

---

## 1. Technology decisions

### 1.1 Language and runtime

**Choice:** TypeScript on Node.js 22 LTS for every process — web, workers, scheduler,
automation. One `pnpm` workspace, one shared `packages/domain` holding the state machines,
the frozen-payload builders, and the Instagram client.

**Rejected:** Python 3.12 + FastAPI + Celery for the backend with a TypeScript frontend.
It is a genuinely better fit for the media pipeline (`PyAV`, mature `ffmpeg` bindings) and
Celery's canvas primitives are richer than anything in Node.

**Why:** one engineer maintains this. A single language means the queue-item state machine,
the frozen-caption assembler, and the Zod schemas that validate Instagram responses exist
exactly once and are imported by both the page that renders a preview and the worker that
publishes it. A two-language split duplicates every one of those in a place where the two
copies can drift, and drift in the frozen-payload builder is a §4 violation (the user
previews one caption, the worker publishes another). Playwright's first-class language is
also TypeScript, which matters for §1.11.

### 1.2 Datastore

**Choice:** PostgreSQL 17, single primary, no read replica. Every piece of durable state
lives here: identity, queue, leases, job rows, counters, analytics history, billing state,
audit log, idempotency records.

**Rejected:** PostgreSQL for relational state plus Redis for the job queue, counters, and
rate limits. This is the conventional split and it is faster.

**Why:** the hard rules in the brief are almost all "one thing crosses a boundary at most
once." Enforcing that requires the queue transition and the side-effect record to commit in
the *same* transaction. With Redis holding the queue, enqueue-after-commit and
commit-after-enqueue are both wrong: the first loses jobs on a crash between the two, the
second creates jobs for work that rolled back. Postgres-only makes `INSERT INTO
publish_attempts` and the `UPDATE queue_items SET status='publishing'` and the job insert a
single atomic act. Redis also cannot express "daily publishing use cannot be forgotten by a
restart or cache loss" without AOF `appendfsync always`, at which point its write latency
advantage is gone. The cost of the choice is that the job queue polls; §2.6 shows the poll
budget.

### 1.3 Background job runner

**Choice:** [Graphile Worker](https://worker.graphile.org) v0.16 (`graphile-worker`),
running as separate OS processes with disjoint task lists.

**Rejected:** BullMQ on Redis. Better dashboards, better rate-limiter primitives, and
`Worker` groups map cleanly onto the per-account serialization this product needs.

**Why:** three Graphile Worker features map one-to-one onto hard rules, and each is a
concrete API rather than a pattern I would have to build:

- `graphile_worker.add_job(identifier, payload, queue_name, run_at, max_attempts, job_key,
  priority, flags, job_key_mode)` is a **SQL function**, so a job is enqueued inside the
  same transaction as the state change that justifies it. This is the transactional outbox
  without an outbox table.
- `queue_name` gives strict serial execution: at most one job from a named queue runs at any
  instant, across all workers. `publish:<ig_account_id>` and `cleanup:<ig_account_id>` are
  therefore serialized by the runner itself, and a paused reconciliation job at the head of
  `cleanup:<account>` holds every later cleanup for that account behind it (brief §3.8:
  "later cleanup runs for that account remain ordered behind it").
- `job_key` with `job_key_mode='preserve_run_at'` collapses duplicate enqueues of the same
  logical work (one analytics refresh per post per window) instead of running it twice.
- `forbiddenFlags` (a function returning an array of flag strings, re-evaluated each poll)
  lets a worker refuse to dequeue jobs tagged `ws:<workspace_id>` while that workspace is
  suspended. Suspension therefore *holds* jobs in the table rather than deleting or failing
  them, which is hard rule "queue work is held, not destroyed."

A worker process only dequeues tasks whose identifier is in its own `taskList`; Graphile
Worker adds `task_id = ANY($known_task_ids)` to its dequeue query. That is the structural
reason media transcoding cannot starve publishing (§2.3).

### 1.4 Web framework and delivery

**Choice:** Next.js 15 (App Router) in Node runtime, server components for reads, server
actions disabled in favour of explicit `POST /api/*` Route Handlers. Marketing pages,
legal pages, app, and admin are one deployable with three route groups.

**Rejected:** Remix / React Router 7 on a Fastify server. Its loader/action model is a
better fit for a form-heavy operations console and it has no server-action footgun.

**Why:** the queue view, library, and analytics pages are read-heavy and benefit from
streaming server components fetching directly from Postgres inside an RLS-scoped
transaction, with no intermediate JSON API to keep in sync. Explicit Route Handlers are used
for mutations because every mutation needs an `Idempotency-Key` header, a CSRF double-submit
check, and a JSON error envelope — three things that are uniform in a hand-written handler
and easy to forget in a server action. Rejecting server actions is recorded in §13.

### 1.5 Hosting and infrastructure

**Choice:** Hetzner Cloud, single region (`fsn1`), 6 VMs provisioned by Terraform, each
running Docker Compose under `systemd`. One Hetzner load balancer (LB11) terminating TLS.
Cloudflare in front for DNS, WAF, and bot rules.

**Rejected:** AWS (ECS Fargate + RDS Multi-AZ + S3 + ALB). Managed Postgres with
point-in-time recovery removes the single largest operational burden from a one-person team,
and that is a real advantage, not a fashion one.

**Why:** cost arithmetic and blast radius. The AWS shape for this workload is roughly
`db.t4g.medium` Multi-AZ ($122/mo) + 2 Fargate web tasks (~$35/mo) + 3 worker tasks with
16 GB for ffmpeg and Chromium (~$180/mo) + ALB ($20/mo) + NAT Gateway ($35/mo + data) ≈
$390/mo before storage — above the stated budget on its own. The Hetzner footprint in §1.13
is €111/mo for strictly more CPU and RAM. The thing AWS would have bought me, PITR, I buy
instead with pgBackRest (§1.6) plus a restore drill that is an *exit criterion* of Phase 0
and runs nightly thereafter (§7.5) — a rehearsed restore is a stronger guarantee than an
un-rehearsed managed one, and the brief demands the rehearsal either way.

Kubernetes is rejected for the same footprint: it adds a control plane to operate and buys
nothing at 6 VMs that `systemd` + Compose + a Terraform `docker_container` resource does not.

### 1.6 Backups

**Choice:** pgBackRest 2.5x on the database VM. Weekly full backup, daily differential,
continuous WAL archiving (`archive_mode=on`, `archive_command = 'pgbackrest --stanza=tbp
archive-push %p'`) to Hetzner Object Storage, `repo1-retention-full=4` (28 days of PITR).
`repo2` is a second stanza pushing to Cloudflare R2 so the backup and the primary media
store do not share a vendor.

**Rejected:** nightly `pg_dump` to object storage. Simpler, restorable with one command, no
WAL plumbing.

**Why:** `pg_dump` has a recovery point objective of up to 24 hours. At 500 publishes/day a
24-hour loss destroys up to 500 receipts for posts that are *actually live on Instagram* —
irreconcilable, because the evidence that we published is gone while the effect remains.
WAL archiving gives an RPO of the `archive_timeout` setting, which I set to 60 s. Worst-case
loss is 60 s of commits.

### 1.7 Object storage for customer media

**Choice:** Cloudflare R2, one bucket `tbp-media`, all objects private, keys of the form
`w/<workspace_id>/a/<asset_id>/<variant>.<ext>`. Browser access is by presigned `GET` from
`@aws-sdk/client-s3` + `@aws-sdk/s3-request-presigner` `getSignedUrl(client,
new GetObjectCommand({...}), {expiresIn})`. Uploads are presigned `PUT` (single part under
100 MB, `CreateMultipartUpload` above).

**Rejected:** Amazon S3. Better tooling, lifecycle rules that R2 lacked historically, and
`s3:ObjectCreated` events straight into a queue.

**Why:** egress. Instagram fetches every published asset from us over the public internet,
previews are re-fetched by browsers, and the operator inspects media during support. At
300 GB/month of new media and roughly 3× read amplification (preview, Instagram fetch,
support/export), S3 egress at $0.09/GB is ~$81/mo, more than half the entire infrastructure
budget. R2 egress is $0. R2's presign implementation is S3-compatible, which is the only API
surface this product uses.

### 1.8 Media processing

**Choice:** `sharp` 0.33 (libvips) for images; `ffmpeg` 7.x invoked with `execa` and an
explicit argv array for video. No wrapper library.

**Rejected:** a hosted pipeline (Mux Video or Transloadit).

**Why:** arithmetic. Mux encoding is $0.040/minute of input. At 500 videos/day averaging
35 s, that is 500 × 0.583 min × $0.040 × 30 = $350/mo — above the entire budget for one
component. Self-hosted ffmpeg on the media VM costs the VM (€16.40/mo) and §2.3 shows the
CPU budget fits. `fluent-ffmpeg` is rejected specifically because it is unmaintained and
because building the argv by hand is what lets me assert exact flags in a unit test.

### 1.9 Authentication

**Choice:** first-party email + password. `@node-rs/argon2` `hash()` with `Argon2id`,
`m=19456 KiB, t=2, p=1` (the OWASP first-choice parameter set). Sessions are a 32-byte
random token in an `HttpOnly; Secure; SameSite=Lax; Path=/` cookie; the database stores
`sha256(token)` as the primary key of `sessions`. Platform-administrator access additionally
requires a WebAuthn credential (`@simplewebauthn/server` `verifyAuthenticationResponse`) and
is served on a separate hostname with its own cookie.

**Rejected:** Clerk or WorkOS. Both ship organizations, invitations, and MFA out of the box —
weeks of work this plan otherwise pays for.

**Why:** the workspace is not a directory concept here; it owns Instagram grants, frozen
publish payloads, entitlements, and a Stripe customer, and every authorization decision is a
join against those tables. With an external identity provider the authorization decision
still lives in my database, so I would operate both, and the tenant-isolation mechanism I
rely on most (Postgres RLS keyed on a session GUC, §9.3) needs the workspace id inside the
database transaction regardless. Clerk's B2B tier is also $25/mo + $0.02/MAU on top of a
budget where every €10 is visible. The specific thing I give up — a maintained MFA and
device-management UI for customers — is disclosed in §11.

### 1.10 Payments

**Choice:** Stripe Billing. Checkout Sessions for purchase, Customer Portal for changes and
invoices, `stripe.webhooks.constructEvent` for signature verification, Stripe Tax for EU VAT
calculation.

**Rejected:** Paddle. As merchant of record it removes VAT registration, OSS filing, and
invoice-compliance work entirely from a one-person company — a genuine operational saving.

**Why:** entitlement correctness. This product must map subscription state onto hard limits
(connected-account allowance, seats, feature entitlements) that are enforced server-side on
every request, and must survive repeated and out-of-order webhooks. Stripe's object model
exposes `subscription.items[].price`, `subscription.status`, `cancel_at_period_end`, and
`current_period_end` directly on an object I can re-fetch by id at any time, which is what
makes the out-of-order-event resolution in §5.11 work: I never trust the event body, I
re-fetch and reconcile. The VAT burden I take on instead is disclosed in §11 and priced in
§13 (Stripe Tax at 0.5% of transaction volume).

### 1.11 Instagram integration

**Choice:** the official **Instagram API with Instagram Login** (Graph API v23.0 on
`graph.instagram.com`), scopes `instagram_business_basic`,
`instagram_business_content_publish`, `instagram_business_manage_insights`. Publishing is
the documented two-step: `POST /{ig-user-id}/media` to create a container, then `POST
/{ig-user-id}/media_publish` with `creation_id`. Container readiness and post-crash
reconciliation both use `GET /{container-id}?fields=status_code`, which returns one of
`EXPIRED`, `ERROR`, `FINISHED`, `IN_PROGRESS`, `PUBLISHED`. Remaining capacity comes from
`GET /{ig-user-id}/content_publishing_limit?fields=config,quota_usage`. Insights come from
`GET /{ig-media-id}/insights?metric=...`. Long-lived tokens are refreshed with `GET
/refresh_access_token?grant_type=ig_refresh_token`.

**Rejected:** a private/unofficial Instagram client (`instagrapi`-style) driving the mobile
API with the user's password. It supports everything, including the archive and delete
operations the official API omits.

**Why:** it requires collecting Instagram passwords, which promise 5 forbids in the public
product, and it is the fastest path to mass account bans — a product-ending outcome for a
publishing tool. The capability I lose is archive/delete of published media, which the Graph
API does not offer at all; §1.12 covers that separately, behind an entitlement, precisely so
that the public product's failure surface is disjoint from it.

**The `status_code = PUBLISHED` transition is the single most important external fact in
this plan**: it is what converts an ambiguous publish timeout into a decidable question
(§5.2), and it is why reconciliation is an investigation rather than a guess.

### 1.12 Restricted automation (sourcing and managed cleanup)

**Choice:** Playwright 1.4x driving Chromium, on a dedicated VM (`automation-1`) with its
own Docker network, its own database role, and no route to the media bucket's write
credentials. Sessions are captured through an operator-supervised flow in which the customer
types their Instagram credentials into a real instagram.com page rendered inside a remote
browser streamed over the Chrome DevTools Protocol screencast (`Page.startScreencast`,
`Input.dispatchKeyEvent`); the product's servers never receive the password field's value.
What is retained afterwards is only `browserContext.storageState()` — cookies and
localStorage — encrypted at rest (§9.5). `storageState({path})` persists cookies and
localStorage and **does not** persist DOM state, service workers, or IndexedDB, so every
automation job begins by navigating from a known URL and asserting a logged-in selector
rather than assuming a resumed view.

**Rejected:** Browserbase or a similar hosted browser grid. It removes the fleet, the
Chromium memory tuning, and the proxy plumbing.

**Why:** the restricted session is the most sensitive secret in the system — it is
equivalent to the customer's Instagram login. Sending it to a third-party browser host puts
it in a vendor's memory and logs, which conflicts with "restricted browser sessions are never
returned to the browser, written to receipts, or included in ordinary logs." Keeping the
browsers on a VM I control also keeps the entire capability physically separable (§2.5): if
the entitlement is withdrawn from every workspace, `automation-1` is powered off and the rest
of the product is unaffected.

Egress for automation goes through a residential proxy pool (Decodo, $3.50/GB, EU exit
nodes) because Hetzner datacenter ranges are blocked by Instagram's edge for interactive
sessions. This is priced in §1.13 and only incurred while at least one workspace holds the
entitlement.

### 1.13 Supporting services and the monthly bill

| Component | Spec | Monthly |
|---|---|---|
| `lb-1` | Hetzner LB11, TLS termination | €5.39 |
| `web-1`, `web-2` | CX32 (4 vCPU, 8 GB) each | €13.60 |
| `worker-media-1` | CX42 (8 vCPU, 16 GB) | €16.40 |
| `worker-core-1` | CX32 (4 vCPU, 8 GB) — publish, analytics, scheduler | €6.80 |
| `automation-1` | CX42 (8 vCPU, 16 GB) — Playwright only | €16.40 |
| `db-1` | CCX23 dedicated (4 vCPU, 16 GB, 160 GB NVMe) | €42.00 |
| DB data volume | 200 GB block storage @ €0.044/GB | €8.80 |
| Hetzner Object Storage | pgBackRest repo1, ~400 GB | €2.40 |
| **Hetzner subtotal** | | **€111.79** |
| Cloudflare R2 | 300 GB month-1 media @ $0.015/GB + 30k Class-A ops | $4.64 |
| Cloudflare | DNS, WAF, bot rules (Free plan) | $0.00 |
| Postmark | 10k transactional emails | $15.00 |
| Sentry | Team plan, 50k errors | $26.00 |
| Grafana Cloud | Free tier: 10k series, 50 GB logs, 14-day retention | $0.00 |
| **Fixed total** | | **≈ €112 + $46 ≈ $167/mo** |
| Residential proxy | 20 GB/mo @ $3.50/GB, only while sourcing entitlement is live | $70.00 |
| **With restricted automation on** | | **≈ $237/mo** |

Growth term: media accrues at 300 GB/month (§2.4 derivation), so R2 adds $4.50/month every
month. Month 12 media cost is $4.64 + 11 × $4.50 = $54/mo. The retention rule in §9.11
(publish-ready variants deleted 30 days after publish, originals retained) removes ~40% of
that; month 12 lands near $33/mo, keeping the all-in bill under $210 fixed for the first
year without further action.

Stripe fees (1.5% + €0.25 for EEA cards) and Stripe Tax (0.5%) are transaction costs, not
infrastructure, and scale with revenue.

### 1.14 Observability

**Choice:** Sentry for exceptions in every process (`@sentry/node`, `@sentry/nextjs`) with
`beforeSend` running a redaction pass that drops any key matching
`/token|cookie|storage_state|password|secret|authorization/i`. Structured JSON logs to
stdout, shipped by Grafana Alloy to Grafana Cloud Loki. Business-state alerts are **SQL
queries run by a `health-check` cron task every 60 s**, writing to `health_signals` and
paging via Pushover when a threshold trips.

**Rejected:** Prometheus + Alertmanager self-hosted on the same fleet.

**Why:** the conditions that matter here are not process metrics, they are database facts:
"how many queue items are in `needs_review`", "oldest un-leased `ready` item past its slot",
"count of `ig_accounts` in `needs_reauth`". Expressing those as SQL against the primary is
exact and needs no exporter. A self-hosted Prometheus would also be down in exactly the
scenarios I most want paging (VM or network failure), whereas Pushover's API is off-fleet.

### 1.15 Live updates to the browser

**Choice:** Server-Sent Events on `GET /api/stream`, one EventSource per tab. Each web
process holds **one** dedicated Postgres connection issuing `LISTEN workspace_events` and
fans messages out in-process to the SSE clients whose session resolves to that workspace.
Producers call `pg_notify('workspace_events', json)` inside the committing transaction. The
payload carries only `{w: workspace_id, t: entity_type, id: entity_id, v: version}`; the
browser re-fetches through the normal RLS-scoped API.

**Rejected:** WebSockets via `ws` with a Redis pub/sub backplane.

**Why:** the traffic is one-directional server→client status ticks. SSE rides plain HTTP/2
through the existing load balancer with no upgrade handling and no sticky sessions, and
`EventSource` reconnects with `Last-Event-ID` for free. `LISTEN/NOTIFY` removes the Redis
backplane that a multi-process WebSocket deployment would need, which keeps §1.2's
single-datastore rule intact. NOTIFY payloads are capped at 8000 bytes by Postgres; carrying
ids only keeps every message under 160 bytes, and carrying ids only is *also* what prevents
a cross-tenant leak through the fan-out path — nothing readable is in the message.

Constants: heartbeat comment every 25 s; `X-Accel-Buffering: no`; LB `proxy_read_timeout`
300 s; client reconnect backoff 1 s → 30 s.

### 1.16 Testing stack

**Choice:** Vitest for unit and integration; `@testcontainers/postgresql` to bring up a real
PostgreSQL 17 per integration suite; Playwright Test for browser end-to-end; **`ig-sim`**, a
first-party Fastify service implementing the exact Instagram endpoints this product calls,
with fault injection selected by an `X-Sim-Fault` header (`timeout_before_send`,
`timeout_after_accept`, `rate_limited`, `token_expired`, `container_error`,
`duplicate_callback`). Toxiproxy sits between workers and both Postgres and `ig-sim` for
partition and latency drills.

**Rejected:** `nock`/`msw` HTTP mocks in-process.

**Why:** in-process mocks cannot express the one failure this product is actually built
around — a request whose response never arrives while the side effect lands. `ig-sim` can
accept a `media_publish`, record it, and then hang the socket; that is the `timeout_after_accept`
case that drives §5.2, and it is not simulatable with a mock that never touches a socket.
`ig-sim` doubles as the target for CI's race and quota drills, where the same interface must
answer `content_publishing_limit` consistently with what it has accepted.

---

## 2. System architecture

### 2.1 Processes

Seven long-running process types. Every one connects to the same Postgres primary as a
distinct database role.

| Process | Host | Count | DB role | Responsibility |
|---|---|---|---|---|
| `web` | web-1, web-2 | 2 | `app_web` | Pages, JSON mutation endpoints, SSE, Stripe & Instagram webhooks, presigned URL minting |
| `worker-core` | worker-core-1 | 1 | `app_worker` | Tasks: `publish.*`, `schedule.*`, `notify.*`, `billing.*`, `token.*` — concurrency 8 |
| `worker-analytics` | worker-core-1 | 1 | `app_worker` | Tasks: `analytics.*` — concurrency 4 |
| `worker-media` | worker-media-1 | 1 | `app_media` | Tasks: `media.*` — concurrency 3 |
| `scheduler` | worker-core-1 | 1 | `app_worker` | Graphile Worker `crontab`; materializes schedule occurrences, runs `health-check`, retention sweeps |
| `worker-automation` | automation-1 | 1 | `app_automation` | Tasks: `source.*`, `cleanup.*` — concurrency 4 browser contexts |
| `db` | db-1 | 1 | — | PostgreSQL 17 + pgBackRest |

`worker-core` and `worker-analytics` are separate OS processes on the same VM so that a
long analytics backfill cannot occupy publish concurrency, and so either can be restarted
alone.

### 2.2 How work moves

Every transition follows the same shape, and the shape is the reason the invariants hold:

```
  HTTP request or cron tick
        │
        ├─ BEGIN
        │   SET LOCAL app.workspace_id = '<uuid>'      -- RLS scope (§9.3)
        │   conditional UPDATE ... WHERE <precondition> RETURNING id   -- 0 rows = lost race, exit
        │   INSERT INTO <evidence table> (...)
        │   SELECT graphile_worker.add_job(...)        -- same transaction
        │   PERFORM pg_notify('workspace_events', ...) -- delivered only on commit
        └─ COMMIT
```

There is no enqueue outside a transaction anywhere in this system, and no state change
without an evidence row. A reviewer can verify this mechanically: the `add_job` wrapper in
`packages/domain/src/jobs.ts` takes a `PoolClient` and throws if the client is not inside a
transaction (`SELECT txid_current_if_assigned() IS NOT NULL`), and an ESLint rule forbids
importing the raw pool into `packages/domain`.

Named flows:

1. **Upload → prepare.** `POST /api/assets` creates `media_assets` (`state='awaiting_bytes'`)
   and returns presigned PUT URLs. The browser uploads directly to R2. `POST
   /api/assets/:id/complete` verifies the object with `HeadObjectCommand` (size + `ETag`),
   flips to `probing`, and enqueues `media.probe`. `media.probe` runs `ffprobe`, validates
   against Instagram's published constraints, and enqueues `media.transcode` or fails with a
   typed reason. `media.transcode` writes variants and flips the asset to `ready`.
2. **Queue → slot.** `schedule.materialize` (cron, every 15 min, per account) inserts
   `schedule_occurrences` rows up to 14 days ahead. `schedule.dispatch` (cron, every 60 s)
   finds occurrences with `slot_at <= now()` and `state='planned'`, and in one transaction
   claims the occurrence, claims the head-of-queue item, increments the daily counter, and
   enqueues `publish.run` on queue `publish:<account>`.
3. **Publish.** `publish.run` executes the two-phase Instagram publish with the attempt
   ledger of §3.7 and §5.
4. **Receipt → analytics.** On confirmation, `publish.run` inserts `posts` + `receipts` and
   enqueues `analytics.collect` at `now() + 1h`, which reschedules itself on the curve in
   §2.7.
5. **Restricted sourcing.** `source.poll` (cron per source, interval from the source row)
   runs on `automation-1`, writes `source_candidates`, and never enqueues a publish. Refill
   is a separate task, `source.refill`, on queue `refill:<account>`.
6. **Cleanup.** `cleanup.run` on queue `cleanup:<account>` steps one item at a time.

### 2.3 Starvation and blast-radius isolation

Five mechanisms, each structural rather than a policy:

1. **Disjoint task lists.** Graphile Worker's dequeue includes `task_id = ANY($1)` built from
   the worker's own `taskList`. `worker-core` does not know the identifier `media.transcode`,
   so it cannot take one, no matter how deep the media backlog is. This is the primary answer
   to "heavy media work must not starve normal publishing."
2. **Separate VMs for CPU-bound and browser work.** ffmpeg is pinned with `nice -n 10` and
   `--cpus=6` in Compose on `worker-media-1`; Chromium lives on `automation-1`. Neither can
   consume the CPU that serves pages, because they are not on those hosts.
3. **Per-account serial queues.** `publish:<account>` and `cleanup:<account>` serialize
   within an account and are fully parallel across accounts. One account stuck in
   reconciliation blocks only itself; hard rule "a failed or delayed item must not block
   unrelated accounts forever."
4. **Per-role connection caps.** PgBouncer (transaction pooling) on `db-1` with
   `max_db_connections=120` and per-role `pool_size`: `app_web` 60, `app_worker` 25,
   `app_media` 10, `app_automation` 10, `app_admin` 5. A worker leak cannot exhaust the pool
   the customer-facing app needs. Postgres `max_connections=200`.
5. **Statement timeouts by role.** `ALTER ROLE app_web SET statement_timeout = '5s'`,
   `app_worker '30s'`, `app_media '60s'`, `app_automation '60s'`, `app_admin '30s'`. A
   pathological query in a page cannot hold a lock long enough to stall publishing.

### 2.4 Sizing derivation

Scale from the brief: 1,000 registered users, 200 weekly active, 350 connected accounts,
"hundreds to a few thousand" prepared or published items per day.

- Published items/day: 350 accounts × 1.4 posts/day = **490**. Peak day (campaign burst,
  2.5×): **1,225**.
- Prepared items/day: users build backlog, so preparation runs ahead at 1.8× publishes =
  **880** median, **2,200** peak.
- Burst shape: the brief says bursts cluster at common posting times. Assume 35% of a day's
  publishes fall in a 60-minute window: 0.35 × 1,225 = **429 publishes/hour peak** = 7.2/min.
- Publish job wall time: container create 1.2 s + container poll (images 0 s, Reels median
  22 s) + `media_publish` 2.5 s + bookkeeping 0.3 s ≈ **4 s images, 26 s Reels**. At a 60/40
  Reels/image mix the mean is 17.2 s. Required concurrency = 7.2/min × 17.2 s / 60 = **2.1**.
  Configured concurrency is **8**, a 3.8× headroom that absorbs Reels containers sitting at
  `IN_PROGRESS` for the full 5-minute ceiling.
- Media CPU: a 35 s 1080×1920 H.264 clip re-encoded with `-preset veryfast -crf 23` at
  `-threads 2` measures 0.55× realtime on a Hetzner CX42 vCPU pair → 19 s per video. 880
  prepared/day × 60% video × 19 s = **2.8 CPU-hours/day**, against 6 allotted cores × 24 h =
  144 core-hours available. Peak hour: 2,200 × 0.35 × 0.6 × 19 s = 2.4 core-hours in one
  hour against 6 available. Concurrency 3 (3 × 2 threads = 6 cores) is the binding
  configuration.
- Media storage: 490 published/day × (12 MB original + 8 MB variant) = 9.8 GB/day ≈
  **300 GB/month**, matching §1.13.
- Postgres growth: the largest table is `analytics_snapshots` at 490 posts/day × 8
  collections in the first week + 1/week after. Steady state after 12 months ≈ 490 × 365 ×
  (8 + 45) rows ≈ 9.5M rows × ~180 bytes = **1.7 GB**. Everything else is under 500 MB. The
  200 GB volume is sized for 20× that plus WAL staging.

### 2.5 Separability of the restricted capabilities

The brief requires the public upload-first product and the restricted automation product to
be separable in access, operations, testing, and failure containment. Concretely:

- **Access:** `entitlements` rows gate them (`restricted_sourcing`, `managed_cleanup`).
  Every route and every task begins with `assertEntitlement(workspaceId, key, tx)`, which
  reads the same table the UI reads. There is no client-side-only gate anywhere (§4).
- **Operations:** the code paths run on a different VM under a different database role with a
  different Docker network. `app_automation` has `SELECT` on exactly 9 tables and `INSERT`
  on 6; it has **no** grant at all on `subscriptions`, `users`, `sessions`,
  `ig_account_credentials`, or `receipts`.
- **Testing:** the automation suites live in `apps/automation/test` and run in a separate CI
  job that does not run for changes outside `apps/automation` and `packages/domain`. The
  public product's CI never needs a browser.
- **Failure containment:** if `automation-1` is powered off, `source.*` and `cleanup.*` jobs
  accumulate in `graphile_worker.jobs` untouched (no worker knows those task identifiers),
  the UI shows sources as `stalled` after 3× their interval, and publishing is unaffected —
  because no publish path calls anything on that host.

### 2.6 Poll budget (the cost of choosing Postgres over Redis)

Graphile Worker polls with `pollInterval` and is woken early by `pg_notify` on
`graphile_worker:jobs:*`. With 4 worker processes at `pollInterval=2000ms`, the idle floor is
4 × 0.5 = 2 dequeue queries/second, each an indexed `SELECT ... FOR UPDATE SKIP LOCKED`
measured at 0.4 ms on the 160 GB NVMe. Idle cost: 0.08% of one core. The cron tick
(`schedule.dispatch`, every 60 s) adds one query per account with a due occurrence. This is
the entire price of §1.2 and it is negligible against a 4-vCPU dedicated database.

### 2.7 Analytics collection curve

Instagram insights stabilise over days, so a fixed interval is either wasteful or stale.
Each post is collected at **+1 h, +6 h, +24 h, +48 h, +7 d, then weekly until +90 d, then
monthly until +365 d**. Per post that is 5 + 12 + 10 = 27 collections over a year.
API calls: 490 posts/day × 27 / 365 days of spread ≈ 490 × 27 = 13,230 calls per daily
cohort, spread across a year; the daily steady-state call volume is
490 × (1 + 1 + 1 + 1)/1 for young cohorts plus the long tail ≈ **3,100 insight calls/day**,
or 2.2/minute. Each `analytics.collect` job batches all metrics for one media id into one
request. Manual refresh is rate-limited to 1 per post per 15 minutes and 20 per workspace per
hour by the token bucket in §3.16, which is what stops a refresh button from exhausting the
external limit.
---

## 3. Data model

PostgreSQL 17. Migrations are plain numbered SQL files applied by `graphile-migrate` in
`--forceActions` CI mode. Extensions: `pgcrypto` (for `gen_random_uuid`), `citext`,
`btree_gist` (for the exclusion constraint in §3.11).

Two conventions carry most of the tenancy safety:

- Every workspace-scoped table has `workspace_id UUID NOT NULL` **and** a composite foreign
  key through its parent, so a child row cannot reference a parent in another workspace even
  if application code is wrong. This requires redundant `UNIQUE (workspace_id, id)` keys on
  parents; they cost one index each and they make cross-tenant grafting impossible in SQL.
- Every workspace-scoped table has RLS enabled with the policy in §9.3.

### 3.1 Identity, workspaces, membership

```sql
CREATE TABLE users (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           CITEXT NOT NULL UNIQUE,
    password_hash   TEXT,                       -- argon2id; NULL until first credential set
    display_name    TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','suspended','deletion_pending','deleted')),
    email_verified_at TIMESTAMPTZ,
    last_login_at   TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    token_sha256    BYTEA PRIMARY KEY,          -- sha256 of the 32-byte cookie value
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    active_workspace_id UUID,                   -- FK added after workspaces
    ip_hash         BYTEA NOT NULL,             -- hmac(ip, pepper); raw IP is never stored
    user_agent      TEXT NOT NULL DEFAULT '',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ
);
CREATE INDEX sessions_user_live ON sessions (user_id) WHERE revoked_at IS NULL;
CREATE INDEX sessions_expiry ON sessions (expires_at) WHERE revoked_at IS NULL;

CREATE TABLE workspaces (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 80),
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','suspended','deletion_pending','deleted')),
    suspended_reason TEXT,
    onboarding      JSONB NOT NULL DEFAULT '{}'::jsonb,  -- niche, goal, cadence answers
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (status <> 'suspended' OR suspended_reason IS NOT NULL)
);

ALTER TABLE sessions
  ADD CONSTRAINT sessions_active_ws_fk
  FOREIGN KEY (active_workspace_id) REFERENCES workspaces(id) ON DELETE SET NULL;

CREATE TABLE workspace_members (
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('owner','admin','publisher')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace_id, user_id)
);
-- Every workspace keeps at least one owner; enforced by trigger because it is a
-- cross-row rule. Function: assert_workspace_has_owner() (AFTER DELETE OR UPDATE).
CREATE UNIQUE INDEX workspace_members_owner_present
    ON workspace_members (workspace_id, user_id) WHERE role = 'owner';
```

Roles, and what the brief's "distinguish billing and destructive administration from
day-to-day publishing" resolves to:

| Capability | owner | admin | publisher |
|---|:--:|:--:|:--:|
| Upload, queue, reorder, caption, publish now, pause account | ✅ | ✅ | ✅ |
| Connect / disconnect Instagram account | ✅ | ✅ | ❌ |
| Invite / remove members, change roles | ✅ | ✅ | ❌ |
| Checkout, change plan, open billing portal | ✅ | ❌ | ❌ |
| Run or schedule managed cleanup | ✅ | ✅ | ❌ |
| Delete workspace, export all data | ✅ | ❌ | ❌ |

```sql
CREATE TABLE invitations (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE CASCADE,  -- NULL = beta invite
    email           CITEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('owner','admin','publisher')),
    token_sha256    BYTEA NOT NULL UNIQUE,
    invited_by      UUID REFERENCES users(id),
    kind            TEXT NOT NULL CHECK (kind IN ('beta_access','workspace_member')),
    expires_at      TIMESTAMPTZ NOT NULL,
    accepted_at     TIMESTAMPTZ,
    accepted_by     UUID REFERENCES users(id),
    revoked_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (kind = 'beta_access' OR workspace_id IS NOT NULL)
);
-- Single use: acceptance is an UPDATE ... WHERE accepted_at IS NULL AND revoked_at IS NULL
-- AND expires_at > now(); a second submission of the same token matches 0 rows.
CREATE UNIQUE INDEX invitations_one_open_per_email_ws
    ON invitations (workspace_id, email)
    WHERE accepted_at IS NULL AND revoked_at IS NULL;

CREATE TABLE waitlist_entries (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email           CITEXT NOT NULL UNIQUE,
    answers         JSONB NOT NULL DEFAULT '{}'::jsonb,
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','invited','declined')),
    invited_invitation_id UUID REFERENCES invitations(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Signup is gated in SQL, not in the UI: the only INSERT path into users runs inside
-- accept_invitation(token), which requires a matching open invitation row.
```

### 3.2 Content-rights acceptance (attributable and versioned)

```sql
CREATE TABLE policy_documents (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind            TEXT NOT NULL CHECK (kind IN ('terms','privacy','content_rights','dpa')),
    version         INT NOT NULL,
    body_sha256     BYTEA NOT NULL,
    published_at    TIMESTAMPTZ NOT NULL,
    UNIQUE (kind, version)
);

CREATE TABLE policy_acceptances (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE CASCADE,
    policy_id       UUID NOT NULL REFERENCES policy_documents(id),
    accepted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    ip_hash         BYTEA NOT NULL,
    UNIQUE (user_id, policy_id, workspace_id)
);
```

Every `queue_items` row records the exact `policy_acceptances.id` in force when it was
queued (§3.6), so a rights dispute resolves to a person, a document version, and a time.

### 3.3 Instagram accounts and credentials

```sql
CREATE TABLE ig_accounts (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id        UUID NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
    ig_user_id          TEXT NOT NULL,               -- Instagram professional account id
    username            TEXT NOT NULL,
    profile_picture_url TEXT,
    account_type        TEXT NOT NULL CHECK (account_type IN ('BUSINESS','MEDIA_CREATOR')),
    connection_state    TEXT NOT NULL DEFAULT 'connected'
                        CHECK (connection_state IN ('connected','needs_reauth','disconnected')),
    publishing_state    TEXT NOT NULL DEFAULT 'active'
                        CHECK (publishing_state IN
                          ('active','paused_by_user','paused_by_system','held_over_plan','held_reconcile')),
    publishing_hold_reason TEXT,
    timezone            TEXT NOT NULL DEFAULT 'Europe/Berlin',   -- IANA name
    daily_allowance     INT  NOT NULL DEFAULT 25 CHECK (daily_allowance BETWEEN 1 AND 50),
    queue_version       BIGINT NOT NULL DEFAULT 1,  -- optimistic concurrency for reorder
    prep_profile        JSONB NOT NULL DEFAULT '{}'::jsonb,  -- reels/aspect/strip-exif/logo/caption template
    disconnected_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),                       -- target for composite FKs
    CHECK (timezone IN (SELECT name FROM pg_timezone_names)) NOT VALID  -- validated by trigger
);

-- Hard rule: "the same Instagram account must not accidentally belong to two workspaces".
CREATE UNIQUE INDEX ig_accounts_one_live_connection
    ON ig_accounts (ig_user_id)
    WHERE connection_state <> 'disconnected';

-- Reconnect preserves history: a disconnected row for the same (workspace_id, ig_user_id)
-- is reactivated by reconnect_account(), never re-inserted.
CREATE UNIQUE INDEX ig_accounts_ws_ig_unique ON ig_accounts (workspace_id, ig_user_id);

CREATE INDEX ig_accounts_publishable
    ON ig_accounts (workspace_id)
    WHERE connection_state = 'connected' AND publishing_state = 'active';
```

Credentials live in their own table so that grants, not code, decide who can read them:

```sql
CREATE TABLE ig_account_credentials (
    ig_account_id   UUID PRIMARY KEY REFERENCES ig_accounts(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL,
    token_ct        BYTEA NOT NULL,          -- AES-256-GCM ciphertext of the long-lived token
    token_nonce     BYTEA NOT NULL CHECK (octet_length(token_nonce) = 12),
    token_tag       BYTEA NOT NULL CHECK (octet_length(token_tag) = 16),
    kek_version     INT NOT NULL,
    token_expires_at TIMESTAMPTZ NOT NULL,
    scopes          TEXT[] NOT NULL,
    last_refresh_at TIMESTAMPTZ,
    last_error      TEXT,
    FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts (workspace_id, id)
);
CREATE INDEX ig_creds_refresh_due ON ig_account_credentials (token_expires_at);

REVOKE ALL ON ig_account_credentials FROM app_web, app_media, app_automation, app_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON ig_account_credentials TO app_worker;
```

`app_web` therefore **cannot** read an access token even with SQL injection in a page
handler; the connect flow writes the token through a `SECURITY DEFINER` function
`store_ig_token(...)` that returns `void`.

Restricted browser sessions are stored the same way, in a table `app_web` cannot see and
`app_worker` cannot see either:

```sql
CREATE TABLE automation_sessions (
    ig_account_id   UUID PRIMARY KEY REFERENCES ig_accounts(id) ON DELETE CASCADE,
    workspace_id    UUID NOT NULL,
    storage_state_ct BYTEA NOT NULL,       -- AES-256-GCM of Playwright storageState() JSON
    storage_nonce   BYTEA NOT NULL,
    storage_tag     BYTEA NOT NULL,
    kek_version     INT NOT NULL,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_valid_at   TIMESTAMPTZ,
    state           TEXT NOT NULL DEFAULT 'valid'
                    CHECK (state IN ('valid','expired','revoked')),
    FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts (workspace_id, id)
);
REVOKE ALL ON automation_sessions FROM app_web, app_worker, app_media, app_admin;
GRANT SELECT, INSERT, UPDATE, DELETE ON automation_sessions TO app_automation;
```

```sql
CREATE TABLE account_requests (              -- operator review before connection
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    requested_handle TEXT NOT NULL,
    requested_by    UUID NOT NULL REFERENCES users(id),
    state           TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending','approved','declined','invited')),
    customer_visible_reason TEXT,            -- shown to requester
    internal_note   TEXT,                    -- never serialized to any customer response
    decided_by      UUID REFERENCES users(id),
    decided_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

The split between `customer_visible_reason` and `internal_note` is enforced at the boundary
by a Zod response schema (`AccountRequestPublic`) that has no `internal_note` key and is
applied with `.strict()`; a test asserts `JSON.stringify(response)` never contains the note
text (§4).

### 3.4 Media assets and variants

```sql
CREATE TABLE media_assets (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
    uploaded_by     UUID REFERENCES users(id),
    origin          TEXT NOT NULL CHECK (origin IN ('upload','restricted_source')),
    source_item_id  UUID,                     -- FK added in §3.12
    state           TEXT NOT NULL DEFAULT 'awaiting_bytes'
                    CHECK (state IN ('awaiting_bytes','probing','transcoding','ready','rejected','purged')),
    reject_code     TEXT CHECK (reject_code IN
                      ('unsupported_container','unsupported_codec','too_large','too_long',
                       'too_short','bad_aspect','corrupt','zero_bytes','virus_suspected')),
    original_key    TEXT NOT NULL,            -- R2 object key
    original_bytes  BIGINT CHECK (original_bytes > 0),
    original_sha256 BYTEA,
    media_kind      TEXT CHECK (media_kind IN ('image','video')),
    width           INT, height INT, duration_ms INT,
    probe           JSONB,                    -- raw ffprobe output, retained as evidence
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    CHECK (state <> 'rejected' OR reject_code IS NOT NULL)
);
-- Same bytes uploaded twice in a workspace resolve to one asset.
CREATE UNIQUE INDEX media_assets_content_dedup
    ON media_assets (workspace_id, original_sha256)
    WHERE original_sha256 IS NOT NULL AND state <> 'purged';

CREATE TABLE media_variants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    asset_id        UUID NOT NULL,
    purpose         TEXT NOT NULL CHECK (purpose IN ('publish_reel','publish_feed','preview','thumbnail')),
    recipe_sha256   BYTEA NOT NULL,           -- sha256 of the exact ffmpeg/sharp argv
    object_key      TEXT NOT NULL,
    bytes           BIGINT NOT NULL CHECK (bytes > 0),
    width INT NOT NULL, height INT NOT NULL, duration_ms INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    purged_at       TIMESTAMPTZ,
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, asset_id) REFERENCES media_assets (workspace_id, id) ON DELETE CASCADE
);
-- Retry of a preparation produces the same variant row, not a second one.
CREATE UNIQUE INDEX media_variants_idempotent
    ON media_variants (asset_id, purpose, recipe_sha256);
```

`recipe_sha256` is the mechanism for "a preparation failure can be retried without creating a
duplicate queue item": the retry recomputes the same recipe hash, and the `INSERT ... ON
CONFLICT (asset_id, purpose, recipe_sha256) DO UPDATE SET object_key = EXCLUDED.object_key`
yields the same row id, which the queue item already points at.

### 3.5 Plan entitlements

```sql
CREATE TABLE plans (
    code            TEXT PRIMARY KEY,        -- 'free','studio_monthly','studio_annual','agency_monthly','agency_annual'
    display_name    TEXT NOT NULL,
    stripe_price_id TEXT UNIQUE,             -- NULL for 'free'
    interval        TEXT CHECK (interval IN ('month','year')),
    max_ig_accounts INT NOT NULL,
    included_seats  INT NOT NULL,
    extra_seat_price_id TEXT,
    storage_gb      INT NOT NULL,
    monthly_prepare_allowance INT NOT NULL,
    features        TEXT[] NOT NULL DEFAULT '{}'   -- e.g. '{managed_cleanup}'
);

CREATE TABLE entitlement_grants (            -- operator-granted, time-limited beta access
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    feature         TEXT NOT NULL CHECK (feature IN ('restricted_sourcing','managed_cleanup')),
    granted_by      UUID NOT NULL REFERENCES users(id),
    reason          TEXT NOT NULL,
    granted_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    revoked_at      TIMESTAMPTZ,
    CHECK (expires_at > granted_at)
);
CREATE UNIQUE INDEX entitlement_grants_one_live
    ON entitlement_grants (workspace_id, feature) WHERE revoked_at IS NULL;

-- Effective entitlements = plan features ∪ live grants. One place, read by UI and by
-- every server path.
CREATE VIEW effective_entitlements AS
SELECT w.id AS workspace_id, f.feature
FROM workspaces w
LEFT JOIN subscriptions s ON s.workspace_id = w.id
LEFT JOIN plans p ON p.code = COALESCE(s.plan_code, 'free')
CROSS JOIN LATERAL unnest(COALESCE(p.features, '{}')) AS f(feature)
WHERE w.status = 'active'
UNION
SELECT g.workspace_id, g.feature
FROM entitlement_grants g
JOIN workspaces w2 ON w2.id = g.workspace_id AND w2.status = 'active'
WHERE g.revoked_at IS NULL AND g.expires_at > now();
```

### 3.6 Queue items and the frozen payload

```sql
CREATE TABLE queue_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    ig_account_id   UUID NOT NULL,
    asset_id        UUID NOT NULL,
    created_by      UUID REFERENCES users(id),
    origin          TEXT NOT NULL CHECK (origin IN ('upload','restricted_source')),
    source_item_id  UUID,

    status          TEXT NOT NULL DEFAULT 'preparing' CHECK (status IN
                    ('preparing','prep_failed','ready','hidden','publishing',
                     'published','failed','needs_review','removed')),
    fail_class      TEXT CHECK (fail_class IN
                    ('customer_fixable','external_temporary','permission_expired',
                     'account_limit','invalid_media','uncertain')),
    fail_detail     TEXT,

    position        NUMERIC(30,12),           -- NULL once terminal
    -- Frozen at the moment the item became 'ready'. Nothing below changes afterwards
    -- except by an explicit user edit, which re-freezes and bumps frozen_version.
    frozen_version  INT NOT NULL DEFAULT 0,
    frozen_at       TIMESTAMPTZ,
    frozen_caption  TEXT,
    frozen_variant_id UUID,
    frozen_prep_profile JSONB,
    frozen_attribution JSONB,                 -- source handle, permalink, credit line
    frozen_policy_acceptance_id UUID REFERENCES policy_acceptances(id),
    frozen_sha256   BYTEA,                    -- sha256 over the six columns above

    lease_token     UUID,
    lease_worker    TEXT,
    lease_expires_at TIMESTAMPTZ,

    dedup_key       TEXT NOT NULL,            -- see below
    hidden_at TIMESTAMPTZ, removed_at TIMESTAMPTZ, published_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts (workspace_id, id),
    FOREIGN KEY (workspace_id, asset_id)      REFERENCES media_assets (workspace_id, id),
    FOREIGN KEY (workspace_id, frozen_variant_id) REFERENCES media_variants (workspace_id, id),

    CHECK (status <> 'ready' OR (frozen_at IS NOT NULL AND frozen_variant_id IS NOT NULL
                                 AND frozen_sha256 IS NOT NULL)),
    CHECK (status NOT IN ('failed','prep_failed') OR fail_class IS NOT NULL),
    CHECK (status <> 'publishing' OR lease_expires_at IS NOT NULL)
);

-- Queue order. Fractional positions; midpoint insert; renormalized when the gap closes.
CREATE UNIQUE INDEX queue_items_position_unique
    ON queue_items (ig_account_id, position)
    WHERE status IN ('preparing','prep_failed','ready','hidden','publishing');
CREATE INDEX queue_items_next_ready
    ON queue_items (ig_account_id, position)
    WHERE status = 'ready';
CREATE INDEX queue_items_ws_status ON queue_items (workspace_id, status);

-- Same content cannot be queued twice into the same account concurrently.
CREATE UNIQUE INDEX queue_items_dedup
    ON queue_items (ig_account_id, dedup_key)
    WHERE status NOT IN ('removed','failed');
```

`dedup_key` is computed at insert time by `packages/domain/src/dedupKey.ts`:

- upload origin: `'u:' || encode(media_assets.original_sha256,'hex')`
- restricted source origin: `'s:' || source_items.source_media_id`

so a double-clicked "Add to queue", two concurrent refills, and a collection that re-sees the
same source post all collide on one unique index rather than on application logic. The insert
uses `ON CONFLICT (ig_account_id, dedup_key) WHERE status NOT IN ('removed','failed') DO
NOTHING RETURNING id`; zero rows returned means "already queued", and the API replies `200`
with the existing item rather than an error.

Renormalization (`renormalizeQueue(accountId, tx)` in `packages/domain/src/queueOrder.ts`)
runs inside the reorder transaction when the computed midpoint gap falls below `1e-9`:
`UPDATE queue_items SET position = t.rn * 1000 FROM (SELECT id, row_number() OVER (ORDER BY
position) rn ...) t WHERE ...`. At 500 items per account the rewrite touches 500 rows and
measures under 6 ms.

Reorder concurrency: `POST /api/accounts/:id/queue/order` carries the client's
`queue_version`; the handler runs `UPDATE ig_accounts SET queue_version = queue_version + 1
WHERE id = $1 AND queue_version = $2 RETURNING queue_version`. Zero rows → `409` with the
current order, so two operators dragging simultaneously cannot interleave into an order
neither chose.

### 3.7 Publish attempts and receipts

```sql
CREATE TABLE publish_attempts (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    queue_item_id   UUID NOT NULL,
    ig_account_id   UUID NOT NULL,
    attempt_no      INT NOT NULL CHECK (attempt_no >= 1),
    frozen_sha256   BYTEA NOT NULL,          -- copy of queue_items.frozen_sha256 at attempt start
    idempotency_key TEXT NOT NULL,           -- 'pub:' || queue_item_id || ':' || frozen_version

    phase           TEXT NOT NULL CHECK (phase IN
                    ('container_creating','container_pending','container_ready',
                     'publish_sent','publish_confirmed','publish_uncertain',
                     'failed_pre','failed_post','abandoned')),
    ig_container_id TEXT,
    ig_media_id     TEXT,
    ig_permalink    TEXT,
    ig_error        JSONB,                   -- code, subcode, fbtrace_id, message
    request_started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    boundary_crossed_at TIMESTAMPTZ,         -- set immediately BEFORE the media_publish call
    resolved_at     TIMESTAMPTZ,
    UNIQUE (workspace_id, id),
    UNIQUE (queue_item_id, attempt_no),
    FOREIGN KEY (workspace_id, queue_item_id) REFERENCES queue_items (workspace_id, id),
    CHECK (phase NOT IN ('publish_sent','publish_confirmed','publish_uncertain')
           OR boundary_crossed_at IS NOT NULL),
    CHECK (phase <> 'publish_confirmed' OR ig_media_id IS NOT NULL)
);

-- HARD RULE: "Only one active publication of a queue item may cross the outside
-- side-effect boundary."
CREATE UNIQUE INDEX publish_attempts_one_past_boundary
    ON publish_attempts (queue_item_id)
    WHERE phase IN ('publish_sent','publish_uncertain','publish_confirmed');

-- Reconciliation work list.
CREATE INDEX publish_attempts_uncertain
    ON publish_attempts (ig_account_id, request_started_at)
    WHERE phase = 'publish_uncertain';

CREATE TABLE posts (                          -- the library
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    ig_account_id   UUID NOT NULL,
    queue_item_id   UUID NOT NULL,
    ig_media_id     TEXT NOT NULL,
    permalink       TEXT NOT NULL,
    media_kind      TEXT NOT NULL CHECK (media_kind IN ('image','video')),
    published_at    TIMESTAMPTZ NOT NULL,
    lifecycle       TEXT NOT NULL DEFAULT 'live'
                    CHECK (lifecycle IN ('live','archived','trashed','gone','protected_live')),
    protected       BOOLEAN NOT NULL DEFAULT false,
    protected_by    UUID REFERENCES users(id),
    protected_at    TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    UNIQUE (queue_item_id),                   -- one post per queue item, forever
    FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts (workspace_id, id)
);
CREATE UNIQUE INDEX posts_ig_media_unique ON posts (ig_account_id, ig_media_id);

CREATE TABLE receipts (                       -- durable evidence of a final claim
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('publish','archive','trash','charge','deletion')),
    subject_type    TEXT NOT NULL,
    subject_id      UUID NOT NULL,
    ig_account_id   UUID,
    frozen_sha256   BYTEA,
    payload         JSONB NOT NULL,           -- redacted; see redactReceipt()
    external_ref    TEXT,                     -- ig_media_id / stripe invoice id / deletion ref
    occurred_at     TIMESTAMPTZ NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, subject_type, subject_id, external_ref)
);
CREATE INDEX receipts_ws_time ON receipts (workspace_id, occurred_at DESC);
```

`redactReceipt()` in `packages/domain/src/receipts.ts` is applied by the only insert path;
it drops keys matching `/token|cookie|storage|password|authorization|set-cookie/i` and
truncates any value over 4 KB. A unit test feeds it a synthetic payload containing a fake
`storageState` blob and asserts it does not appear in the output (§4).

### 3.8 Daily publishing use (survives restarts and cache loss)

```sql
CREATE TABLE account_daily_usage (
    ig_account_id   UUID NOT NULL REFERENCES ig_accounts(id) ON DELETE CASCADE,
    local_date      DATE NOT NULL,           -- date in ig_accounts.timezone
    slots_consumed  INT NOT NULL DEFAULT 0 CHECK (slots_consumed >= 0),
    ig_quota_usage  INT,                     -- last observed content_publishing_limit
    ig_quota_seen_at TIMESTAMPTZ,
    PRIMARY KEY (ig_account_id, local_date)
);
```

The counter is incremented in the **same transaction** that moves the attempt to
`publish_sent`, never in a cache and never after the call:

```sql
INSERT INTO account_daily_usage (ig_account_id, local_date, slots_consumed)
VALUES ($1, $2, 1)
ON CONFLICT (ig_account_id, local_date) DO UPDATE
  SET slots_consumed = account_daily_usage.slots_consumed + 1
WHERE account_daily_usage.slots_consumed < $3   -- daily_allowance
RETURNING slots_consumed;
```

Zero rows returned means the allowance is exhausted; the dispatcher defers instead of
failing (§5.6). Because this is a committed row and not a counter in memory, a restart, a
deploy, or a Postgres failover cannot forget it — the hard rule "daily publishing use cannot
be forgotten by a restart or cache loss."
### 3.9 Schedules and materialized slots

```sql
CREATE TABLE schedule_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    ig_account_id   UUID NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('fixed_times','interval_window')),
    weekdays        SMALLINT[] NOT NULL DEFAULT '{1,2,3,4,5,6,7}',  -- ISO 1=Mon
    times_local     TIME[],                  -- kind='fixed_times'
    window_start    TIME,                    -- kind='interval_window'
    window_end      TIME,
    interval_minutes INT CHECK (interval_minutes BETWEEN 30 AND 1440),
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE,
    CHECK (kind <> 'fixed_times' OR (times_local IS NOT NULL AND array_length(times_local,1) > 0)),
    CHECK (kind <> 'interval_window' OR
           (window_start IS NOT NULL AND window_end IS NOT NULL AND interval_minutes IS NOT NULL))
);

CREATE TABLE schedule_occurrences (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    ig_account_id   UUID NOT NULL,
    rule_id         UUID REFERENCES schedule_rules(id) ON DELETE SET NULL,
    slot_at         TIMESTAMPTZ NOT NULL,     -- absolute instant
    local_slot_key  TEXT NOT NULL,            -- 'YYYY-MM-DDTHH:MM' rendered in account tz
    state           TEXT NOT NULL DEFAULT 'planned'
                    CHECK (state IN ('planned','claimed','consumed','skipped_dst',
                                     'skipped_empty','skipped_paused','expired')),
    claimed_queue_item_id UUID,
    claimed_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE
);
-- Two rules landing on the same instant produce one slot, not two.
CREATE UNIQUE INDEX schedule_occ_instant ON schedule_occurrences (ig_account_id, slot_at);
-- DST fall-back repeats a local wall-clock time; this makes the second one impossible.
CREATE UNIQUE INDEX schedule_occ_local  ON schedule_occurrences (ig_account_id, local_slot_key);
-- A queue item can be claimed by at most one slot, ever.
CREATE UNIQUE INDEX schedule_occ_claim
    ON schedule_occurrences (claimed_queue_item_id) WHERE claimed_queue_item_id IS NOT NULL;
CREATE INDEX schedule_occ_due ON schedule_occurrences (slot_at) WHERE state = 'planned';
```

Materialization uses Luxon: `DateTime.fromObject({year,month,day,hour,minute}, {zone:
account.timezone})`. For a spring-forward gap (e.g. `Europe/Berlin 2026-03-29 02:30` does
not exist), Luxon returns the instant one hour later; `materialize()` detects this by
asserting `dt.hour === requestedHour && dt.minute === requestedMinute` and, when it fails,
inserts the occurrence with `state='skipped_dst'` instead of `planned`. The user sees "02:30
does not exist on 29 Mar; skipped" in the upcoming-runs preview. For fall-back, the two
candidate instants share `local_slot_key`, and the unique index keeps the first
(pre-transition) one; the second insert hits `ON CONFLICT DO NOTHING`.

Editing a rule does not delete `claimed` or `consumed` occurrences. `schedule.materialize`
deletes only `state='planned'` rows with `slot_at > now() + interval '5 minutes'` before
re-inserting; the 5-minute skirt is what prevents the race in §5.7 where a slot is being
dispatched while its rule is edited.

Timezone change on an account is a distinct operation (`POST /api/accounts/:id/timezone`)
that (1) refuses if any occurrence is `claimed`, (2) deletes future `planned` rows, (3)
re-materializes, (4) writes an `audit_log` row, and (5) shows a before/after preview of the
next 10 runs which the user confirms. This is why a timezone edit cannot double-post.

### 3.10 Analytics history (append-only, never overwritten)

```sql
CREATE TABLE analytics_snapshots (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    UUID NOT NULL,
    post_id         UUID NOT NULL,
    collected_at    TIMESTAMPTZ NOT NULL,
    age_bucket      TEXT NOT NULL CHECK (age_bucket IN
                    ('1h','6h','24h','48h','7d','weekly','monthly','manual')),
    reach           INT, views INT, likes INT, comments INT, saved INT, shares INT,
    total_interactions INT,
    missing_metrics TEXT[] NOT NULL DEFAULT '{}',   -- metrics IG did not return
    raw             JSONB NOT NULL,
    collector_error TEXT,
    FOREIGN KEY (workspace_id, post_id) REFERENCES posts (workspace_id, id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX analytics_snapshots_one_per_bucket
    ON analytics_snapshots (post_id, age_bucket) WHERE age_bucket <> 'manual';
CREATE INDEX analytics_snapshots_post_time ON analytics_snapshots (post_id, collected_at DESC);

CREATE MATERIALIZED VIEW post_latest_metrics AS
SELECT DISTINCT ON (post_id) post_id, workspace_id, collected_at, reach, views, likes,
       comments, saved, shares, total_interactions, missing_metrics
FROM analytics_snapshots WHERE collector_error IS NULL
ORDER BY post_id, collected_at DESC;
CREATE UNIQUE INDEX post_latest_metrics_pk ON post_latest_metrics (post_id);
-- REFRESH MATERIALIZED VIEW CONCURRENTLY every 5 minutes from the scheduler.
```

Nothing updates a snapshot. "Old snapshots remain useful for trends" is structural: there is
no `UPDATE analytics_snapshots` statement in the codebase, and `app_worker` is granted only
`SELECT, INSERT` on it — `REVOKE UPDATE, DELETE ON analytics_snapshots FROM app_worker`.
Honesty about comparability is carried by `missing_metrics`: any chart series that spans a
snapshot with the metric in `missing_metrics` renders a gap, not an interpolation, and the
comparison UI labels it "not reported by Instagram for this post".

### 3.11 Rate limiting and external-call budget

```sql
CREATE TABLE rate_buckets (
    scope           TEXT NOT NULL,           -- 'ig_account','workspace','user','ip'
    scope_id        TEXT NOT NULL,
    bucket          TEXT NOT NULL,           -- 'ig_api','manual_refresh','login','invite_accept'
    window_start    TIMESTAMPTZ NOT NULL,
    tokens_used     INT NOT NULL DEFAULT 0,
    PRIMARY KEY (scope, scope_id, bucket, window_start)
);
```

Limits, with derivations:

| Bucket | Limit | Window | Derivation |
|---|---|---|---|
| `ig_api` per account | 180 calls | 1 h | Instagram's business-use-case limit varies by account size; 180/h is below the smallest published tier and leaves room for the 27-point analytics curve plus publishing |
| `manual_refresh` per post | 1 | 15 min | Insights do not move meaningfully faster than the 1 h collection point; 15 min bounds abuse without feeling locked |
| `manual_refresh` per workspace | 20 | 1 h | 10 accounts × 2 investigations/h |
| `login` per IP | 10 | 15 min | Blocks credential stuffing; a real user needs ≤ 3 |
| `login` per email | 5 | 15 min | Independent of IP rotation |
| `invite_accept` per IP | 20 | 1 h | Invitation tokens are 32 bytes; this is defence in depth |
| `presign` per session | 300 | 1 h | A 50-item queue page mints ≤ 100 preview URLs |
| `source_poll` per workspace | 60 | 1 h | 12 sources × 5 polls/h ceiling |

### 3.12 Restricted sourcing

```sql
CREATE TABLE sources (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    ig_account_id   UUID NOT NULL,           -- destination account
    kind            TEXT NOT NULL CHECK (kind IN ('account','hashtag','reels_feed')),
    handle          TEXT,                    -- kind='account'
    hashtag         TEXT,                    -- kind='hashtag'
    state           TEXT NOT NULL DEFAULT 'pending_verification' CHECK (state IN
                    ('pending_verification','active','paused','retrying','blocked','removed')),
    state_detail    TEXT,
    trust           TEXT NOT NULL DEFAULT 'untrusted'
                    CHECK (trust IN ('untrusted','trusted')),  -- gates auto-refill
    media_types     TEXT[] NOT NULL DEFAULT '{video}',
    max_age_days    INT NOT NULL DEFAULT 30 CHECK (max_age_days BETWEEN 1 AND 365),
    min_likes       INT NOT NULL DEFAULT 0,
    min_comments    INT NOT NULL DEFAULT 0,
    min_plays       INT NOT NULL DEFAULT 0,
    exclude_words   TEXT[] NOT NULL DEFAULT '{}',
    candidates_per_run INT NOT NULL DEFAULT 10 CHECK (candidates_per_run BETWEEN 1 AND 50),
    poll_interval_minutes INT NOT NULL DEFAULT 360 CHECK (poll_interval_minutes >= 60),
    auto_refill_target INT CHECK (auto_refill_target BETWEEN 0 AND 100),
    last_polled_at  TIMESTAMPTZ,
    consecutive_failures INT NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts (workspace_id, id),
    CHECK (trust = 'trusted' OR auto_refill_target IS NULL)   -- auto-refill needs operator trust
);
CREATE UNIQUE INDEX sources_unique_target
    ON sources (ig_account_id, kind, coalesce(handle,''), coalesce(hashtag,''))
    WHERE state <> 'removed';

CREATE TABLE source_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    source_id       UUID NOT NULL,
    ig_account_id   UUID NOT NULL,
    source_media_id TEXT NOT NULL,           -- Instagram's id for the discovered post
    source_permalink TEXT NOT NULL,
    source_author   TEXT NOT NULL,
    source_caption  TEXT,
    media_kind      TEXT NOT NULL CHECK (media_kind IN ('image','video')),
    observed_likes INT, observed_comments INT, observed_plays INT,
    posted_at       TIMESTAMPTZ,
    discovered_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    state           TEXT NOT NULL DEFAULT 'held' CHECK (state IN
                    ('held','eligible','fetching','fetch_failed','accepted','rejected','expired')),
    hold_reason     TEXT,                    -- 'below_min_likes','excluded_word','too_old',...
    asset_id        UUID,
    media_sha256    BYTEA,
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, source_id)     REFERENCES sources (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts (workspace_id, id)
);
-- HARD RULE: "Source content cannot be duplicated into the same account's queue through
-- concurrent collection or refill activity."
CREATE UNIQUE INDEX source_items_dedup_by_id
    ON source_items (ig_account_id, source_media_id);
CREATE UNIQUE INDEX source_items_dedup_by_bytes
    ON source_items (ig_account_id, media_sha256) WHERE media_sha256 IS NOT NULL;
CREATE INDEX source_items_eligible ON source_items (ig_account_id, discovered_at DESC)
    WHERE state = 'eligible';

ALTER TABLE media_assets
  ADD CONSTRAINT media_assets_source_fk
  FOREIGN KEY (workspace_id, source_item_id) REFERENCES source_items (workspace_id, id);

CREATE TABLE source_runs (                   -- one row per poll, evidence of every check
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    source_id       UUID NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    outcome         TEXT CHECK (outcome IN ('ok','no_results','blocked','session_invalid',
                                            'proxy_failed','timeout','error')),
    candidates_seen INT NOT NULL DEFAULT 0,
    candidates_new  INT NOT NULL DEFAULT 0,
    held_counts     JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_redacted  TEXT,
    FOREIGN KEY (workspace_id, source_id) REFERENCES sources (workspace_id, id) ON DELETE CASCADE
);
```

Refill is serialized per account by Graphile Worker `queue_name = 'refill:<account>'`, and
the insert of each queue item goes through the same `queue_items_dedup` index as an upload.
Two concurrent refills therefore cannot both add the same source post; the second `INSERT ...
ON CONFLICT DO NOTHING` returns no row and the refill counts it as "already queued".

Auto-refill stops at depth: `refill()` reads `SELECT count(*) FROM queue_items WHERE
ig_account_id=$1 AND status IN ('ready','preparing')` inside the same transaction that
inserts, under `SELECT ... FROM ig_accounts WHERE id=$1 FOR UPDATE`, and inserts
`min(target - depth, eligible)` items. The row lock is what stops two schedulers from each
seeing depth 8 against a target of 10 and both adding 2.

### 3.13 Managed cleanup

```sql
CREATE TABLE cleanup_rules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    ig_account_id   UUID NOT NULL,
    name            TEXT NOT NULL,
    media_kinds     TEXT[] NOT NULL CHECK (media_kinds <@ ARRAY['image','video']),
    min_age_days    INT NOT NULL CHECK (min_age_days >= 7),
    max_reach       INT, max_views INT, max_likes INT, max_comments INT,
    metrics_max_staleness_hours INT NOT NULL DEFAULT 48 CHECK (metrics_max_staleness_hours <= 168),
    max_items_per_run INT NOT NULL DEFAULT 20 CHECK (max_items_per_run BETWEEN 1 AND 50),
    schedule_kind   TEXT NOT NULL DEFAULT 'manual'
                    CHECK (schedule_kind IN ('manual','daily','weekly')),
    schedule_time_local TIME,
    schedule_weekday SMALLINT CHECK (schedule_weekday BETWEEN 1 AND 7),
    enabled         BOOLEAN NOT NULL DEFAULT true,
    rule_sha256     BYTEA NOT NULL,          -- hash of the rule body; frozen into each run
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts (workspace_id, id) ON DELETE CASCADE,
    CHECK (schedule_kind = 'manual' OR schedule_time_local IS NOT NULL),
    CHECK (coalesce(max_reach,max_views,max_likes,max_comments) IS NOT NULL)
);

CREATE TABLE cleanup_runs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    ig_account_id   UUID NOT NULL,
    rule_id         UUID REFERENCES cleanup_rules(id) ON DELETE SET NULL,
    frozen_rule     JSONB NOT NULL,          -- full rule body at confirmation time
    frozen_rule_sha256 BYTEA NOT NULL,
    selection_sha256 BYTEA NOT NULL,         -- sha256 of sorted post_id list + metric values
    trigger         TEXT NOT NULL CHECK (trigger IN ('manual','scheduled')),
    requested_by    UUID REFERENCES users(id),
    confirmed_at    TIMESTAMPTZ,
    state           TEXT NOT NULL DEFAULT 'previewing' CHECK (state IN
                    ('previewing','confirmed','running','paused_reconcile','stopped',
                     'completed','aborted_selection_changed','aborted_precheck')),
    abort_reason    TEXT,
    started_at TIMESTAMPTZ, finished_at TIMESTAMPTZ,
    lease_token UUID, lease_expires_at TIMESTAMPTZ,
    UNIQUE (workspace_id, id),
    FOREIGN KEY (workspace_id, ig_account_id) REFERENCES ig_accounts (workspace_id, id)
);
-- HARD RULE: "Only one cleanup item per Instagram account may cross its destructive
-- boundary at a time." Step 1 of 2: at most one live run per account.
CREATE UNIQUE INDEX cleanup_runs_one_live
    ON cleanup_runs (ig_account_id)
    WHERE state IN ('confirmed','running','paused_reconcile');

CREATE TABLE cleanup_run_items (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL,
    run_id          UUID NOT NULL,
    post_id         UUID NOT NULL,
    ordinal         INT NOT NULL,
    action          TEXT NOT NULL CHECK (action IN ('archive','trash')),
    metrics_used    JSONB NOT NULL,          -- the exact numbers that qualified it
    metrics_collected_at TIMESTAMPTZ NOT NULL,
    phase           TEXT NOT NULL DEFAULT 'pending' CHECK (phase IN
                    ('pending','sent','confirmed','uncertain','failed','skipped_protected',
                     'skipped_not_found','skipped_stopped')),
    boundary_crossed_at TIMESTAMPTZ,
    verified_at     TIMESTAMPTZ,
    evidence        JSONB,                   -- redacted: screenshot key, final URL, selector seen
    UNIQUE (run_id, ordinal),
    UNIQUE (run_id, post_id),
    FOREIGN KEY (workspace_id, run_id)  REFERENCES cleanup_runs (workspace_id, id) ON DELETE CASCADE,
    FOREIGN KEY (workspace_id, post_id) REFERENCES posts (workspace_id, id),
    CHECK (phase NOT IN ('sent','uncertain','confirmed') OR boundary_crossed_at IS NOT NULL)
);
-- Step 2 of 2: at most one item of a run past the boundary at a time.
CREATE UNIQUE INDEX cleanup_items_one_past_boundary
    ON cleanup_run_items (run_id) WHERE phase IN ('sent','uncertain');
```

`selection_sha256` is what invalidates a stale confirmation. `cleanup.run` recomputes the
selection from current analytics, current `posts.protected`, and the frozen rule; if the
recomputed hash differs from `selection_sha256`, the run transitions to
`aborted_selection_changed` and the user is asked to review again. This is the brief's "if
the selection changes before execution, the old confirmation is no longer valid," enforced by
a comparison rather than by hoping nothing changed.

Feed photos map to `action='archive'`; Reels map to `action='trash'` (Instagram's Recently
Deleted). The confirmation dialog renders a distinct, non-dismissable block for any
`action='trash'` item stating that Instagram permanently removes it after its recovery
window and that ToolBox Poster cannot restore it. `posts.lifecycle` moves to `archived` or
`trashed`; there is no code path that sets `trashed` → `live`.

### 3.14 Billing

```sql
CREATE TABLE subscriptions (
    workspace_id    UUID PRIMARY KEY REFERENCES workspaces(id) ON DELETE CASCADE,
    stripe_customer_id     TEXT NOT NULL UNIQUE,
    stripe_subscription_id TEXT UNIQUE,
    plan_code       TEXT NOT NULL REFERENCES plans(code) DEFAULT 'free',
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN
                    ('active','trialing','past_due','canceled','unpaid','incomplete','incomplete_expired')),
    seats_purchased INT NOT NULL DEFAULT 0 CHECK (seats_purchased >= 0),
    cancel_at_period_end BOOLEAN NOT NULL DEFAULT false,
    current_period_end   TIMESTAMPTZ,
    -- Ordering guard for out-of-order webhooks:
    last_event_created_at TIMESTAMPTZ,
    last_synced_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE stripe_events (
    event_id        TEXT PRIMARY KEY,        -- Stripe's evt_… id: replay-proof by construction
    type            TEXT NOT NULL,
    created_at_stripe TIMESTAMPTZ NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at    TIMESTAMPTZ,
    outcome         TEXT CHECK (outcome IN ('applied','ignored_stale','ignored_unknown_type','error')),
    error           TEXT,
    payload_digest  BYTEA NOT NULL
);
CREATE INDEX stripe_events_unprocessed ON stripe_events (received_at) WHERE processed_at IS NULL;
```

The webhook handler is three statements: `INSERT INTO stripe_events ... ON CONFLICT
(event_id) DO NOTHING RETURNING event_id` (a repeat delivery returns zero rows and the
handler answers `200` immediately), `SELECT graphile_worker.add_job('billing.sync', ...,
job_key => 'billing.sync:'||workspace_id)`, `COMMIT`. `billing.sync` ignores the event body
entirely and calls `stripe.subscriptions.retrieve(id, {expand:['items.data.price']})`, then
writes the current truth. Out-of-order events therefore cannot corrupt state: whichever runs
last re-reads the same authoritative object.

### 3.15 Notifications, feedback, audit, idempotency, deletion

```sql
CREATE TABLE notifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    user_id         UUID REFERENCES users(id) ON DELETE CASCADE,  -- NULL = whole workspace
    severity        TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    kind            TEXT NOT NULL,           -- 'publish_failed','needs_review','token_expiring',...
    title           TEXT NOT NULL,
    body            TEXT NOT NULL,
    deep_link       TEXT NOT NULL,
    dedup_key       TEXT NOT NULL,
    read_at TIMESTAMPTZ, dismissed_at TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX notifications_dedup
    ON notifications (workspace_id, dedup_key) WHERE dismissed_at IS NULL;
CREATE INDEX notifications_unread ON notifications (workspace_id, created_at DESC)
    WHERE read_at IS NULL AND dismissed_at IS NULL;

CREATE TABLE feedback (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id    UUID REFERENCES workspaces(id) ON DELETE SET NULL,
    user_id         UUID REFERENCES users(id) ON DELETE SET NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('bug','confusing','idea','praise','other')),
    body            TEXT NOT NULL CHECK (length(body) BETWEEN 1 AND 5000),
    page_context    JSONB NOT NULL,          -- route, entity ids, viewport, app version
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE audit_log (
    id              BIGSERIAL PRIMARY KEY,
    workspace_id    UUID,
    actor_kind      TEXT NOT NULL CHECK (actor_kind IN ('user','operator','system','stripe','instagram')),
    actor_user_id   UUID REFERENCES users(id),
    actor_ip_hash   BYTEA,
    action          TEXT NOT NULL,           -- 'account.disconnect','cleanup.confirm','entitlement.grant'
    subject_type    TEXT NOT NULL,
    subject_id      TEXT NOT NULL,
    before          JSONB,
    after           JSONB,
    request_id      TEXT,
    occurred_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_subject ON audit_log (subject_type, subject_id, occurred_at DESC);
CREATE INDEX audit_log_ws_time ON audit_log (workspace_id, occurred_at DESC);
REVOKE UPDATE, DELETE ON audit_log FROM app_web, app_worker, app_media, app_automation, app_admin;

CREATE TABLE idempotency_keys (
    workspace_id    UUID NOT NULL,
    key             TEXT NOT NULL CHECK (length(key) BETWEEN 8 AND 200),
    route           TEXT NOT NULL,
    request_sha256  BYTEA NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('in_progress','completed')),
    response_status INT,
    response_body   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    PRIMARY KEY (workspace_id, key)
);
CREATE INDEX idempotency_keys_gc ON idempotency_keys (created_at);

CREATE TABLE deletion_requests (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subject_kind    TEXT NOT NULL CHECK (subject_kind IN ('user','workspace')),
    subject_id      UUID NOT NULL,
    requested_by    UUID REFERENCES users(id),
    reference       TEXT NOT NULL UNIQUE,    -- shown to the customer, e.g. 'DEL-7Q3K2M'
    state           TEXT NOT NULL DEFAULT 'grace' CHECK (state IN
                    ('grace','running','completed','canceled','failed')),
    grace_until     TIMESTAMPTZ NOT NULL,
    steps           JSONB NOT NULL DEFAULT '{}'::jsonb,  -- {"revoke_ig":"done","purge_media":"running",...}
    requested_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    UNIQUE (subject_kind, subject_id)
);

CREATE TABLE deletion_receipts (             -- all that survives a completed deletion
    reference       TEXT PRIMARY KEY,
    subject_kind    TEXT NOT NULL,
    subject_id_hmac BYTEA NOT NULL,          -- hmac(subject_id, pepper): not reversible to a person
    completed_at    TIMESTAMPTZ NOT NULL,
    counts          JSONB NOT NULL           -- {"posts":412,"assets":903,"bytes":48210000000}
);
```

`deletion_requests.steps` is what makes deletion resumable: each step is written `done` in
its own transaction, and `deletion.run` re-enters at the first step not marked `done`
(§5.13). `deletion_receipts` deliberately holds no reversible identifier, satisfying
"provides a status reference without retaining the deleted data in disguise."

### 3.16 Operator health signals

```sql
CREATE TABLE health_signals (
    name            TEXT NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    value           NUMERIC NOT NULL,
    threshold       NUMERIC NOT NULL,
    state           TEXT NOT NULL CHECK (state IN ('ok','warn','alarm')),
    PRIMARY KEY (name, observed_at)
);
```

The `health-check` cron task writes one row per signal per minute. The operator health screen
reads the latest row per name; Pushover is paged when a signal is `alarm` for 3 consecutive
observations (3 minutes) to avoid paging on a single slow tick.

| Signal | Query | warn | alarm |
|---|---|---|---|
| `publish_backlog` | `ready` items whose claimed slot is > 10 min past | 5 | 20 |
| `needs_review_open` | `queue_items` in `needs_review` | 1 | 5 |
| `uncertain_unresolved_minutes` | oldest `publish_attempts.phase='publish_uncertain'` age | 15 | 60 |
| `accounts_needs_reauth` | `ig_accounts.connection_state='needs_reauth'` | 3 | 10 |
| `media_queue_depth` | pending `media.*` jobs | 50 | 200 |
| `media_oldest_wait_s` | age of oldest pending `media.*` job | 600 | 1800 |
| `job_failures_1h` | jobs with `attempts >= max_attempts` in 1 h | 3 | 15 |
| `wal_archive_lag_s` | `now() - last_archived_time` from `pg_stat_archiver` | 300 | 900 |
| `r2_put_error_rate` | failed R2 writes / total, 15 min | 0.02 | 0.10 |
| `automation_session_invalid` | `automation_sessions.state='expired'` count | 1 | 5 |
| `stripe_events_unprocessed` | rows with `processed_at IS NULL` older than 5 min | 1 | 10 |
| `backup_age_hours` | hours since last successful pgBackRest backup | 26 | 50 |
| `restore_drill_age_days` | days since last passing restore drill | 8 | 14 |
---

## 4. Invariant enforcement map

Test names are file-scoped and writable as stated: `packages/domain/test/*.spec.ts` for
integration tests against Testcontainers PostgreSQL, `apps/web/e2e/*.spec.ts` for Playwright,
`drills/*.ts` for the nightly chaos suite.

### 4.1 The five promises

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| **P1** A post belongs to the correct workspace and account | Composite FKs `(workspace_id, ig_account_id) → ig_accounts (workspace_id, id)` on `queue_items`, `schedule_occurrences`, `sources`, `cleanup_runs`; RLS policy `workspace_id = current_setting('app.workspace_id')::uuid` on every workspace table; `app_web` lacks `BYPASSRLS` | `tenancy.spec.ts::cross_tenant_graft_rejected` inserts a `queue_items` row with workspace A's id and workspace B's account id and asserts SQLSTATE `23503`. `tenancy.spec.ts::rls_blocks_read` sets `app.workspace_id` to A and asserts `SELECT count(*) FROM queue_items` returns only A's rows for all 14 workspace tables (table list read from `pg_class`, so a new table without a policy fails the test) |
| **P1** Uses the reviewed media and caption | `queue_items.frozen_*` columns + `frozen_sha256`; `publish_attempts.frozen_sha256` copied at attempt start; `publish.run` recomputes the hash from the frozen columns and aborts with `fail_class='customer_fixable'` if it differs | `freeze.spec.ts::settings_change_after_freeze` freezes an item, mutates `ig_accounts.prep_profile` and the caption template, runs `publish.run` against `ig-sim`, and asserts the container request body equals the frozen caption byte-for-byte |
| **P1** Never publishes twice | Partial unique index `publish_attempts_one_past_boundary` on `(queue_item_id) WHERE phase IN ('publish_sent','publish_uncertain','publish_confirmed')`; `posts UNIQUE (queue_item_id)`; Graphile Worker `queue_name='publish:<account>'` serializes per account | `race.spec.ts::twenty_concurrent_publishes` starts 20 `publish.run` calls for one item across 4 worker processes and asserts exactly 1 row in `posts`, exactly 1 attempt past the boundary, and 19 failures with SQLSTATE `23505`. `e2e/double_click.spec.ts` fires two `POST /publish-now` with the same `Idempotency-Key` and asserts one `ig-sim` `media_publish` call |
| **P2** Order, time, progress, failures, pauses, limits are visible | `queue_items.status` + `fail_class` + `fail_detail`; `schedule_occurrences.state`; `account_daily_usage`; `ig_accounts.publishing_state` + `publishing_hold_reason`; all surfaced by `GET /api/accounts/:id/queue` which serializes every one of those fields | `e2e/queue_truth.spec.ts` drives each of the 9 `status` values and 6 `fail_class` values through `ig-sim` and asserts a distinct visible label and a non-empty explanation for each (snapshot of rendered text) |
| **P2** A post never silently disappears or changes account | `queue_items` has no `DELETE` path — `removed` is a status, and `REVOKE DELETE ON queue_items FROM app_web, app_worker`; `ig_account_id` is immutable, enforced by trigger `queue_items_immutable_account` raising on `OLD.ig_account_id <> NEW.ig_account_id` | `immutability.spec.ts::account_move_rejected` attempts the update and asserts the raised message. `immutability.spec.ts::no_delete_grant` asserts `has_table_privilege('app_web','queue_items','DELETE')` is false |
| **P3** Uncertainty never becomes a second destructive action | `boundary_crossed_at` is written and committed **before** the HTTP call; on any error the attempt moves to `publish_uncertain`, which is inside the partial unique index, so no further attempt can be created; retry endpoints check `phase` and refuse | `drills/uncertain_publish.ts` sends `X-Sim-Fault: timeout_after_accept`, SIGKILLs the worker, restarts it, and asserts: item is `needs_review`, `ig-sim` recorded exactly 1 `media_publish`, and the "Retry" control returns `409 needs_reconciliation` |
| **P4** Holding or leaving never destroys work | Suspension sets `workspaces.status='suspended'` and Graphile Worker `forbiddenFlags` returns `ws:<id>`, so jobs stay in `graphile_worker.jobs`; disconnect sets `connection_state='disconnected'` and leaves `queue_items` untouched; downgrade sets `publishing_state='held_over_plan'` | `hold.spec.ts::suspend_preserves_queue` suspends a workspace with 30 queued items and 12 pending jobs, advances the clock 24 h, un-suspends, and asserts all 30 items and 12 jobs are present and publishing resumes. `hold.spec.ts::disconnect_preserves` asserts `count(queue_items)` is unchanged after disconnect and after reconnect the items are still bound to the same account row |
| **P5** Instagram passwords never requested by the public product | There is no password field for Instagram anywhere in `apps/web`; credentials are only ever typed into instagram.com inside the streamed remote browser (§1.12) | `grep.spec.ts::no_ig_password_input` fails the build if any `.tsx` under `apps/web` contains an input whose name/id matches `/instagram.*pass|ig_pass/i`. `e2e/connect.spec.ts` asserts the connect flow's only outbound navigation is to `https://www.instagram.com/oauth/authorize` |
| **P5** Tokens, media, billing, sessions do not leak across users, workspaces, logs, or support tools | Per-role grants: `app_web` has **no** privilege on `ig_account_credentials` or `automation_sessions`; `app_automation` has no privilege on `users`, `sessions`, `subscriptions`, `receipts`; Sentry `beforeSend` + `redactReceipt()` + pino redaction paths | `grants.spec.ts::role_matrix` asserts the full `has_table_privilege` matrix (5 roles × 34 tables × 4 privileges) against a checked-in expected matrix, so any new grant must be declared. `redaction.spec.ts::no_secret_in_sinks` pushes a payload containing 6 sentinel secret strings through the logger, Sentry transport, receipt writer, and admin serializer and asserts none appear |

### 4.2 The hard product rules (brief §4)

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| Tenant isolation applies to **every** action including media links, live updates, notifications, analytics, operator recovery | RLS on all workspace tables; presigned URLs minted only inside an RLS-scoped transaction that already read the row; SSE fan-out keyed by the session's workspace and payload containing ids only; admin console reads through `app_admin`, which also has RLS with a policy requiring an `admin_sessions` row and writes an `audit_log` entry per read of customer content | `isolation.spec.ts::presign_requires_row` asks workspace B to presign workspace A's object key directly and asserts `404` before any S3 call is attempted (assert on the S3 client spy call count = 0). `isolation.spec.ts::sse_scope` connects two EventSources for A and B, emits 50 notifications for A, and asserts B's stream received 0 bytes of event data |
| Every action that can publish, charge, archive, delete, invite, or change access is safe against repeated requests | `idempotency_keys (workspace_id, key)` primary key with `request_sha256` mismatch → `409`; middleware `withIdempotency()` wraps all 23 such routes; a route registered without it fails a startup assertion | `idempotency.spec.ts::all_mutating_routes_covered` enumerates the route table, filters to methods in `{POST,PATCH,DELETE}` and effects in the mutating set, and asserts each is wrapped. `idempotency.spec.ts::replay_returns_same_body` replays each of the 23 routes and asserts identical status + body and exactly one side effect recorded |
| Only one active publication of a queue item may cross the outside boundary | `publish_attempts_one_past_boundary` partial unique index (§3.7) | `race.spec.ts::twenty_concurrent_publishes` (above) |
| Only one cleanup item per Instagram account may cross its destructive boundary at a time | Two composed partial unique indexes: `cleanup_runs_one_live` on `(ig_account_id)` and `cleanup_items_one_past_boundary` on `(run_id)`; plus `queue_name='cleanup:<account>'` serial execution | `cleanup_race.spec.ts::two_runs_one_account` starts a manual run and a scheduled run simultaneously and asserts one gets `23505` and the account has exactly 1 live run. `cleanup_race.spec.ts::one_item_at_a_time` asserts the automation harness never observes 2 rows with `phase='sent'` for one run across 200 polls |
| Known pre-action failures may retry; ambiguous post-action failures may not until reconciled | The retry decision reads `publish_attempts.phase`: `failed_pre` → retryable, `publish_uncertain` → the API returns `409 needs_reconciliation` and the UI shows "Reconcile" instead of "Retry" | `retry_policy.spec.ts::matrix` drives all 9 phases through `canRetry()` and asserts the boolean matrix; `e2e/needs_review.spec.ts` asserts the retry button is absent and the endpoint returns 409 even when called directly |
| The media, caption, destination, settings, rule, and metrics a user approved are frozen | `queue_items.frozen_*`/`frozen_sha256`; `cleanup_runs.frozen_rule`/`frozen_rule_sha256`/`selection_sha256`; `cleanup_run_items.metrics_used`/`metrics_collected_at` | `freeze.spec.ts::settings_change_after_freeze` (above) and `cleanup_freeze.spec.ts::selection_drift_aborts`: confirm a selection, protect one post, run, assert state `aborted_selection_changed` and zero automation actions taken |
| Queue work is held, not destroyed, when paused, disconnected, suspended, over plan, over quota, or awaiting reauth | Six distinct hold states (`publishing_state` ∈ {`paused_by_user`,`paused_by_system`,`held_over_plan`,`held_reconcile`}, `connection_state='needs_reauth'`, `workspaces.status='suspended'`); the dispatcher's `WHERE` clause excludes them; no path sets `queue_items.status='removed'` from any of them | `hold.spec.ts::six_hold_states` parameterizes over all six, applies it to an account with 10 ready items, ticks the dispatcher 100 times, and asserts 0 publishes and 10 items still `ready` |
| Daily publishing use cannot be forgotten by a restart or cache loss | `account_daily_usage` row incremented in the same transaction as the boundary transition (§3.8) | `drills/quota_restart.ts` publishes 24 of a 25 allowance, SIGKILLs every worker, restarts, and asserts the 26th dispatch defers and `slots_consumed = 25` |
| Source and uploaded content cannot be duplicated into the same account's queue | `queue_items_dedup` unique index on `(ig_account_id, dedup_key)`; `source_items_dedup_by_id` and `source_items_dedup_by_bytes`; refill under `SELECT ... FOR UPDATE` on the account row | `dedup.spec.ts::concurrent_refill` runs 8 concurrent refills against a backlog of 5 eligible items with target 10 and asserts exactly 5 queue items. `dedup.spec.ts::same_bytes_twice` uploads identical bytes twice and asserts one asset and one queue item |
| Restricted source and cleanup capabilities are invisible **and unreachable** without an entitlement | `assertEntitlement()` reads `effective_entitlements` at the top of every route handler and every task; the Next.js route group `(restricted)` is registered behind the same check server-side | `entitlement.spec.ts::direct_call_denied` calls all 18 restricted endpoints with a valid session for a workspace with no grant and asserts `404` (not `403`, to avoid disclosing the capability). `entitlement.spec.ts::task_denied` enqueues `source.poll` for an unentitled workspace and asserts the task exits without an outbound request (proxy spy call count 0) |
| Customer media is private by default; temporary outside access is narrow and expires | R2 bucket has no public access policy; every read is a presigned GET scoped to one object key; TTL 300 s for browser previews, 3600 s for the URL handed to Instagram's fetcher; `presign` rate bucket (§3.11) | `media_access.spec.ts::bucket_not_public` asserts an unsigned GET returns `401`. `media_access.spec.ts::ttl_expiry` mints a preview URL, advances 301 s, and asserts `403` |
| Private grants and restricted sessions never returned to the browser, written to receipts, or in ordinary logs | Grants (§3.3) make the tables unreadable to `app_web`; `redactReceipt()`; pino `redact` paths; Sentry `beforeSend` | `redaction.spec.ts::no_secret_in_sinks` (above); `grants.spec.ts::role_matrix` (above) |
| Content-rights acceptance is attributable and versioned | `policy_documents(kind, version, body_sha256)` + `policy_acceptances(user_id, policy_id, ip_hash)`; `queue_items.frozen_policy_acceptance_id NOT NULL` for `origin='upload'`, enforced by trigger `queue_items_require_rights` | `rights.spec.ts::queue_without_acceptance_rejected` inserts a ready item with a NULL acceptance and asserts the trigger raises. `rights.spec.ts::version_pinned` publishes a new `content_rights` version and asserts an existing queued item still resolves to v1 |
| External callbacks, billing events, and deletion requests can arrive repeatedly or out of order without corrupting state | `stripe_events.event_id` primary key; `billing.sync` re-fetches from Stripe rather than trusting the body; `subscriptions.last_event_created_at` monotonic guard; Instagram webhook deduped on `(object, entry.id, entry.time, field)`; `deletion_requests UNIQUE (subject_kind, subject_id)` | `billing.spec.ts::shuffled_replay` replays a recorded 14-event lifecycle in 30 random orders with 3× duplication of each and asserts the final `subscriptions` row is identical in all 30 runs |
| Every final claim has inspectable evidence | `receipts` rows for `publish`, `archive`, `trash`, `charge`, `deletion`, each with `external_ref` and a redacted payload; `audit_log` for every privileged action; `publish_attempts`, `cleanup_run_items.evidence`, `source_runs` | `evidence.spec.ts::every_final_claim_has_receipt` drives one of each of the 5 claim kinds end-to-end and asserts a `receipts` row exists whose `external_ref` matches the external system's identifier |

### 4.3 Requirement-level invariants worth naming separately

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| Public account creation cannot bypass the invitation gate | The only `INSERT INTO users` in the codebase is inside `accept_invitation(token)`, a `SECURITY DEFINER` function; `REVOKE INSERT ON users FROM app_web` | `signup.spec.ts::no_insert_grant` asserts `has_table_privilege('app_web','users','INSERT')` is false; `signup.spec.ts::expired_token` asserts an expired or already-accepted token yields 0 rows and no user |
| The same Instagram account cannot belong to two workspaces | `ig_accounts_one_live_connection` unique index on `(ig_user_id) WHERE connection_state <> 'disconnected'` | `connect.spec.ts::second_workspace_blocked` connects the same `ig_user_id` in workspace B and asserts a `409` with the "already connected elsewhere" copy, and that no token was written |
| Reconnect preserves queue and history | `ig_accounts_ws_ig_unique` on `(workspace_id, ig_user_id)` makes reconnect an `UPDATE` of the existing row; ownership is verified by the `ig_user_id` returned from the new OAuth exchange matching the stored one | `connect.spec.ts::reconnect_preserves` disconnects with 15 queued items and 40 posts, reconnects, and asserts the same `ig_accounts.id`, 15 items, 40 posts |
| A preparation retry does not create a duplicate queue item | `media_variants_idempotent` on `(asset_id, purpose, recipe_sha256)`; the queue item points at the asset and is re-pointed at the same variant id | `media.spec.ts::retry_no_duplicate` fails a transcode 3 times then succeeds and asserts 1 queue item, 1 variant row |
| Changing a schedule has a predictable effect; DST and duplicate rules cannot double-post | `schedule_occ_instant`, `schedule_occ_local`, `schedule_occ_claim` unique indexes; the 5-minute materialization skirt | `dst.spec.ts::berlin_spring_forward` materializes across `2026-03-29` with a 02:30 rule and asserts one `skipped_dst` occurrence and zero publishes. `dst.spec.ts::berlin_fall_back` materializes across `2026-10-25` with a 02:30 rule and asserts exactly one occurrence. `dst.spec.ts::duplicate_rules` creates two rules with identical times and asserts one occurrence |
| Cancel works only while safely cancelable | `cancelPublish()` runs `UPDATE publish_attempts SET phase='abandoned' WHERE id=$1 AND phase IN ('container_creating','container_pending','container_ready') RETURNING id`; zero rows → `409 point_of_no_return` and the UI replaces "Cancel" with "This post has been sent to Instagram" | `cancel.spec.ts::phase_matrix` asserts cancel succeeds in 3 phases and returns 409 in the other 6 |
| A manual analytics refresh cannot exhaust external limits | `rate_buckets` check inside the request transaction before enqueue; `job_key='analytics.collect:<post_id>'` collapses queued duplicates | `ratelimit.spec.ts::refresh_flood` issues 500 refreshes for one post in 10 s and asserts 1 outbound insights call and 499 `429` responses |
| Publishing continues after the browser closes | Publishing is a Graphile Worker job, not a request; the HTTP handler's only job is the transactional enqueue | `e2e/close_tab.spec.ts` triggers publish-now, closes the browser context, waits for `ig-sim` to record the publish, reopens and asserts the receipt is visible |
| Accessibility and state coverage | Every list and form component renders through `<AsyncBoundary>` which requires `empty`, `loading`, `error`, and `blocked` props (TypeScript makes them non-optional); `axe-core` runs in Playwright on 18 routes | `a11y.spec.ts::axe_all_routes` asserts zero `serious` or `critical` violations on 18 routes in light and dark themes. `states.spec.ts::boundary_props_required` is a `tsd` type test asserting omission of any of the four props is a compile error |
---

## 5. Failure-mode walkthrough

Reference timings used below: container-create HTTP timeout 30 s (5 s connect); container
status poll every 5 s to a ceiling of 300 s; `media_publish` HTTP timeout 60 s; publish lease
600 s with a 120 s heartbeat; pre-boundary retry schedule 15 s, 30 s, 60 s, 120 s, 240 s
(5 attempts) with ±20% jitter.

### 5.1 Crash before publish

**Scenario:** `worker-core` is SIGKILLed after the Instagram container is created and before
`media_publish` is sent.

1. `publish.run` had committed, in one transaction: `queue_items.status='publishing'`,
   `lease_token=T`, `lease_expires_at=now()+600s`, and `publish_attempts` row
   `attempt_no=1, phase='container_creating'`.
2. The container create returned `{id: '178...'}`; a second committed transaction set
   `phase='container_pending'`, `ig_container_id='178...'`. `boundary_crossed_at` is still
   NULL — no destructive side effect has occurred, because creating a container publishes
   nothing.
3. SIGKILL. The Postgres connection drops; any open transaction rolls back. Graphile Worker's
   job row still exists with its `locked_at`/`locked_by` set.
4. Graphile Worker's own recovery releases jobs whose `locked_at < now() - interval '4 hours'`
   by default; this deployment sets it lower by running `graphile_worker.force_unlock_workers(
   ARRAY['worker-core-1:*'])` from the `systemd` `ExecStartPre` of the worker unit, so a
   restart immediately reclaims that host's jobs.
5. The reclaimed `publish.run` re-enters and reads the existing attempt. Because
   `phase='container_pending'` and `boundary_crossed_at IS NULL`, `resumeAttempt()` takes the
   pre-boundary path: it polls `GET /{ig_container_id}?fields=status_code`.
6. `status_code` is `FINISHED` → the attempt moves to `container_ready` and the publish
   proceeds normally. If `EXPIRED` or `ERROR`, the attempt moves to `failed_pre` with
   `fail_class='external_temporary'`, and retry attempt 2 creates a **new** container — safe,
   because the old container was never published.
7. The daily counter was **not** incremented in step 1; it increments only at the boundary
   (§3.8), so a pre-boundary crash does not consume the customer's allowance.

**Evidence:** `publish_attempts` shows one row, `attempt_no=1`, with `request_started_at`,
`ig_container_id`, `boundary_crossed_at IS NULL`, and a `phase` history reconstructable from
`audit_log` rows `publish.phase` for that subject. `graphile_worker.jobs` shows the job's
`attempts` count. `account_daily_usage.slots_consumed` is unchanged.

### 5.2 Crash after a publish may have been accepted

**Scenario:** `media_publish` is sent; the response never arrives (socket hang, or the worker
dies mid-flight).

1. Immediately **before** the HTTP call, `publish.run` commits:
   `UPDATE publish_attempts SET phase='publish_sent', boundary_crossed_at=now()` and the
   `account_daily_usage` increment, in one transaction. The commit precedes the call, so the
   record of "we may have acted" cannot be lost by the same event that loses the response.
2. The call is issued with a 60 s timeout. It hangs, or the process dies.
3. On timeout: a `catch` block commits `phase='publish_uncertain'`, `ig_error={code:'timeout'}`,
   `queue_items.status='needs_review'`, a `notifications` row (`severity='critical'`,
   kind `uncertain_outcome`), and enqueues `publish.reconcile` on
   `queue_name='publish:<account>'` at `now()+30s`.
   On SIGKILL: nothing commits, but the row is already `publish_sent`. The reclaimed job's
   `resumeAttempt()` sees `boundary_crossed_at IS NOT NULL` and performs exactly the same
   transition as the timeout path. Both routes converge.
4. `publish_attempts_one_past_boundary` now contains a row for this item, so **no** second
   attempt can be inserted. Every retry entry point (`POST /items/:id/retry`,
   `publish.run` re-entry, operator "safe retry") reads `phase` first and refuses.
5. `publish.reconcile` investigates in this order:
   a. `GET /{ig_container_id}?fields=status_code`. `PUBLISHED` → resolved: published.
   b. If `FINISHED` or `IN_PROGRESS`, it queries `GET /{ig-user-id}/media?fields=id,
      timestamp,permalink,caption&limit=25` and matches any media whose `timestamp` is within
      ±10 minutes of `boundary_crossed_at` **and** whose caption equals `frozen_caption`.
      A match → resolved: published.
   c. If neither resolves, it re-polls at 30 s, 2 min, 5 min, 15 min, 60 min, 6 h (6 attempts
      over ~7 h). Instagram containers expire after 24 h, so an `EXPIRED` status after that
      window with no matching media resolves the attempt as **not published**.
6. Resolved-published: `phase='publish_confirmed'`, `ig_media_id`, `ig_permalink`, `posts` row,
   `receipts` row, `queue_items.status='published'`. Resolved-not-published: `phase='failed_post'`,
   `queue_items.status='ready'`, the daily counter is decremented by the compensating
   statement `UPDATE account_daily_usage SET slots_consumed = slots_consumed - 1 WHERE ...
   AND slots_consumed > 0`, and the item returns to the head of the queue.
7. Unresolved after the 6 attempts: the item stays `needs_review`, `health_signals`
   `uncertain_unresolved_minutes` goes to `alarm`, and the operator console offers exactly two
   actions, both of which write `audit_log`: "mark as published (paste permalink)" and "mark
   as not published (return to queue)". Neither action calls Instagram.

**Evidence:** the `publish_attempts` row retains `boundary_crossed_at`, the container id, the
raw `ig_error`, and `resolved_at`. Every reconcile poll appends an `audit_log` row
`publish.reconcile.poll` with the observed `status_code`. The customer-visible timeline shows
"sent to Instagram — confirming outcome" with the same timestamps.

### 5.3 Duplicate publish work

**Scenario:** the user double-clicks "Publish now" while the scheduler simultaneously
dispatches the same item's slot, and a stale worker from a previous deploy is still running.

1. Both browser clicks carry the same `Idempotency-Key` (minted once per button mount).
   `withIdempotency()` runs `INSERT INTO idempotency_keys (...) VALUES (..., 'in_progress')
   ON CONFLICT DO NOTHING RETURNING key`. The second click returns zero rows, waits up to 5 s
   polling for `state='completed'`, and replays the stored response.
2. The scheduler's dispatch transaction and the publish-now transaction both attempt
   `UPDATE queue_items SET status='publishing', lease_token=..., lease_expires_at=now()+600s
   WHERE id=$1 AND status='ready' AND (lease_expires_at IS NULL OR lease_expires_at < now())
   RETURNING id`. Exactly one matches a row; the other gets zero rows and exits without
   enqueuing anything.
3. Suppose both had somehow enqueued. Graphile Worker's `queue_name='publish:<account>'`
   admits one job at a time, so they cannot run concurrently on one account.
4. Suppose they ran concurrently anyway (different accounts is impossible here; assume a bug).
   The first to reach the boundary commits `phase='publish_sent'`; the second's `INSERT INTO
   publish_attempts` violates `publish_attempts_one_past_boundary` with SQLSTATE `23505`, the
   job fails, and its retry re-reads the item as `publishing`/`published` and exits.
5. If the stale worker from the old deploy holds a lease and is unreachable, its lease expires
   at 600 s; only then can another worker claim the item. The heartbeat
   (`UPDATE queue_items SET lease_expires_at = now()+600s WHERE lease_token=$1` every 120 s)
   means a live worker is never preempted.

**Evidence:** `idempotency_keys` shows one row with `state='completed'` and the served
response. `queue_items.lease_token` shows a single winner. `publish_attempts` has one row.
`ig-sim`'s (or Instagram's) call log shows one `media_publish`.

### 5.4 Media preparation failure

**Scenario:** a user uploads a 900 MB `.mov` whose audio codec Instagram rejects; the
transcode then fails twice on the media worker before succeeding.

1. `POST /api/assets` created `media_assets` with `state='awaiting_bytes'` and returned
   multipart presigned URLs (parts of 32 MB; 900 MB → 29 parts). The browser uploads directly
   to R2; navigating away does not abort the upload of already-issued parts, and the resume
   token is the R2 `UploadId` stored on the asset row.
2. `POST /api/assets/:id/complete` calls `CompleteMultipartUpload`, then `HeadObjectCommand`
   to verify size, then flips to `probing` and enqueues `media.probe` — in one transaction.
3. `media.probe` runs `ffprobe -v error -print_format json -show_format -show_streams`.
   Output is stored verbatim in `media_assets.probe`. Validation against Instagram's published
   Reels constraints (MP4/MOV container, H.264 video, AAC audio, ≤ 1 GB, 3 s–15 min, aspect
   ratio between 0.01:1 and 10:1) finds `pcm_s16le` audio. This is fixable by transcoding, so
   it is not a rejection; `media.transcode` is enqueued.
4. `media.transcode` builds an explicit argv: `ffmpeg -y -i in.mov -c:v libx264 -profile:v
   high -pix_fmt yuv420p -preset veryfast -crf 23 -vf "scale=1080:1920:force_original_aspect_
   ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black" -c:a aac -b:a 128k -ar 44100
   -movflags +faststart -map_metadata -1 out.mp4`. `-map_metadata -1` is the mechanism for
   "removing unnecessary metadata". `recipe_sha256` is the SHA-256 of this argv array.
5. Attempt 1 dies: the media VM hits its 6-core cgroup limit and the job exceeds the 900 s
   task timeout. Graphile Worker records the failure and retries after 15 s.
6. Attempt 2 fails on a corrupt intermediate. Attempt 3 succeeds. The variant insert is
   `INSERT INTO media_variants (...) ON CONFLICT (asset_id, purpose, recipe_sha256) DO UPDATE
   SET object_key=EXCLUDED.object_key RETURNING id` — the same row id every time, so the
   queue item's `frozen_variant_id` (set at freeze time) is never orphaned and **no second
   queue item is created**.
7. If the file had been genuinely unsupported (a 20-minute clip), `media.probe` sets
   `state='rejected'`, `reject_code='too_long'`, the queue item goes to `prep_failed` with
   `fail_class='invalid_media'` and the message "Instagram Reels are limited to 15 minutes;
   this file is 20:14. Trim it and re-upload." The item is not removed from the queue; the
   user can replace the asset in place, which re-freezes and bumps `frozen_version`.

**Evidence:** `media_assets.probe` holds the ffprobe JSON; `media_variants.recipe_sha256`
holds the exact argv hash; `graphile_worker.jobs` (and after success, `graphile_worker.
job_queues`/the archived failure rows) show 3 attempts with their `last_error`;
`queue_items.frozen_variant_id` points at one variant row.

### 5.5 Account revocation mid-queue

**Scenario:** the customer removes ToolBox Poster from their Instagram app settings at 09:00;
a slot fires at 09:05 with 40 items queued.

1. `publish.run` calls container create and receives HTTP 400 with `error.code=190`
   (`OAuthException`, invalid/expired token).
2. `classifyIgError()` maps code 190 and subcodes 458/460/463/467 to
   `fail_class='permission_expired'`. Because `boundary_crossed_at IS NULL`, the attempt is
   `failed_pre`, and this class is explicitly **not** retryable — retrying a revoked token
   consumes rate budget for nothing.
3. One transaction commits: `ig_accounts.connection_state='needs_reauth'`,
   `publishing_state='paused_by_system'`, `publishing_hold_reason='permission_expired'`;
   `queue_items.status` returns from `publishing` to `ready` with its position intact; the
   claimed `schedule_occurrences` row goes to `state='skipped_paused'`; a `notifications` row
   (`severity='critical'`, deep link to the account's reconnect page) is inserted; `pg_notify`
   fires so the open tab updates without a refresh.
4. The dispatcher's `WHERE` clause requires `connection_state='connected' AND
   publishing_state='active'`, so the remaining 39 slots for this account are materialized but
   immediately marked `skipped_paused` at dispatch time. Other accounts in the same workspace
   are untouched — the dispatcher iterates per account.
5. All 40 items stay `ready`. Nothing is deleted, nothing is marked failed.
6. The user reconnects through the normal OAuth flow. `store_ig_token()` verifies the returned
   `ig_user_id` matches the row, writes the new token, and sets `connection_state='connected'`,
   `publishing_state='active'`. The next materialized slot picks up item #1 in unchanged order.
7. Proactive avoidance: `token.refresh` cron runs daily and refreshes any credential with
   `token_expires_at < now() + interval '10 days'` via `GET /refresh_access_token?
   grant_type=ig_refresh_token`. At 3 failures it raises `needs_reauth` before a publish ever
   fails.

**Evidence:** `publish_attempts` shows `phase='failed_pre'`, `ig_error` containing code 190
and the `fbtrace_id`. `audit_log` shows `account.state_change` with before/after.
`schedule_occurrences` shows `skipped_paused` rows with their `slot_at`. The notification row
records what the customer saw and when they read it.

### 5.6 Quota exhaustion

**Scenario:** an account with `daily_allowance=25` has 25 publishes done by 16:00 and 12 more
slots today; separately, Instagram's own limit is reached at 50.

1. At the 26th dispatch, the conditional `INSERT ... ON CONFLICT DO UPDATE ... WHERE
   slots_consumed < 25` (§3.8) returns zero rows.
2. The dispatcher does not fail the item. It commits: the occurrence goes to
   `state='skipped_empty'` with detail `daily_allowance_reached`, the queue item stays
   `ready` at position 1, and a single `notifications` row is inserted with
   `dedup_key='quota:'||account||':'||local_date` — the unique index means 12 more deferrals
   today produce one notification, not twelve.
3. The account card shows "Daily limit reached (25/25). Next post: tomorrow 09:00" computed
   from the next `planned` occurrence after the local-date boundary.
4. Instagram's own limit is tracked separately. Before each publish, if
   `account_daily_usage.ig_quota_seen_at` is older than 30 minutes, `publish.run` calls
   `GET /{ig-user-id}/content_publishing_limit?fields=config,quota_usage` and stores
   `quota_usage`. If `quota_usage >= config.quota_total`, the dispatch defers exactly as in
   step 2 with detail `instagram_limit_reached` — a deferral, never a failure.
5. If Instagram returns HTTP 429 or `error.code=4`/`17` (application/user request limit) at
   any point, `classifyIgError()` yields `fail_class='account_limit'`, the attempt is
   `failed_pre`, and the item is deferred with an exponential account-level cooldown recorded
   as `ig_accounts.publishing_state='paused_by_system'`, `publishing_hold_reason=
   'rate_limited_until:<iso8601>'` — 15 min, then 60 min, then 4 h on consecutive hits.
6. Backlog does not grow without bound in the scheduler: `schedule.materialize` inserts at
   most 14 days ahead, and occurrences older than 24 h that are still `planned` are set to
   `expired` by the retention sweep, so the dispatcher's due-work query stays small.

**Evidence:** `account_daily_usage` row for the local date with `slots_consumed=25` and the
last observed `ig_quota_usage`/`ig_quota_seen_at`. `schedule_occurrences` rows in
`skipped_empty` with their detail. One `notifications` row. No `publish_attempts` row at all,
because no external call was made.

### 5.7 Schedule edit near a slot

**Scenario:** a slot is due at 10:00:00. At 09:59:58 the user deletes the rule that produced
it and adds a rule at 10:05.

1. `schedule.dispatch` runs every 60 s. Its claim transaction is:
   `UPDATE schedule_occurrences SET state='claimed', claimed_queue_item_id=$item,
   claimed_at=now() WHERE id=$occ AND state='planned' RETURNING id`, in the same transaction
   as the queue-item lease and the daily-usage increment.
2. The rule edit runs `schedule.materialize` for the account. Materialization deletes only
   `state='planned'` rows with `slot_at > now() + interval '5 minutes'`. The 10:00 occurrence
   is inside the skirt, so it is **not** deleted regardless of which transaction commits
   first.
3. Case A — dispatch commits first: the occurrence is `claimed`, outside the delete predicate
   twice over. The post goes out at 10:00 as previewed. The 10:05 rule produces its first
   occurrence tomorrow-onwards plus today at 10:05 if that instant is still ≥ `now()+5min`;
   here it is not (09:59:58 + 5 min = 10:04:58 < 10:05, so it *is*), and it is inserted.
4. Case B — materialize commits first: the 10:00 occurrence survives the skirt, dispatch
   claims it at 10:00, and the item publishes. The user sees both the 10:00 run and the new
   10:05 rule in the upcoming-runs preview, which is regenerated from `schedule_occurrences`,
   not from the rules — so the preview cannot disagree with what will happen.
5. There is no window in which both the old and the new rule produce a run for the same item:
   the item is claimed by exactly one occurrence, enforced by `schedule_occ_claim` unique
   index on `claimed_queue_item_id`.
6. The upcoming-runs preview always states the skirt explicitly: "Changes take effect for
   slots more than 5 minutes away. The 10:00 run is already locked in."

**Evidence:** `schedule_occurrences` rows with `state`, `claimed_at`, `claimed_queue_item_id`;
`audit_log` `schedule.rule.update` with the before/after rule body and the count of deleted
planned occurrences; the preview endpoint's response is reproducible by re-querying the same
table.
### 5.8 Duplicate source collection

**Scenario:** a source's poll interval and a manual "collect now" run at the same instant, and
the source account has reposted a Reel that was already imported last month.

1. Both polls enqueue `source.poll` with `job_key='source.poll:<source_id>'` and
   `job_key_mode='preserve_run_at'`. Graphile Worker collapses them into one job row, so only
   one poll executes. (If the first is already `locked`, the second becomes a fresh job and
   the `queue_name='source:<source_id>'` serializes it behind the first.)
2. The poll collects up to `candidates_per_run` items and, for each, runs
   `INSERT INTO source_items (...) ON CONFLICT (ig_account_id, source_media_id) DO NOTHING
   RETURNING id`. The reposted Reel has the same `source_media_id`; zero rows returned; the
   run counts it in `source_runs.candidates_seen` but not `candidates_new`.
3. A repost with a *different* `source_media_id` but identical bytes is caught later:
   `source.fetch` downloads the media, computes SHA-256, and the insert of
   `media_assets`/the update of `source_items.media_sha256` hits
   `source_items_dedup_by_bytes`. The item goes to `state='rejected'`,
   `hold_reason='duplicate_bytes'`, and its downloaded object is deleted.
4. Filters run before eligibility and their outcome is recorded rather than discarded: an item
   failing `min_likes` is inserted with `state='held'`, `hold_reason='below_min_likes'`.
   Held items are visible in a separate "Filtered out" tab, which is the brief's "items held
   back by filters remain distinguishable from items ready to fill the queue."
5. Refill from backlog is `source.refill` on `queue_name='refill:<account>'`. It takes
   `SELECT ... FROM ig_accounts WHERE id=$1 FOR UPDATE`, counts current depth, and inserts at
   most `target - depth` queue items, each through `queue_items_dedup`. Two concurrent refills
   serialize on the row lock, so they cannot both fill the last slot.
6. If verification of the source has never succeeded, the source is `pending_verification` and
   `source.poll` exits immediately — an unverified source never contributes candidates.

**Evidence:** `source_runs` rows with `candidates_seen`, `candidates_new`, `held_counts`
(`{"below_min_likes":4,"excluded_word":1,"too_old":2}`), and `outcome`. `source_items` rows
carry `source_media_id`, `source_permalink`, `source_author`, `discovered_at`, `state`, and
`hold_reason`, which is also the provenance record the brief requires.

### 5.9 Browser hang during cleanup

**Scenario:** Chromium hangs after clicking "Move to Recently Deleted" on a Reel and before
the confirmation dialog resolves.

1. `cleanup.run` holds the run lease (`cleanup_runs.lease_token`, `lease_expires_at =
   now()+1800s`, heartbeat every 300 s) and processes item `ordinal=7`.
2. Before the click, one transaction commits: `cleanup_run_items.phase='sent'`,
   `boundary_crossed_at=now()`. `cleanup_items_one_past_boundary` now blocks any other item of
   this run from being sent.
3. The Playwright call is wrapped in `page.setDefaultTimeout(30_000)` with a whole-item budget
   enforced by `Promise.race([action, setTimeout(90_000)])`. At 90 s the item is still
   unresolved.
4. The worker captures evidence *before* tearing down: `page.screenshot()` to R2 under
   `w/<ws>/cleanup/<run>/<item>.png`, `page.url()`, and the presence/absence of a set of named
   selectors. `browserContext.close()` then `browser.close()` release ~800 MB.
5. One transaction commits: `cleanup_run_items.phase='uncertain'`, `evidence={screenshot_key,
   url, selectors_seen}` (passed through `redactReceipt()` so no cookie or storage value can
   enter), `cleanup_runs.state='paused_reconcile'`, a `notifications` row with
   `severity='critical'`, and `graphile_worker.add_job('cleanup.reconcile', ...,
   queue_name => 'cleanup:'||ig_account_id, run_at => now()+120s)`.
6. Because the run stays in `paused_reconcile`, `cleanup_runs_one_live` blocks any new run for
   the account, and because reconciliation sits at the head of `cleanup:<account>`, every
   later cleanup job for that account waits behind it. Publishing on the same account is
   unaffected — it runs on `publish:<account>`.
7. `cleanup.reconcile` opens a fresh context and reads the post's current state: it navigates
   to `posts.permalink` and to the account's Recently Deleted view. Three outcomes: post
   absent from the grid and present in Recently Deleted → `phase='confirmed'`,
   `posts.lifecycle='trashed'`; post still live → `phase='failed'` and the item is returned to
   `pending` for the *next* run, not retried inside this one; indeterminate after 3 checks
   spaced 2 min / 10 min / 60 min → the run stays `paused_reconcile` and the operator resolves
   it with an attributable action.
8. Nothing in this path retries the destructive click automatically. That is the hard rule "if
   the product crashes near an archive or delete action, it must not repeat an uncertain
   action automatically."

**Evidence:** `cleanup_run_items` row 7 with `phase`, `boundary_crossed_at`, `verified_at`,
and `evidence` including the screenshot key; `cleanup_runs.state='paused_reconcile'`;
`audit_log` `cleanup.reconcile.check` per probe; `receipts` gets a `trash` row only when
`phase='confirmed'`.

### 5.10 Changed cleanup selection

**Scenario:** the user previews and confirms a 12-item cleanup at 14:00. At 14:03, before the
run starts, a collaborator protects two of those posts and fresh analytics lift a third above
the threshold.

1. Confirmation stored `frozen_rule`, `frozen_rule_sha256`, and `selection_sha256 =
   sha256(sorted post_ids ‖ each post's metric tuple ‖ metrics_collected_at)`, plus 12
   `cleanup_run_items` rows with their `metrics_used`.
2. The collaborator's protect action is `UPDATE posts SET protected=true, protected_by=...,
   protected_at=now()`, plus an `audit_log` row.
3. `cleanup.run` starts with a precheck that recomputes the selection from current data:
   credentials valid, `connection_state='connected'`, `automation_sessions.state='valid'`,
   fresh analytics within `metrics_max_staleness_hours`, `posts.protected=false`, and no other
   live run.
4. The recomputed selection yields 9 posts, so the recomputed hash differs from
   `selection_sha256`. The run commits `state='aborted_selection_changed'`,
   `abort_reason='2 posts protected, 1 no longer qualifies'`, and a notification with a deep
   link back to a fresh preview. **Zero** browser actions occur.
5. The user re-previews. A new `cleanup_runs` row is created; the aborted one is retained as
   history.
6. For a **scheduled** occurrence there is no prior confirmation to invalidate, so the rule
   itself is the standing authorization: `cleanup.scheduled` builds the selection at run time,
   applies the same precheck, and additionally refuses if the freshest analytics snapshot for
   any candidate is older than `metrics_max_staleness_hours` — it aborts with
   `state='aborted_precheck'` and enqueues `analytics.collect` for those posts rather than
   acting on stale numbers.

**Evidence:** the aborted `cleanup_runs` row with both hashes and `abort_reason`; the 12
`cleanup_run_items` rows with the metrics that were used at confirmation; `audit_log`
`post.protect` rows naming the collaborator and time; the new run's `selection_sha256`
differing from the old one.

### 5.11 Repeated or out-of-order billing events

**Scenario:** Stripe delivers `customer.subscription.updated` (downgrade) before
`checkout.session.completed` (upgrade), then redelivers both twice, and an
`invoice.payment_failed` arrives during a network partition.

1. Every delivery hits `POST /api/webhooks/stripe`, which first verifies the signature with
   `stripe.webhooks.constructEvent(rawBody, sig, endpointSecret)` using the **raw** body
   (Next.js route handler configured to read the body as a `Buffer`, not parsed JSON).
   A bad signature returns `400` and writes nothing.
2. `INSERT INTO stripe_events (event_id, ...) ON CONFLICT (event_id) DO NOTHING RETURNING
   event_id`. Redeliveries return zero rows → the handler returns `200` immediately and does
   no work. This is the entire replay defence, and it is a primary key, not a check.
3. New events enqueue `billing.sync` with `job_key='billing.sync:'||workspace_id` and
   `job_key_mode='preserve_run_at'`, so a burst of 6 events for one workspace collapses to one
   sync job.
4. `billing.sync` **ignores the event payloads**. It calls
   `stripe.customers.retrieve(customer_id)` and `stripe.subscriptions.list({customer, status:
   'all', limit: 10, expand: ['data.items.data.price']})` and derives the current state from
   the live objects. Order of arrival therefore cannot matter: the last sync to run reads the
   same authoritative truth as any other.
5. The write is guarded for the case where two syncs interleave:
   `UPDATE subscriptions SET plan_code=..., status=..., ..., last_event_created_at=$evt_created
   WHERE workspace_id=$1 AND (last_event_created_at IS NULL OR last_event_created_at <=
   $evt_created)`. An older sync landing late updates nothing.
6. Effect on product access is derived, never stored twice: `effective_entitlements` (§3.5) is
   a view over `subscriptions`, `plans`, and `entitlement_grants`. A downgrade below the
   connected-account allowance runs `applyPlanLimits()`, which sets the **excess** accounts
   (most recently connected first) to `publishing_state='held_over_plan'`. It does not
   disconnect them, does not delete queue items, and does not touch history.
7. `invoice.payment_failed` moves `status` to `past_due`. `past_due` holds publishing after a
   7-day grace (`ig_accounts.publishing_state='held_over_plan'`, reason `payment_past_due`)
   and never deletes anything. Recovery is a single successful invoice → next `billing.sync`
   → `status='active'` → `applyPlanLimits()` releases the holds.
8. Partition case: if the webhook endpoint is unreachable, Stripe retries for up to 3 days.
   Independently, `billing.reconcile` cron runs hourly over workspaces with
   `last_synced_at < now() - interval '6 hours'` and syncs them, so state converges even if
   every webhook is lost.

**Evidence:** `stripe_events` shows each `event_id` once with `outcome` and `processed_at`;
duplicates never appear because the primary key rejected them. `subscriptions.last_synced_at`
and `last_event_created_at` show convergence. `audit_log` `billing.plan_change` shows
before/after. `receipts` of kind `charge` carry the Stripe invoice id as `external_ref`.

### 5.12 Storage failure

**Scenario:** R2 returns `503` for 40 minutes, spanning uploads, transcodes, previews, and a
publish.

1. **Uploads:** the browser's presigned `PUT` fails. The uploader retries each part 5 times
   with backoff 1 s/2 s/4 s/8 s/16 s, then surfaces "Instagram media storage is unavailable —
   your file is safe on this device; press Resume." The `media_assets` row stays
   `awaiting_bytes`; the multipart `UploadId` is retained, so Resume re-uploads only the
   missing parts.
2. **Transcodes:** `media.transcode` fails on `GetObject` or `PutObject`. The error is
   classified `external_temporary`; Graphile Worker retries at 15 s/30 s/60 s/120 s/240 s.
   After 5 attempts the queue item becomes `prep_failed` with `fail_class='external_temporary'`
   and a visible "Retry preparation" control. The asset is not rejected and the original bytes
   are untouched.
3. **Previews:** `getSignedUrl` itself never contacts R2 (it is a local HMAC computation), so
   URL minting keeps working; the `<img>` request fails and the preview component renders its
   `error` state with a retry, not a broken image.
4. **Publish:** `publish.run` mints the 3600 s presigned URL and passes it to Instagram's
   container create. Instagram's fetcher fails and the API returns `error.code=9004`
   (media fetch failure). `classifyIgError()` maps it to `external_temporary`; the attempt is
   `failed_pre` (no boundary crossed), and retry attempts 2–5 follow the pre-boundary schedule.
   No duplicate publish is possible because no publish was sent.
5. **Total, permanent loss of R2:** originals are replicated nightly to Hetzner Object Storage
   by `rclone sync --checksum --transfers 8` (300 GB/month of new objects → €1.80/mo storage,
   Hetzner ingress free). `media_assets.original_key` is provider-relative, and a
   `STORAGE_PRIMARY=hetzner` environment flip repoints the S3 client; a `media.reindex` task
   verifies `HeadObject` for every non-purged asset and marks unrecoverable ones
   `state='purged'` with a customer-visible notice. Published posts are unaffected: they live
   on Instagram and the receipt holds the permalink.
6. `health_signals.r2_put_error_rate` reaches `alarm` at 10% over 15 minutes, and the operator
   status page shows "stored media: degraded" — derived from that signal, not asserted.

**Evidence:** `media_assets.state` and the retained multipart `UploadId`; `graphile_worker`
failure rows with `last_error`; `publish_attempts` rows with `ig_error.code=9004` and
`boundary_crossed_at IS NULL`; the nightly `rclone` log and the `media.reindex` report row
count.

### 5.13 Deletion interrupted halfway

**Scenario:** a workspace deletion is running; the worker is SIGKILLed after Instagram access
is revoked and media purge is 60% done.

1. `POST /api/workspace/:id/delete` (owner only, re-authentication with password required,
   `Idempotency-Key` required) creates `deletion_requests` with `state='grace'`,
   `grace_until=now()+7 days`, `reference='DEL-…'`, sets `workspaces.status='deletion_pending'`
   (which suspends all activity through the `forbiddenFlags` path), and emails the reference.
2. At `grace_until`, `deletion.run` moves to `state='running'` and executes ordered steps,
   each committing its own `steps` key on completion:
   1. `revoke_ig` — `DELETE /{ig-user-id}/permissions` for each connected account, then delete
      `ig_account_credentials` and `automation_sessions` rows.
   2. `cancel_billing` — `stripe.subscriptions.cancel(id, {prorate: false})`, or no-op if none.
   3. `purge_media` — page over `media_assets` and `media_variants` in batches of 500,
      `DeleteObjects` on R2 and on the Hetzner replica, then set `state='purged'` and null the
      keys. Progress is recorded as `steps.purge_media = {"cursor":"<asset_id>","done":54120}`.
   4. `purge_rows` — delete customer rows in FK order; `audit_log` and `receipts` rows are
      **rewritten** rather than deleted: personal fields are set to NULL and `subject_id` is
      replaced by its HMAC, so the proof that an action occurred survives without the person.
   5. `finalize` — insert `deletion_receipts` (reference, `subject_id_hmac`, counts), delete
      the `users`/`workspaces` rows, set `deletion_requests.state='completed'`.
3. SIGKILL during step 3. The transaction in flight rolls back; `steps` still records
   `{"revoke_ig":"done","cancel_billing":"done","purge_media":{"cursor":"…","done":54120}}`.
4. Graphile Worker retries `deletion.run` (`max_attempts=50`, backoff capped at 300 s). The
   task reads `steps` and re-enters at `purge_media` from `cursor`. Steps 1 and 2 are skipped
   because they are marked `done`; re-running them would be harmless anyway — `DELETE
   /permissions` on an already-revoked grant returns success, and cancelling an already-
   cancelled subscription is a no-op — but the marker avoids the calls.
5. Object deletion is idempotent by nature: `DeleteObjects` on a missing key returns success,
   so partial progress plus a full re-scan of the remaining page cannot fail.
6. If a step fails 50 times (e.g. Instagram permanently returns 500 on revoke), the request
   moves to `state='failed'`, the operator console shows exactly which step and the raw error,
   and the customer-visible status page for the reference says "in progress — support has been
   notified", never "completed".
7. Cancellation during grace is a single `UPDATE deletion_requests SET state='canceled'
   WHERE reference=$1 AND state='grace'`; after grace it is impossible, and the UI says so
   before the request is made.

**Evidence:** `deletion_requests.steps` shows the exact resume point and per-step completion;
`deletion_receipts` holds counts and the non-reversible subject HMAC; `audit_log` retains
`workspace.delete.requested` and each `deletion.step.completed` with actor and time; the R2
bucket returns `404` for a sample of the purged keys, which the operator can check with the
reference alone.
---

## 6. AI strategy

**Decision: no AI model — hosted or local — is part of ToolBox Poster v1.** No caption
generation, no media generation, no automated content moderation, no support agent. The
product ships zero inference calls.

Two candidate roles were evaluated seriously, and both are rejected on arithmetic and on
liability, not on taste.

### 6.1 Candidate A — caption assistance

The brief's §5 non-goals already exclude automatic caption generation absent a compelling
case. The case does not exist here: the customer supplies the caption, and the product's
promise is that it publishes **the caption the customer reviewed**. Inserting a generator
between the user and `frozen_caption` adds a source of variance to the one field the entire
freeze machinery exists to protect. Cost if built anyway: 1,000 caption drafts/day at 600 in /
200 out tokens with Claude Haiku 4.5 ($1.00/MTok in, $5.00/MTok out) = 1,000 × (0.0006 ×
$1.00 + 0.0002 × $5.00) = $1.60/day = **$48/month**, or 29% of the fixed infrastructure bill,
to produce text the user rewrites. Rejected.

### 6.2 Candidate B — automated screening of restricted-source media

This is the genuinely tempting one: restricted sourcing pulls third-party media into a
customer's account, and publishing something prohibited is a platform-policy and legal event.
A vision model scoring each candidate for nudity, violence, watermarks, and detectable brand
marks looks like a control.

Cost, multiplied out against the scale in §2.4: restricted sourcing is entitled and expected
on ≤ 20 tester workspaces at launch, collecting `candidates_per_run=10` at
`poll_interval_minutes=360` — 20 workspaces × 2 sources × 4 polls/day × 10 candidates = **1,600
screenings/day**. A single 1080×1920 frame is ~1,600 image tokens; add 200 text tokens in and
150 out. Per screening with Claude Haiku 4.5: (1,800 × $1.00 + 150 × $5.00) / 1,000,000 =
$0.00255. Daily: $4.08. **Monthly: $122.40** — 73% of the fixed infrastructure bill, for a
feature serving 20 workspaces. Video requires sampling frames (3 frames/clip triples it to
$367/month).

Rejected on two grounds:

1. **It does not remove the obligation it appears to remove.** A classifier's output is
   probabilistic; the legal exposure of republishing third-party media is about *rights*, not
   about depicted content, and no model determines whether the customer has the right to
   repost a Reel. The controls that actually address the risk are deterministic and are in the
   plan already: an operator-approved source pool, `sources.trust` gating auto-refill until an
   operator has reviewed 20 items from that source, mandatory `policy_acceptances` per queued
   item, retained provenance (`source_items.source_permalink`/`source_author`), and a backlog
   that never publishes without a human refill action for untrusted sources.
2. **Cost per protected workspace is indefensible at this scale.** $122/month across 20
   workspaces is $6.12/workspace/month for a control that still requires the human review it
   was meant to replace.

### 6.3 Deterministic substitutes actually shipped

| Need AI was considered for | Deterministic mechanism in this plan |
|---|---|
| Caption safety words | `sources.exclude_words TEXT[]` matched case-insensitively against `source_caption` before eligibility; hold reason `excluded_word` |
| Duplicate/repost detection | `source_items_dedup_by_id` and `source_items_dedup_by_bytes` (SHA-256), plus `queue_items_dedup` |
| Media validity | `ffprobe` against Instagram's published constraints, with the raw probe retained |
| Which post to cleanup | Explicit numeric thresholds in `cleanup_rules` against `analytics_snapshots`, with `metrics_used` frozen per item |
| Support triage | `feedback.page_context` carries route, entity ids, and app version, and the operator work-item view (§8 P6) resolves any id to its full history |

### 6.4 The trigger that would revisit this

One condition, stated so it is falsifiable: if restricted sourcing exits the tester program to
more than 200 workspaces, the screening volume rises to 16,000/day ($1,224/month), at which
point a **self-hosted** classifier on the existing `worker-media-1` GPU-less CPU budget is
evaluated instead of a hosted model, and the decision is re-made with measured precision and
recall against a labelled set of 2,000 items. That evaluation is not v1 work.

---

## 7. Testing and release confidence

### 7.1 On every change (pull request, ~7 minutes)

Runs on GitHub Actions, `ubuntu-latest`, no external network beyond the package registry.

1. `tsc --noEmit` across the workspace; `eslint` including the custom rules
   `no-raw-pool-in-domain` and `mutating-route-requires-idempotency`.
2. `vitest run` unit suites: state machines (`canRetry`, `classifyIgError`, `cancelPublish`
   phase matrix), `dedupKey`, `redactReceipt`, caption assembly, `recipe_sha256` construction,
   fractional-position math, Luxon slot materialization including the two DST dates.
3. Integration suites against `@testcontainers/postgresql` PostgreSQL 17 with the real
   migrations applied: every `*.spec.ts` named in §4. This includes `grants.spec.ts::role_matrix`
   and `tenancy.spec.ts::rls_blocks_read`, which enumerate tables from `pg_class`, so adding a
   workspace table without a policy or a declared grant fails the build.
4. `apps/web` build, then Playwright end-to-end against the built app + Testcontainers
   Postgres + `ig-sim` + MinIO (S3-compatible, standing in for R2) + `stripe-mock`.
5. `axe-core` accessibility assertions on 18 routes (`a11y.spec.ts`).
6. Migration safety: `graphile-migrate` applies to a clone of the previous release's schema,
   and a check rejects any migration containing `DROP COLUMN`, `DROP TABLE`, or a
   non-`CONCURRENTLY` `CREATE INDEX` without an `-- @allow-destructive` marker reviewed by a
   human.

### 7.2 Requires real supporting services (merge to `main`, ~14 minutes)

Runs against a persistent staging environment on a single CX32.

- Real Cloudflare R2 bucket (`tbp-media-staging`): multipart upload of a 900 MB file,
  presigned GET expiry at 300 s, `DeleteObjects` idempotency on missing keys.
- Real Stripe test mode: checkout → webhook → `billing.sync` → entitlement change, including
  the `shuffled_replay` suite driven by `stripe trigger` and by replaying captured event
  bodies out of order.
- Real Postmark sandbox: invitation, deletion reference, and critical-notification emails.
- Real Instagram **only** through `ig-sim` at this stage; no live account is touched by CI.

### 7.3 Nightly drills (`drills/*.ts`, against staging)

Each drill is a program that asserts a post-condition and exits non-zero on failure; a failure
pages via Pushover.

| Drill | What it does | Assertion |
|---|---|---|
| `crash_pre_publish` | SIGKILL `worker-core` 200 ms after container create | Item publishes exactly once on resume; `boundary_crossed_at` was NULL at kill time |
| `uncertain_publish` | `X-Sim-Fault: timeout_after_accept`, then SIGKILL | Item ends `needs_review`; exactly 1 `media_publish` recorded; retry endpoint returns 409 |
| `reconcile_resolves` | As above, then `ig-sim` reports `status_code=PUBLISHED` | Attempt reaches `publish_confirmed`; one `posts` row; one `receipts` row |
| `race_publish` | 20 concurrent `publish.run` for one item across 4 processes | 1 post, 19 × SQLSTATE 23505 |
| `race_refill` | 8 concurrent refills, 5 eligible items, target 10 | Exactly 5 queue items |
| `quota_restart` | 24/25 published, SIGKILL all workers, restart | 26th dispatch defers; `slots_consumed=25` |
| `rate_limit_429` | `ig-sim` returns 429 for 10 minutes | Account enters cooldown 15 min; zero items marked `failed`; all return to `ready` |
| `dst_spring` / `dst_fall` | Clock injected at `Europe/Berlin` 2026-03-29 and 2026-10-25 | 1 skipped occurrence / exactly 1 occurrence; zero double publishes |
| `tz_change` | Change account timezone with a claimed slot pending | Change refused; with no claimed slot, preview matches materialized rows |
| `partition_db` | Toxiproxy cuts worker→Postgres for 90 s mid-publish | No duplicate publish; lease expiry respected; item resolves |
| `partition_ig` | Toxiproxy adds 120 s latency to `ig-sim` | Publish timeout classified `uncertain`, not `failed` |
| `restore` | pgBackRest `restore --delta --type=time` of last night's backup into a scratch VM | Row counts within 1 minute of RPO for 8 named tables; 100 random media keys resolve; `SELECT` of a known receipt matches |
| `cleanup_hang` | `ig-sim`-backed cleanup harness hangs mid-action | Run ends `paused_reconcile`; zero automatic repeats; screenshot evidence present |
| `deletion_kill` | SIGKILL mid `purge_media` | Resume from cursor; final counts match pre-deletion counts |
| `media_starvation` | Enqueue 300 transcodes, then publish 20 items | All 20 publish within 3 minutes; median publish latency change < 15% |

### 7.4 Proven against safe test Instagram accounts (weekly, and gating each phase exit)

Two dedicated professional Instagram accounts (`@tbp_test_a`, `@tbp_test_b`) on a Meta app in
development mode, connected to a `staging` workspace. Nothing customer-facing touches them.

- `live_publish_image` and `live_publish_reel`: publish, assert `posts.permalink` resolves
  with HTTP 200, assert `receipts` content, then delete the post through the account UI.
- `live_token_refresh`: force a refresh and assert `token_expires_at` moves forward.
- `live_revocation`: revoke the app from Instagram settings, assert the next publish yields
  code 190, the account enters `needs_reauth`, and the queue is intact; then reconnect and
  assert the same `ig_accounts.id` and unchanged queue.
- `live_publishing_limit`: read `content_publishing_limit` and assert the parsed
  `quota_usage` matches the count of posts made this cycle.
- `live_insights`: publish, wait 65 minutes, assert an `analytics_snapshots` row with
  `age_bucket='1h'` and a non-empty metric set, and that any metric Instagram omits appears in
  `missing_metrics`.
- `live_cleanup` (before Phase 8 exit only): publish a throwaway image and a throwaway Reel to
  `@tbp_test_b`, run a cleanup rule that selects exactly those two, and assert the photo is
  archived and the Reel is in Recently Deleted, with `receipts` rows for both.

Weekly cadence is chosen because the publishing limit is 50/24 h per account and these suites
consume 4–6 posts; running them per-commit would exhaust the test accounts' real quota and
make CI depend on an external system's availability.

### 7.5 Restore rehearsal

The `restore` drill runs nightly and is also a **Phase 0 exit criterion**, before a single
customer row exists. `health_signals.restore_drill_age_days` alarms at 14. The drill restores
to a scratch CX32, runs `pg_amcheck --all --heapallindexed`, compares row counts for `users`,
`workspaces`, `ig_accounts`, `queue_items`, `posts`, `receipts`, `analytics_snapshots`,
`audit_log` against the primary at the restore timestamp, and verifies 100 randomly sampled
`media_variants.object_key` values with `HeadObject` against both R2 and the Hetzner replica.

### 7.6 Release mechanics

Deploys are `docker compose pull && docker compose up -d` driven by a GitHub Actions job over
SSH, web first (two hosts, one at a time behind the load balancer's health check), workers
second. Workers receive SIGTERM and Graphile Worker completes the in-flight job before
exiting, with a 240 s `stop_grace_period` (longer than the 60 s publish timeout plus the 90 s
container-poll tail). Migrations run before the web deploy and are additive-only within a
release; a column is dropped no earlier than the release after the one that stopped writing it.
Rollback is `docker compose up -d` with the previous image tag; because migrations are
additive, the previous image runs against the new schema.

---

## 8. Delivery phases

No dates and no durations. Each phase closes on statements that are true or false.

### Phase 0 — Foundation, tenancy, and provable restore

Terraform for the 6 VMs, Compose units, PostgreSQL 17 with the 5 database roles and PgBouncer,
`graphile-migrate`, CI pipeline of §7.1, Sentry + Alloy, the `users`/`sessions`/`workspaces`/
`workspace_members`/`invitations`/`audit_log`/`idempotency_keys` schema with RLS policies and
grants, email+password auth, the invitation gate, the `<AsyncBoundary>` component contract,
and pgBackRest with the restore drill.

**Exit criteria:** an invited user creates a workspace and no uninvited request can create a
user (`signup.spec.ts` green) · `tenancy.spec.ts::rls_blocks_read` passes for every table in
`pg_class` marked workspace-scoped · `grants.spec.ts::role_matrix` matches the checked-in
matrix · a deploy of two web hosts completes with zero failed health checks · the `restore`
drill restores last night's backup to a scratch VM and reports matching row counts and
`pg_amcheck` clean · every privileged action performed so far appears in `audit_log` with an
actor.

*Justification for preceding the end-to-end phase:* every later invariant is expressed as a
grant, a policy, or a constraint in this schema; building publishing first would mean
retrofitting isolation, which is the one thing this plan cannot retrofit credibly. The restore
rehearsal is here rather than later because it is cheap with an empty database and because a
first customer must never be the first test of it.

### Phase 1 — Instagram connection and health

OAuth through Instagram Login, `store_ig_token()` with envelope encryption, the
one-live-connection index, `token.refresh` cron, connection health surface, disconnect and
reconnect, `account_requests` with the operator/customer note split, account settings
(timezone, daily allowance, prep profile).

**Exit criteria:** `@tbp_test_a` connects and its identity, profile image, and health render ·
connecting the same account into a second workspace returns 409 and writes no token ·
`app_web` cannot read `ig_account_credentials` (grant assertion green) · revoking the app in
Instagram settings flips the account to `needs_reauth` within one `token.refresh` cycle ·
disconnect then reconnect returns the same `ig_accounts.id`.

*Justification:* there is nothing to upload for and nothing to schedule against without an
account, and the credential-isolation grants must be in place before any token exists.

### Phase 2 — Media ingest and preparation

Presigned single and multipart upload, `media.probe`, `media.transcode` with the explicit
argv, `media_variants` idempotency, the reject-code taxonomy with customer-readable messages,
originals retained, preview rendering, progress that survives navigation.

**Exit criteria:** a 900 MB `.mov` uploads, transcodes, and previews · closing the tab
mid-upload and pressing Resume completes without re-uploading finished parts · a 20-minute
clip is rejected with `too_long` and a message naming the limit · three forced transcode
failures followed by a success produce exactly one `media_variants` row · the original object
is downloadable by its owner and returns 401 unsigned.

*Justification:* preparation is the slowest and least predictable step; proving it before it
sits on the critical path of a publish keeps the first end-to-end run from failing for reasons
unrelated to publishing.

### Phase 3 — Queue, schedule, publish, receipt, analytics ⟵ **first end-to-end**

This is the earliest phase that completes connect → upload → prepare → queue → publish →
receipt → analytics against a safe test account. It adds `queue_items` with the freeze,
fractional ordering and reorder concurrency, `schedule_rules`/`schedule_occurrences` with the
DST handling, `schedule.dispatch`, `publish.run` with the attempt ledger and boundary
discipline, `publish.reconcile`, `account_daily_usage`, `posts`, `receipts`, and
`analytics.collect` on the §2.7 curve.

**Exit criteria:** an image and a Reel publish to `@tbp_test_a` from the queue on a schedule,
each producing a `posts` row whose permalink returns HTTP 200 and a `receipts` row containing
the frozen hash · `race.spec.ts::twenty_concurrent_publishes` yields exactly one post ·
`drills/uncertain_publish` leaves the item in `needs_review` with the retry endpoint returning
409 · `drills/quota_restart` shows the counter surviving a kill · `dst.spec.ts` passes for both
2026 Berlin transitions · an `analytics_snapshots` row with `age_bucket='1h'` exists for both
live posts, with `missing_metrics` populated for anything Instagram omitted · changing account
settings after freeze does not change the published caption.

### Phase 4 — Live status, notifications, and the resilience surface

SSE + LISTEN/NOTIFY, notification model with dedup keys and deep links, the six hold states
and their customer-visible copy, pause/resume, cancel with the phase matrix, retry with the
pre/post-boundary distinction, bulk queue actions, hide/restore/remove.

**Exit criteria:** a publish transition appears in an open tab within 2 s with no refresh ·
`isolation.spec.ts::sse_scope` shows zero cross-workspace bytes · all six hold states are
reachable in a test and each shows a distinct explanation and a recovery path ·
`cancel.spec.ts::phase_matrix` passes · deferral for a reached daily limit produces exactly one
notification per account per local date.

### Phase 5 — Plans, billing, entitlements

Stripe products and prices, Checkout, Customer Portal, webhook with `stripe_events`,
`billing.sync`, `billing.reconcile` cron, `plans`, `effective_entitlements`,
`applyPlanLimits()`, billing pages, seat counting.

**Exit criteria:** `billing.spec.ts::shuffled_replay` produces an identical final subscription
row across 30 shuffled, triplicated replays · a downgrade below the account allowance sets the
excess accounts to `held_over_plan` and deletes nothing (row counts asserted before and after)
· a non-owner receives 403 on checkout, portal, and plan-change endpoints · `past_due` holds
publishing after the 7-day grace and a successful payment releases it · every plan limit is
enforced server-side with the UI control removed *and* the endpoint denied.

### Phase 6 — Operator console and support

Separate admin hostname with WebAuthn, waitlist and invitation management, connection requests,
user/workspace suspension, temporary entitlement grants, feedback inbox, the work-item
inspector (given any id, show state, attempt history, whether retry is safe, and what the
customer has seen), safe-action buttons, the health screen fed by `health_signals`.

**Exit criteria:** the operator resolves a `needs_review` item to published and to
not-published, each writing an attributable `audit_log` row · the operator suspends and
un-suspends a workspace and the queue is byte-identical afterwards · the work-item inspector
renders the full history for a publish, a cleanup item, and a source item · every operator
read of customer media writes an `audit_log` row · the admin host rejects a valid customer
session cookie.

### Phase 7 — Public site, legal, export, deletion

Marketing pages, pricing, waitlist, privacy/terms/copyright/security-contact/data-deletion
pages, onboarding questionnaire, personal and workspace settings, leave workspace, data
export, deletion with grace and resumable steps.

**Exit criteria:** all five legal pages are reachable without a session and are linked from the
footer and from signup · an export produces a ZIP containing every original asset, all
receipts, and the full queue and post history, downloadable through a 24-hour presigned URL ·
`drills/deletion_kill` resumes from its cursor and completes · after completion, a sampled R2
key returns 404 and `deletion_receipts` holds counts with no reversible identifier.

*This phase precedes any real customer, per the brief's "public before real customers are
onboarded."*

### Phase 8 — Restricted sourcing (entitled)

`automation-1` build-out, session capture through the streamed remote browser, proxy pool,
`sources`/`source_items`/`source_runs`, verification, filters, backlog, manual refill,
trust-gated auto-refill, one-time Reels sample, the entitlement gate.

**Exit criteria:** all 18 restricted endpoints return 404 for an unentitled workspace, and the
corresponding tasks make zero outbound requests · `dedup.spec.ts::concurrent_refill` passes ·
revoking the entitlement stops new acquisition while every previously accepted item, its
provenance, and its queue position remain · `app_automation` fails a `SELECT` on `users`,
`sessions`, `subscriptions`, and `receipts` · powering off `automation-1` leaves publishing
latency unchanged (measured over 200 publishes) · a source in each of the five states renders
its own explanation.

### Phase 9 — Managed cleanup (entitled)

`cleanup_rules`, preview with per-item justification, confirmation with `selection_sha256`,
one-at-a-time execution with stop, scheduled occurrences with the precheck,
`cleanup.reconcile`, protection, cleanup history with redacted evidence.

**Exit criteria:** `live_cleanup` archives a real feed photo and moves a real Reel to Recently
Deleted on `@tbp_test_b`, with `receipts` for both · a selection changed between confirmation
and execution aborts with zero browser actions · `drills/cleanup_hang` ends in
`paused_reconcile` with no automatic repeat and a stored screenshot · a second run for the same
account cannot start while one is live (23505 asserted) · `redaction.spec.ts` confirms no
session material reaches `cleanup_run_items.evidence` or `receipts` · "Stop" prevents the next
item and the UI states that it cannot reverse the previous one.

### Phase 10 — Launch readiness

Invite the first 25 waitlist users. Load rehearsal at 3× peak (3,675 publishes/day simulated
against `ig-sim`), a full failover exercise (restore into a fresh VM and repoint), and the
operator runbook.

**Exit criteria:** 3× peak sustained for 2 hours with publish p95 under 90 s from slot to
receipt and zero duplicates · the restore-and-repoint exercise brings the product back with the
operator following only the written runbook · all 13 nightly drills green for 7 consecutive
nights.
---

## 9. Security and privacy

**9.1 Identity.** Email + password, argon2id (`m=19456 KiB, t=2, p=1`). Session token 32 random
bytes; only `sha256(token)` is stored. Cookie `HttpOnly; Secure; SameSite=Lax; Path=/`,
30-day absolute expiry, 7-day idle expiry, rotated on privilege change. Login rate limits per
IP (10/15 min) and per email (5/15 min) from `rate_buckets`. Password reset tokens are
single-use, 30-minute expiry, stored hashed, and invalidate all sessions on use.

**9.2 Authorization.** One function, `can(actor, action, subject, tx)`, backed by the role
matrix in §3.1 and `effective_entitlements`. It is called inside the request transaction, so
the decision and the write share a snapshot. Route registration requires a declared action;
`authz.spec.ts::every_route_declares` fails the build for a route without one. Hiding a
control never substitutes: `entitlement.spec.ts::direct_call_denied` calls every restricted
endpoint directly.

**9.3 Workspace isolation.** RLS on all 24 workspace-scoped tables:

```sql
ALTER TABLE queue_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE queue_items FORCE ROW LEVEL SECURITY;
CREATE POLICY ws_isolation ON queue_items USING (
    workspace_id = current_setting('app.workspace_id', true)::uuid
);
```

Every request and every task opens with `SET LOCAL app.workspace_id`. `app_web`, `app_worker`,
`app_media`, `app_automation`, and `app_admin` are all `NOSUPERUSER NOBYPASSRLS`. PgBouncer
runs in transaction pooling mode, so `SET LOCAL` cannot leak to another client's transaction.

**9.4 Secrets.** A 32-byte KEK per environment, delivered by `systemd` `LoadCredential=` from
a root-owned file (mode 0400) and never present in an environment variable, an image, or the
repository. Envelope encryption is AES-256-GCM (`node:crypto` `createCipheriv('aes-256-gcm')`)
with a random 12-byte nonce per row and `kek_version` for rotation; rotation re-encrypts in
batches of 200 rows. Stripe and Meta app secrets, the Postmark token, and the proxy credentials
follow the same path. `git-secrets` patterns run in CI.

**9.5 Private media.** Bucket has no public access. Every read is a presigned GET for one key,
minted only after an RLS-scoped read of the owning row: 300 s for previews, 3600 s for the URL
given to Instagram's fetcher (the longer TTL is the shortest that survives a Reels container's
observed fetch-and-process tail plus one retry). Presign volume is capped at 300/session/hour.
Object keys embed the workspace id but are never guessable identifiers on their own, and
`isolation.spec.ts::presign_requires_row` proves key knowledge alone grants nothing.

**9.6 Restricted sessions.** `automation_sessions` is readable only by `app_automation`. The
capture flow streams a real instagram.com page over CDP; keystrokes are forwarded to the
browser and are not logged, buffered, or persisted at any layer — the CDP `Input.dispatchKeyEvent`
call is made from a handler that has no logger bound. What persists is `storageState()` only.
Session material is excluded from receipts (`redactReceipt()`), from logs (pino `redact`), and
from Sentry (`beforeSend`), each proved by `redaction.spec.ts::no_secret_in_sinks`.

**9.7 Billing callbacks.** Raw-body signature verification with `constructEvent`; unknown event
types recorded and ignored; `event_id` primary key is the replay defence; no state is derived
from a payload, only from a re-fetch (§5.11).

**9.8 Abuse controls.** Cloudflare WAF with a bot-fight rule on `/waitlist` and `/login`;
`rate_buckets` for the eight buckets in §3.11; invitation tokens 32 random bytes with
single-use acceptance; upload size caps enforced twice (presign policy and `HeadObject`
verification); `feedback` capped at 5,000 characters and 20/day/user.

**9.9 Audit.** `audit_log` is append-only by grant (`REVOKE UPDATE, DELETE` from every
application role). Every privileged and operator action writes one with actor kind, actor id,
IP hash, before, after, and `request_id`. Operator reads of customer media write
`support.media_view`.

**9.10 Support access.** The admin console runs on a separate hostname with its own cookie name
and requires WebAuthn in addition to the password. It has no impersonation feature; the
operator sees data through `app_admin` with RLS policies that permit reads across workspaces
only while an `admin_sessions` row is live, and every such read is logged. Safe actions are a
fixed list of 8 endpoints, each of which asserts the pre/post-boundary rule before acting.

**9.11 Retention.** Sessions purged 30 days after expiry · `idempotency_keys` 7 days ·
`rate_buckets` 48 hours · `graphile_worker` completed jobs 7 days · `source_runs` 90 days ·
`analytics_snapshots` kept for the life of the post (they are the trend history) ·
publish-ready `media_variants` for published items purged 30 days after publish and regenerated
on demand from the retained original · originals retained for the life of the workspace ·
`audit_log` 24 months · `receipts` 24 months · backups 28 days.

**9.12 Export.** `export.build` writes a ZIP (originals, final captions, receipts, queue and
post history, analytics as CSV, provenance for sourced items) to R2 and emails a 24-hour
presigned link. Rate limit 1 per workspace per 24 hours.

**9.13 Deletion.** As §5.13. Personal data and stored media are removed; `audit_log` and
`receipts` are rewritten with HMAC'd subject ids so the proof of an action survives without the
person; `deletion_receipts` retains only reference, HMAC, timestamp, and counts.

---

## 10. Risk register

| # | Risk | Early warning signal | Mitigation already in this plan |
|---|---|---|---|
| 1 | An ambiguous publish is resolved wrongly and the same post goes out twice | `health_signals.uncertain_unresolved_minutes` in `warn`; any `needs_review` item | `publish_attempts_one_past_boundary` makes a second attempt impossible; `boundary_crossed_at` committed before the call; reconciliation via `status_code=PUBLISHED` and a ±10 min caption match; operator resolution is manual and attributable (§5.2) |
| 2 | Meta rejects or restricts the app (permissions review, policy change), stopping all publishing | Rising `error.code=190`/`code=10` rates; a Meta developer notice | Only documented endpoints and scopes are used; restricted automation is physically separate from the public product and off by default; `ig_accounts` hold states preserve queues indefinitely so an outage holds rather than fails work |
| 3 | Restricted sourcing produces a copyright or platform-policy incident | Operator review of an untrusted source; `source_runs.outcome='blocked'` | Entitlement-gated and invisible without it; operator-approved sources; `sources.trust` gates auto-refill until 20 items reviewed; provenance retained per item; per-item `policy_acceptances`; capability revocable instantly without touching accepted work (§3.12) |
| 4 | A cleanup destroys content the customer wanted, with no undo for Reels | Any `cleanup_run_items.action='trash'` in a preview | Protection flag; frozen `selection_sha256` invalidating stale confirmations; explicit non-dismissable warning that Reels cannot be restored; one item at a time with stop; `min_age_days >= 7` floor; full frozen history (§3.13, §5.10) |
| 5 | Instagram session for automation is detected and the customer's account is actioned | `automation_sessions.state='expired'`; `source_runs.outcome='blocked'` | Residential EU proxies; poll interval floor 60 min and `source_poll` bucket 60/h; automation confined to one VM; capability limited to tester workspaces; the public product never depends on it |
| 6 | Single-primary Postgres loss takes the product down | `wal_archive_lag_s` alarm; `backup_age_hours` | pgBackRest with 60 s `archive_timeout` (RPO 60 s), nightly restore drill with row-count assertions, dual-repo (Hetzner + R2), and a rehearsed restore-and-repoint exercise as a Phase 10 exit criterion |
| 7 | Media storage cost outgrows the budget as backlogs accumulate | R2 storage line item month over month | Variant purge 30 days after publish with regeneration from originals; plan `storage_gb` enforced server-side; §1.13 shows month-12 at ~$33/mo after the retention rule |
| 8 | Media transcoding starves publishing during a burst | `media_queue_depth`, `media_oldest_wait_s` | Disjoint Graphile Worker task lists (a publish worker cannot dequeue a media job), separate VMs, `--cpus=6` cgroup, `nice -n 10`, and the `media_starvation` nightly drill asserting < 15% publish-latency change |
| 9 | A billing edge case grants or removes access wrongly | `stripe_events_unprocessed`; a support ticket about a missing feature | State derived only from a live re-fetch, never from an event body; `event_id` primary key; hourly `billing.reconcile` converges even if all webhooks are lost; `shuffled_replay` in CI |
| 10 | One operator cannot keep up with support and reconciliation | `needs_review_open` and `accounts_needs_reauth` trending up | Every uncertain outcome creates one notification and one work item with a full history in the inspector; safe actions are 8 fixed endpoints, not SQL; deferrals are deduplicated so limits produce one notification per account per day; the operator never edits production data |

---

## 11. Explicit tradeoffs

1. **No multi-region and no database high availability.** A `db-1` failure is a restore, not a
   failover: measured target 25 minutes to restore 20 GB with `--delta` plus 10 minutes to
   repoint. Accepted because the brief sets no HA requirement and a standby doubles the
   database line item.
2. **Reels cleanup cannot be undone by us**, and Instagram may remove the item permanently
   after its recovery window. Disclosed in the confirmation dialog rather than engineered
   around, because no API exists to restore it.
3. **Cleanup depends on browser automation**, which is fragile against Instagram UI changes. It
   is entitled, off by default, one-item-at-a-time, and evidence-bearing; a UI change causes
   `paused_reconcile`, never a wrong action.
4. **Reconciliation of an uncertain publish can take up to ~7 hours** before it reaches the
   operator's manual path, and up to 24 hours when only container expiry can prove non-publish.
   The alternative — deciding early — risks a duplicate post, which the brief ranks higher.
5. **`schedule_occ_local` drops the second occurrence of a repeated wall-clock hour at DST
   fall-back.** The customer loses one slot per year rather than risking a double post.
6. **The 5-minute materialization skirt** means a schedule edit does not affect a slot less
   than 5 minutes away. Predictable and stated in the UI, but it is not "immediate."
7. **Roles are three, not arbitrary.** A "viewer" or per-account permission is not in v1;
   a `publisher` sees every account in the workspace.
8. **No customer-facing MFA in v1** (the admin console has WebAuthn; customers do not). Password
   + session rotation + login rate limits only.
9. **Analytics are collected on a fixed curve, not on demand at scale.** Between curve points a
   figure can be up to a week stale; the UI labels every figure with its `collected_at`.
10. **We become merchant of record** by choosing Stripe over Paddle, taking on EU VAT
    registration and OSS filing (§1.10). Priced as Stripe Tax at 0.5% of volume.
11. **Media originals are retained for the life of the workspace**, which the brief requires but
    which also means storage cost grows monotonically; the plan mitigates variants, not originals.
12. **`app_admin` can read across workspaces** (support requires it). Isolation there is
    logged-and-attributable rather than structurally impossible.
13. **Bulk queue actions are capped at 200 items per request** so one action cannot hold a
    transaction long enough to affect publishing; larger selections are chunked client-side.
14. **`ig-sim` is my model of Instagram.** Where it diverges from reality, my tests are wrong;
    §7.4's weekly live suite is the correction mechanism, and it runs weekly, not per commit.

---

## 12. Where this is stronger than required

1. **Per-role database grants and `FORCE ROW LEVEL SECURITY`.** The brief asks for isolation;
   this makes a token unreadable by the web process even under a total application compromise,
   and makes a missing policy a build failure rather than a review miss.
2. **Composite foreign keys carrying `workspace_id`.** Cross-tenant grafting is rejected by the
   database, not by a code path that could be forgotten in the twenty-fifth handler.
3. **Materialized `schedule_occurrences` with three unique indexes.** The brief asks that DST
   not cause surprise double posts; materializing slots as rows makes the schedule *inspectable*
   and makes "what will happen next" a query rather than a computation the UI and the worker
   might disagree about.
4. **`recipe_sha256` on media variants.** Preparation becomes content-addressed, so retries,
   deploys, and parameter changes cannot silently produce a second variant or orphan a frozen
   reference.
5. **A first-party `ig-sim` with fault injection.** It makes the post-boundary timeout — the
   scenario the whole design exists for — a routine CI case instead of a thought experiment.
6. **Restore rehearsal as a Phase 0 exit criterion and a nightly drill with row-count and
   `pg_amcheck` assertions.** The brief asks for a rehearsed restoration; this proves it before
   any customer data exists and re-proves it every night.
7. **`billing.reconcile` hourly.** Correct billing state survives losing every webhook, which
   the brief does not require.
8. **Nightly cross-provider replication of originals.** Protects against losing the storage
   vendor, not just an object.

---

## 13. Assumptions

1. Region is the EU; all data resides in `eu-central`; default account timezone `Europe/Berlin`.
2. Launch scale is 350 connected accounts, 490 median / 1,225 peak publishes per day (§2.4).
3. "Professional Instagram accounts" means Business and Creator accounts eligible for Instagram
   Login and the Content Publishing API; Creator accounts that cannot publish Reels via API are
   flagged at connect time.
4. Meta approves the app for `instagram_business_content_publish` and
   `instagram_business_manage_insights`; until approval, the two test accounts are app roles.
5. Instagram's per-account publishing ceiling is 50 posts/24 h; our default `daily_allowance` is
   25, and the true remaining figure is read from `content_publishing_limit`.
6. Single image and single video (Reels) posts only in v1; carousels and Stories are out of scope.
7. Instagram's API offers no archive or delete for media, so managed cleanup requires browser
   automation.
8. Roles are `owner`/`admin`/`publisher` with the matrix in §3.1.
9. Plans: `free` (1 account, 1 seat, 5 GB, no advanced features), `studio` (3 accounts, 3 seats,
   50 GB, managed cleanup), `agency` (10 accounts, 10 seats, 250 GB, managed cleanup); restricted
   sourcing is grant-only and never sold.
10. Free-plan workspaces get 10 publishes/month, enforced by `monthly_prepare_allowance`.
11. Deletion grace period is 7 days; billing `past_due` grace is 7 days.
12. Data export format is a ZIP with CSV manifests; 1 per workspace per 24 hours.
13. Email is transactional only; there is no marketing list at launch.
14. Session lifetime 30 days absolute / 7 days idle.
15. The operator is one non-technical person using only the admin console; no SQL access.
16. `worker-media` concurrency 3 (6 cores of the CX42), `worker-core` publish concurrency 8,
   `worker-analytics` 4, `worker-automation` 4 browser contexts (16 GB ÷ ~800 MB per context,
   leaving 12 GB headroom).
17. Restricted sourcing launches for at most 20 tester workspaces.
18. Residential proxy egress is 20 GB/month while sourcing is active.
19. Media averages 12 MB original + 8 MB variant; 60% of items are video averaging 35 s.
20. Server actions are not used; all mutations are explicit Route Handlers (§1.4).
21. Instagram webhooks are used only for account-level deauthorization and data-deletion callbacks;
   publishing outcomes are polled, not pushed.
22. `ffmpeg` 7.x, `sharp` 0.33, Playwright 1.4x, Node 22 LTS, PostgreSQL 17.
23. Prices in §1.13 are vendor list prices at authoring time, ex-VAT.
24. Analytics metric set is `reach, views, likes, comments, saved, shares` plus derived
   `total_interactions`; anything Instagram omits is recorded in `missing_metrics`, never zero-filled.
25. The `content_rights` policy document is versioned and re-acceptance is required when a new
   version is published; queued items keep the acceptance in force when they were queued.
26. Cleanup `min_age_days` has a hard floor of 7 in the schema, independent of what a rule asks for.
