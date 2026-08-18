# Patchbay — Plan Evaluation (Reviewer: Fable)

## FINAL RECONCILED RANKING

Reconciled with GPT/Codex independent review (`analysis-gpt.md`) per
`grader-instructions.md` §2. Verifiable disagreements resolved by performing
the check (binding on both graders); judgment scores averaged; ranking
direction agrees (model-2 > model-1). See §R at the bottom for the full
reconciliation ledger.

| Rank | Plan | Reconciled V | Reconciled J | Total | Fable solo | GPT solo |
|---:|---|---:|---:|---:|---:|---:|
| 1 | **model-2** | 49.5 | 19.25 | **68.75** | 95.0 | 64.5 |
| 2 | **model-1** | 24.0 | 17.0 | **41.0** | 62.5 | 44.5 |

Fable's original review was too lenient on model-2 (missed: no production
jailer for the VMM, GRANT gaps in DDL, cross-tenant FK gap, VPC SG
allow-only claim, log-retention conflict, capacity-volume contradiction, and
secret persistence against the brief's literal text). GPT's original review
was too harsh on model-2's `claim_job()` concurrency (the tenant-row lock in
`FOR UPDATE OF j, t SKIP LOCKED` serializes claims per tenant — the race GPT
describes does not materialize). Net effect: model-2 drops 26.25 points from
Fable's solo score; model-1 drops 21.5. The gap narrows from 32.5 to 27.75
but the ranking is unchanged. The pre-reconciliation analysis is preserved
unmodified below.

---

Hostile review of two plans in `patchbay/` (`model-1`, `model-2`) against
`product.md`, `engineering-plan-standard.md`, scored per the adapted rules of
`grader-instructions.md`.

**Protocol adaptation (stated up front).** `grader-instructions.md` governs
plan evaluations; `review-protocol.md` governs executable submissions. These
are plans, so nothing can be built and run. I applied the scoring machinery
to what is mechanically checkable in a plan per `grader-instructions.md` §4:
DDL that parses and does what it says, Firecracker/PostgreSQL/AWS facilities
that exist as named (R10), arithmetic that recomputes, and walkthroughs whose
steps survive an adversarial trace against the declared DDL and state table.
Each plan's §4 invariant-enforcement map is treated as the **claims register**:
a named mechanism shown false by trace is a confirmed finding; findings are
CONFIRMED only when I can cite the exact plan text and give a definitive
facility-level refutation or step-numbered trace; otherwise PLAUSIBLE and
moves no score. Section scores use the product.md §4 weighting table, each
split into verifiable (≥70%) and judgment (≤30%) per `grader-instructions.md`
§3.

Severity weights per protocol §4: CRITICAL −25, HIGH −12, MEDIUM −5, LOW −2.
Doubling when a broken mechanism is claimed in the invariant map and the claim
is false. No doubling when the invariant holds through an alternate mechanism
the plan also names.

---

## Verdict

| Rank | Plan | Verifiable (of 70) | Judgment (of 30) | Total |
|---:|---|---:|---:|---:|
| 1 | **model-2** | 70.0 | 25.0 | **95.0** |
| 2 | **model-1** | 40.5 | 22.0 | **62.5** |

model-2 survives with zero confirmed findings — its DDL executes, its API
references check against documentation, its arithmetic recomputes, and its
state machine is complete. model-1 takes −33 across seven confirmed findings,
dominated by an R10 violation (a Firecracker API endpoint that does not
exist) and two state-machine gaps (no boot-failure transition, queued-cancel
stuck in a non-terminal state with no mechanism to escape). The judgment gap
is small (3 points); the verifiable gap is not (29.5 points).

---

## Section-by-section scores

| Section | Wt | m-1 V | m-1 J | m-1 Tot | m-2 V | m-2 J | m-2 Tot |
|---|---:|---:|---:|---:|---:|---:|---:|
| §4 Invariants | 30 | 4.0 | 7.0 | 11.0 | 21.0 | 7.0 | 28.0 |
| §3 Data model | 20 | 7.0 | 3.5 | 10.5 | 14.0 | 5.0 | 19.0 |
| §5 Walkthroughs | 15 | 8.5 | 3.5 | 12.0 | 10.5 | 3.5 | 14.0 |
| §9 Security | 10 | 7.0 | 2.5 | 9.5 | 7.0 | 3.0 | 10.0 |
| §8 Delivery | 10 | 7.0 | 2.5 | 9.5 | 7.0 | 2.5 | 9.5 |
| §7 Testing | 10 | 7.0 | 2.5 | 9.5 | 7.0 | 2.5 | 9.5 |
| Cost arithmetic | 5 | 0.0 | 0.5 | 0.5 | 3.5 | 1.5 | 5.0 |
| **Total** | **100** | **40.5** | **22.0** | **62.5** | **70.0** | **25.0** | **95.0** |

---

## Cross-model observations

**X1 — Snapshot-resume vs. rerun-from-scratch.** The central design split.
model-1 commits to Firecracker snapshot/restore for spot-terminated jobs
(W1: pause VM → snapshot memory to S3 → resume on another host). model-2
explicitly rejects this (§11.2: "Firecracker has no live migration; CRIU over
a process tree with network sockets is not dependable") and reruns from
scratch with `billable=FALSE` on the interrupted attempt. model-1's approach
preserves 3h58m of execution but adds the `suspended` state, snapshot S3
storage, and the assumption that CPU features match across hosts. model-2's
approach is simpler, loses progress, but has no snapshot-compatibility
failure mode. Neither approach is wrong — but model-2 discloses and prices
the tradeoff (§11.2, §11.3), while model-1 assumes snapshot restore "works
reliably" (§13.7) without a fallback path for when it doesn't.

**X2 — DDL-level billing integrity.** model-2 enforces billing immutability
structurally: hash-chained `metering_ticks` (CHECK constraint + `check_chain`
trigger), `reject_mutation` trigger blocking UPDATE/DELETE, S3 Object Lock
mirror with COMPLIANCE retention. model-1's billing integrity relies on
application code (host agent writes records) and network isolation (iptables
blocks guest from DB). Both satisfy the brief's literal requirement
("customer code cannot inflate, deflate, or forge"), but model-2's approach
survives internal threats (compromised host agent, DB admin error) while
model-1's does not. model-2 correctly identifies this as exceeding the
requirement (§12.2).

**X3 — Database role separation.** model-2 defines five PostgreSQL roles
(`role_api`, `role_scheduler`, `role_controlplane`, `role_worker`,
`role_billing`) with explicit GRANT statements bounding what each process can
write. `claim_job()` is `SECURITY DEFINER`, so the worker role only executes
the function — it cannot run arbitrary claim queries. model-1 defines no
roles; every process has full access. This is a defense-in-depth difference,
not a finding (model-1's invariants don't claim DDL-level access control),
but it widens the gap on §3 judgment.

---

## model-1 — Findings

### F1-1 · CONFIRMED · HIGH · −12 · §4

**Firecracker does not have a `PUT /actions {"action_type": "Stop"}` endpoint.**

The cancellation sequence (I5) and walkthrough W2 claim: "worker calls
`PUT /actions` with `{"action_type": "Stop"}` — halts vCPU threads
immediately (not a signal; the VMM stops the KVM vCPU ioctl loop). VM is
stopped."

Firecracker's `PUT /actions` endpoint accepts three action types:
`InstanceStart`, `SendCtrlAltDel`, and `FlushMetrics`. There is no `Stop`
action. The VM can be paused via `PATCH /vm {"state": "Paused"}` (which
model-1 correctly uses in W1 for snapshot), or force-stopped by killing the
Firecracker process. The plan DOES include SIGKILL as the subsequent step
("Worker kills the Firecracker process (SIGKILL)"), so the cancellation
invariant holds through that mechanism. But the intermediate step attributes
a specific vCPU-halt capability to a Firecracker API that does not exist.

product.md: "misattributing a security property to a tool that does not
provide it is the single heaviest deduction in this eval." The `Stop` action
is a security property (force-stop a VM's vCPU for promise 4) attributed to
a tool that does not provide it (in that form). Scored HIGH rather than
CRITICAL because the plan includes the correct mechanism (SIGKILL) alongside
the incorrect one, so the invariant holds. Not doubled: the invariant
(canceled = not executing) is enforced by SIGKILL.

### F1-2 · CONFIRMED · MEDIUM · −5 · Cost

**Capacity arithmetic uses a fabricated "median-weighted estimate."**

> "Sustained: 1.2 jobs/sec × 25s average duration (weighted: 80% × 4s +
> 15% × 60s + 4% × 600s + 1% × 3600s = 72s; using 25s as conservative
> median-weighted estimate for concurrency) = 30 concurrent jobs."

The weighted mean is 72.2s. The plan substitutes 25s, calling it a
"conservative median-weighted estimate" — a term with no statistical
meaning. The correct approach is Little's law: L = λ × W = 1.2 × 72.2 =
86.6 concurrent jobs (not 30). At peak: 30 × 72.2 = 2,166 concurrent (not
750). This underestimates fleet requirements by ~2.9× and cascades through
all cost calculations.

Resulting fleet: model-1 claims 8 hosts sustained / 188 peak. Correct
values: ~22 sustained / ~540 peak. Monthly compute cost understated by
~2.8×. R5 requires arithmetic with derivation; substituting a fabricated
concept for the mean is an arithmetic failure.

### F1-3 · CONFIRMED · LOW · −2 · Cost

**Revenue calculation counts only 80% of jobs.**

> "Monthly revenue (100K exec/day, 80% at p50): 80,000 × $0.01044 × 30 =
> $25,056/mo."

The remaining 20% of jobs (at 60s, 600s, 3600s durations) generate far
higher per-job revenue — the 1% at 3600s alone yields ~$9.40/job vs
$0.01/job at p50. Total monthly revenue is roughly $565K, not $25K. The
margin of 94.5% is computed against this partial revenue and understated
costs, making it unreliable as a business metric. R5 requires complete
arithmetic.

### F1-4 · CONFIRMED · MEDIUM · −5 · §3

**State table missing boot-failure transitions from `leased` and
`preparing`.**

The state transition table shows:

> | leased | preparing | VM boot initiated |
> | preparing | running | VM ready, code executing |

There is no `leased → failed` or `preparing → failed` transition for boot
failure (corrupt rootfs, Firecracker API error, snapshot restore failure).
A job that fails to boot is stuck: `preparing` has no exit to a terminal
state except through `running`, and the reaper monitors `lease_expires_at`
— which handles `leased` (§2: "detects expired leases") but the plan does
not describe what happens to a job in `preparing` with an expired lease.
The `jobs_lease_expiry` index covers `WHERE status IN
('leased','preparing','running')`, suggesting the reaper SHOULD handle
`preparing`, but no state transition permits `preparing → failed`.

model-2 explicitly handles this: `starting | failed | boot failure | host
agent report → ApplyReport (fail_reason='boot_failed')`.

### F1-5 · CONFIRMED · MEDIUM · −5 · §4

**Queued-job cancel stuck in `canceling` with no mechanism to reach terminal
state.**

The state table shows:

> | queued | canceling | user cancel | API `UPDATE ... WHERE status='queued'` |
> | canceling | canceled | VM stopped or never started | worker confirms VM
>   halted |

For a queued job: no worker has claimed it (`worker_id` is NULL), no VM
exists, and no NOTIFY is sent (the `pg_notify('cancel', worker_id || ':'
|| job_id)` requires a `worker_id`). The `canceling → canceled` transition
requires "worker confirms VM halted" — but there is no worker. The reaper
monitors `lease_expires_at < now()` — but a queued job has no lease
(`lease_expires_at` is NULL). No described mechanism transitions the job
from `canceling` to `canceled`.

The job is stuck in `canceling` indefinitely. The UI would show "canceling"
forever for a job that never executed.

model-2 handles this correctly: `queued | canceled | user cancel |
CancelJob: UPDATE WHERE status='queued' (nothing executes)` — direct
transition, no intermediate state for a job that never started a VM.

### F1-6 · CONFIRMED · LOW · −2 · §5

**Inline self-correction left in W7 (port scan walkthrough).**

> "the egress proxy's IP on the host bridge (which is in a non-RFC1918
> range on the bridge, e.g., 172.31.0.1 — wait, that's RFC1918).
> Correction: the VM-to-proxy communication uses a point-to-point
> link-local pair (169.254.x.y, configured per-VM, distinct from the MMDS
> address)."

The author realized mid-write that 172.31.x.x is RFC1918 (conflicting with
the FORWARD DROP rules), corrected to link-local, but left the correction
inline. This reads as a draft artifact, not a reviewed plan, and reveals
that the initial network design had an internal inconsistency.

### F1-7 · CONFIRMED · LOW · −2 · §3

**No `claim_job` function in DDL.**

The queue claim logic is described in prose (§4 I1: "`SELECT ... FOR UPDATE
SKIP LOCKED WHERE status='queued'`; concurrent-job count < `tenants.max_concurrent`
checked in same txn") but no SQL function appears in the §3 DDL. R2
requires: "If a constraint cannot be expressed in DDL, say which code path
enforces it and name the function." model-1 describes the constraint without
formalizing or naming the function. model-2 provides a complete `claim_job()`
function with `SECURITY DEFINER` and the full claim logic in DDL.

---

## model-1 — Confirmed findings summary

| # | Finding | Sev | Wt | Section |
|---|---|---|---:|---|
| F1-1 | Non-existent Firecracker `Stop` action (R10) | HIGH | −12 | §4 |
| F1-2 | Fabricated "median-weighted estimate" in capacity math | MED | −5 | Cost |
| F1-3 | Revenue calculation counts only 80% of jobs | LOW | −2 | Cost |
| F1-4 | State table missing boot-failure transitions | MED | −5 | §3 |
| F1-5 | Queued-cancel stuck in `canceling` (no exit mechanism) | MED | −5 | §4 |
| F1-6 | Inline self-correction left in W7 | LOW | −2 | §5 |
| F1-7 | No claim_job function in DDL | LOW | −2 | §3 |
| | **Total** | | **−33** | |

---

## model-1 — PLAUSIBLE (recorded, no score movement)

**P1-1.** `jobs_active_per_tenant` index excludes `canceling` — a job in
`canceling` state may still be executing (up to 10s), allowing a new claim
to temporarily exceed `max_concurrent`. Defensible design choice (don't let
a canceling job block new work), but not discussed.

**P1-2.** Snapshot timing is tight for 8 GB VMs on c6i.xlarge. c6i.xlarge
has 8 GB RAM; snapshotting an 8 GB VM to tmpfs leaves zero host RAM. The
plan mentions c6i.2xlarge (16 GB) for 4-vCPU jobs, which provides headroom.
Plausible concern under specific workload mixes.

**P1-3.** `priority` column with negative-count fairness (`-(SELECT count(*)
FROM jobs WHERE tenant_id = $1 AND status = 'queued')`) is computed
non-atomically — between the count and the insert, another job could be
inserted, making the priority stale. Minor fairness skew, not a correctness
issue.

---

## model-2 — Findings

**No confirmed findings.**

Every mechanical check passes: DDL executes top-to-bottom (pgcrypto
extension, then roles, then functions, then tables with no forward
references, then triggers, then the claim function, then grants); every
Firecracker facility named is a real capability (SIGKILL, vsock, PATCH /vm
Paused, seccomp); every PostgreSQL facility exists (`FOR UPDATE SKIP LOCKED`,
`pg_advisory_lock`, `SECURITY DEFINER`, CHECK constraints calling IMMUTABLE
functions); every AWS facility is real (KMS envelope encryption, S3 Object
Lock COMPLIANCE mode, IMDS disable, SQS spot notices); arithmetic
recomputes (Little's law, fleet sizing, unit costs, margin — all verified
below); state machine is complete (every state reachable, terminals
absorbing, every failure mode has a named transition); and walkthroughs
reference only states, columns, and transitions the DDL defines.

---

## model-2 — Arithmetic verification

| Claim | Check | Result |
|---|---|---|
| Duration mean 57.9s | 0.6×4 + 0.25×30 + 0.10×180 + 0.05×600 | 57.9 ✓ |
| Mean concurrent 67.0 jobs | 1.157 × 57.9 | 67.0 ✓ |
| Mean vCPU 87.1 | 67.0 × 1.3 | 87.1 ✓ |
| Peak concurrent 1,737 jobs | 30 × 57.9 | 1,737 ✓ |
| Peak vCPU 2,258 | 1,737 × 1.3 | 2,258 ✓ |
| Mean fleet 6 hosts | ceil(87.1×1.1/16) | 6 ✓ |
| Peak fleet 156 hosts | ceil(2,258×1.1/16) | 156 ✓ |
| Spot/OD split 94/62 | 156×0.6 / 156×0.4 | 93.6/62.4 ✓ |
| Compute $/day $164.6 | 7,104 × 0.023165 | $164.6 ✓ |
| Mean billed seconds 99s | 0.6×60+0.25×60+0.10×180+0.05×600 | 99 ✓ |
| Mean revenue/job $0.006435 | 99 × 0.000065 | $0.006435 ✓ |
| Monthly revenue $19,953 | 100K×0.006435×30 + $648 | $19,953 ✓ |
| Monthly cost $6,692 | 4,937+1,172+583 | $6,692 ✓ |
| Margin 66.5% | (19,953−6,692)/19,953 | 66.5% ✓ |
| p50 revenue $0.003108 | 60×0.00005 + 0.00108×0.10 | $0.003108 ✓ |
| p50 cost $0.000770 | 4×0.00002186+0.000585+0.0000972 | $0.000770 ✓ |
| p50 margin 75% | 0.002338/0.003108 | 75.2% ✓ |

model-2's 60-second minimum bill is explicitly justified (§11.5: "fixed-cost
amortization exceeds a 4-s job's compute price") and correctly flows through
the revenue calculation (a 4s job billed for 60s). model-1 has no minimum
bill; its pricing implicitly assumes every second of execution generates
enough margin, but the understated fleet cost undermines this claim.

---

## model-2 — PLAUSIBLE (recorded, no score movement)

**P2-1.** W1 (spot) describes a running→failed(worker_lost) transition via
`ApplyReport` (host agent proactively kills VM and reports). The state table
lists this transition only via `HeartbeatChecker`. Both reach the same
terminal state; the walkthrough describes an optimization path the state
table omits. Minor state-table incompleteness, not an invariant violation.

**P2-2.** `workers` table has no `ram_mb` or resource-capacity columns.
`claim_job()` doesn't check whether the claiming worker has sufficient
resources for the job's tier. The host agent manages capacity locally, and
cgroup enforcement (invariant 10) prevents overcommit at the OS level. Gap
is in DDL defense-in-depth, not in the invariant mechanism.

**P2-3.** `schedules` table has no `next_fire_at` column (model-1 has one);
the scheduler computes fire times from `cron` + `last_fired_at` on each
tick. No index supports this computation (model-1 has `schedules_pending ON
schedules (next_fire_at) WHERE enabled = true`). Performance concern at
scale, not correctness.

**P2-4.** Fair-share ordering in `claim_job()` is
`ORDER BY (max_duration_s <= 60) DESC, created_at ASC` — FIFO within
duration classes, no cross-tenant interleaving. A tenant dumping 50 short
jobs (max_queued_jobs=50) delays other tenants' short jobs. Per-tenant caps
(max_concurrent_jobs=2) limit the blast radius. model-1's negative-count
priority provides finer-grained fairness. Design tradeoff, not a defect —
the invariant (I8: "4-h jobs cannot starve 4-s jobs") holds because short
jobs are always ordered first.

---

## model-1 — Strengths (quoted, checkable)

**S1-1. Snapshot-resume for spot.** The most ambitious design choice in
either plan. The W1 walkthrough is technically sound (correct Firecracker
APIs for pause, snapshot create, snapshot load) and would save tenants hours
of re-execution. The `suspended` state, priority boost, and multi-segment
billing are coherent. The assumption (§13.7: snapshot restore across hosts
in the same instance family succeeds reliably) is the weak point.

**S1-2. Cross-tenant fairness.** The negative-count priority
(`-(SELECT count(*) FROM jobs WHERE tenant_id = $1 AND status = 'queued')`)
interleaves tenants in queue order, preventing a high-volume tenant from
monopolizing the queue. model-2's FIFO ordering lacks this property.

**S1-3. Ten walkthroughs with good coverage.** W5 (zip bomb: tmpfs bounded
by VM memory, host never decompresses) and W6 (metadata redirect: MMDS
intercepts, cleared after secret read) are well-reasoned and correctly
reference Firecracker's MMDS behavior.

**S1-4. Thorough security section.** The secret injection path
(Vault → MMDS → guest reads → MMDS cleared) is specific and correct. The
`prctl(PR_SET_DUMPABLE, 0)` and `core_pattern` empty references are valid
Linux APIs.

---

## model-2 — Strengths (quoted, checkable)

**S2-1. Hash-chained, trigger-enforced, Object-Lock-mirrored billing.**
`metering_ticks` has: `reject_mutation` trigger (blocks UPDATE/DELETE),
`check_chain` trigger (verifies `prev_hash` against predecessor's
`row_hash`), CHECK constraint (verifies `row_hash` matches `tick_hash()`
computation), and S3 Object Lock mirror (`ObjectLockMode: COMPLIANCE`,
5-year retention). The chain is anchored to `jobs.code_sha256` — tampering
with the anchor breaks the chain, making it self-detecting. This is the
strongest billing-integrity mechanism of the two plans by a wide margin.

**S2-2. Complete state machine with explicit boot-failure path.** Every
non-terminal state has a named exit for every failure class: boot failure
(`starting → failed`), lease expiry (`leased → queued`), worker loss
(`running → failed`), deadline (`running → timed_out` via guest init +
`DeadlineChecker` backstop), and queued expiry (`queued → failed` at 24h).
The `cancel_requested` intermediate state requires `ConfirmDestruction` (host
agent `vm_destroyed` OR EC2 `terminated`) before writing the terminal
`canceled`/`timed_out` — the state machine itself enforces promise 4.

**S2-3. Four independent layers against the metadata endpoint.** (a)
Instance-level IMDS disabled (`HttpEndpoint=disabled`); (b) host nftables
DROP dst 169.254.169.254; (c) proxy IP denylist (10/8, 172.16/12,
192.168/16, 169.254/16, 127/8, 100.64/10, VPC CIDR); (d) VPC SG no egress
to 169.254.0.0/16. Each is independently verifiable; any single one holds.
model-1 relies on two layers (MMDS interception + iptables FORWARD DROP).

**S2-4. Database role separation with least-privilege GRANTs.**
`role_worker` can only INSERT into `metering_ticks` and UPDATE `workers` —
it cannot touch `jobs`, `tenants`, `secrets`, or `invoice_lines`. A
compromised worker process cannot modify job state or read secrets. model-1
has no role separation.

**S2-5. Eight explicit tradeoffs (§11) with justifications.** Each names
what is weaker, why, and the escape hatch. Notably: reruns duplicate side
effects for non-idempotent workflows (escape: `rerun_on_platform_fault=
FALSE`); canceled jobs billed for consumed seconds; no log redaction (and
why pattern-based redaction fails); 60-second minimum bill justified by
unit economics; single-region Postgres justified by 2-engineer constraint.

**S2-6. `claim_job()` as a SECURITY DEFINER function with tenant-level
locking.** `FOR UPDATE OF j, t SKIP LOCKED` locks the tenant row in the
same transaction as the job claim, preventing two workers from
simultaneously reading count < max_concurrent for the same tenant.
Concurrent-job enforcement is atomic at the database level, not a
check-then-act in application code.

**S2-7. Correct Firecracker API usage throughout.** No non-existent API
endpoints. The cancellation sequence uses SIGTERM (via guest init), SIGKILL
(via guest init at +5s), and Firecracker process SIGKILL (via host agent) —
all real mechanisms. `PATCH /vm {"state": "Paused"}` is not used for cancel
(correctly reserved for snapshot in model-1 but not needed in model-2's
no-snapshot design).

---

## Judgment notes

**model-1 judgment (22/30):** The plan is architecturally literate — the
Firecracker isolation argument, MMDS secret path, and Envoy egress proxy
are well-chosen. The snapshot-resume design is ambitious and would be
genuinely valuable if reliable. The 14 named adversarial tests cover the
required scenarios. Deductions: DDL lacks role separation, append-only
enforcement, and the claim function; the cost model's fabricated metric
undermines trust in the arithmetic; the inline self-correction reveals
insufficient review of the final document. The plan reads as a strong draft
one editing pass short of submission quality.

**model-2 judgment (25/30):** The plan is operationally focused — every
decision explicitly weighs 2-engineer operability (D2: "Operability at 2
engineers outranks features the orchestrator re-implements anyway"; D4:
"One fewer service for 2 engineers"). The DDL is the most complete of the
two plans: hash chains, triggers, roles, claim function, and a SECURITY
DEFINER boundary. The cost model is thorough, with worst-case analysis and
mitigation. Deductions: no workflow versioning table (code_sha256 on
workflows directly, no version history); the custom Go egress proxy is
higher implementation risk than model-1's Envoy; the fair-share mechanism
is coarser than model-1's negative-count priority (FIFO within duration
classes, no cross-tenant interleaving). The plan reads as
implementation-ready.

---

## One-line verdicts

**model-1:** Architecturally ambitious (snapshot-resume, Envoy proxy,
cross-tenant fairness) but undercut by an R10 violation on the cancel
mechanism, two state-machine gaps, and broken cost arithmetic — a plan
with strong ideas and insufficient self-verification.

**model-2:** Mechanically sound across every checkable dimension — correct
APIs, complete state machine, verified arithmetic, structural billing
integrity — at the cost of a simpler spot-termination strategy (rerun, not
resume) and a coarser fair-share mechanism.

---

## §R — Reconciliation with GPT/Codex review

Per `grader-instructions.md` §2: verifiable claims are resolved by performing
the check (binding outcome); judgment calls are averaged; disagreements are
reported with both orderings if they persist. Both reviewers rank model-2
first; no ordering disagreement exists.

### R.1 GPT findings on model-2 — disposition

**ACCEPTED (8 findings, −22 total):**

| # | GPT finding | My check | Sev | Wt | Section |
|---|---|---|---|---:|---|
| R2-1 | No production jailer for VMM | D1 says "VMM managed by the host agent (root, no network)." §9 describes guest seccomp (~150 syscalls), not VMM seccomp. No chroot, unprivileged UID, or VMM-specific namespaces. Firecracker docs recommend the jailer for production. The brief asks for "syscall policy" — answered for guest, not for VMM. | MED | −5 | §4 |
| R2-2 | Cross-tenant FK gap | `jobs(tenant_id, workflow_id)` — independent FKs. No composite FK or CHECK preventing tenant-A job referencing tenant-B's workflow. Same in `schedules`. Defense-in-depth gap; primary isolation is via Firecracker VMs and S3 paths, not FK relationships. | LOW | −2 | §3 |
| R2-3 | GRANT gaps: INSERT on workers, INSERT on schedules, REVOKE from PUBLIC | DDL verified: no role has INSERT ON workers (registration blocked); no role has INSERT ON schedules (creation blocked); `claim_job()` is SECURITY DEFINER but EXECUTE not revoked from PUBLIC (PG defaults PUBLIC execute on new functions). | MED | −5 | §3 |
| R2-4 | VPC SG allow-only (not independent deny layer) | AWS SGs contain allow rules only. A standard 0.0.0.0/0 outbound allow (needed for internet egress) implicitly admits 169.254.0.0/16. The "four independent layers" claim credits the SG as an independent deny — it cannot serve this role as specified. Three layers remain valid (IMDS disabled, nftables, proxy denylist). | LOW | −2 | §9 |
| R2-5 | Capacity contradiction (308K vs 100K/day) | A2: "30/s peak is a 2 h daily burst." 30/s × 7,200s = 216,000 executions before off-peak. Adding 22h × 1.157/s = ~92K gives ~308K/day. The brief says 100K/day. The fleet and cost are sized for 308K; the revenue is priced for 100K. Direction is conservative (overstates cost), but an error is an error. | LOW | −2 | Cost |
| R2-6 | Log retention conflict (90d vs 30d) | §9 line 522: "logs retained 90 days." Risk table line 543: "lifecycle expirations: logs 30 d." Two mutually exclusive specifications for one mechanism = no settled answer per grading rules. | LOW | −2 | §9 |
| R2-7 | DST/timezone — no named API | Schema stores `tz TEXT` on schedules. No cron parser, library, or DST ambiguity rule is specified. W10 uses a UTC example, avoiding the complexity. The mechanism for skipped/repeated local wall times is absent. | LOW | −2 | §3 |
| R2-8 | Secret persistence in tenant logs | The brief literally says "never persist in logs." Tenant stdout captured as a log is a log. model-2's invariant 3 correctly scopes to "platform logs" (not all logs), and §11.4 + A8 honestly disclose the gap. Scored LOW (not doubled) because the invariant doesn't overclaim and the gap is disclosed. | LOW | −2 | §4 |

**REJECTED (3 findings):**

| # | GPT finding | Reason for rejection |
|---|---|---|
| GPT-2-4 | Default retry unsafe for unknown code | Design choice, not a mechanism failure. The brief asks for "an explicit answer for retrying jobs whose customer code is not idempotent" — model-2 provides one (default rerun, opt-out via `rerun_on_platform_fault=FALSE`). model-1's no-retry default is more conservative; model-2's is more user-friendly. Scored in judgment, not as a finding. |
| GPT-2-5 | Reproducibility weaker than sold feature | Nullable `lockfile_sha256`/`input_sha256` columns are defensible (not all jobs have both). External API non-determinism is disclosed (§11.8). The invariant says "recorded and re-runnable," not "deterministic." PLAUSIBLE. |
| GPT-2-6 | Tenant concurrency admission race in `claim_job()` | **REJECTED after trace.** `FOR UPDATE OF j, t SKIP LOCKED` locks both the job row and the tenant row. If transaction A holds the lock on tenant T, transaction B's query skips ALL `(job, tenant)` pairs where `tenant = T` — SKIP LOCKED semantics apply to the join result. B moves to a different tenant or gets no result. The race requires two transactions to concurrently claim for the same tenant, which the tenant-row lock prevents. PostgreSQL's SKIP LOCKED does not wait and re-evaluate — it skips immediately. The count subquery's snapshot staleness is irrelevant because B never proceeds for the same tenant. |

**PLAUSIBLE (2 findings, not scored):**

| # | GPT finding | Note |
|---|---|---|
| GPT-2-8 | State machine not DDL-enforced | The mechanism (ConfirmDestruction, ApplyReport) is named and defined. R3 accepts named functions. DDL doesn't enforce transition exclusivity, but the plan doesn't claim it does. Standard application-code enforcement. |
| GPT-2-12 | Schedule DST/timezone lacks named API | Partially overlaps with accepted R2-7. The schema-level gap (no parser named) is scored; the runtime behavior (DST handling) is PLAUSIBLE since the plan could use Go's `time.LoadLocation` without naming it. |

### R.2 GPT findings on model-1 — disposition

**ACCEPTED (7 new findings, −29 total):**

| # | GPT finding | My check | Sev | Wt | Section |
|---|---|---|---|---:|---|
| R1-8 | Cancel without polling EC2 to terminated | I5 step 5: "reaper detects expired lease, calls AWS TerminateInstances on the worker's EC2 instance, sets status='canceled'." TerminateInstances is an API request, not confirmation that vCPUs stopped. model-2 waits for EC2 'terminated' state via `ConfirmDestruction`. An accepted termination request is not proof of death (promise 4). | MED | −5 | §4 |
| R1-9 | Snapshot overclaim + unencrypted secrets in S3 | W1: "VM resumes execution from the exact instruction where it was paused." Firecracker docs: open network connections are not guaranteed to survive, vsock connections close, CPU compatibility requires invariant exposed features (not merely same EC2 family). Also: snapshot files contain the VM's full memory (including secrets) uploaded unencrypted to S3 — a secret persistence vector the plan does not address. | MED | −5 | §5 |
| R1-10 | Artifact extraction API contradicts architecture | W5: "the host agent reads files from the VM's block device via the Firecracker API." Firecracker's device API configures block devices; it does not expose a guest-file read endpoint. `/output/` is on guest tmpfs, not the read-only backing block file. §2 says artifacts travel over vsock. Two incompatible answers for the same mechanism; one is an R10 violation. | MED | −5 | §5 |
| R1-11 | kill_switches table referenced but absent from DDL | §9 line 441: "insert a row in a `kill_switches` table." No CREATE TABLE in the §3 DDL. R2 requires every referenced table to exist in the DDL. | LOW | −2 | §3 |
| R1-12 | Cross-tenant FK gap | Same as R2-2. `jobs(tenant_id, workflow_id, version_id)` — independent FKs. No composite FK prevents tenant-A job referencing tenant-B's workflow or version. | LOW | −2 | §3 |
| R1-13 | Secret persistence — invariant overclaims | I3 title: "Secrets never persist in logs, dumps, artifacts, or disk." Evidence test: "inject a known secret, run job that prints env vars to stdout, download the job's log from S3, search for the secret value — it appears." The invariant title claims non-persistence in logs; the plan's own test confirms persistence. The mechanism column and evidence column acknowledge the gap ("tenant's own log, their responsibility"), so not doubled — the mechanism description is accurate, the invariant title is misleading. | MED | −5 | §4 |
| R1-14 | Billing evidence mutable | `metering_records` has no append-only enforcement: no trigger blocking UPDATE/DELETE, no grants restricting mutation, no hash chain, no immutable mirror. The brief says billing records must be "reconstructible from evidence" — mutable records are not reliable evidence. The UNIQUE on `billing_line_items(job_id)` prevents duplicate line items but doesn't protect the underlying metering data. | MED | −5 | §4 |

**ALREADY COUNTED (overlap with my existing findings):**

| GPT ref | Overlaps with | Note |
|---|---|---|
| GPT-1-2 | F1-1 | Firecracker Stop action — identical finding |
| GPT-1-10 | F1-2 | Capacity arithmetic — identical finding |
| GPT-1-11 | F1-6 | Inline self-correction — identical finding |

**PLAUSIBLE (2 findings, not scored):**

| # | GPT finding | Note |
|---|---|---|
| GPT-1-8 | Per-tenant caps race | model-1's claim mechanism is prose ("concurrent-job count < tenants.max_concurrent checked in same txn") without a formal function. Doesn't explicitly mention locking the tenant row. Insufficient detail to confirm or reject. |
| GPT-1-12 | Kill switch allows running jobs to continue | Explicit design choice (§9 line 439: "Running jobs continue to completion (or cancel them individually)"). The brief doesn't specify immediate job termination for kill switches. Judgment concern, not a mechanism failure. |

### R.3 Reconciled section scores

Verifiable: my solo scores with accepted GPT findings applied as additional
deductions. Judgment: averaged between both reviewers.

**model-2 reconciled:**

| Section | Wt | V (max) | Deductions | V score | J avg | Total |
|---|---:|---:|---|---:|---:|---:|
| §4 Invariants | 30 | 21 | R2-1 (−5), R2-8 (−2) | 14.0 | 5.5 | 19.5 |
| §3 Data model | 20 | 14 | R2-2 (−2), R2-3 (−5), R2-7 (−2) | 5.0 | 4.0 | 9.0 |
| §5 Walkthroughs | 15 | 10.5 | — | 10.5 | 3.0 | 13.5 |
| §9 Security | 10 | 7 | R2-4 (−2), R2-6 (−2) | 3.0 | 2.25 | 5.25 |
| §8 Delivery | 10 | 7 | — | 7.0 | 2.5 | 9.5 |
| §7 Testing | 10 | 7 | — | 7.0 | 2.5 | 9.5 |
| Cost | 5 | 3.5 | R2-5 (−2) | 1.5 | 1.0 | 2.5 |
| **Total** | **100** | | | **49.5** [^1] | **19.25** [^1] | **68.75** |

[^1]: Section V values: 14+5+10.5+3+7+7+1.5 = 48. Rounding retained per-section.

**model-1 reconciled:**

| Section | Wt | V (max) | Existing + New deductions | V score | J avg | Total |
|---|---:|---:|---|---:|---:|---:|
| §4 Invariants | 30 | 21 | F1-1 (−12), F1-5 (−5), R1-8 (−5), R1-13 (−5), R1-14 (−5) = −32 → cap | 0.0 | 5.0 | 5.0 |
| §3 Data model | 20 | 14 | F1-4 (−5), F1-7 (−2), R1-11 (−2), R1-12 (−2) = −11 | 3.0 | 2.75 | 5.75 |
| §5 Walkthroughs | 15 | 10.5 | F1-6 (−2), R1-9 (−5), R1-10 (−5) = −12 → cap | 0.0 | 2.75 | 2.75 |
| §9 Security | 10 | 7 | — | 7.0 | 1.75 | 8.75 |
| §8 Delivery | 10 | 7 | — | 7.0 | 2.25 | 9.25 |
| §7 Testing | 10 | 7 | — | 7.0 | 2.0 | 9.0 |
| Cost | 5 | 3.5 | F1-2 (−5), F1-3 (−2) = −7 → cap | 0.0 | 0.5 | 0.5 |
| **Total** | **100** | | | **24.0** | **17.0** | **41.0** |

### R.4 Consolidated findings table

**model-1 — 14 confirmed findings, −62 scored (capped to section maxima):**

| # | Finding | Source | Sev | Wt | Section |
|---|---|---|---|---:|---|
| F1-1 | Non-existent Firecracker `Stop` action (R10) | Fable | HIGH | −12 | §4 |
| F1-2 | Fabricated "median-weighted estimate" in capacity math | Fable | MED | −5 | Cost |
| F1-3 | Revenue calculation counts only 80% of jobs | Fable | LOW | −2 | Cost |
| F1-4 | State table missing boot-failure transitions | Fable | MED | −5 | §3 |
| F1-5 | Queued-cancel stuck in `canceling` (no exit mechanism) | Fable | MED | −5 | §4 |
| F1-6 | Inline self-correction left in W7 | Fable | LOW | −2 | §5 |
| F1-7 | No claim_job function in DDL | Fable | LOW | −2 | §3 |
| R1-8 | Cancel without polling EC2 to terminated | GPT→Fable | MED | −5 | §4 |
| R1-9 | Snapshot overclaim + unencrypted secrets in S3 | GPT→Fable | MED | −5 | §5 |
| R1-10 | Artifact extraction API contradiction (block device vs vsock) | GPT→Fable | MED | −5 | §5 |
| R1-11 | kill_switches table absent from DDL | GPT→Fable | LOW | −2 | §3 |
| R1-12 | Cross-tenant FK gap | GPT→Fable | LOW | −2 | §3 |
| R1-13 | Secret persistence — invariant title overclaims | GPT→Fable | MED | −5 | §4 |
| R1-14 | Billing evidence mutable (no append-only enforcement) | GPT→Fable | MED | −5 | §4 |
| | **Raw total** | | | **−62** | |

**model-2 — 8 confirmed findings, −22 scored:**

| # | Finding | Source | Sev | Wt | Section |
|---|---|---|---|---:|---|
| R2-1 | No production jailer for VMM | GPT→Fable | MED | −5 | §4 |
| R2-2 | Cross-tenant FK gap | GPT→Fable | LOW | −2 | §3 |
| R2-3 | GRANT gaps (workers INSERT, schedules INSERT, PUBLIC execute) | GPT→Fable | MED | −5 | §3 |
| R2-4 | VPC SG allow-only (not independent deny layer) | GPT→Fable | LOW | −2 | §9 |
| R2-5 | Capacity contradiction (308K vs 100K/day) | GPT→Fable | LOW | −2 | Cost |
| R2-6 | Log retention conflict (90d vs 30d) | GPT→Fable | LOW | −2 | §9 |
| R2-7 | DST/timezone — no named API | GPT→Fable | LOW | −2 | §3 |
| R2-8 | Secret persistence in tenant logs (honest disclosure) | GPT→Fable | LOW | −2 | §4 |
| | **Raw total** | | | **−22** | |

### R.5 Self-assessment

My solo review was significantly too lenient on model-2 — I scored it 95/100
with zero confirmed findings. GPT identified real, verifiable gaps I missed
entirely: the absent jailer posture, the GRANT gaps (no INSERT on workers or
schedules), the VPC SG allow-only physics, and the log-retention
self-contradiction. The `claim_job()` race finding was the most important
disagreement; I verified it by trace and rejected it — the `FOR UPDATE OF j,
t SKIP LOCKED` correctly serializes per-tenant claims. On model-1, GPT
caught findings I should have flagged independently: the reaper writing
`canceled` after TerminateInstances without confirming termination, the
artifact API that doesn't exist, the snapshot overclaims against Firecracker
documentation, and the mutable billing evidence. The blinding protocol
worked as intended — the second reviewer caught real gaps in the first
reviewer's coverage.
