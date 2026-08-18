# Patchbay — Engineering Plan

## §1 Technology Decisions

### Sandbox isolation: Firecracker microVMs

**Choice:** Firecracker microVMs (KVM-based). Each job runs in a dedicated VM with its own Linux guest kernel, booted by a minimal VMM process.

**Rejected:** gVisor (runsc). Strongest container-based isolation — interposes a userspace Sentry on every guest syscall, reducing host kernel exposure far below standard seccomp. Rejected because gVisor shares the host kernel for memory management and scheduling; container escapes exploit this shared surface (CVE-2020-15257: containerd-shim's abstract unix socket in shared network namespace; CVE-2019-5736: overwrite host runc binary via /proc/self/exe). Firecracker's boundary is KVM hardware virtualization (Intel VT-x EPT / AMD-V NPT). A guest kernel exploit yields control of the guest — not the host. The VMM process runs under seccomp-bpf (24 allowed syscalls, applied by the jailer via `prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &bpf_prog)`), inside new PID/mount/net/cgroup namespaces, chrooted, unprivileged UID. A KVM escape is required — categorically harder than a container escape.

**Why:** Promise 1 (absolute tenant isolation) against assumed-hostile code. The attack surface difference — hardware hypervisor boundary vs. shared kernel — is the deciding factor.

### Database: PostgreSQL 16

**Choice:** PostgreSQL 16.

**Rejected:** CockroachDB. Horizontally scalable and would eliminate the single-writer bottleneck. Rejected because `SELECT ... FOR UPDATE SKIP LOCKED` — required for the job queue (§3/§4) — has no CockroachDB equivalent that provides non-blocking lock skipping. The job queue processes 30 claims/sec at peak; an alternative design using `AS OF SYSTEM TIME` follower reads cannot enforce mutual exclusion on lease acquisition.

**Why:** Job lifecycle invariants (§4) are enforced by conditional updates under row-level locking and transaction isolation. That requirement outranks horizontal scalability at 100K executions/day.

### Compute: AWS EC2 c6i family, single instance family

**Choice:** c6i instances exclusively (c6i.xlarge for default workloads, c6i.2xlarge for 4-vCPU jobs).

**Rejected:** Mixed instance families (c6i + m6i). Lower spot pricing on m6i. Rejected because Firecracker snapshot restore requires the destination host to support a superset of the source host's CPU features (`CPUID` leaf matching, per Firecracker snapshot documentation). A single instance family guarantees CPU feature compatibility for spot-evacuation snapshot restores across the fleet.

**Why:** Spot evacuation (§5 walkthrough 1) requires snapshot portability; CPU homogeneity is the mechanism that provides it.

## §2 System Architecture

Five processes, each independently deployable:

1. **API server** (2 instances, behind ALB): receives webhooks, manual triggers, tenant CRUD, cancel requests. Writes to PostgreSQL. Publishes cancel notifications via `pg_notify('cancel', worker_id || ':' || job_id)`.
2. **Scheduler** (1 active + 1 hot standby): polls `schedules` table every 1s. Claims schedules with `FOR UPDATE SKIP LOCKED`, inserts jobs transactionally. Standby takes over via leader election on a PostgreSQL advisory lock (`pg_try_advisory_lock(1)`).
3. **Worker agent** (1 per EC2 host): polls job queue, manages Firecracker VM lifecycle, captures logs/artifacts via virtio-vsock, writes metering records, polls EC2 metadata for spot-termination notices every 5s.
4. **Billing service** (1 instance): runs every 60s, reads metering records, computes billing line items, updates tenant-visible usage. Idempotent — reprocessing the same records produces the same output (keyed by `job_id`).
5. **Reaper** (1 instance): runs every 30s, detects expired leases (`lease_expires_at < now()` with no heartbeat), kills orphaned VMs, marks jobs `failed` with reason `worker_lost`.

Communication: all inter-process coordination is through PostgreSQL (job queue, LISTEN/NOTIFY for cancellation, advisory locks for leader election). No message broker. Workers poll the job queue every 500ms; NOTIFY latency for cancellation is <100ms.

## §3 Data Model

```sql
CREATE TABLE tenants (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name            TEXT NOT NULL,
    max_concurrent  INT NOT NULL DEFAULT 20 CHECK (max_concurrent BETWEEN 1 AND 100),
    suspended       BOOLEAN NOT NULL DEFAULT false,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workflows (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    name            TEXT NOT NULL,
    runtime         TEXT NOT NULL CHECK (runtime IN ('node20','python312')),
    vcpu            INT NOT NULL DEFAULT 1 CHECK (vcpu IN (1,2,4)),
    memory_mb       INT NOT NULL DEFAULT 512 CHECK (memory_mb IN (512,1024,2048,4096,8192)),
    timeout_s       INT NOT NULL DEFAULT 300 CHECK (timeout_s BETWEEN 1 AND 14400),
    egress_domains  TEXT[] NOT NULL DEFAULT '{}',
    retry_policy    TEXT NOT NULL DEFAULT 'none' CHECK (retry_policy IN ('none','auto_idempotent')),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE workflow_versions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id     UUID NOT NULL REFERENCES workflows(id),
    version         INT NOT NULL,
    code_sha256     TEXT NOT NULL,
    lockfile_sha256 TEXT NOT NULL,
    code_s3_key     TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workflow_id, version)
);

CREATE TABLE jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    workflow_id     UUID NOT NULL REFERENCES workflows(id),
    version_id      UUID NOT NULL REFERENCES workflow_versions(id),
    status          TEXT NOT NULL DEFAULT 'queued'
                    CHECK (status IN ('queued','leased','preparing','running',
                                      'canceling','canceled','completed','failed',
                                      'timed_out','suspended')),
    trigger_type    TEXT NOT NULL CHECK (trigger_type IN ('webhook','schedule','manual')),
    idempotency_key TEXT,
    input_sha256    TEXT NOT NULL,
    input_s3_key    TEXT NOT NULL,
    priority        INT NOT NULL DEFAULT 0,
    duration_tier   TEXT NOT NULL DEFAULT 'fast'
                    CHECK (duration_tier IN ('fast','medium','long')),
    worker_id       UUID,
    lease_expires_at TIMESTAMPTZ,
    snapshot_s3_key TEXT,
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Deduplication: webhook and schedule triggers carry an idempotency key;
-- manual triggers leave it NULL (PG UNIQUE allows multiple NULLs)
CREATE UNIQUE INDEX jobs_idempotency
    ON jobs (tenant_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Queue index: workers pull by tier then priority then age
CREATE INDEX jobs_queue
    ON jobs (duration_tier, priority DESC, created_at ASC)
    WHERE status = 'queued';

-- Lease expiry: reaper scans for expired leases
CREATE INDEX jobs_lease_expiry
    ON jobs (lease_expires_at)
    WHERE status IN ('leased','preparing','running') AND lease_expires_at IS NOT NULL;

-- Concurrent job count enforcement
CREATE INDEX jobs_active_per_tenant
    ON jobs (tenant_id)
    WHERE status IN ('leased','preparing','running');

CREATE TABLE job_events (
    id              BIGSERIAL PRIMARY KEY,
    job_id          UUID NOT NULL REFERENCES jobs(id),
    from_status     TEXT NOT NULL,
    to_status       TEXT NOT NULL,
    reason          TEXT,
    worker_id       UUID,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE metering_records (
    id              BIGSERIAL PRIMARY KEY,
    job_id          UUID NOT NULL REFERENCES jobs(id),
    tenant_id       UUID NOT NULL,
    event_type      TEXT NOT NULL CHECK (event_type IN ('start','heartbeat','end')),
    vcpu            INT NOT NULL,
    memory_mb       INT NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX metering_by_job ON metering_records (job_id, recorded_at);

CREATE TABLE billing_line_items (
    id              BIGSERIAL PRIMARY KEY,
    tenant_id       UUID NOT NULL REFERENCES tenants(id),
    job_id          UUID NOT NULL REFERENCES jobs(id),
    vcpu_seconds    NUMERIC NOT NULL CHECK (vcpu_seconds >= 0),
    memory_mb_s     NUMERIC NOT NULL CHECK (memory_mb_s >= 0),
    cost_usd        NUMERIC NOT NULL CHECK (cost_usd >= 0),
    billing_month   DATE NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id)
);

CREATE TABLE schedules (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workflow_id     UUID NOT NULL REFERENCES workflows(id),
    tenant_id       UUID NOT NULL,
    cron_expr       TEXT NOT NULL,
    next_fire_at    TIMESTAMPTZ NOT NULL,
    last_fired_at   TIMESTAMPTZ,
    enabled         BOOLEAN NOT NULL DEFAULT true,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX schedules_pending
    ON schedules (next_fire_at)
    WHERE enabled = true;

CREATE TABLE workers (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instance_id     TEXT NOT NULL UNIQUE,
    is_spot         BOOLEAN NOT NULL,
    capacity_vcpu   INT NOT NULL,
    capacity_mb     INT NOT NULL,
    allocated_vcpu  INT NOT NULL DEFAULT 0,
    allocated_mb    INT NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','draining','dead')),
    last_heartbeat  TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

**State transition table** (every valid transition; all others are forbidden by application code and audited by `job_events`):

| From | To | Trigger | Guard |
|---|---|---|---|
| queued | leased | worker claims | `SELECT ... FOR UPDATE SKIP LOCKED WHERE status='queued'`; concurrent-job count < `tenants.max_concurrent` checked in same txn |
| leased | preparing | VM boot initiated | worker holds lease |
| preparing | running | VM ready, code executing | worker holds lease |
| running | completed | exit code 0 | worker writes `end` metering record in same txn |
| running | failed | exit code ≠ 0, guest crash | worker writes `end` metering record in same txn |
| running | timed_out | wall clock ≥ `workflows.timeout_s` | host agent timer fires; `Stop` action sent to VMM |
| queued | canceling | user cancel | API `UPDATE ... WHERE status='queued'` |
| leased/preparing/running | canceling | user cancel | API writes; NOTIFY sent to worker |
| canceling | canceled | VM stopped or never started | worker confirms VM halted |
| running | suspended | spot termination notice | snapshot created, uploaded to S3 |
| suspended | queued | re-queued for resume | `snapshot_s3_key` set, priority boosted by 1000 |

Terminal/absorbing states: `completed`, `failed`, `timed_out`, `canceled`. No transitions exit these.

**Queue fairness:** the `priority` column implements weighted fair-share. On each job insertion, priority is set to `-(SELECT count(*) FROM jobs WHERE tenant_id = $1 AND status = 'queued')`. This interleaves tenants in queue order — a tenant with 100 queued jobs does not block a tenant with 1. Workers pull 60% from `fast`, 30% from `medium`, 10% from `long` tier (implemented as weighted random tier selection per poll cycle). Long jobs (estimated > 30 min) are capped at 2 concurrent per tenant (`CHECK` in the claim transaction: `SELECT count(*) FROM jobs WHERE tenant_id=$1 AND status IN ('leased','preparing','running') AND duration_tier='long'` must be < 2).

## §4 Invariant Enforcement Map

| # | Invariant | Mechanism | Evidence |
|---|---|---|---|
| I1 | No cross-tenant code/data access | Each job runs in a separate Firecracker microVM. Memory isolation: KVM EPT/NPT (hardware). Filesystem: read-only ext4 rootfs (`PUT /drives/{id}` with `is_read_only: true`) + tmpfs overlay (RAM-backed, destroyed on VM termination). Network: per-VM TAP device in its own network namespace. No state from VM N persists for VM N+1 — the Firecracker process exits, the kernel reclaims all memory pages, the TAP device is destroyed, and a new VMM process starts with a fresh rootfs for the next job. | `test_cross_tenant_read`: run job A (tenant 1) that writes a marker file to /tmp and a marker string to a known memory address; run job B (tenant 2) on the same worker slot immediately after; job B reads /tmp (empty) and scans its address space (no marker). Assert both reads return nothing. |
| I2 | No tenant access to control plane, cloud metadata, or other VMs | iptables on the host: `iptables -I FORWARD -i tap+ -d 10.0.0.0/8 -j DROP` (blocks all RFC1918); `iptables -I FORWARD -i tap+ -d 169.254.169.254 -j DROP` is unnecessary — Firecracker VMM intercepts all guest traffic to the MMDS address (configurable, default 169.254.169.254) before it reaches the TAP fd, so guest packets to 169.254.169.254 never enter the host network stack. Egress to the internet routes through the host's egress proxy exclusively (VM default gateway points to proxy IP on the host-side bridge). | `test_metadata_unreachable`: job executes `curl -m 5 http://169.254.169.254/latest/meta-data/` — receives MMDS response (empty JSON, since MMDS is cleared post-secret-read), NOT AWS metadata. `test_controlplane_unreachable`: job attempts TCP connect to control plane IP — connection times out (FORWARD DROP on RFC1918). |
| I3 | Secrets never persist in logs, dumps, artifacts, or disk | Secrets travel: Vault → host agent (TLS, in-memory) → Firecracker MMDS (`PUT /mmds` API, held in VMM process memory) → guest agent reads from MMDS HTTP endpoint → loads into runtime process memory → host agent clears MMDS (`PUT /mmds` with `{}`). Secrets never touch disk: rootfs is read-only, tmpfs overlay is RAM. Core dumps disabled: jailer sets `prctl(PR_SET_DUMPABLE, 0)` on VMM process; guest `/proc/sys/kernel/core_pattern` set to empty in rootfs image. Logs: stdout/stderr captured via virtio-vsock by host agent; host agent writes logs to S3 at `logs/{tenant_id}/{job_id}/`. System logs (host agent, API server) never contain customer output — they log only job IDs and status transitions. | `test_secret_not_in_logs`: inject a known secret, run job that prints env vars to stdout, download the job's log from S3, search for the secret value — it appears (tenant's own log, their responsibility). Search system logs (journald on host, API server CloudWatch) — secret does not appear. `test_secret_not_on_disk`: after job completion, run `grep -r $SECRET /` on the host — no match. |
| I4 | Billing records unforgeable by customer code | Metering records originate on the host agent, outside the VM. The host agent writes `start` record (with timestamp from host clock, not guest) when the VM begins executing, `heartbeat` every 10s, and `end` record when the VM exits. The guest has no network path to PostgreSQL (blocked by iptables) and no vsock channel to the metering system. Records are written to PostgreSQL with `tenant_id` and `job_id` from the host agent's own state, not from any guest-supplied value. `billing_line_items.job_id` has a UNIQUE constraint — each job produces exactly one billing line item. | `test_metering_forgery`: job attempts to POST to the metering endpoint (TCP to PostgreSQL port) — connection dropped by iptables. Verify the only metering records for the job are those written by the host agent (check `received_at` timestamp is within 1s of host agent's write time). |
| I5 | Canceled = provably not executing | Cancel sequence for a running job: (1) API sets `status='canceling'`, writes `job_events` row, sends `pg_notify('cancel', '{worker_id}:{job_id}')`. (2) Worker receives NOTIFY within 100ms, calls Firecracker API `PUT /actions` with `{"action_type": "SendCtrlAltDel"}` — triggers ACPI shutdown in guest. (3) 5s timer starts. (4a) If VM exits within 5s: worker sets `status='canceled'`, writes `end` metering record. (4b) If VM does NOT exit (code ignores shutdown): worker calls `PUT /actions` with `{"action_type": "Stop"}` — halts vCPU threads immediately (not a signal; the VMM stops the KVM vCPU ioctl loop). VM is stopped. Worker kills the Firecracker process (`SIGKILL`), sets `status='canceled'`. (5) If worker has stopped heartbeating: reaper detects expired lease, calls AWS `TerminateInstances` on the worker's EC2 instance, sets `status='canceled'`. UI shows `canceling` until step 4 or 5 completes — never `canceled` while the VM could still be running. | `test_cancel_ignores_sigterm`: run a job with `trap '' TERM; sleep 3600`. Cancel it. Assert job reaches `canceled` within 10s. Assert Firecracker process for that VM is no longer running (`kill -0` returns ESRCH). Assert no further metering records appear after the `end` record. |
| I6 | Webhook dedup | `jobs_idempotency` UNIQUE index on `(tenant_id, idempotency_key)`. Webhook handler computes `idempotency_key = sha256(webhook_source || ':' || delivery_id)`. Second insert with same key fails with unique violation; handler returns 200 (idempotent). | `test_webhook_dedup`: send the same webhook payload with the same delivery ID 3 times. Assert exactly 1 job is created. Assert the 2nd and 3rd calls return 200 (not 409). |
| I7 | Schedule fires exactly N times | Scheduler claims schedule row with `SELECT ... FOR UPDATE SKIP LOCKED`. In the same transaction: inserts job with `idempotency_key = '{schedule_id}:{next_fire_epoch}'`, updates `last_fired_at`, computes next `next_fire_at`. If the scheduler crashes: transaction rolls back, standby scheduler picks up the row on next tick. Two schedulers racing: `SKIP LOCKED` means one gets it, the other skips. | `test_schedule_exactly_once`: create a per-minute schedule, run for 5 minutes with both schedulers active. Assert exactly 5 jobs created. Kill the active scheduler mid-tick, assert the standby fires the next occurrence within 2s. |
| I8 | Non-idempotent jobs never auto-retried | `workflows.retry_policy` defaults to `'none'`. Jobs with `retry_policy = 'none'` that fail due to `worker_lost` (lease expiry) are marked `failed` with `reason = 'worker_lost'` — never re-queued. The tenant is notified via webhook callback. Only workflows with `retry_policy = 'auto_idempotent'` are re-queued on `worker_lost`, and the tenant must explicitly opt in. | `test_no_auto_retry`: set `retry_policy='none'`. Kill the worker mid-job. Assert job status becomes `failed` with reason `worker_lost`. Assert no new job is created. |

## §5 Failure-Mode Walkthroughs

**1. Spot termination at hour 3:58 of a 4-hour job**

**Scenario:** A 4-hour job has been running for 3h58m on a spot instance. AWS sends a 2-minute termination notice.

1. Worker agent polls EC2 instance metadata endpoint (`GET /latest/meta-data/spot/instance-action`) every 5s. Receives `{"action": "terminate", "time": "..."}`.
2. Worker marks all running jobs on this host as `evacuating` in its local state (not a DB status — internal coordination only).
3. For the 4-hour job: worker calls Firecracker `PATCH /vm` with `{"state": "Paused"}` — pauses vCPU execution. Then `PUT /snapshot/create` with `{"snapshot_type": "Full", "snapshot_path": "/tmp/snap", "mem_file_path": "/tmp/mem"}`. VM memory (up to 8 GB) is written to local disk.
4. Worker uploads snapshot files to S3 at `snapshots/{job_id}/`. 8 GB at ~1 GB/s network = ~8s. Upload completes within the 2-minute window.
5. Worker updates job: `status = 'suspended'`, `snapshot_s3_key = 'snapshots/{job_id}/'`, writes `end` metering record with `reason = 'spot_eviction'`.
6. Worker re-inserts the job into the queue: `status = 'queued'`, `priority += 1000` (priority boost for resumed jobs), `snapshot_s3_key` preserved.
7. A worker on a different host (same c6i family) claims the job. Detects `snapshot_s3_key` is set. Downloads snapshot from S3. Calls `PUT /snapshot/load`. VM resumes execution from the exact instruction where it was paused.
8. Metering: suspended time (between `end` record on old host and `start` record on new host) is not billed. The billing service computes: `vcpu_seconds = sum of (end.recorded_at - start.recorded_at) across all segments`.

**Evidence:** `job_events` shows `running→suspended→queued→leased→running→completed`. Metering shows two segments; billing excludes the gap.

**2. Cancellation of a fork-bombing job**

**Scenario:** A job executes `while true; do bash & done` inside the VM.

1. The fork bomb creates thousands of processes inside the guest kernel. The VM's memory is fixed at 512 MB (set via `PUT /machine-config` with `{"mem_size_mib": 512}`). Guest OOM killer activates when the process table and stacks exhaust VM RAM. The VM's vCPU is limited by the cgroup the jailer configured (1 vCPU = 1 host CPU thread via `cgroup /cpuset`).
2. User clicks Cancel. API sets `status = 'canceling'`, sends `pg_notify`.
3. Worker calls `SendCtrlAltDel`. The guest kernel, under OOM pressure, may not process the ACPI shutdown within 5s.
4. 5s timer expires. Worker calls `PUT /actions` with `{"action_type": "Stop"}`. The VMM stops the KVM vCPU ioctl loop. All guest processes halt — this is a host-level operation, not a guest-level signal. The fork bomb is irrelevant because the VMM controls the vCPU thread from outside the VM.
5. Worker kills the Firecracker process (`SIGKILL`), tears down the TAP device. `status = 'canceled'`.

**Evidence:** Host CPU for the VMM bounded to 1 core. `job_events`: `running→canceling→canceled`, reason `user_cancel_force_stop`. `pgrep firecracker` returns empty for that VM ID.

**3. Webhook delivered three times for one logical trigger**

**Scenario:** Stripe sends the same `invoice.paid` webhook three times (provider retry).

1. First delivery: API computes `idempotency_key = sha256('stripe:evt_abc123')`. Inserts job row. `INSERT` succeeds. Returns 200.
2. Second delivery (retry): same `idempotency_key`. `INSERT` hits `jobs_idempotency` UNIQUE index. PostgreSQL raises unique_violation. API catches the error, returns 200 (acknowledging receipt to stop retries).
3. Third delivery: same path as second. Returns 200.
4. Exactly one job executes.

**Evidence:** `jobs` has 1 row for that idempotency key. API logs show 3 POSTs, all 200.

**4. Worker crash between job completion and usage-record persistence**

**Scenario:** Job exits successfully. Host agent is about to write the `end` metering record and update status to `completed`. Host agent crashes (segfault, OOM-killed).

1. The `end` metering record is NOT written. The job status remains `running`. The `lease_expires_at` is not extended (no heartbeat from dead agent).
2. Reaper detects expired lease (30s after last heartbeat). Checks EC2: instance is still running (agent crashed, not the instance).
3. Reaper sets `status = 'failed'`, `reason = 'worker_lost'`. The job is NOT marked `completed` because the agent never confirmed it.
4. Billing reconciliation: the billing service computes duration as `last heartbeat recorded_at - start recorded_at`. The last heartbeat was ≤10s before the crash. The tenant is billed for actual execution time up to the last heartbeat, not up to the crash. Maximum underbilling: 10s (one heartbeat interval).
5. The tenant's output artifacts may be lost (the agent hadn't uploaded them). The job is marked `failed` with a reason that allows manual re-run.

**Evidence:** `metering_records` has `start` + heartbeats but no `end`. Billing computed from last heartbeat − start. `job_events`: `running→failed`, reason `worker_lost`.

**5. Zip-bomb artifact**

**Scenario:** Job writes a 1 KB file to `/output/` that decompresses to 10 TB.

1. `/output/` is on the VM's tmpfs overlay, backed by the VM's allocated RAM (512 MB). Writing a file that expands beyond 512 MB triggers `ENOSPC` or OOM inside the guest. The 10 TB decompression never completes — it is physically bounded by VM memory.
2. If the job writes a compressed file directly (no decompression inside the VM): the file size on tmpfs is the compressed size (1 KB). At job completion, the host agent reads files from the VM's block device via the Firecracker API and validates total artifact size ≤ 100 MB before uploading to S3. 1 KB < 100 MB, so the upload proceeds.
3. The host agent NEVER decompresses artifacts. They are stored as opaque blobs in S3 at `artifacts/{tenant_id}/{job_id}/`. Downstream consumers (the tenant's own systems) handle decompression. The platform's storage cost is bounded by the 100 MB raw-byte cap per job.

**Evidence:** S3 artifact is 1 KB. No storage explosion. If decompression was attempted in-VM, exit code 137 (OOM-killed).

**6. Metadata endpoint via redirect chain**

**Scenario:** Tenant's egress allowlist includes `api.stripe.com`. Job code sends `GET https://evil.stripe-lookalike.com/redirect` (not allowed — but suppose the code sends `GET https://api.stripe.com/exploit` and the response is a 302 redirect to `http://169.254.169.254/latest/meta-data/`).

1. The VM's HTTP client receives the 302 response from `api.stripe.com`.
2. The client follows the redirect, attempting to connect to 169.254.169.254.
3. Firecracker VMM intercepts all packets from the guest destined for 169.254.169.254 (the MMDS address). The packets never reach the host network stack. The VMM returns an MMDS response — which is empty JSON (`{}`) because the host agent cleared MMDS data after secret injection (`PUT /mmds` with `{}`).
4. The guest receives `{}`. No AWS metadata is exposed.
5. Even if the MMDS address were changed to a non-default IP: the host iptables rule `DROP` on FORWARD for all RFC1918 destinations from TAP interfaces blocks any private-range redirect target. The only egress path is through the proxy, which validates the HTTP Host header / TLS SNI against the tenant's `egress_domains` allowlist.

**Evidence:** tcpdump on host bridge shows no VM packets to 169.254.169.254. Firecracker MMDS metrics show a GET returning `{}`.

**7. Tenant code scans the host network (port scan)**

**Scenario:** Job runs `nmap 10.0.0.0/8` to discover internal services.

1. All packets from the VM's TAP device are processed by iptables FORWARD chain on the host.
2. Rule: `iptables -I FORWARD -i tap+ -d 10.0.0.0/8 -j DROP`. All RFC1918-destined packets are dropped.
3. The only permitted path is to the egress proxy's IP on the host bridge (which is in a non-RFC1918 range on the bridge, e.g., 172.31.0.1 — wait, that's RFC1918). Correction: the VM-to-proxy communication uses a point-to-point link-local pair (169.254.x.y, configured per-VM, distinct from the MMDS address). The iptables rule allows FORWARD from TAP to the proxy's link-local IP only, and DROP all else.
4. `nmap` receives no responses. All 16 million /8 addresses are unreachable.

**Evidence:** `iptables -L FORWARD -v` shows DROP counter incrementing. Job log: nmap reports all hosts down.

**8. Schedule double-fire during scheduler failover**

**Scenario:** Active scheduler crashes after claiming a schedule row but before committing the transaction.

1. Active scheduler: `BEGIN; SELECT * FROM schedules WHERE next_fire_at <= now() FOR UPDATE SKIP LOCKED` — returns the schedule row. Scheduler inserts job row. Scheduler crashes before `COMMIT`.
2. PostgreSQL detects the broken connection. The transaction rolls back. The schedule row is unlocked. The inserted job row is rolled back.
3. Standby scheduler acquires the advisory lock (`pg_try_advisory_lock(1)` succeeds now that the active scheduler's connection dropped). On its next 1s tick, it claims the schedule row and fires the job.
4. Exactly one job is created.

**Evidence:** Exactly 1 job row for this schedule occurrence. `schedules.last_fired_at` matches expected fire time.

**9. Tenant exhausts memory via tmpfs writes**

**Scenario:** Job writes 500 MB to `/tmp/` in a VM with 512 MB RAM.

1. `/tmp/` is on the tmpfs overlay, which shares the VM's 512 MB RAM with the guest kernel and all processes.
2. At ~450 MB written, the guest kernel's OOM killer activates (kernel + init + guest agent consume ~60 MB).
3. OOM killer kills the job's main process (highest OOM score). The guest agent (OOM score adjusted to -1000 via `/proc/{pid}/oom_score_adj`) survives.
4. Guest agent reports exit code 137 via vsock. Host agent writes `end` metering record, sets `status = 'failed'`.
5. No impact on the host or other VMs — the 512 MB allocation is enforced by KVM's memory mapping, not by a soft limit.

**Evidence:** `job_events`: `running→failed`, exit 137. Host `free -m` unchanged.

**10. Worker spot-terminated without notice (hardware failure)**

**Scenario:** EC2 instance running 3 jobs is terminated instantly (no 2-minute warning).

1. All 3 Firecracker processes die with the instance. No snapshot is possible.
2. Reaper detects 3 jobs with expired `lease_expires_at` (30s after last heartbeat). Reaper calls `DescribeInstances` — instance is `terminated`.
3. Reaper sets all 3 jobs to `status = 'failed'`, `reason = 'worker_lost'`.
4. For jobs with `retry_policy = 'auto_idempotent'`: re-queued with fresh `idempotency_key` suffix (`:retry:1`). For `retry_policy = 'none'`: marked failed, tenant notified.
5. Billing: computed from `last_heartbeat.recorded_at - start.recorded_at` for each job. Maximum underbilling per job: 10s.

**Evidence:** All 3 jobs: `running→failed`, reason `worker_lost`. Metering gap ≤10s. Worker: `status='dead'`.

## §6 AI Strategy

No AI features at v1. No models, prompts, or token costs.

## §7 Testing and Release Confidence

Named adversarial tests (each runnable in CI):

| Test | What it does | Asserts |
|---|---|---|
| `test_vm_escape_attempt` | Job attempts to read host /etc/hostname via /proc, mount host filesystems, write to /dev/sda | All operations fail with EPERM or ENOENT; host filesystem unchanged |
| `test_cross_tenant_isolation` | Two jobs from different tenants on same worker; job B searches for job A's marker file and memory pattern | Job B finds nothing |
| `test_network_escape` | Job attempts connections to host IP, control plane, metadata endpoint, RFC1918 range, other VM TAP IPs | All connections time out or reset |
| `test_egress_enforcement` | Job with allowlist `['api.stripe.com']` attempts HTTPS to google.com | Proxy blocks with 403; Stripe connection succeeds |
| `test_cancel_sigterm_ignore` | Job traps SIGTERM and sleeps forever; cancel issued | Job reaches `canceled` within 10s; no Firecracker process remains |
| `test_billing_worker_crash` | Kill worker agent mid-job via SIGKILL | Job marked `failed`; billing computed from heartbeats; no underbilling >10s |
| `test_billing_no_forgery` | Job attempts to connect to PostgreSQL port | Connection dropped; only host-agent-originated metering records exist |
| `test_spot_evacuation` | Simulate spot termination (send SIGTERM to worker agent with spot flag) | Snapshot created, job re-queued, resumed on another worker, output matches |
| `test_webhook_triple_delivery` | Send identical webhook 3 times concurrently | Exactly 1 job created |
| `test_schedule_exactly_once` | Run 2 scheduler instances for 5 minutes on a per-minute schedule | Exactly 5 jobs created |
| `test_fork_bomb` | Job runs `:(){ :|:& };:` | Host CPU usage on that VM's cgroup stays ≤ 1 core; job can be canceled within 10s |
| `test_zip_bomb_artifact` | Job writes a gzip bomb to /output/ | Artifact stored at compressed size; host storage unchanged; no OOM on host |
| `test_secret_cleanup` | After job completes, search host disk for secret value | Not found |
| `test_dependency_install_in_sandbox` | npm package with malicious postinstall script attempts host filesystem access | Access fails; sandbox contains the install-time code |

## §8 Delivery Phases

**Phase 0 — Sandbox foundation (first end-to-end)**

Build: Firecracker VM lifecycle on a single host — boot, execute code, capture stdout, destroy. API endpoint to submit a job (no auth, no queue). PostgreSQL with `jobs` and `job_events` tables. Manual trigger only.

Exit criteria: submit a Node.js job via curl that prints "hello" → see "hello" in the job's S3 log → see `completed` status in the DB → run `test_vm_escape_attempt` and `test_cross_tenant_isolation` passing.

Justification for placing before Phase 1: the sandbox is the foundation — nothing else matters if isolation is broken.

**Phase 1 — Job lifecycle**

Build: job queue with fair-share scheduling, lease/heartbeat/reaper, cancel flow, timeout enforcement, webhook and schedule triggers, idempotency deduplication.

Exit criteria: `test_cancel_sigterm_ignore` passes. `test_webhook_triple_delivery` passes. `test_schedule_exactly_once` passes. A 10-minute job times out at exactly 10 minutes. The reaper marks a killed-worker job as failed within 60s.

**Phase 2 — Secrets, network policy, metering**

Build: Vault integration, MMDS secret injection, egress proxy with per-tenant allowlists, metering pipeline, billing computation.

Exit criteria: `test_secret_cleanup` passes. `test_egress_enforcement` passes. `test_network_escape` passes. `test_billing_worker_crash` passes. `test_billing_no_forgery` passes. Usage dashboard shows job cost within 5 minutes of completion.

**Phase 3 — Spot, snapshots, dependency cache, artifacts**

Build: spot instance support, snapshot evacuation, dependency cache (per-tenant S3), artifact upload with size cap, Python runtime.

Exit criteria: `test_spot_evacuation` passes (end-to-end: spot-terminated job resumes on new host, produces correct output). `test_zip_bomb_artifact` passes. `test_dependency_install_in_sandbox` passes. A Python job with `poetry.lock` installs cached dependencies on second run.

**Phase 4 — Production hardening**

Build: kill-switches (per-tenant suspend, per-worker drain, fleet-wide pause), rate limiting, monitoring dashboards, runbooks.

Exit criteria: suspending a tenant prevents new job creation and drains running jobs within `timeout_s`. Draining a worker evacuates running jobs to other workers. Fleet-wide pause stops all job claiming within 1s. 2-engineer on-call rotation: each alert has a runbook that a non-specialist can follow.

## §9 Security and Privacy

### Sandbox boundary

Covered in §1 and §4. Summary: Firecracker microVM, KVM hardware isolation, guest Linux 5.10 kernel, jailer-enforced seccomp-bpf (24 syscalls) + namespace isolation + chroot + unprivileged UID. Rootfs: read-only ext4 block device (O_RDONLY), writes to tmpfs overlay in guest RAM. No writable block device. User namespaces: not used inside the VM (the VM boundary is isolation, not user namespaces); VMM runs unprivileged via jailer `--uid`/`--gid`. Between consecutive jobs: nothing shared — Firecracker process killed, TAP device removed, chroot deleted. No sanitization needed because no mutable state persists.

### Network policy

Egress: all VM traffic routes through a host-side egress proxy (Envoy, listening on a link-local address on the VM's bridge). iptables FORWARD rules on the host:
- `ACCEPT` from `tap+` to the proxy's bridge IP on port 3128 (HTTP proxy port)
- `DROP` all other FORWARD from `tap+`

The VM's `/etc/environment` sets `HTTP_PROXY` and `HTTPS_PROXY` to the proxy address. For code that ignores proxy env vars and makes direct connections: those packets hit the FORWARD DROP rule and are silently discarded.

Per-tenant egress policy: Envoy's ext_authz filter calls the host agent's local endpoint, which checks `workflows.egress_domains` for the SNI (TLS) or Host header (HTTP). Requests to unlisted domains receive a 403 response from the proxy.

DNS: the VM's `/etc/resolv.conf` points to the proxy's DNS forwarder (running on the same bridge IP, port 53). iptables blocks the VM from reaching any other DNS server (port 53 UDP/TCP to any destination other than the proxy is DROPped). The DNS forwarder resolves only domains in the tenant's allowlist; queries for other domains return NXDOMAIN.

### Secret injection path

1. Host agent calls Vault API (`GET /v1/secret/data/{tenant_id}/{secret_name}`) with an AppRole token (TTL = job timeout + 60s, renewable: false).
2. Host agent writes secret data to Firecracker MMDS (`PUT /mmds` with JSON body).
3. VM boots. Guest agent (PID 1's child) reads MMDS at `http://169.254.169.254/` via HTTP GET.
4. Guest agent writes secrets to `/run/secrets/env` (tmpfs, mode 0400, owned by job user). This file is read by the runtime wrapper which `source`s it, then unlinks the file.
5. Host agent clears MMDS: `PUT /mmds` with `{}`. From this point, any MMDS read from inside the VM returns empty JSON.
6. Secrets exist only in the runtime process's environment memory. When the process exits and the VM is destroyed, the memory is reclaimed.

What "redact known secret strings from logs" cannot catch: base64-encoded secrets, split-across-lines, Unicode homoglyph substitution, secrets embedded in binary output. Our mitigation is structural, not pattern-based: customer stdout/stderr is stored exclusively in the tenant's own log prefix (`logs/{tenant_id}/`). It is never indexed, searched, or processed by any platform system. System logs contain only job metadata (IDs, status, timestamps). A secret leaked to stdout is visible only to the tenant who owns it — which is equivalent to the tenant logging their own secret in their own application.

### Dependency supply chain

"Pinned dependencies" means: a lockfile (`package-lock.json` for Node, `poetry.lock` for Python) is required at workflow version upload. The platform stores `lockfile_sha256` in `workflow_versions`. At execution time, the guest agent verifies `sha256sum lockfile == workflow_versions.lockfile_sha256` before running install.

Lockfiles contain integrity hashes for every package (npm: `integrity` field with `sha512-` prefix; Poetry: `content-hash` in metadata). If a registry replaces a package with different content, the integrity check fails and install aborts.

Install-time code execution (npm `postinstall` scripts, `setup.py`): runs INSIDE the sandbox. This is the only correct answer — running install-time code outside the sandbox allows arbitrary code from the npm/PyPI registry to execute on the host, bypassing all isolation.

Dependency cache: stored in S3 at `cache/{tenant_id}/{runtime}/{lockfile_sha256}.tar.zst`. The `tenant_id` path prefix prevents cross-tenant cache poisoning. The S3 bucket policy restricts worker IAM role access to `cache/{tenant_id}/*` via STS session policy with a condition on the job's tenant_id. Even a host agent bug cannot read another tenant's cache — IAM enforces the boundary.

### Incident containment kill-switches

1. **Per-tenant suspend:** set `tenants.suspended = true`. API rejects new jobs (`WHERE suspended = false` in insert). Running jobs continue to completion (or cancel them individually). Activation: API call or direct DB update.
2. **Per-worker drain:** set `workers.status = 'draining'`. Worker stops claiming new jobs. Running jobs complete or are evacuated (snapshot + re-queue if spot). Activation: API call.
3. **Fleet-wide pause:** insert a row in a `kill_switches` table (`scope = 'global'`). All workers check this table on every poll cycle; if a global kill switch exists, they stop claiming. Running jobs continue. Activation: API call or direct DB update.
4. **Per-region:** same as fleet-wide, scoped by `region` column.

Evidence trail for isolation-suspected incidents: `job_events` (full state machine history), `metering_records` (execution timeline), host agent's structured logs (Firecracker API calls with timestamps), Envoy access logs (all egress requests with SNI/Host, response codes), VPC Flow Logs (network-level evidence).

## §10 Risk Register

| # | Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|---|
| R1 | KVM escape vulnerability | Low (1-2 CVEs/year historically, most require specific guest kernel config) | Critical — cross-tenant access | Pin Firecracker version, subscribe to AWS security bulletins, patch within 24h. Jailer's seccomp filter limits VMM to 24 syscalls, reducing post-escape capability. |
| R2 | Spot capacity unavailable in-region | Medium (happens during re:Invent, large launches) | High — job queue backs up | 40% on-demand floor handles sustained load. Long jobs preferentially scheduled to on-demand. Multi-AZ fleet. |
| R3 | PostgreSQL single-writer bottleneck | Low at 100K/day (30 writes/sec peak) | Medium — queue latency | Connection pooler (PgBouncer, 50 connections). If write throughput exceeds 1000/sec, shard by tenant_id hash to 4 PostgreSQL instances. Assumption: this threshold is not reached at launch scale (§13). |
| R4 | Snapshot restore CPU mismatch | Low (single instance family) | Medium — job fails to resume | Pre-flight check: compare CPUID on destination vs snapshot metadata. Reject mismatches, re-queue to compatible host. |
| R5 | Secret exposure via guest agent vulnerability | Low | Critical | Guest agent is a 200-line static binary (no network, no dynamic linking, no user input beyond MMDS JSON). Attack surface is the JSON parser. Use Go's `encoding/json` (memory-safe). |

## §11 Explicit Tradeoffs

1. **Non-idempotent jobs are not retried on worker loss.** The platform marks them `failed` instead of re-running. A re-run of non-idempotent code (e.g., "charge my users") could cause real-world harm. The tenant must opt into `auto_idempotent` retry policy and accept responsibility for idempotency. This weakens the implicit expectation that the platform "just handles" worker failures, but it is the only safe default when code is assumed hostile and side effects are unknown.

2. **Snapshot restore is limited to the same instance family.** This prevents using cheaper instance types for resumed jobs. The constraint exists because Firecracker snapshot compatibility requires matching CPU features. Accepted because CPU homogeneity simplifies fleet management for a 2-engineer team.

3. **Maximum underbilling of 10s on worker crash.** If the host agent dies between heartbeats, up to 10s of execution time is not billed. Accepted because over-billing (charging for time after a crash) is worse than under-billing, and 10s at $0.005/min = $0.0008 maximum loss per incident.

4. **Artifacts from crashed-worker jobs are lost.** The host agent uploads artifacts after job completion. If the agent crashes before upload, artifacts are unrecoverable (they were in VM RAM). Accepted because the alternative (streaming artifacts during execution) adds complexity and latency for a rare failure case.

## §12 Where This Is Stronger Than Required

1. **Hardware-level isolation (KVM) instead of the minimum viable sandbox.** gVisor or even seccomp-hardened containers might satisfy "hostile code" at lower operational cost. Firecracker's KVM boundary provides defense-in-depth: a container escape CVE affects us only if it's also a KVM escape CVE, which is a drastically smaller set. For a platform where cross-tenant read is "company-ending," the extra isolation boundary justifies the operational cost of managing VM images.

2. **IAM-enforced cache isolation.** Code-level tenant scoping (constructing the right S3 path) would be sufficient if the code is correct. STS session policies add a second enforcement layer at the AWS API level, so a path-construction bug cannot cause cross-tenant cache reads. Belt-and-suspenders on a data-isolation boundary.

## §13 Assumptions

1. AWS c6i instances remain available in at least 2 AZs in the deployment region. If c6i is deprecated, the fleet migrates to the successor family with matching CPU features.
2. PostgreSQL write throughput of 30 transactions/sec (peak job claim rate) is well within a single db.r6g.xlarge instance's capacity. Sharding is deferred until writes exceed 1000/sec.
3. Tenants provide a lockfile. The platform rejects workflow versions uploaded without one. No "auto-generate lockfile" feature.
4. The 100 MB artifact size cap and 10 MB log size cap are acceptable to tenants. These are configurable per-tenant in a future release but not at v1.
5. The 2-engineer team uses Infrastructure-as-Code (Terraform) for fleet management. No manual EC2 instance management.
6. Vault is operated as a managed service (HCP Vault) — not self-hosted. This eliminates Vault operations from the 2-engineer team's responsibilities.
7. Firecracker snapshot restore across hosts in the same c6i instance family succeeds reliably. If CPU microcode updates break compatibility, the fallback is to re-run the job from the start (with tenant notification).
8. Job duration tier (`fast`/`medium`/`long`) is declared by the tenant when configuring the workflow. Misclassification (declaring a 4-hour job as "fast") results in the job occupying a fast-tier slot, which self-corrects as the job hits its timeout.

### Capacity and Cost

**Fleet sizing from load shape:**
- Sustained: 1.2 jobs/sec × 25s average duration (weighted: 80% × 4s + 15% × 60s + 4% × 600s + 1% × 3600s = 72s; using 25s as conservative median-weighted estimate for concurrency) = 30 concurrent jobs.
- Peak: 30 jobs/sec × 25s = 750 concurrent jobs.
- c6i.xlarge (4 vCPU, 8 GB): runs 4 default jobs (1 vCPU / 512 MB each) or 1 max job (4 vCPU / 8 GB).
- Sustained fleet: 30 / 4 = 8 hosts. Peak fleet: 750 / 4 = 188 hosts.
- With 60% spot: peak = 75 on-demand + 113 spot.

**Cost per execution:**
- c6i.xlarge on-demand: $0.17/hr. Spot: $0.051/hr (70% discount, us-east-1 historical median).
- Blended rate (40% OD + 60% spot): $0.17 × 0.4 + $0.051 × 0.6 = $0.0986/hr per host.
- Per-host-second: $0.0986 / 3600 = $0.0000274.
- Per-job-vCPU-second: $0.0000274 / 4 (jobs per host) = $0.00000685.
- p50 job (4s, 1 vCPU): $0.00000685 × 4 = $0.0000274.
- p95 job (180s, 1 vCPU): $0.00000685 × 180 = $0.00123.

**Pricing:** $0.00005/vCPU-second + $0.000005/MB-second.
- p50 revenue: (4 × $0.00005) + (4 × 512 × $0.000005) = $0.01044. Cost: $0.0000274. Margin: 99.7%.
- p95 revenue: (180 × $0.00005) + (180 × 512 × $0.000005) = $0.4698. Cost: $0.00123. Margin: 99.7%.
- Memory pricing dominates revenue by design — memory is the scarcer resource per host.
- Monthly revenue (100K exec/day, 80% at p50): 80,000 × $0.01044 × 30 = $25,056/mo.
- Monthly infra (8 avg hosts × 24h × $0.0986/hr × 30d = $568) + RDS $300 + S3 $100 + Vault $200 + egress $200 = $1,368/mo. Gross margin: 94.5%.
- Worst-case concurrent demand bounded by `tenants.max_concurrent` (20) and long-tier cap (2 per tenant). Maximum theoretical: 5,000 × 2 = 10,000 long jobs. This triggers the fleet-wide kill switch; the 2-engineer team investigates before scaling.
