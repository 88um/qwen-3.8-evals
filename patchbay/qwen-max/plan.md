# Patchbay — Engineering Plan

Hostile multi-tenant code execution (Node/Python), webhook/schedule/manual triggers, per-second billing. 5,000 tenants, 100k executions/day (mean 1.157/s; peak 30/s), p50 4 s, p95 180 s, hard cap 14,400 s. Tiers: t1 = 1 vCPU/512 MB, t2 = 2/1,024, t3 = 4/8,192. Fleet: EC2, 60% spot (2-min notice). Ops: 2 engineers.

## 1. Technology decisions

### D1. Isolation: one Firecracker microVM per job
**Choice:** One Firecracker microVM per job. Guest: custom kernel (`CONFIG_USER_NS=n`), read-only ext4 base image + per-job tmpfs; VMM managed by the host agent (root, no network).
**Rejected:** gVisor (runsc) — its trust boundary is the runsc process (a bug in its Go syscall implementation is a guest→host escape); Firecracker's is KVM (VT-x) + a minimal Rust VMM.
**Why:** Promise 1 is the company-ending invariant; I rest on hardware (VT-x), not a large userspace program. ~125 ms boot, <30 MB overhead (NSDI'20) — negligible vs a 4 s p50.

### D2. Fleet: plain EC2 + custom Go orchestrator
**Choice:** Workers are EC2 (m6i.4xlarge) managed by a ~2k-LOC Go orchestrator: queue-depth autoscaler (10 s loop), spot watcher (SQS), instance lifecycle (EC2 API), worker registration.
**Rejected:** Kubernetes (EKS) — built-in autoscaling, self-healing, a mature pod security model; it adds a control plane to operate (etcd, API server, CNI, CRI) and a security-specialist surface (RBAC, admission controllers) for orchestration that at ≤156 workers is a queue + autoscaler + spot watcher.
**Why:** Operability at 2 engineers outranks features the orchestrator re-implements anyway; K8s self-healing is replaced by the heartbeat checker (15 s) + EC2 TerminateInstances.

### D3. Queue: PostgreSQL + `FOR UPDATE SKIP LOCKED`
**Choice:** The queue is the `jobs` table; claims are `claim_job()` (DDL §3), a conditional UPDATE under `FOR UPDATE OF j, t SKIP LOCKED`.
**Rejected:** Kafka — a durable, replayable log with consumer groups; the claim rate is ≤30/s (brief peak), within Postgres row-locking capacity at zero added infrastructure, and Kafka adds a broker cluster plus a second consistency story (queue state vs DB state).
**Why:** The §4 invariants are enforced by conditional updates under concurrency; one datastore serves queue, state machine, metering, billing. Log replay is replaced by the append-only `job_events` table.

### D4. Secrets: PostgreSQL + AWS KMS envelope encryption
**Choice:** `secrets.kms_ciphertext` = KMS envelope ciphertext (per-tenant key); the control plane decrypts in `DeliverSecrets` and sends values over the mTLS worker channel only.
**Rejected:** AWS Secrets Manager — managed rotation, fine-grained IAM, audit; KMS provides the at-rest property either way (Secrets Manager wraps KMS), containment is enforced by the delivery path (tenant-match + mTLS worker credential), and a second service adds a hop on the 30/s critical path plus an operational dependency.
**Why:** One fewer service for 2 engineers; the security property does not move. Built-in rotation is replaced by a UI action that replaces ciphertext in one transaction.

### D5. Egress: per-VM forward proxy in a per-VM network namespace
**Choice:** Each microVM gets a Linux netns; its only route is a forward proxy (our Go process) in that netns. The proxy enforces: hostname allowlist (SNI for HTTPS, Host/URI host for HTTP), IP-literal rejection, proxy-side DNS resolution checked against a global denylist (10/8, 172.16/12, 192.168/16, 169.254/16, 127/8, 100.64/10, VPC 172.20.0.0/16), and no-SNI = deny.
**Rejected:** nftables IP allowlist (L3/L4) — kernel-enforced, no userspace hop; the policy is hostname-based ("only api.stripe.com") and customer APIs sit behind CDNs with rotating IPs; an IP allowlist cannot express it and breaks on rotation.
**Why:** Hostname policy requires L7 inspection; SNI/Host are cleartext before the handshake, so a non-MITM forward proxy enforces it without breaking TLS pinning. The proxy resolves once per connection and dials the returned IP directly (no re-resolution, no client-visible IP), closing the DNS-rebinding window.

### D6. Billing evidence: append-only Postgres + S3 Object Lock mirror
**Choice:** `metering_ticks` (DDL §3): append-only (no UPDATE/DELETE grants + `reject_mutation` trigger), hash-chained (`row_hash` CHECK + chain trigger), mirrored per job to S3 with `ObjectLockMode: COMPLIANCE`, 5-year retention (`ObjectLockRetainUntilDate`).
**Rejected:** Kinesis as system of record — an ordered, durable stream with replay; billing requires a joinable, constraint-enforced store (invoices join jobs × ticks; the chain needs a CHECK/trigger), so the Postgres table is needed regardless and the stream is a redundant second record.
**Why:** The immutability constraint must be structural (R3): grants + trigger + Object Lock (unalterable until the retention date even by the root account). Stream replay is replaced by the `(job_id, seq)` primary key ordering.

### D7. Dependency install: offline install inside the microVM
**Choice:** The host agent fetches registry tarballs (S3 cache, keyed by sha256 of the lockfile), verifies each against the lockfile integrity hash (npm `integrity` sha512; pip `--hash`), and streams them to the guest over vsock. Install runs offline in the guest (`npm install --offline` from a pre-populated cacache; `pip install --no-index --find-links <dir>`). Install-time code (postinstall, setup.py) executes inside the job's microVM under the same egress policy and cgroup caps.
**Rejected:** Prebuilt node_modules cache on a shared build worker — install runs once, not per job; install-time code is arbitrary code execution, and running it on a shared worker outside the per-tenant sandbox is an escape vector (the postinstall can read the build worker's credentials or poison a shared cache other tenants consume).
**Why:** The sandbox boundary must not have a second, shared execution surface. Install time is billed as job time (tarball download is cached; local install is fast) — honest, since it is the customer's dependency choice.

### D8. Guest image: immutable, content-addressed rootfs
**Choice:** Base image (OS + runtime + guest init) built once per platform release, stored content-addressed by sha256; the host agent verifies the image sha256 at boot and records it in `jobs.image_sha256`. Per-job content (code zip, dependency tarballs, workspace) arrives over vsock onto the tmpfs; the rootfs is never written.
**Rejected:** Per-job image build (Docker build per execution) — the image matches the job exactly; a 30–120 s build exceeds the 4 s p50 job's entire lifetime and the build itself executes customer code on a shared builder.
**Why:** Boot time and attack surface. The reproducibility record (code + image + lockfile + input hashes on the job row) is intact because all four are content-addressed.

## 2. System architecture

```
 customer ──HTTPS──> API (Go) ──SQL──> Postgres 16 (multi-AZ, us-east-1)
 scheduler (single writer, pg_advisory_lock(42)) ──SQL──>┘
 control plane (Go): Reaper, DeadlineChecker, HeartbeatChecker, ApplyReport,
   ConfirmDestruction, DeliverSecrets, ScheduleRerun ──SQL──>┘  ──gRPC/mTLS──> workers
 orchestrator (Go): autoscaler, spot watcher (SQS), EC2 lifecycle ──EC2 API──> instances
 worker (EC2 m6i.4xlarge): host agent (Go, root)
   ├─ Firecracker VMM (1 per job) ──vsock──> guest init (static Go)
   │     guest: RO rootfs + tmpfs; job process uid 1000, seccomp, cgroup v2
   ├─ per-VM netns: veth + egress proxy (Go) ──NAT──> ENI
   └─ S3 (IAM role): code(R), cache(RW), logs(W), artifacts(W)
```

Responsibilities: **API** — webhooks, job CRUD, cancel, tenant/workflow/secret management (role_api). **Scheduler** — cron fires; single writer via `pg_advisory_lock(42)` (released on session death). **Control plane** — all `jobs` status writes from worker reports; Reaper (5 s: lease expiry 60 s, queue expiry 24 h); DeadlineChecker (5 s, +30 s backstop); HeartbeatChecker (15 s threshold, then DescribeInstances / TerminateInstances); DeliverSecrets; ScheduleRerun; cancel pushes over the gRPC stream. **Host agent** — claims via `claim_job()` (2 s poll); microVM lifecycle (Firecracker unix-socket API); netns/tap/cgroup setup; per-VM proxy spawn; vsock endpoints (1024 secrets, 1025 logs, 1026 artifacts, 1027 code+cache); metering ticks (5 s, direct INSERT); heartbeat (5 s). **Guest init** — tmpfs mount (`/workspace` = ram_mb/2); job cgroup (`cpu.max = vcpu`, `memory.max = ram_mb`, `pids.max = 256`); seccomp (~150-syscall allowlist; `SECCOMP_RET_KILL_PROCESS`); `RLIMIT_CORE=0`; secret env; exec; deadline SIGKILL; log/artifact streaming; exit report.

**Fleet sizing and cost.** Load model (A1–A5, §13): duration mix 60% @4 s / 25% @30 s / 10% @180 s / 5% @600 s → mean 57.9 s (p50 4 s, p95 180 s); tier mix 80/15/5 → mean 1.3 vCPU; peak 30/s for 2 h/day. Little's law: mean concurrent = 1.157 × 57.9 = 67.0 jobs = 87.1 vCPU; peak = 30 × 57.9 = 1,737 jobs = 2,258 vCPU. m6i.4xlarge = 16 vCPU (vCPU binds: 16×t1 = 16 vCPU + 8 GB ≤ 64 GB). Mean fleet = ceil(87.1×1.1/16) = 6 (96 vCPU); peak = ceil(2,258×1.1/16) = 156 (2,496 vCPU) = 94 spot + 62 on-demand (60/40, A4).

| Item | Derivation | Value |
|---|---|---|
| Provisioned vCPU-h/day | 2 h×2,496 + 22 h×96 | 7,104 |
| Compute $/day | 7,104 × (0.6×$0.011875 + 0.4×$0.0401) = 7,104 × $0.023165 | $164.6 → **$4,937/mo** |
| Fixed $/mo | Postgres r6g.large Multi-AZ $0.214/h×730 = $156; 2×t3.large $0.256/h×730 = $187; EBS 156×50 GB×$0.08 = $624; S3 5 TB×$0.023 + requests = $155; KMS = $50 | **$1,172** |
| Egress | mean 1 MB + 20 KB/s = 2.16 MB/job → 6,480 GB/mo; cost ×$0.09, revenue ×$0.10 | **$583 / $648** |
| **Total cost** | 4,937+1,172+583 | **$6,692/mo** |

Unit prices: t1 $0.00005/s, t2 $0.0001/s, t3 $0.0002/s; 1-s granularity; **60-s minimum bill per job**. Customer-visible usage = the latest persisted tick (≤5 s lag; contract: ≤5 min). Mean billed seconds = 0.6×60+0.25×60+0.10×180+0.05×600 = 99 s. Mean revenue/job = 99 × (0.8×0.00005+0.15×0.0001+0.05×0.0002) = 99 × $0.000065 = $0.006435 → $19,305/mo compute + $648 egress = **$19,953/mo revenue. Margin = 66.5%.**

Cost per execution = $6,692/3,000,000 = **$0.00223**. Blended delivered cost = $164.6/day ÷ 7,527,000 vCPU-s/day (100,000×57.9×1.3) = $0.00002186/vCPU-s; fixed+egress amortized = $0.000585/job.
- **p50** (4 s→60 s billed, t1, 1.08 MB egress): revenue $0.003108; cost 4×0.00002186+0.000585+1.08×0.09 = $0.000770 → **+$0.002338 (75%)**.
- **p95** (180 s, t1, 4.6 MB egress): revenue $0.00946; cost 180×0.00002186+0.000585+4.6×0.09 = $0.004934 → **+$0.004526 (48%)**.

Worst realistic case (R5): 8 h burst (4× longer, same volume): provisioned 8×2,496+16×96 = 21,504 vCPU-h → compute $498.2/day = $14,945/mo → total $16,700 → margin 16.3%. Mitigation: orchestrator raises `spot_preference` to 80% during sustained bursts → compute $11,306/mo → margin 34.5%. Hard cap: global queue 10,000 → 429 + Retry-After beyond it, so cost is capped by construction. `spot_exempt` workflows run on the on-demand pool at 1.5× rate; assumed ≤10% of peak vCPU (226 vCPU = 14 workers ≤ 62 on-demand).

## 3. Data model

```sql
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE ROLE role_api NOLOGIN;
CREATE ROLE role_scheduler NOLOGIN;
CREATE ROLE role_controlplane NOLOGIN;
CREATE ROLE role_worker NOLOGIN;
CREATE ROLE role_billing NOLOGIN;

CREATE FUNCTION tick_hash(p_prev TEXT, p_job UUID, p_seq BIGINT, p_t TIMESTAMPTZ,
                          p_vcpu INT, p_ram_mb INT, p_worker UUID,
                          p_final BOOLEAN, p_duration_ms BIGINT)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
SELECT encode(digest(p_prev || '|' || p_job::text || '|' || p_seq::text || '|' ||
                     p_t::text || '|' || p_vcpu::text || '|' || p_ram_mb::text || '|' ||
                     p_worker::text || '|' || p_final::text || '|' ||
                     COALESCE(p_duration_ms::text, ''), 'sha256'), 'hex');
$$;

CREATE FUNCTION event_hash(p_prev TEXT, p_job UUID, p_seq BIGINT, p_from TEXT,
                           p_to TEXT, p_reason TEXT, p_actor TEXT, p_at TIMESTAMPTZ)
RETURNS TEXT LANGUAGE sql IMMUTABLE AS $$
SELECT encode(digest(p_prev || '|' || p_job::text || '|' || p_seq::text || '|' ||
                     COALESCE(p_from, '') || '|' || p_to || '|' || p_reason || '|' ||
                     p_actor || '|' || p_at::text, 'sha256'), 'hex');
$$;

CREATE FUNCTION reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'append-only table %: % blocked', TG_TABLE_NAME, TG_OP;
END $$;

CREATE TABLE tenants (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  name TEXT NOT NULL,
  suspended BOOLEAN NOT NULL DEFAULT FALSE,
  max_concurrent_jobs INT NOT NULL DEFAULT 2 CHECK (max_concurrent_jobs BETWEEN 1 AND 20),
  max_queued_jobs INT NOT NULL DEFAULT 50 CHECK (max_queued_jobs BETWEEN 1 AND 500),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE workflows (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id),
  name TEXT NOT NULL,
  runtime TEXT NOT NULL CHECK (runtime IN ('node22','py312')),
  code_object_key TEXT NOT NULL,
  code_sha256 TEXT NOT NULL CHECK (code_sha256 ~ '^[0-9a-f]{64}$'),
  lockfile_object_key TEXT,
  lockfile_sha256 TEXT CHECK (lockfile_sha256 IS NULL OR lockfile_sha256 ~ '^[0-9a-f]{64}$'),
  tier TEXT NOT NULL DEFAULT 't1' CHECK (tier IN ('t1','t2','t3')),
  max_duration_s INT NOT NULL DEFAULT 600 CHECK (max_duration_s BETWEEN 10 AND 14400),
  egress_allowlist JSONB NOT NULL DEFAULT '[]',
  secret_names TEXT[] NOT NULL DEFAULT '{}',
  rerun_on_platform_fault BOOLEAN NOT NULL DEFAULT TRUE,
  spot_exempt BOOLEAN NOT NULL DEFAULT FALSE,
  artifact_retention_days INT NOT NULL DEFAULT 30 CHECK (artifact_retention_days IN (30,90,365)),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE secrets (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id),
  name TEXT NOT NULL,
  kms_ciphertext BYTEA NOT NULL,
  kms_key_id TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, name)
);

CREATE TABLE webhook_events (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  provider TEXT NOT NULL,
  event_id TEXT NOT NULL,
  payload JSONB NOT NULL,
  received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (provider, event_id)
);

CREATE TABLE schedules (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id),
  workflow_id UUID NOT NULL REFERENCES workflows(id),
  cron TEXT NOT NULL,
  tz TEXT NOT NULL DEFAULT 'UTC',
  enabled BOOLEAN NOT NULL DEFAULT TRUE,
  last_fired_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE schedule_fires (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  schedule_id UUID NOT NULL REFERENCES schedules(id),
  fire_time TIMESTAMPTZ NOT NULL,
  job_id UUID,
  UNIQUE (schedule_id, fire_time)
);

CREATE TABLE jobs (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id),
  workflow_id UUID NOT NULL REFERENCES workflows(id),
  source TEXT NOT NULL CHECK (source IN ('webhook','schedule','manual')),
  source_ref UUID,
  attempt INT NOT NULL DEFAULT 1 CHECK (attempt >= 1),
  parent_attempt_id UUID REFERENCES jobs(id),
  status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN
    ('queued','leased','starting','running','cancel_requested','succeeded','failed','canceled','timed_out')),
  cancel_reason TEXT CHECK (cancel_reason IS NULL OR cancel_reason IN ('user','deadline')),
  fail_reason TEXT,
  tier TEXT NOT NULL,
  vcpu INT NOT NULL,
  ram_mb INT NOT NULL,
  max_duration_s INT NOT NULL,
  deadline_at TIMESTAMPTZ,
  lease_id UUID,
  worker_id UUID,
  lease_expires_at TIMESTAMPTZ,
  code_sha256 TEXT NOT NULL CHECK (code_sha256 ~ '^[0-9a-f]{64}$'),
  image_sha256 TEXT NOT NULL CHECK (image_sha256 ~ '^[0-9a-f]{64}$'),
  lockfile_sha256 TEXT CHECK (lockfile_sha256 IS NULL OR lockfile_sha256 ~ '^[0-9a-f]{64}$'),
  input_sha256 TEXT CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
  billable BOOLEAN NOT NULL DEFAULT TRUE,
  log_truncated BOOLEAN NOT NULL DEFAULT FALSE,
  artifact_truncated BOOLEAN NOT NULL DEFAULT FALSE,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  started_at TIMESTAMPTZ,
  ended_at TIMESTAMPTZ,
  CHECK ((tier='t1' AND vcpu=1 AND ram_mb=512) OR (tier='t2' AND vcpu=2 AND ram_mb=1024)
         OR (tier='t3' AND vcpu=4 AND ram_mb=8192)),
  CHECK (status NOT IN ('leased','starting') OR lease_id IS NOT NULL),
  CHECK (status NOT IN ('leased','starting','running','cancel_requested') OR worker_id IS NOT NULL),
  CHECK (status IN ('succeeded','failed','canceled','timed_out') OR ended_at IS NULL),
  CHECK (fail_reason IS NULL OR status IN ('failed','timed_out')),
  CHECK (status NOT IN ('failed','timed_out') OR fail_reason IS NOT NULL),
  CHECK (cancel_reason IS NULL OR status IN ('cancel_requested','canceled','timed_out'))
);

CREATE INDEX jobs_claim_idx ON jobs (created_at) WHERE status = 'queued';
CREATE INDEX jobs_lease_reaper_idx ON jobs (lease_expires_at) WHERE status = 'leased';
CREATE INDEX jobs_deadline_idx ON jobs (deadline_at) WHERE status IN ('running','cancel_requested');
CREATE INDEX jobs_tenant_active_idx ON jobs (tenant_id, status)
  WHERE status IN ('queued','leased','starting','running','cancel_requested');
CREATE INDEX jobs_worker_active_idx ON jobs (worker_id)
  WHERE status IN ('leased','starting','running','cancel_requested');

CREATE TABLE job_events (
  job_id UUID NOT NULL REFERENCES jobs(id),
  seq BIGINT NOT NULL,
  from_status TEXT,
  to_status TEXT NOT NULL,
  reason TEXT NOT NULL,
  actor TEXT NOT NULL,
  at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  row_hash TEXT NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
  prev_hash TEXT NOT NULL,
  PRIMARY KEY (job_id, seq),
  CHECK (row_hash = event_hash(prev_hash, job_id, seq, from_status, to_status, reason, actor, at))
);

CREATE TABLE workers (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  instance_id TEXT NOT NULL UNIQUE,
  region TEXT NOT NULL,
  vcpu INT NOT NULL CHECK (vcpu > 0),
  state TEXT NOT NULL DEFAULT 'registering' CHECK (state IN ('registering','ready','draining','dead')),
  last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE worker_bootstrap_tokens (
  token_hash TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  consumed_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE metering_ticks (
  job_id UUID NOT NULL REFERENCES jobs(id),
  seq BIGINT NOT NULL,
  t TIMESTAMPTZ NOT NULL,
  vcpu INT NOT NULL,
  ram_mb INT NOT NULL,
  worker_id UUID NOT NULL,
  final BOOLEAN NOT NULL DEFAULT FALSE,
  duration_ms BIGINT CHECK (duration_ms IS NULL OR (final AND duration_ms >= 0)),
  row_hash TEXT NOT NULL CHECK (row_hash ~ '^[0-9a-f]{64}$'),
  prev_hash TEXT NOT NULL,
  PRIMARY KEY (job_id, seq),
  CHECK (row_hash = tick_hash(prev_hash, job_id, seq, t, vcpu, ram_mb, worker_id, final, duration_ms))
);

CREATE TABLE invoice_lines (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id),
  job_id UUID NOT NULL UNIQUE REFERENCES jobs(id),
  tier TEXT NOT NULL CHECK (tier IN ('t1','t2','t3')),
  billable_seconds BIGINT NOT NULL CHECK (billable_seconds >= 0),
  unit_price_micros BIGINT NOT NULL CHECK (unit_price_micros >= 0),
  amount_micros BIGINT NOT NULL CHECK (amount_micros >= 0),
  egress_gb NUMERIC(12,3) NOT NULL DEFAULT 0 CHECK (egress_gb >= 0),
  tick_chain_head TEXT NOT NULL CHECK (tick_chain_head ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','billed','disputed','written_off')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE invoices (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tenant_id UUID NOT NULL REFERENCES tenants(id),
  period_start DATE NOT NULL,
  period_end DATE NOT NULL,
  total_micros BIGINT NOT NULL CHECK (total_micros >= 0),
  line_hash TEXT NOT NULL CHECK (line_hash ~ '^[0-9a-f]{64}$'),
  status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','final','paid')),
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE (tenant_id, period_start, period_end),
  CHECK (period_end > period_start)
);

CREATE FUNCTION check_chain() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE prev TEXT;
BEGIN
  IF NEW.seq = 1 THEN
    IF NEW.prev_hash <> (SELECT code_sha256 FROM jobs WHERE id = NEW.job_id) THEN
      RAISE EXCEPTION '% chain anchor mismatch, job %', TG_TABLE_NAME, NEW.job_id;
    END IF;
  ELSE
    EXECUTE 'SELECT row_hash FROM ' || quote_ident(TG_TABLE_NAME) ||
            ' WHERE job_id = $1 AND seq = $2'
      INTO prev USING NEW.job_id, NEW.seq - 1;
    IF NEW.prev_hash IS DISTINCT FROM prev THEN
      RAISE EXCEPTION '% chain link mismatch, job % seq %', TG_TABLE_NAME, NEW.job_id, NEW.seq;
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER metering_ticks_chain BEFORE INSERT ON metering_ticks
  FOR EACH ROW EXECUTE FUNCTION check_chain();
CREATE TRIGGER metering_ticks_immutable BEFORE UPDATE OR DELETE ON metering_ticks
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();
CREATE TRIGGER job_events_chain BEFORE INSERT ON job_events
  FOR EACH ROW EXECUTE FUNCTION check_chain();
CREATE TRIGGER job_events_immutable BEFORE UPDATE OR DELETE ON job_events
  FOR EACH ROW EXECUTE FUNCTION reject_mutation();

CREATE FUNCTION claim_job(p_worker UUID) RETURNS jobs
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
DECLARE r jobs%ROWTYPE;
BEGIN
  SELECT j.* INTO r
  FROM jobs j
  JOIN tenants t ON t.id = j.tenant_id
  WHERE j.status = 'queued'
    AND t.suspended = FALSE
    AND j.created_at > NOW() - INTERVAL '24 hours'
    AND (SELECT count(*) FROM jobs j2
         WHERE j2.tenant_id = j.tenant_id
           AND j2.status IN ('leased','starting','running','cancel_requested')) < t.max_concurrent_jobs
  ORDER BY (j.max_duration_s <= 60) DESC, j.created_at ASC
  LIMIT 1
  FOR UPDATE OF j, t SKIP LOCKED;
  IF NOT FOUND THEN
    RETURN NULL;
  END IF;
  UPDATE jobs
     SET status = 'leased', lease_id = gen_random_uuid(),
         worker_id = p_worker, lease_expires_at = NOW() + INTERVAL '60 seconds'
   WHERE id = r.id
  RETURNING * INTO r;
  RETURN r;
END $$;

GRANT USAGE ON SCHEMA public TO role_api, role_scheduler, role_controlplane, role_worker, role_billing;

GRANT SELECT ON tenants, workflows, webhook_events, schedules, schedule_fires,
                jobs, job_events, workers, metering_ticks, invoice_lines, invoices
    TO role_api, role_scheduler, role_controlplane, role_worker, role_billing;
GRANT SELECT ON secrets TO role_api, role_controlplane;

GRANT INSERT, UPDATE ON jobs TO role_api, role_scheduler, role_controlplane;
GRANT INSERT ON job_events TO role_api, role_scheduler, role_controlplane;
GRANT INSERT ON webhook_events TO role_api;
GRANT INSERT, UPDATE ON tenants, workflows TO role_api;
GRANT INSERT, DELETE ON secrets TO role_api;
GRANT INSERT ON schedule_fires TO role_scheduler;
GRANT UPDATE ON schedules TO role_scheduler;
GRANT UPDATE ON workers TO role_controlplane, role_worker;
GRANT INSERT ON worker_bootstrap_tokens TO role_controlplane;
GRANT INSERT ON metering_ticks TO role_worker, role_controlplane;
GRANT INSERT ON invoice_lines, invoices TO role_billing;
GRANT UPDATE ON invoice_lines, invoices TO role_billing;
GRANT EXECUTE ON FUNCTION claim_job(UUID) TO role_worker;
```

Notes: `jobs.source_ref` references `webhook_events.id` (source='webhook') or `schedule_fires.id` (source='schedule'); the cross-table FK is not expressible on one column, so `CreateJob` (API) and the scheduler fire transaction set it. Admission (per-tenant caps + global queue cap 10,000) is enforced in `CreateJob`: `SELECT … FOR UPDATE` on the tenant row, count active/queued jobs, 429 on breach, INSERT in the same transaction.

**Job state machine** (the only transitions; terminal states are absorbing; every state is reachable via a row):

| from | to | event | mechanism |
|---|---|---|---|
| — | queued | creation | `CreateJob` / scheduler fire transaction (INSERT) |
| queued | leased | claim | `claim_job()` |
| queued | canceled | user cancel | `CancelJob`: UPDATE WHERE status='queued' (nothing executes) |
| queued | failed | 24 h queue expiry | `Reaper` (5 s): UPDATE WHERE status='queued' AND created_at < now()−24 h |
| leased | starting | VM booted | host agent report → `ApplyReport` (WHERE status='leased' AND lease_id=$l) |
| leased | queued | lease expiry 60 s | `Reaper`: UPDATE WHERE status='leased' AND lease_expires_at < now(); clears lease_id, worker_id |
| leased | cancel_requested | user cancel | `CancelJob` (cancel_reason='user') + gRPC push |
| starting | running | process exec'd | guest init report (vsock) → `ApplyReport`; sets started_at, deadline_at = now()+max_duration_s |
| starting | failed | boot failure | host agent report → `ApplyReport` (fail_reason='boot_failed') |
| starting | cancel_requested | user cancel | `CancelJob` + push |
| running | succeeded | exit 0 | guest init exit report → `ApplyReport` (ended_at) |
| running | failed | exit ≠0 / OOM / pids cap | guest init report → `ApplyReport` (fail_reason='exit_N'/'oom'/'pids_limit') |
| running | timed_out | deadline | guest init local SIGKILL at deadline_at + report → `ApplyReport` (fail_reason='deadline') |
| running | cancel_requested | user cancel | `CancelJob` + push |
| running | cancel_requested | deadline backstop | `DeadlineChecker` (5 s): UPDATE WHERE status='running' AND deadline_at < now()−30 s; cancel_reason='deadline' + push |
| running | failed | worker lost | `HeartbeatChecker` (now()−last_heartbeat_at > 15 s) + EC2 DescribeInstances 'terminated' → UPDATE (fail_reason='worker_lost'); `ScheduleRerun` inserts attempt+1 if policy allows |
| cancel_requested | canceled | destruction confirmed, reason='user' | `ConfirmDestruction` on host agent `vm_destroyed` OR EC2 'terminated' |
| cancel_requested | timed_out | destruction confirmed, reason='deadline' | `ConfirmDestruction` (same triggers) |

A rerun is a new row (attempt+1, parent_attempt_id), not a transition. Retry policy (committed): failure before user-code start (boot failure, worker loss in leased/starting) → auto-retry, max 3 attempts, failed attempt billable=FALSE. Failure after start, cause=code (exit/OOM/timeout) → terminal, no rerun; the customer re-runs manually (new execution, billed). Failure after start, cause=platform (worker loss in running, incl. spot) → auto-rerun, max 3 attempts, interrupted attempt billable=FALSE; reruns execute from the beginning (no checkpoint: Firecracker has no live migration; CRIU over a process tree with network sockets is not dependable); workflows with side effects that must not duplicate set `rerun_on_platform_fault=FALSE` (platform fault = terminal failure + manual re-run decision).

## 4. Invariant enforcement map

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| 1. No cross-tenant read of code/secrets/artifacts/logs/cache | One Firecracker microVM per job (KVM/VT-x boundary); tenant-scoped S3 prefixes, keys built by the host agent from the jobs row (guest cannot name a key); per-tenant egress allowlist at the proxy; no per-job state persists on the worker (RO hash-verified rootfs + per-VM tmpfs, destroyed with the VM) | `TestIsolationCrossTenant`: A's job probes B's code/cache/log/artifact prefixes → all 403; post-job worker disk scan for B's marker → absent |
| 2. No tenant reaches control plane, metadata endpoint, or internal services | Four layers: (a) instance IMDS disabled; (b) host nftables DROP dst 169.254.169.254 from all netns; (c) proxy IP denylist (10/8, 172.16/12, 192.168/16, 169.254/16, 127/8, 100.64/10, VPC 172.20.0.0/16) + IP-literal reject + no-SNI deny; (d) VPC SG no egress to 169.254.0.0/16 | `TestMetadataEndpoint` (W6): direct IP, redirect, DNS-rebinding → 403/refused; nftables drop counter unchanged |
| 3. Secrets reach only the job's own process; never persist in platform logs, dumps, artifacts, or worker disk | `DeliverSecrets` (mTLS, worker credential, tenant-match) → host agent memory → vsock 1024 → guest init env → job process; `RLIMIT_CORE=0`; no swap on the worker AMI; host agent zeroes its buffer after send; platform logs carry secret ids only | `TestSecretContainment`: job prints its secret → present in tenant log prefix; absent from host agent log, control plane log, worker disk scan, other tenant's prefix |
| 4. Billing records cannot be inflated, deflated, or forged by customer code; survive worker death | Ticks written only by role_worker/role_controlplane (GRANTs); `reject_mutation` blocks UPDATE/DELETE; hash chain (row_hash CHECK + chain trigger anchored at code_sha256); S3 Object Lock mirror (COMPLIANCE, 5 y); duration from host agent monotonic clock in the final tick | `TestBillingChaos` (W4): EC2-terminate mid-job → ticks stop, chain verifies, invoice = recomputed value; UPDATE as role_worker → exception |
| 5. "Canceled"/"timed_out" shown only when provably not executing | `cancel_requested` intermediate state; only `ConfirmDestruction` writes 'canceled'/'timed_out', requiring host agent `vm_destroyed` or EC2 'terminated'; guest init SIGKILL at +5 s; host agent firecracker SIGKILL at +10 s | `TestCancelSIGTERM` (W7): UI shows cancel_requested until the vm_destroyed timestamp; worker process count returns to baseline |
| 6. Webhook triggers fire exactly once per logical event | UNIQUE (provider, event_id); INSERT … ON CONFLICT DO NOTHING + job INSERT in one transaction | `TestWebhookTriple` (W3): 3 deliveries → exactly 1 job row |
| 7. Schedules fire exactly the contracted number of times | UNIQUE (schedule_id, fire_time); single writer via `pg_advisory_lock(42)` (released on session death); fire + job + last_fired_at in one transaction; 30 s fire lag | `TestCronCatchup` (W10): scheduler killed across a fire time → exactly 1 fire row, 1 job |
| 8. 4-h jobs cannot starve 4-s jobs | tenants CHECK caps (concurrent ≤20, queued ≤50); global queue cap 10,000 (429); `claim_job()` ORDER BY max_duration_s ≤ 60 first | `TestStarvation`: 400 tenants × 4-h jobs + 1 new 4-s job → short job claimed first; worst-case long-job wait = (50/2)×60 s = 25 min, measured |
| 9. Reproducible execution: a run's code+deps+inputs are recorded and re-runnable | jobs columns code_sha256, image_sha256, lockfile_sha256, input_sha256 (NOT NULL where applicable); code/images/tarballs content-addressed in S3; re-run = new job with the same four hashes | `TestReproducibleRun`: two runs of same workflow+input → identical four hashes; S3 code object byte-identical (sha256 verified at upload) |
| 10. A job cannot exceed its purchased resources or exhaust the worker | Host cgroup v2 on the firecracker process (cpu.max=vcpu, memory.max=ram_mb+128 MB, pids.max=300); guest cgroup on the job (cpu.max=vcpu, memory.max=ram_mb, pids.max=256); workspace tmpfs = ram_mb/2 | `TestForkBomb`: fork bomb → pids_limit kill, co-tenant jobs complete; `TestMining`: 4-h CPU job → cpu.stat shows the cap held |

## 5. Failure-mode walkthroughs

**W1. Spot termination at 3:58 of a 4-hour job.** J (t3) on spot worker W since 00:00; AWS interruption at 03:58.
1. SQS notice (2-min warning) → orchestrator sets W.state='draining'; W's claim loop stops calling `claim_job()`.
2. J still running at t+110 s: host agent SIGKILLs J's firecracker, tears down netns/cgroup/tap, reports `vm_destroyed {reason:'spot_preempt'}`; `ApplyReport` marks J failed(worker_lost) via running→failed.
3. `ScheduleRerun` (rerun_on_platform_fault, attempt<3) INSERTs J2 (attempt=2, parent_attempt_id=J); J.billable=FALSE; control plane closes J's chain (final tick, duration_ms = last_tick.t − first_tick.t + 5000); J2 claimed on a healthy worker.
**Evidence:** J's ticks end at 03:58:05, no final row until step 3; J2's chain final=TRUE; invoice: J billable_seconds=0, J2 = J2's duration.

**W2. Cancellation of a fork-bombing job.** J runs `:(){ :|:& };:`; guest pids.max=256 has stalled the bomb at the cap; user cancels at t0.
1. API: running→cancel_requested (1 row); job_events; UI "canceling"; control plane pushes cancel over the gRPC stream.
2. Guest init SIGTERMs the job's process group + starts a 5 s timer; the untrapped bomb processes die, group empties at t0+1 s.
3. Guest init reports `job_killed`; host agent SIGKILLs firecracker (whole-VM kill), tears down, reports `vm_destroyed`; `ConfirmDestruction`: cancel_requested→canceled; UI "canceled".
4. Host agent sends the final tick (duration_ms = t0−start); J stays billable (canceled jobs bill consumed seconds).
**Evidence:** job_events: cancel_requested t0, canceled t0+~2 s (UI never showed "canceled" before vm_destroyed); cgroup pids.current=0 before teardown; invoice billable_seconds = ceil((t0−start)/1 s).

**W3. Webhook delivered three times for one logical trigger.** Provider P delivers evt_123 at t0, t0+2 s, t0+30 s.
1. t0: API INSERTs webhook_events (P, evt_123); same transaction INSERTs job J (source='webhook', source_ref=event.id); COMMIT; 202.
2. t0+2 s and t0+30 s: `ON CONFLICT (provider, event_id) DO NOTHING` → 0 rows; 200; no job. J runs exactly once.
**Evidence:** `count(*) FROM jobs WHERE source_ref=<event id>` = 1; webhook_events rows for (P, evt_123) = 1; API log: 202, 200, 200.

**W4. Worker crash between job completion and usage-record persistence.** J on W; at t0 the job exits 0; at t0+0.1 s W dies hard (no spot notice).
1. The exit report and final tick are lost with the host agent's memory (the tick INSERT did not commit).
2. t0+15 s: `HeartbeatChecker` (NOW() − last_heartbeat_at > 15 s) → DescribeInstances(W) 'terminated' → W.state='dead'.
3. J's last persisted tick is seq=N at t0−5 s (5 s cadence); J: running→failed(worker_lost); `ScheduleRerun` INSERTs J2 (attempt=2); J.billable=FALSE; control plane closes J's chain (final tick, duration_ms = (t0−5 s − first) + 5000). J2 runs on a healthy worker; final tick commits; J2 → succeeded.
**Evidence:** J's ticks end at seq=N then the closed final row; J2's chain final=TRUE; the period's invoice has one billable line (J2).

**W5. Zip-bomb artifact.** J (t1, 512 MB) unzips a 1 MB zip expanding to 4.2 PB into /workspace, exits 0 with /workspace/out as artifact.
1. Expansion writes to /workspace, a tmpfs at 256 MB (ram_mb/2); guest RAM capped by the 512 MB microVM + host cgroup memory.max.
2. The tmpfs fills at 256 MB; further writes → ENOSPC; unzip exits non-zero; the job catches the error, exits 0.
3. Guest init streams /workspace/out over vsock 1026; host agent enforces 100 MB/file and 1 GB total (byte counters); on breach stops the stream, sets jobs.artifact_truncated=TRUE.
**Evidence:** host cgroup memory.current for J's VM ≤ 512 MB; S3 objects in the tenant's artifact prefix ≤ 100 MB; jobs.artifact_truncated=TRUE; no host temp file (artifact path is a pipe).

**W6. Metadata endpoint via a redirect from an allowed domain.** Allowlist = ['api.example.com:443']; the job GETs https://api.example.com/redirect, which 302s to http://169.254.169.254/latest/meta-data/.
1. The client sends CONNECT api.example.com:443 through the proxy (the netns routes all traffic to the proxy; the guest's only DNS server is the proxy).
2. Proxy: SNI in allowlist → allowed; resolves api.example.com itself, checks the resolved IP against the denylist → public, allowed; dials the resolved IP directly.
3. The 302 (Location: http://169.254.169.254/…) is relayed to the guest.
4. The client follows the redirect: new request, host = IP literal. Proxy: IP-literal hosts are rejected (allowlist entries parse as hostname:port; an IP literal never matches) → 403; log `egress_denied {host, reason:'ip_literal'}`.
5. Variants — redirect to internal.corp: SNI not in allowlist → 403. Redirect to api.example.com whose DNS now resolves to 10.1.2.3: the proxy re-resolves at connect time; denylist (10/8) → connect refused. If the proxy were bypassed by a bug: host nftables DROPs dst 169.254.169.254 from all netns; IMDS disabled; VPC SG has no egress allow to 169.254.0.0/16.
**Evidence:** proxy log for J: allowed CONNECT, then egress_denied entries (host/reason); the job's 403s appear in the tenant's log stream; no 169.254.169.254 connection in the ENI flow logs.

**W7. Cancellation of a job that ignores SIGTERM, mid-syscall.** J traps SIGTERM, blocked in write(2). Cancel at t0: API running→cancel_requested; guest init SIGTERMs the group (trap handler runs, process returns to the blocked write); t0+5 s the guest init's timer SIGKILLs the group (untrappable, even mid-syscall), reports `job_killed`; host agent SIGKILLs firecracker, reports `vm_destroyed`; `ConfirmDestruction`: cancel_requested→canceled.
**Evidence:** job_events: cancel_requested t0, canceled t0+5.2 s; host agent log: SIGTERM t0, SIGKILL t0+5.

**W8. Worker network partition during a running job.** J on W; at t0 the network between W and the control plane drops; the instance stays alive. t0+15 s: `HeartbeatChecker` flags W; DescribeInstances(W) → 'running' (partition, not death) — the job stays 'running', no false terminal state (promise 4). The checker escalates: EC2 TerminateInstances(W); polls every 5 s until 'terminated' (cap 120 s); at t0+~75 s the instance is terminated — its death kills the microVM, so the workload is provably dead. W.state='dead'; J: running→failed(worker_lost); `ScheduleRerun` INSERTs J2 (attempt=2); J.billable=FALSE; J2 completes on a healthy worker.
**Evidence:** W: state='dead', last_heartbeat_at=t0−5 s; CloudTrail: TerminateInstances by the control plane IAM role, then 'terminated'; UI history: 'running' until t0+75 s, then 'failed'.

**W9. Cross-tenant cache poisoning attempt.** Tenant A's lockfile L_A (sha256 hA); tenant B's L_B (hB ≠ hA). A's job tries to poison B's entry. The host agent derives the cache key from the jobs row — s3://patchbay-cache/{tenant_A}/{hA}/… — never from guest input; the guest's only cache interface is vsock 1027, serving only the key derived from the current job's record (the protocol has no tenant-id or hash field, so a request for B's key is structurally impossible). Direct S3 access is impossible: the guest env has no AWS credentials; the allowlist excludes S3 endpoints; the cache bucket policy allows only the host agent IAM role. Even a hypothetical poisoned object: B's install re-verifies the tarball's sha512 against B's own lockfile `integrity` before use → mismatch → job fails, fail_reason='supply_chain_integrity_mismatch'.
**Evidence:** host agent log for A's job: every cache request resolves to a tenant_A key; S3 server access log: zero writes to tenant_B prefixes from A's worker; B's job succeeds (integrity check passed).

**W10. Scheduler outage across a cron fire ("charge my users").** Schedule S, cron '0 9 * * *' UTC; the scheduler is down 08:59:00–09:02:00 (deploy); fire time 09:00:00 is missed while down. The scheduler is a single writer: on start it takes `pg_advisory_lock(42)`; the crashed instance's session death released the lock. 09:02:00: the new instance acquires the lock; it computes fire times in (last_fired_at, now − 30 s] → 09:00:00 is eligible. One transaction: INSERT schedule_fires (S, 09:00:00) ON CONFLICT (schedule_id, fire_time) DO NOTHING → 1 row; INSERT job J (source='schedule', source_ref=fire.id); UPDATE schedules SET last_fired_at='09:00:00'; COMMIT. Crash before COMMIT → rollback, recomputed and inserted exactly once on restart; after COMMIT → a duplicate insert hits the UNIQUE constraint → DO NOTHING → no second job. J runs exactly once.
**Evidence:** `count(*) FROM schedule_fires WHERE schedule_id=S AND fire_time='09:00:00'` = 1; jobs with that source_ref = 1; the egress proxy log shows exactly one POST to the customer's billing endpoint.

## 6. AI strategy

No AI features at v1 — no models, prompts, or AI cost; revisit trigger: >50 billing disputes/month, at which point LLM-assisted dispute triage (input: tick chain + job_events; output: suggested resolution; a human approves) is scoped.

## 7. Testing and release confidence

Named adversarial tests (Go, run against a real worker in the test VPC):

| Test | Drives | Asserts |
|---|---|---|
| TestIsolationCrossTenant | A's job probes B's S3 prefixes; post-job disk scan | 403s logged; B's marker absent from worker disk |
| TestMetadataEndpoint | W6 variants (direct IP, redirect, rebinding) | 403/refused; nftables counter unchanged |
| TestSecretContainment | job prints its secret | value only in tenant log prefix; absent from host/control-plane logs, disk scan, other tenants |
| TestForkBomb | `:(){ :|:& };:` | fail_reason='pids_limit'; co-tenant jobs complete; worker memory within cap |
| TestZipBomb | W5 | ENOSPC; artifact caps; worker disk clean |
| TestSeccompViolation | job calls mount(2) | SECCOMP_RET_KILL_PROCESS death; dmesg line in serial log |
| TestCancelSIGTERM | W7 | UI history: cancel_requested until vm_destroyed; process baseline restored |
| TestCancelForkBomb | W2 | same, under pid-cap load |
| TestBillingChaos | W4 (EC2 terminate mid-job) + UPDATE as role_worker | chain verifies; invoice = recomputed; trigger exception |
| TestWebhookTriple | W3 | 3 deliveries → 1 job |
| TestCronCatchup | W10 (kill scheduler across fire time) | 1 fire row, 1 job |
| TestSpotPreempt | W1 (injected SQS notice at 3:58 of a 4-h job) | rerun; partial attempt billable=FALSE; invoice reconstructs |
| TestStateMachineExhaustive | random event sequences against the real DB | every state reachable; terminals absorbing; no transition outside the §3 table |
| TestReproducibleRun | same workflow+input twice | identical four hashes; byte-identical code object |
| TestCachePoisoning | W9 | structurally impossible + integrity backstop |
| TestStarvation | 400 tenants × 4-h jobs + 1 × 4-s job | short job claimed first; long-job wait ≤ 25 min |

Release confidence: a release ships only when the full suite passes on a staging worker fleet; TestBillingChaos, TestCancelSIGTERM, and TestSpotPreempt are release gates (they exercise the four promises). The state-machine test runs on every change touching the §3 table.

## 8. Delivery phases

**Phase 0 — sandbox proof (1 worker).** Host agent, Firecracker, guest init, egress proxy, secret injection, vsock channels, seccomp.
*Exit:* TestIsolationCrossTenant, TestMetadataEndpoint, TestSecretContainment, TestForkBomb, TestZipBomb, TestSeccompViolation pass on one worker; a hello-world node22 and py312 job each produce an artifact and a log.
*Why first:* the sandbox boundary is the foundation of promise 1, and the queue/scheduler build on the host agent's report interface, which this phase fixes.

**Phase 1 — first end-to-end slice** (1 region, on-demand only, 1 internal tenant). API, webhook endpoint, queue + claim, control plane (Reaper, DeadlineChecker, HeartbeatChecker, ApplyReport, ConfirmDestruction), metering → invoice line, UI states.
*Exit:* TestWebhookTriple, TestBillingChaos, TestCancelSIGTERM, TestCancelForkBomb, TestStateMachineExhaustive pass; a webhook produces a job with a downloadable artifact, a log, and an invoice line matching the ticks; the UI shows "canceled" only after vm_destroyed (state-history assertion).

**Phase 2 — scale-out.** Orchestrator (autoscaler, spot watcher, bootstrap), us-east-2 overflow, scheduler (cron), dependency cache, admission caps, per-tenant quotas.
*Exit:* 30/s sustained for 1 h with p95 queue delay (created_at→started_at) < 5 s; TestSpotPreempt, TestCronCatchup, TestCachePoisoning, TestStarvation pass; a forced-interruption drill on 10% of workers produces zero cross-tenant effects and correct billing.

**Phase 3 — billing hardening.** S3 Object Lock mirror, daily reconciler (ticks vs scheduler records; discrepancy > 10 s flags the line for review), `patchbay-billing-reconstruct <job_id>` tool, egress metering + billing.
*Exit:* a disputed invoice reconstructs via the tool from ticks + S3 mirror with the hash chain verified; 100 worker kills across a day produce a zero-discrepancy reconciliation; egress billing matches S3 transfer logs within 1%.

**Phase 4 — GA.** 5,000-tenant onboarding, kill switches, incident runbooks, load test.
*Exit:* SuspendTenant drill stops all of a tenant's jobs within 60 s; a 100k-job 24 h load holds p95 queue delay < 5 s and measured margin ≥ 40%; DrainRegion drill: us-east-1 workers terminated, jobs re-queued and claimed by us-east-2 within 90 s (lease expiry 60 s + claim).

## 9. Security and privacy

**Sandbox boundary.** Enforced by KVM (VT-x) + the Firecracker device model (virtio-net, virtio-block, vsock, serial). Filesystem: read-only ext4 base image (sha256 verified at boot, recorded in jobs.image_sha256) + per-job tmpfs (workspace = ram_mb/2). `CONFIG_USER_NS=n` — the facility does not exist in the guest; the seccomp filter (allowlist ~150 syscalls, SECCOMP_RET_KILL_PROCESS) is defense-in-depth, not the boundary. **Shared between two jobs of different tenants on the same worker, consecutively:** host CPU (capped per VM by cgroup cpu.max), host RAM (capped by memory.max; KVM page tables freed when the VM process exits), the KVM module (the VT-x boundary), and the NIC (traffic confined to the per-VM netns, destroyed with the VM). **Nothing tenant-specific persists on the worker** — the choice that makes a sanitization step unnecessary: the microVM process, netns, cgroup, tap, and tmpfs are all destroyed at job end; logs, artifacts, and cache transit over the network to tenant-scoped S3 and never touch worker disk. **Log caps:** the relay caps each job's log at 10 MB (on breach it stops the stream, sets jobs.log_truncated=TRUE); logs retained 90 days in the tenant's S3 prefix.

**Network policy.** Guest → veth → per-VM netns (nftables: only the proxy's UID may open outbound sockets, `meta skuid` match) → forward proxy → NAT → ENI. The proxy enforces the tenant allowlist (hostname:port; `*.suffix` wildcards allowed, bare `*` not), IP-literal rejection, no-SNI deny, and the global IP denylist on its own resolution. The guest's resolv.conf points only at the proxy; the netns drops port 53 to any other address. Layers against internal/metadata reach: IMDS disabled at the instance, host nftables DROP, proxy denylist, VPC SG. The internal API is on a private subnet; its SG allows only worker ENIs on 8443 (mTLS); the guest cannot reach it (proxy denylist includes the VPC CIDR and the guest's only egress is the proxy).

**Secret injection.** Path: `secrets.kms_ciphertext` → `DeliverSecrets` (verifies the mTLS worker credential, workers.id = jobs.worker_id for the lease, secret.tenant_id = job.tenant_id; KMS Decrypt) → host agent memory → vsock 1024 → guest init → env vars of the job process (uid 1000). Form: environment variables. Lifetime: host agent memory from fetch to vsock send (seconds; buffer zeroed after), guest RAM for the job's duration, nowhere else. Exclusion: logs — the platform never logs values (ids only); the tenant's own stdout is the tenant's own log stream (§11.4). Dumps/core — RLIMIT_CORE=0 on the job process and host agent; no swap on the worker AMI. Artifacts — the artifact path is a vsock pipe to S3; no host temp file (TestSecretContainment disk scan). Worker disk after the job — the VM is destroyed, tmpfs freed, no per-job files. What it cannot catch: the tenant printing its own secret into its own log; a secret written into an artifact the tenant produces; a secret sent to an allowlisted endpoint (the tenant's choice — the secret is typically that endpoint's credential).

**Dependency supply chain.** "Pinned" mechanically = the lockfile's per-tarball integrity hash (npm `package-lock.json` `integrity`, sha512 SRI; pip `--hash`). The cache is keyed by sha256(lockfile bytes + platform) under the tenant prefix; the host agent verifies each fetched tarball against the lockfile integrity before it reaches the guest; the guest re-verifies before install. A retracted/replaced registry package (same name@version, different bytes) fails the check → job fails, fail_reason='supply_chain_integrity_mismatch', page-level alert. A daily monitor re-fetches the top 1,000 cached tarballs and re-verifies integrity. Install-time code (postinstall/setup.py) runs **inside** the microVM under the job's egress policy and cgroup caps — never on a shared builder (D7).

**Incident containment.** Kill switches: **per-tenant** — `SuspendTenant`: UPDATE tenants SET suspended=TRUE (claim_job stops within one 2 s cycle) + control plane cancels the tenant's active jobs via the §3 cancel sequence (running jobs destroyed ≤10 s + teardown). **Per-worker** — `DrainWorker`: workers.state='draining', jobs finish or are killed, EC2 TerminateInstances. **Per-region** — `DrainRegion`: DrainWorker over the region's workers (EC2 tag filter); jobs re-queue via lease expiry (60 s) to the other region. **Evidence trail for an isolation-suspected incident**, from the job row: jobs (worker_id, image_sha256) → job_events (hash-verified history) → host agent log for that VM → per-VM egress proxy log (every request: hostname, resolved IP, bytes, allowed/denied) → metering_ticks → S3 server access logs for the tenant's prefixes. IAM scoping: the worker instance role covers only patchbay-code (R), patchbay-cache (RW), patchbay-logs (W), patchbay-artifacts (W); the guest holds no credentials.

## 10. Risk register

| Risk | L | I | Mitigation (named) |
|---|---|---|---|
| Firecracker/KVM escape | low | critical | per-VM isolation; IMDS disabled; proxy denylist bounds blast radius (no internal reach); per-worker kill switch; host agent is the only root process, no network exposure |
| Spot capacity shortage at peak | med | med | 40% on-demand floor; global queue cap 10,000 (429 beyond); us-east-2 overflow at queue > 5,000 |
| Single global Postgres (us-east-1) | low | high | Multi-AZ; monthly partitions on metering_ticks (2 y in PG, then S3 mirror only); read replica for UI reads; RDS cross-region replica for DR |
| 2-engineer bus factor | high | med | one Go monorepo; no Kubernetes; runbooks + kill switches; phase exit criteria double as operability tests |
| TLS without SNI / ECH inner hostname | low | low | no-SNI = deny (named proxy rule); the proxy dials the SNI's own resolution, so an ECH inner hostname cannot redirect the connection target |
| Malicious-tenant DoS | high | med | per-tenant caps (DDL CHECKs); cgroup caps; proxy per-tenant rate 100 new conns/s (named config); global queue cap |
| Supply-chain retraction | med | med | lockfile integrity at fetch and install; daily re-verification monitor (top 1,000 tarballs); page-level alert on mismatch |
| S3 cost growth | low | low | lifecycle expirations: logs 30 d, artifacts per workflow setting (30/90/365 d), cache 30 d after last access |

## 11. Explicit tradeoffs

1. **Depth triage under the 12k token cap:** the four promises (isolation, secrets, billing, cancel) get full mechanism depth; UI/UX, onboarding flows, support tooling, and DR automation are specified at mechanism-reference level (one line each) rather than fully. This is a deliberate scope cut, not an omission.
2. **Platform-fault reruns re-execute from the beginning**; partial-execution side effects are not rolled back (a spot termination at 3:58 of a "charge my users" job may duplicate the charge). Escape: `rerun_on_platform_fault=FALSE`. Reason: general-purpose checkpointing of arbitrary Node/Python process trees (CRIU) is not dependable with network sockets and subprocesses, and Firecracker has no live migration; rerun is the only correct mechanism at v1.
3. **Canceled/timed-out jobs are billed for consumed seconds** (no refund). Reason: the resource was consumed; the ticks are the evidence.
4. **No log redaction.** A tenant printing its own secret persists it in its own log stream. Reason: redaction cannot catch transformed secrets (base64, split, hashed); the cross-tenant invariant holds because the log stream is tenant-scoped and platform logs never carry values (TestSecretContainment).
5. **60-second minimum bill** means a 4-s job pays for 60 s. Reason: fixed-cost amortization ($0.000585/job) exceeds a 4-s job's compute price; the minimum keeps unit economics positive (§2).
6. **Single global Postgres in us-east-1** with cross-region worker links; a us-east-1 failure degrades to us-east-2 workers waiting on RDS cross-region replica promotion (manual step, ~15 min). Reason: a multi-active global queue is a distributed-systems project beyond the 2-engineer constraint; the brief's scale fits a single-region core.
7. **Metering cadence 5 s**; a worker death between ticks bills to last tick + 5 s (conservative maximum). Reason: a 1 s cadence multiplies control-plane writes 5× (1.2M → 6M ticks/day) for sub-second precision the per-second contract does not require.
8. **Reproducibility records and re-runs the exact code+deps+inputs but does not make customer code deterministic** (external API responses vary). Reason: determinism of arbitrary code calling external services is not a platform property.

## 12. Where this is stronger than required

1. **Four independent layers** against the metadata endpoint (instance-level IMDS disabled + host nftables + proxy denylist + VPC SG) — the brief requires "provably not"; each layer is independently verifiable and any single one holds.
2. **Hash-chained, trigger-enforced metering + Object Lock mirror** — the brief requires "reconstructible"; the chain makes reconstruction verifiable by a third party via `patchbay-billing-reconstruct`.
3. **The per-VM egress proxy logs every connection attempt, including denials** (host, reason, resolved IP) — a per-job network audit trail the brief does not require; it is the incident evidence trail's starting point.
4. **Install-time code inside the sandbox + tarball-only cache** — the brief poses the question; this commits the answer (D7) and tests it (TestCachePoisoning).
5. **Cancel proof requires `vm_destroyed` or EC2 'terminated'** — stronger than signal delivery; the only writer of 'canceled'/'timed_out' is `ConfirmDestruction` (named, covered by TestCancelSIGTERM's state-history assertion), so an early "canceled" display requires a code change, not a race.

## 13. Assumptions

- **A1.** Duration mix 60% @4 s / 25% @30 s / 10% @180 s / 5% @600 s (mean 57.9 s; consistent with the brief's p50 4 s, p95 3 min).
- **A2.** The 30/s peak is a 2 h daily burst; the remaining 22 h run at the 1.157/s mean.
- **A3.** Tier mix 80/15/5 (t1/t2/t3) → mean 1.3 vCPU.
- **A4.** Spot price = 70% off on-demand (m6i.4xlarge $0.19 vs $0.6416/h, us-east-1); measured at launch, margin recomputed.
- **A5.** Egress per job = 1 MB + 20 KB/s (mean 2.16 MB).
- **A6.** Primary region us-east-1 (Multi-AZ Postgres); overflow us-east-2 (workers connect to the global Postgres over AWS Transit Gateway).
- **A7.** Webhook providers send a stable event id; a provider without one uses dedup key sha256(provider || payload) (named fallback in `CreateJob`).
- **A8.** Promise 2 interpretation: platform mechanisms never persist a secret; a tenant's own code printing its own secret persists it in its own log (§11.4).
- **A9.** Public registries only at v1 (registry.npmjs.org, pypi.org/files.pythonhosted.org); private registries are v2.
- **A10.** Worker type m6i.4xlarge; guest kernel is a custom build (CONFIG_USER_NS=n); runtimes Node 22 (LTS) and Python 3.12.
- **A11.** Metering retention 5 years (billing dispute window).
- **A12.** `spot_exempt` demand ≤ 10% of peak vCPU; priced at 1.5× the tier rate.
