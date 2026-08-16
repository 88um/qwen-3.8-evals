# Erasure Lake grader guide

This file is for graders, not candidates. Apply the workspace-level `grader-instructions.md`
in addition to this project-specific guide. Grade mechanisms rather than document volume.

## 1. Score composition

| Category | Weight | Verifiable | Judgment |
|---|---:|---:|---:|
| Erasure completion and non-resurrection | 25 | 21 | 4 |
| Identity, lineage, and derived-data honesty | 15 | 12 | 3 |
| Legal holds and policy ordering | 10 | 8 | 2 |
| Tenant isolation and security | 10 | 8 | 2 |
| Data-model fidelity and executability | 15 | 13 | 2 |
| Failure recovery and uncertainty | 10 | 8 | 2 |
| Capacity, operations, and cost | 5 | 4 | 1 |
| Testing, delivery, and risk control | 10 | 7 | 3 |
| **Total** | **100** | **81** | **19** |

Award verifiable credit only when the enforcing construct is named, defined, and applicable
to every state claimed. A test name does not repair a missing mechanism.

## 2. Critical score caps

Apply the lowest applicable cap after section scoring:

- **59 maximum:** restored or replayed data can become customer-accessible before current
  erasure/hold state is reapplied and verified.
- **59 maximum:** erasure completion is a best-effort fan-out with no durable per-system work,
  idempotency, verification evidence, and reconciliation of uncertain outcomes.
- **64 maximum:** the design cannot prevent ordinary delayed events/imports/rebuilds from
  recreating completed-erasure data.
- **69 maximum:** hold-versus-erasure ordering is undefined, relies on wall-clock observation,
  or requires a new request after hold release.
- **69 maximum:** identity resolution has no pinned graph/version or equivalent stable basis,
  so request scope can silently change while executing.
- **74 maximum:** tenant scope is predominantly application convention and a single wrong
  tenant argument can read, export, hold, or erase another tenant's data.
- **79 maximum:** derived data is declared anonymous solely because direct identifiers were
  removed, hashed, encrypted, or aggregated, with no release/retention mechanism.

Do not apply a cap for an honestly admitted product limitation when the design fails closed
and the limitation is compatible with the brief. Do apply it when the plan claims the promise.

## 3. Mechanical verification checklist

Every confirmed finding must quote the plan and record the check performed.

### 3.1 Schema and state machines

- Trace PostgreSQL DDL top-to-bottom in a clean logical database: referenced types, tables,
  columns, functions, indexes, and roles must exist before use.
- Inventory every state named in prose and SQL for requests, tasks, imports, exports, holds,
  tenants, and restore gates. Mutually inconsistent state machines receive no mechanism credit.
- Check foreign keys and uniqueness in tenant context. A globally unique child ID does not
  prove it belongs to the tenant supplied by a job or API call.
- Check every partial index predicate against terminal, uncertain, blocked, superseded, and
  retry states. A fence that releases in a state it claims to cover fails.
- Check grants and service identities against each walkthrough. A worker cannot receive credit
  for least privilege if its declared grants cannot perform its own writes—or if all workers
  share an owner credential.
- Check whether append-only or immutable claims are supported by privileges, triggers,
  storage policy, or an equivalent boundary.

### 3.2 Erasure completion

Construct a matrix of every representation named by the plan: control database, recent-event
store, raw objects, compactions, search, caches, replicas, segments, exports, temporary files,
dead letters, logs/traces, preserved data, and backups.

For each representation verify:

- stable tenant-scoped address or manifest of affected data;
- durable work identity and idempotent retry;
- behavior for success, known failure, timeout-after-possible-success, and permanent absence;
- verification mechanism stronger than trusting the same command's success response;
- evidence recorded without copying erased personal values;
- reconciliation after worker death and after the request-level deadline;
- a defined criterion before request state can become `completed`.

If a plan says a representation contains no personal data, trace its fields and joins. Treat
stable hashes, free text, rare dimensions, IP/device identifiers, and per-subject segment rows
as potentially identifying until the plan's mechanism proves otherwise.

### 3.3 Non-resurrection

Walk these counterexamples:

1. A delayed event has an event time before erasure but a receive time after completion.
2. An old import without customer event IDs is retried after completion.
3. A dead-letter entry is replayed after the ordinary deduplication window.
4. Search and segment indexes are rebuilt from immutable raw objects.
5. A compaction job uses a snapshot predating the erasure.
6. A backup predating both an erasure and a hold release is restored into a new region.
7. An identifier is recycled for a different human.
8. A tenant explicitly begins a new consent epoch after erasure.

Credit requires a durable privacy control plane or equivalent authority whose recovery point
and availability are addressed independently from the data it governs. Verify ordering: a
restored service must be fenced from ingestion, replay, indexing, exports, and customer reads
until current privacy state is present and validated.

Check horizon arithmetic. Tombstones/suppression records, encryption keys, retry queues, raw
objects, imports, replicas, and backups must have compatible retention. Expiring a suppression
record before the oldest replayable data is a concrete resurrection path.

### 3.4 Identity and aliases

- Determine what immutable input pins request scope: graph revision, membership snapshot,
  resolved subject set, or a stronger equivalent.
- Race alias creation, merge, correction, and split against request acceptance, resolution,
  execution, completion, and a later access request.
- Check whether deleting one component accidentally deletes a different person after an
  erroneous merge, and whether splitting later can recreate already erased data.
- Verify tenant/environment is part of every identifier key. The same email or external ID in
  two tenants must remain unrelated.
- Verify the plan distinguishes late historical delivery from a genuinely new consent epoch
  without using event time supplied by an untrusted or buggy customer as sole authority.

### 3.5 Legal holds

- Identify the serialization/order mechanism for hold activation versus erasure work. Merely
  checking for a hold before each delete leaves check-to-delete races.
- Verify hold scope supports tenant, subject, event/property class, and UTC interval without
  preserving unrelated data.
- Verify immutable versioning, authority, release, audit, and emergency-platform-hold expiry.
- Walk partial deletion before a newly ordered hold. The plan must state what can and cannot be
  recovered; it must not claim deleted data can be reconstructed unless a lawful preserved copy
  and mechanism exist.
- Verify hold release reactivates the original blocked work and cannot race to false completion.
- Verify legal exports cannot bypass tenant isolation or expose non-held data.

### 3.6 Exports, caches, and derived data

- Race erasure and permission revocation against export snapshot, generation, publication,
  download authorization, expiry, and object deletion.
- Check whether an already issued URL remains usable after access or privacy state changes.
- Verify cache keys include tenant/environment, permissions or entitlement version, query, and
  source/privacy version as required—or demonstrate an equivalent invalidation scheme.
- Check segment definition version, input snapshot, membership deletion, and rebuild behavior.
- Apply the plan's aggregate-release policy to singleton cells, rare-event combinations,
  high-dimensional properties, stable pseudonyms, and differencing attacks. Arithmetic and
  thresholds must be internally consistent.

### 3.7 Tenant isolation and security

- Substitute the correct object/subject ID with the wrong tenant/environment at every API,
  job, cache, search, object-storage, export, hold, and delete boundary.
- Check composite relational ownership, row policies and bypass roles, object prefixes plus
  credentials, analytical query predicates/policies, cache namespacing, and search aliases.
- Verify support elevation is approved, scoped, expiring, visible, and actually enforced by
  the query/storage path—not just recorded in an audit table.
- Check secret and personal-data exposure through URLs, job payloads, logs, metrics, traces,
  crash dumps, temporary files, and dead letters.
- Verify encryption/key claims against backup restore, key rotation, held data, and shared-key
  blast radius. Encryption at rest alone does not constitute erasure.

### 3.8 Arithmetic and operations

- Recompute events/day, average and peak throughput, raw and replicated storage, index and
  analytical amplification, 13-month retention, 90-day backups, export workspace, and network.
- Recompute tenant-offboarding throughput. A deletion design sized only for 300 daily subjects
  does not satisfy a 20-million-subject closure.
- Check queue partitions, worker concurrency, API quotas, compaction time, backup/restore rate,
  and four-hour RTO against named instance/service limits.
- Check monthly prices against the selected regions, replication, egress, backups, support
  tiers, and peak headroom using current primary-source pricing.
- A conservative arithmetic error remains an error; record its direction and operational
  consequence.

### 3.9 Tests and delivery

- Tests earn evidence credit only when they name the fault boundary and an external observable
  assertion. “Chaos testing” without kill points and invariants is prose.
- Require at least one automated restore from a point predating erasure, with customer-serving
  paths fenced until non-resurrection checks pass.
- Require property/model-based tests for state machines, alias/tenant isolation, duplicate
  work, hold ordering, and late data.
- Require real-service tests for semantics the design delegates to object lifecycle, search,
  analytical deletion/mutation, queue delivery, KMS, or managed backup behavior.
- Delivery phases should reach an event-to-query slice and an erase-and-prove slice early.
  A late privacy retrofit receives less design-quality credit even if the final component list
  is complete.

## 4. Judgment guidance

Judgment credit covers appropriateness for four engineers, clarity of ownership and failure
boundaries, honest tradeoffs, workload isolation, and whether complexity earns its cost.

Do not reward:

- the number of named services;
- indiscriminate event sourcing;
- “exactly once” labels;
- generic compliance vocabulary;
- a longer threat model without enforcing boundaries;
- architecture whose operational burden is ignored.

Reward plans that deliberately fail closed when the evidence cannot distinguish new activity
from replay, or when a downstream deletion outcome is uncertain. A product limitation stated
clearly is better than fabricated certainty.

## 5. Required grader output

For each submission provide:

1. Section score showing verifiable and judgment components separately.
2. Confirmed strengths and weaknesses, each anchored to a quotation and checklist item.
3. Any plausible but unverified finding, with zero score movement.
4. Applied cap, if any, with a concrete counterexample trace.
5. One-line verdict: implement, revise, redesign, or reject.

For the field provide the ranking, cross-submission observations, and reconciliation log
required by the workspace grader instructions. Do not reveal or speculate about identities.

