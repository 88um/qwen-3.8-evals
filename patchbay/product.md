# Build challenge: plan "Patchbay" — hostile multi-tenant code execution

You are the founding engineer; I am the product manager. Greenfield build. Your
task is the complete engineering plan per `engineering-plan-standard.md` (13
sections, all rules). The reviewers operate execution platforms for a living and
will read your plan the way an attacker reads a sandbox: every boundary you name
will be asked "enforced by what, exactly?" — a kernel facility, a network rule,
a cgroup limit, a named configuration — and every claim about an isolation
technology will be checked against that technology's actual documentation (R10
applies with teeth: misattributing a security property to a tool that does not
provide it is the single heaviest deduction in this eval).

**Output cap: 12,000 tokens.** Over-cap plans are truncated where they stand.
The surface here is deliberately larger than the cap allows you to cover evenly
— what you choose to specify deeply versus dismiss in a line is itself graded
(§11/§13): triage explicitly, never silently.

---

## 1. The product

**Patchbay** is an automation platform — Zapier-meets-CI. Customers upload code
(Node and Python at launch) that we run for them: triggered by webhooks, by
schedules, or manually. Their code receives their stored secrets, calls external
APIs, produces logs and artifacts, and is billed by the second. The code is
**assumed hostile**: mining coins, scanning our network, exfiltrating other
tenants' data, forking to exhaustion, unzipping bombs — all of it will be
attempted in week one.

The promises, in order:

1. **Tenant isolation is absolute.** No tenant's code, under any behavior, reads
   another tenant's code, secrets, artifacts, logs, cache entries, or network
   traffic — and no tenant's code reaches *our* control plane, cloud metadata
   endpoints, or internal services. A cross-tenant read is a company-ending
   event and is graded like one.
2. **Secrets go only where they belong.** A tenant's secrets are delivered only
   into that tenant's executing job, and never persist in logs, crash dumps,
   core files, artifacts, error messages, or worker disk after the job ends.
3. **Billing tells the truth.** Usage-based charges are derived from records
   that customer code cannot inflate, deflate, or forge, and that survive
   worker crashes and spot terminations. A disputed invoice must be
   reconstructible from evidence.
4. **Cancellation means stopped.** When the UI says a job is canceled, the
   workload is provably no longer executing — not "signal sent." Same for
   timeout enforcement.

## 2. Users and scale

- 5,000 tenants; **100,000 executions/day** (~1.2/sec sustained, peak 30/sec).
  Job durations: p50 4 s, p95 3 min, hard cap **4 hours**.
- Job resources: default 1 vCPU / 512 MB, purchasable up to 4 vCPU / 8 GB.
- Fleet: cloud VMs, and **60% of capacity is spot instances with 2-minute
  termination notice** — the economics require spot; the plan must make 4-hour
  jobs and billing correct despite it.
- Webhook triggers arrive duplicated and out of order (provider retries);
  scheduled jobs must fire exactly the contracted number of times (a cron that
  double-fires a customer's "charge my users" job is a promise-4-class event).
- **Reproducible execution** is a sold feature: re-running a job with the same
  inputs and pinned dependencies produces the same result, and the platform
  records enough to prove which code + dependencies + inputs a run used.
- Operations team: **2 engineers**, neither on call more than one week in two.
  This constraint is graded: an architecture needing a Kubernetes-security
  specialist on staff fails the brief even if technically sound.
- Billing: per-second of vCPU + memory, monthly invoices, usage visible to the
  customer with at most 5 minutes lag. §6 (AI strategy): no AI features at v1;
  one line per the standard.

## 3. What the plan must cover

- **The sandbox boundary** (§1/§2/§9): commit to the isolation technology (R1:
  the rejected alternative must be the strongest one, and "containers" versus
  "VMs/microVMs" must be argued from named escape classes, not vibes). Specify:
  what kernel/hypervisor facility enforces isolation, the filesystem posture,
  user namespaces, syscall policy, and precisely what is shared between two
  jobs of different tenants that ran on the same worker consecutively —
  and the named sanitization step between them, or the design choice that makes
  sanitization unnecessary.
- **Network policy** (§9): the egress rules a job runs under — what can it
  reach, what provably not (cloud metadata endpoint, worker host services,
  control plane, other jobs), enforced by what mechanism at what layer; how
  per-tenant egress policies ("this workflow may only call api.stripe.com")
  are enforced against code that controls its own DNS resolution.
- **Secret injection** (§4/§9): the path a secret takes from vault to job, its
  form inside the job, its lifetime, and the mechanism that keeps it out of
  each place promise 2 names (logs, dumps, artifacts, disk). "We redact known
  secret strings from logs" is a trap this brief expects you to step around,
  not into — say what you do instead or in addition, and what it cannot catch.
- **Scheduling and lifecycle** (§3/§4): the job state machine as a transition
  table, queue and lease design, resource admission (what stops 400 tenants'
  4-hour jobs from starving the 4-second jobs), spot-termination handling for
  long jobs (checkpoint? migrate? rerun? — commit, per R6, and price it), and
  retry semantics with an explicit answer for **retrying jobs whose customer
  code is not idempotent**.
- **Cancellation and timeout** (§4/§5): the sequence from "user clicks cancel"
  to "provably not executing," including the job that ignores SIGTERM, the job
  mid-syscall, and the worker that has stopped heartbeating — and what state
  the UI shows at each point in between (promise 4 forbids showing "canceled"
  early).
- **Metering and billing** (§3/§4): where usage records originate (a source
  customer code cannot write to), how they survive a worker dying mid-job, the
  reconciliation between metering and scheduling records, and the immutability
  mechanism on billing evidence (R3: name the constraint, not the intention).
- **Artifacts, logs, and the cache** (§3/§9): size caps, retention, tenant
  scoping of the dependency/build cache, and the mechanism that prevents one
  tenant from poisoning a cache entry another tenant's build will consume.
- **Dependency supply chain** (§9): what "pinned dependencies" means
  mechanically (lockfile? hash? vendored?), what the platform does when a
  registry package is retracted or replaced, and whether install-time code
  execution (npm postinstall, setup.py) runs inside or outside the sandbox
  boundary — this question has a wrong answer.
- **Incident containment** (§9/§10): the kill-switches that exist ahead of
  time — per-tenant, per-worker, per-region — and the evidence trail an
  isolation-suspected incident starts from.
- **Capacity and cost model** (R4/R5): worker fleet sizing from the given
  load shape, spot/on-demand mix, cost per execution at p50 and p95, and the
  margin arithmetic against a stated price.
- **Ten numbered worst-window walkthroughs** (§5, R7), which must include at
  least: spot termination at hour 3:58 of a 4-hour job; cancellation of a
  fork-bombing job; a webhook delivered three times for one logical trigger; a
  worker crash between job completion and usage-record persistence; a
  zip-bomb artifact; and a tenant's code attempting the metadata endpoint via
  a redirect chain from an allowed domain.

## 4. Grading

| What | Weight |
|---|---:|
| §4 Invariants: isolation, secret containment, billing integrity, cancel truth | 30 |
| §3 Data model: job/lease/metering/billing DDL and state tables | 20 |
| §5 Worst-window walkthroughs | 15 |
| §9 Security: sandbox, network, supply chain, containment | 10 |
| §8 Delivery: phases, first end-to-end slice, 2-engineer operability | 10 |
| §7 Testing: named adversarial tests (escape attempts, billing chaos, cancel drills) | 10 |
| Capacity and cost arithmetic | 5 |

Mechanical checks reviewers will run: every isolation property is matched to
the named facility's documentation (R10); the state-transition table is checked
for unreachable and absorbing states; billing arithmetic is recomputed; the
walkthroughs are executed step-by-step against the DDL; and every place the
plan triaged scope, §11/§13 must record the cut — an undisclosed narrowing of
"hostile code" to "buggy code" is the classic failure of this genre and is
graded as concealment.
