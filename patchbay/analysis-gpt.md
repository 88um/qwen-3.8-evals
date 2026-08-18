# Patchbay — Independent Plan Evaluation (Reviewer: GPT / Codex)

This is one blind, independent grader report. I evaluated only `product.md`,
`engineering-plan-standard.md`, `grader-instructions.md`, `review-protocol.md`, and the
two masked submissions. I did not attempt to identify either author and did not read any
other Patchbay review.

`review-protocol.md` is specialized for executable software. Its ABORT/build/test rules
do not apply to these plan-only submissions; its confirmation bar does. Final protocol-
compliant reconciliation still requires a second independent grader from another vendor.
The ordering below is therefore this reviewer's ordering, not a reconciled field result.

## Ranking

| Rank | Submission | Invariants /30 | Data /20 | Walkthroughs /15 | Security /10 | Delivery /10 | Tests /10 | Cost /5 | Total |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | `model-2` | 12.5V + 4.0J | 10.0V + 3.0J | 8.5V + 2.5J | 3.5V + 1.5J | 6.0V + 2.5J | 5.0V + 2.5J | 2.5V + 0.5J | **64.5** |
| 2 | `model-1` | 7.0V + 3.0J | 8.0V + 2.0J | 5.0V + 2.0J | 3.5V + 1.0J | 4.5V + 2.0J | 4.0V + 1.5J | 0.5V + 0.5J | **44.5** |

`V` is the mechanically verifiable component; `J` is judgment. Across the rubric,
verifiable checks carry 78 points and judgment 22 points. Totals retain halves to expose
the split rather than manufacture integer precision.

**Ranking:** `model-2` > `model-1`.

Neither plan is implementation-ready. Both explicitly weaken promise 2 by allowing a
secret printed by customer code to persist in the customer's logs and artifacts. The
brief says secrets “never persist in logs ... [or] artifacts” and specifically warns that
known-string redaction is insufficient; tenant scoping changes who can read the leaked
secret, not whether it persisted.

## 1. `model-2` — 64.5/100

### Section scores

| Section | Verifiable | Judgment | Score | Basis |
|---|---:|---:|---:|---|
| §4 invariants | 12.5/24 | 4.0/6 | **16.5/30** | Strong cancel proof, webhook dedup, cgroups, and billing evidence; hard secret-containment failure, weak tenant bindings, unsafe default rerun, and incomplete reproducibility enforcement. |
| §3 data model | 10.0/16 | 3.0/4 | **13.0/20** | Broad, ordered DDL with roles, jobs, events, ticks, and invoices; semantic/permission gaps and absent structural tenant/state enforcement. |
| §5 walkthroughs | 8.5/12 | 2.5/3 | **11.0/15** | All ten required scenarios are present with numbered mechanisms and evidence; several conclusions do not follow from the schema or external facility. |
| §9 security | 3.5/8 | 1.5/2 | **5.0/10** | Good proxy/DNS/cache design and incident trail; no production Firecracker jailer posture, a false independent-SG layer, and explicit secret persistence. |
| §8 delivery | 6.0/7 | 2.5/3 | **8.5/10** | Evidence-led phases, early sandbox proof, clear first E2E slice, sensible two-engineer shape. |
| §7 testing | 5.0/7 | 2.5/3 | **7.5/10** | Strong adversarial suite, but some tests assert weakened requirements or mechanisms the schema does not enforce. |
| Capacity/cost | 2.5/4 | 0.5/1 | **3.0/5** | Detailed unit economics mostly recompute; the peak-duration assumption contradicts 100k executions/day. |

### Confirmed strengths

- **Cancel truth holds on the specified normal and partition paths** `[constraint/state walkthrough]`.
  The plan says only `ConfirmDestruction` writes the terminal cancel/timeout state and
  requires “host agent `vm_destroyed` or EC2 'terminated'” (lines 401–414). W8 keeps the
  UI at `running` during a partition, terminates the instance, polls to `terminated`, and
  only then writes `failed` (lines 464–465). This is materially stronger than signal
  delivery.
- **Webhook dedup is transactionally coherent** `[schema fidelity/failure trace]`.
  `UNIQUE(provider,event_id)` (lines 154–161) plus conditional job creation in the same
  transaction supports W3's three-delivery result. The second and third requests observe
  zero inserted event rows and do not create jobs.
- **Billing evidence has real structural pieces** `[schema fidelity/external claim]`.
  `metering_ticks` has a per-job sequence PK, hash check, predecessor trigger, and mutation
  rejection (lines 261–328). S3 Object Lock compliance mode really does prevent overwrite
  or deletion by any user, including account root, during retention; this named facility
  is accurate ([AWS Object Lock documentation](https://docs.aws.amazon.com/AmazonS3/latest/userguide/object-lock.html)).
- **Network policy closes DNS rebinding at the useful boundary** `[mechanism check]`.
  The custom proxy resolves the allowed hostname itself, rejects forbidden resolved IPs,
  and dials that exact IP (lines 27–30, 453–459). Client-controlled DNS cannot select a
  different destination after the allow decision.
- **Dependency install runs on the correct side of the sandbox boundary** `[mechanism check]`.
  Both install-time code and integrity verification run inside the per-job microVM, while
  the cross-tenant cache protocol exposes no tenant or hash selector to guest code (lines
  37–40, 467–468, 528).
- **Delivery is unusually executable for a two-person team** `[brief criterion]`.
  The plan avoids Kubernetes, names a first end-to-end phase, makes cancellation/billing
  tests release gates, and delays GA until kill-switch and region-drain drills pass.

### Confirmed weaknesses

1. **Promise 2 is explicitly violated** `[constraint-all-states]`. The invariant test
   expects a printed secret to be “present in tenant log prefix” (line 412), and §9 admits
   that a secret written to a log or artifact persists (line 526). Assumption A8 rewrites
   the promise as “platform mechanisms never persist a secret” (line 573), but the product
   promise does not contain that exception. This is an honest disclosure, so it scores
   better than a false redaction guarantee, but it remains a hard scope failure.
2. **The isolation argument misstates the trusted boundary and omits the production jailer**
   `[external-world claim]`. D1 says Firecracker's trust boundary is “KVM (VT-x) + a
   minimal Rust VMM” and rests the guarantee “on hardware” (lines 7–10), while the host
   agent runs as root and no jailer, unique unprivileged UID/GID, chroot, or VMM namespace
   is specified. Firecracker's own design says the userspace VMM handles the emulated
   devices and recommends the jailer, privilege drop, namespaces, cgroups, and chroot as
   nested containment layers—not merely KVM ([Firecracker design](https://github.com/firecracker-microvm/firecracker/blob/main/docs/design.md),
   [production host setup](https://github.com/firecracker-microvm/firecracker/blob/main/docs/prod-host-setup.md)).
   A device-model/VMM compromise is not accurately described as requiring a KVM-kernel
   escape.
3. **Cross-tenant relational consistency is not structural** `[schema fidelity]`.
   `schedules(tenant_id,workflow_id)`, `jobs(tenant_id,workflow_id)`, and
   `invoice_lines(tenant_id,job_id)` use independent foreign keys or no tenant-composite
   FK (lines 163–220, 276–288). The schema accepts a tenant-A job referring to tenant-B's
   workflow. No RLS policies or composite ownership constraints appear, despite invariant
   1 claiming no cross-tenant read under any behavior.
4. **The default spot/crash retry can repeat non-idempotent customer side effects**
   `[failure trace]`. `rerun_on_platform_fault` defaults `TRUE` (line 138). W1 kills a job
   at 3:58 and reruns it from the beginning (lines 423–427); §11 concedes this may duplicate
   “charge my users” (line 548). The opt-out is useful, but safety-critical unknown code
   should not be treated as idempotent by default. This also turns one contracted schedule
   occurrence into multiple executions of arbitrary side effects.
5. **The advertised reproducibility is weaker than the sold feature**
   `[constraint-all-states/internal consistency]`. Lockfile and input hashes are nullable
   (lines 132–133, 204–205), and §11 says the platform “does not make customer code
   deterministic” when external responses vary (line 554). The brief promises the same
   result for a rerun; the plan records provenance but supplies no network-response
   capture/replay mechanism. This is a disclosed gap, not an enforced invariant.
6. **Tenant concurrency admission races** `[constraint-all-states]`. `claim_job()` computes
   the active-job count in a subquery, then locks the selected job and tenant row (lines
   334–345). Two READ COMMITTED transactions can both take snapshots showing count N−1,
   select different queued jobs, and serialize on the unchanged tenant row; locking that
   row does not make the already-read count a stored counter. Both can then claim. The
   plan needs a tenant allocation counter updated conditionally or a tenant lock acquired
   before a fresh count. PostgreSQL documents that locking rows does not turn unrelated
   subquery reads into a new snapshot ([PostgreSQL `SELECT` locking](https://www.postgresql.org/docs/16/sql-select.html)).
7. **Declared permissions do not permit all described writes** `[grants/permissions]`.
   No role receives `INSERT ON workers`, so the registration path in the architecture
   cannot create a worker row. `role_api` cannot insert, enable, or disable `schedules`,
   despite API-owned workflow/schedule management. In addition, `claim_job()` is
   `SECURITY DEFINER` but execution is not revoked from `PUBLIC`; PostgreSQL grants new
   functions public execute by default and recommends an explicit revoke
   ([PostgreSQL privileges](https://www.postgresql.org/docs/16/ddl-priv.html),
   [security-definer guidance](https://www.postgresql.org/docs/16/sql-createfunction.html)).
8. **The state machine is code convention rather than database enforcement**
   `[same-mechanism check]`. The plan calls the transition table “the only transitions”
   (line 381), but API, scheduler, and control-plane roles receive unrestricted `UPDATE`
   on `jobs` (line 364). `ApplyReport` and `ConfirmDestruction` are named but not defined;
   no trigger or column-scoped grant prevents a direct `running→succeeded` or early
   `cancel_requested→canceled` write.
9. **One claimed metadata defense layer is not independently specified**
   `[external-world claim]`. The plan calls “VPC SG no egress to 169.254.0.0/16” an
   independent layer (lines 411, 458, 524, 558). Security groups contain allow rules, not
   deny rules; an ordinary `0.0.0.0/0` egress allow also admits the link-local CIDR
   ([AWS security-group rules](https://docs.aws.amazon.com/vpc/latest/userguide/security-group-rules.html)).
   IMDS disablement itself is valid and remains a strong separate layer
   ([EC2 metadata options](https://docs.aws.amazon.com/AWSEC2/latest/APIReference/API_InstanceMetadataOptionsRequest.html)).
10. **The capacity assumption contradicts the daily volume** `[arithmetic]`. A 30/s peak
    lasting two hours is already 216,000 executions, before the other 22 hours. Adding
    22 hours at 1.157/s yields about 307,645/day, not 100,000/day (lines 64–80, A2 at 567).
    Costs are conservatively overstated relative to the stated 100k/day, but the grader
    rules correctly treat conservative arithmetic errors as errors.
11. **The log-retention mechanism conflicts with itself** `[internal consistency]`.
    §9 says logs are retained 90 days (line 522); the risk table says logs expire after
    30 days (line 543). Under the grading rules, mutually exclusive specifications count
    as no settled answer.
12. **Schedule exactness lacks the named time-zone API** `[library behavior/R10]`.
    The schema stores cron text and a time zone, but no parser/library call or DST
    ambiguity rule is specified. W10 proves crash dedup for a UTC example; it does not
    prove the contracted count for skipped or repeated local wall times.

### One-line verdict

**First place and the better revision base, but blocked on the literal secret promise,
production VMM confinement, tenant bindings, retry default, and corrected load model.**

## 2. `model-1` — 44.5/100

### Section scores

| Section | Verifiable | Judgment | Score | Basis |
|---|---:|---:|---:|---|
| §4 invariants | 7.0/24 | 3.0/6 | **10.0/30** | Useful VM, webhook, and schedule mechanisms; secret, cancel/timeout, billing, and tenant-binding claims fail. |
| §3 data model | 8.0/16 | 2.0/4 | **10.0/20** | Core DDL is ordered and readable, but omits required storage/kill-switch artifacts and structural lifecycle/tenant enforcement. |
| §5 walkthroughs | 5.0/12 | 2.0/3 | **7.0/15** | Ten required scenarios and evidence lines are present; cancellation, snapshot, artifact, crash-billing, and network steps contain non-existent or insufficient mechanisms. |
| §9 security | 3.5/8 | 1.0/2 | **4.5/10** | Better Firecracker jailer posture than model 2 and good dependency placement; hard secret failure and several false/contradictory facility claims. |
| §8 delivery | 4.5/7 | 2.0/3 | **6.5/10** | Clear phases and an early E2E sandbox slice, but exits rely on missing artifacts and weak incident controls. |
| §7 testing | 4.0/7 | 1.5/3 | **5.5/10** | Broad names and assertions; key tests encode the weakened secret promise or rely on APIs that do not exist. |
| Capacity/cost | 0.5/4 | 0.5/1 | **1.0/5** | Duration, concurrency, peak provisioning, and monthly cost do not form one consistent model. |

### Confirmed strengths

- **The production Firecracker process is actually confined** `[external mechanism]`.
  The plan names an unprivileged jailer UID, seccomp, PID/mount/net/cgroup namespaces, and
  a chroot (lines 7–11, 400–402). That follows Firecracker's documented defense-in-depth
  posture more closely than model 2.
- **Webhook and scheduler database transactions are sound in the shown crash windows**
  `[schema/failure trace]`. The partial unique webhook key handles triple delivery, and
  schedule fire plus `next_fire_at` update occur under one schedule-row transaction. W8's
  pre-commit scheduler crash rolls both writes back (lines 304–313).
- **The plan chooses the safe default for unknown side effects** `[brief criterion]`.
  `retry_policy` defaults to `none`, and a worker-lost non-idempotent workflow becomes a
  terminal failure unless the tenant explicitly opts into retry (lines 61, 217, 458).
- **Install-time dependency code runs in the microVM** `[mechanism check]`. The plan
  correctly rejects host/shared-builder execution and uses tenant-scoped cache keys with
  lockfile integrity verification (lines 427–435).
- **It discloses several real weaknesses** `[judgment/honesty]`, including up to ten
  seconds of crash underbilling and lost artifacts (lines 456–464). Candor earns judgment
  credit, though it cannot earn invariant credit.

### Confirmed weaknesses

1. **Promise 2 is explicitly violated** `[constraint-all-states]`. The plan's own test
   injects a secret, prints it, and expects it to appear in the persisted S3 job log
   (line 212). §9 says a tenant leaking its secret to its own output is “equivalent to the
   tenant logging their own secret” (line 425). That is not the product's stated promise,
   which forbids persistence in logs and artifacts without a same-tenant exception.
2. **The forced-stop API does not exist** `[external endpoint/R10]`. I5 and W2 call
   `PUT /actions {"action_type":"Stop"}` (lines 214, 243), but Firecracker's Actions API
   exposes `InstanceStart`, `FlushMetrics`, and `SendCtrlAltDel`; it has no `Stop` action
   ([Firecracker Actions API](https://github.com/firecracker-microvm/firecracker/blob/main/docs/api_requests/actions.md),
   [Firecracker Swagger](https://github.com/firecracker-microvm/firecracker/blob/main/src/firecracker/swagger/firecracker.yaml)).
   The host can kill the Firecracker process directly, but that is not the sequence the
   invariant credits.
3. **Canceled/timed-out can be displayed before execution is proven stopped**
   `[constraint-all-states/failure trace]`. The timeout transition writes `timed_out` when
   the purported Stop action is merely sent (line 195). For a lost worker, I5 says the
   reaper calls `TerminateInstances` and then sets `canceled` without polling EC2 to the
   `terminated` state (line 214). An accepted termination request is not evidence that the
   vCPUs have already stopped.
4. **Spot resume overclaims Firecracker snapshot behavior** `[external-world claim]`.
   W1 says a new host resumes “from the exact instruction” and proceeds (lines 227–234),
   while §1 says a single instance family guarantees compatibility (lines 23–27).
   Firecracker documents that open network connections are not guaranteed to survive,
   vsock connections close, different host kernels are unstable, and CPU compatibility
   requires invariant exposed features—not merely the same EC2 family
   ([snapshot support](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/snapshot-support.md),
   [snapshot versioning](https://github.com/firecracker-microvm/firecracker/blob/main/docs/snapshotting/versioning.md)).
   The plan also omits encryption/authentication for secret-bearing memory snapshots,
   even though Firecracker treats snapshot files as trusted inputs and tells integrators
   to secure them.
5. **The artifact extraction API is invented and contradicts the architecture**
   `[external endpoint/internal consistency]`. W5 says the host agent “reads files from
   the VM's block device via the Firecracker API” (line 276), but the documented device
   API configures block/vsock devices; it does not expose a guest-file read endpoint
   ([Firecracker device API](https://github.com/firecracker-microvm/firecracker/blob/main/docs/device-api.md)).
   `/output` is on guest tmpfs, not the read-only backing block file. §2 instead says
   artifacts travel over vsock (line 35), so the same mechanism has incompatible answers.
6. **The schema permits cross-tenant grafting** `[schema fidelity]`. `jobs.tenant_id`,
   `workflow_id`, and `version_id` are independent references (lines 76–98); neither
   workflow ownership nor version membership is composite-enforced. A tenant-A job can
   refer to tenant-B's workflow/version. The same issue appears in schedules. This is a
   direct structural hole in the highest-weight invariant.
7. **Referenced data artifacts are absent from the DDL** `[schema fidelity]`. The plan
   refers to a `kill_switches` table and `region` column (lines 441–442), but creates
   neither. Logs, artifacts, dependency-cache objects/retention, and snapshot segments
   also have no tables or immutable manifests, despite being used throughout the
   walkthroughs and security section.
8. **The per-tenant and long-job caps race** `[constraint-all-states]`. The claim path
   counts active jobs (lines 190, 204) but does not lock a tenant allocation row or update
   a conditional counter. Two workers can lock different queued jobs, both observe one
   remaining tenant slot, and both claim. `jobs_active_per_tenant` is a non-unique lookup
   index, not enforcement.
9. **Billing does not survive the named crash with truthful evidence**
   `[failure trace/internal consistency]`. W4 intentionally has no end tick, then bills
   only through the last heartbeat and admits up to ten seconds of omitted execution
   (lines 259–269). `metering_records` is mutable, has no sequence/dedup constraint, and
   the DDL has no grants/trigger preventing update or deletion. A unique invoice line
   prevents duplicate line rows; it does not make the underlying evidence immutable or
   reconstruct exact usage.
10. **Capacity arithmetic is internally contradictory** `[arithmetic]`. The stated mix
    computes to `0.8×4 + 0.15×60 + 0.04×600 + 0.01×3600 = 72.2s`, which the plan itself
    first rounds to 72s and then replaces with 25s (line 486). Sustained concurrency is
    therefore about 86.6 jobs, not 30; peak concurrency at 30/s is about 2,166, not 750.
    The monthly compute line then charges only eight sustained hosts and gives no peak
    duration, so it cannot fund the asserted 188-host peak (lines 487–505).
11. **The network section edits itself into a different topology without resolving all
    rules** `[internal consistency]`. W7 first claims all `10/8` traffic is dropped, then
    notices its proxy example is RFC1918 and switches to per-VM link-local addressing in
    a mid-paragraph “Correction” (lines 297–300). §9 later supplies a cleaner proxy-only
    rule (lines 404–414), but the metadata/MMDS exception and proxy path are not rendered
    as one complete ordered ruleset.
12. **Incident kill-switch behavior is weaker than its labels** `[brief/security]`.
    Tenant, fleet, and region switches stop new claims but explicitly allow running jobs
    to continue (lines 437–442), while the production-hardening exit says a suspended
    tenant merely drains by its potentially four-hour timeout (line 396). That is not a
    credible containment switch for a suspected isolation incident.

### One-line verdict

**Second place; useful ideas around jailer confinement and non-idempotent retry defaults,
but the cancellation, snapshot, artifact, billing, tenant, and capacity mechanisms need
redesign rather than implementation.**

## Cross-model observations

1. **Both plans soften the literal secret promise instead of satisfying it.** Tenant-
   scoped storage is necessary for isolation but is not non-persistence. A compliant
   design must either prevent secret-bearing customer output from entering retained
   logs/artifacts by construction, define an enforceable declassification boundary, or
   explicitly renegotiate the product promise.
2. **Both correctly choose one microVM per job**, but only model 1 specifies Firecracker's
   recommended process jail. Model 2 has the better surrounding network design yet leaves
   a compromised VMM with an inadequately described host boundary.
3. **Model 2 is much stronger at cancellation proof and billing evidence.** Model 1 treats
   an unavailable Firecracker action or an EC2 API request as proof of death. Model 2
   retains an intermediate state and waits for `vm_destroyed`/`terminated`.
4. **Neither schema structurally binds tenant-owned relationships.** Composite ownership
   keys and tenant-aware FKs are mandatory for a product whose first promise is absolute
   cross-tenant isolation.
5. **Neither actually delivers deterministic reruns of externally connected arbitrary
   code.** Model 2 admits this; model 1 records hashes but does not address variable API
   responses. Provenance is valuable, but it is not the same property as same-result
   execution.
6. **Both capacity sections require correction.** Model 1 substitutes an unsupported 25s
   duration for its computed 72.2s. Model 2 uses a coherent 57.9s mix but combines a
   two-hour 30/s peak with a 100k/day total that cannot contain that peak.

## Independent findings log

There is no reconciliation yet. The table records the primary checks this reviewer would
hand to the second grader; `score effect` identifies the scored component, not a second
deduction beyond the section tables.

| Finding | Origin | Check | Outcome | Score effect |
|---|---|---|---|---|
| Printed/written secrets persist in tenant logs/artifacts | both | Brief promise 2 executed against the plan's own tests and §9 text | **CONFIRMED** | §4-V, §9-V, §7-V down |
| Model 1 Firecracker `Stop` action | model-1 | Official Actions API and Swagger enumeration | **REJECTED claim / CONFIRMED defect** | §4-V, §5-V, §7-V down |
| Model 1 snapshot gives transparent cross-host continuation | model-1 | Official snapshot connection, host-kernel, CPU, and security limitations | **REJECTED claim / CONFIRMED defect** | §4-V, §5-V, §9-V down |
| Model 1 artifact files readable through Firecracker API | model-1 | Official device/API endpoint inventory; compare tmpfs and vsock statements | **REJECTED claim / CONFIRMED defect** | §3-V, §5-V down |
| Model 1 weighted duration and concurrency | model-1 | Recompute multiplication and Little's law | **CONFIRMED defect** | cost-V down |
| Model 2 S3 Object Lock compliance immutability | model-2 | AWS Object Lock documentation | **CONFIRMED claim** | §4-V retained |
| Model 2 waits for destruction before running-job cancel terminal | model-2 | Trace W7/W8 against transition table | **CONFIRMED claim** | §4-V, §5-V retained |
| Model 2 VPC SG is an independent metadata deny layer | model-2 | AWS SGs are allow-only; compare required internet egress | **REJECTED as specified** | §9-V down |
| Model 2 two-hour peak fits 100k/day | model-2 | `30×7,200 > 100,000` before off-peak traffic | **CONFIRMED defect** | cost-V down |
| Both DDL blocks have a top-to-bottom syntax/forward-reference failure | both | Manual PostgreSQL 16 trace | **REJECTED concern** | no syntax deduction; semantic and permission defects still score |
| Exact current EC2 spot/on-demand prices quoted by either plan | both | Pricing is region/time dependent; no immutable quote was supplied | **PLAUSIBLE — UNVERIFIED** | zero |

## Reconciliation status

Pending a second blind grader from a different vendor. Per `grader-instructions.md`,
verifiable disagreements should be resolved by rerunning the listed checks, judgment
components should be averaged, and conflicting final orderings should both be reported
rather than adjudicated by this reviewer.
