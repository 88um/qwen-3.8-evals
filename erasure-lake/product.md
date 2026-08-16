# Build challenge: plan Erasure Lake — a privacy-correct analytics platform

You are the founding principal engineer for a four-engineer infrastructure team. I am the
product manager. Below is the complete brief for a multi-tenant analytics platform we are
building from scratch. You own the technical plan from first ingestion through launch and
disaster recovery.

Your task is **not to write the product**. Produce the complete engineering plan you would
actually execute. Engineers who have operated privacy-sensitive data platforms will review
it adversarially. They will trace identifiers through every store, kill processes at the
worst moment, restore old backups, replay late events, race legal holds against deletion,
and check your arithmetic.

Do not ask questions or offer menus of alternatives. Commit to decisions, state reasonable
assumptions where the brief is silent, and expose any product promise that cannot honestly
be guaranteed. A named technology or a sentence saying “we delete from every system” is not
a mechanism.

---

## 1. Product

**Erasure Lake** is a hosted analytics platform for SaaS companies. Customers send product
events, inspect individual user histories, build funnels and retention reports, search
event properties, create derived audience segments, and export data to their own systems.

The same event is represented in several forms: recently searchable records, immutable raw
files, compacted analytical tables, cached query results, exports, and derived segment
membership. Some fields are direct identifiers; others become identifying only when joined.
Customers need useful analytics, but they also need credible answers when a person asks to
access or erase their data.

The product's reputation rests on five promises, in this order:

1. **An accepted erasure finishes completely.** Once an erasure request reaches `completed`,
   no live product surface, export, cache, search result, derived segment, or queryable raw
   object contains personal data covered by that request. Completion is supported by
   durable, inspectable evidence rather than a successful HTTP status from a fan-out job.
2. **Old data never silently comes back.** Delayed events, retries, compaction, rebuilding an
   index, replaying a dead-letter queue, or restoring a 90-day backup must not resurrect an
   erased identity. The system must remain safe even if the primary region is lost and the
   service is restored from older data.
3. **Legal holds are obeyed exactly, not broadly.** Data inside an active hold is preserved;
   data outside its subject, tenant, type, and time scope remains erasable. Every conflict
   between erasure and preservation has an explainable decision and authorized evidence.
4. **Tenants never cross.** An identifier collision, bad import, support action, export,
   query, cache key, restore, or deletion job must not reveal, alter, preserve, or erase
   another tenant's data.
5. **Derived data is described honestly.** Hashing, tokenizing, aggregating, or removing a
   display name does not automatically make data anonymous. Retention decisions must follow
   an explicit, testable policy based on whether a person can still be singled out or
   linked—not labels chosen by the implementation.

## 2. Users, scale, team, and budget

- Launch target: 250 customer organizations, 2,500 customer users, and 40 million events per
  day. Normal ingestion averages hundreds of events per second with bursts up to 8,000/s.
- Events average 1.2 KB before compression. A few tenants produce 40% of traffic. Individual
  tenants may define up to 200 custom event types and arbitrary JSON properties within
  documented limits.
- The platform retains queryable event detail for 13 months. Recent interactive queries
  cover 30 days; longer scans may be asynchronous. Customers expect dashboards to feel
  interactive and exports to complete in hours, not days.
- Expect 20,000 identity mutations per day, 1,000 access/export requests per day, and a
  normal erasure load of 300 subjects per day. A tenant-offboarding event can request the
  deletion of 20 million subjects at once.
- One primary geographic region with multiple availability zones is sufficient at launch.
  Disaster recovery into a second region is required, with an RPO of 15 minutes and an RTO
  of four hours.
- The engineering team is four people. One person is on call. Prefer managed services where
  they materially reduce operational risk. Fixed infrastructure should remain below
  $12,000/month at launch volume, excluding unusually large customer-requested exports.
- The plan must show multiplied-out storage, request, throughput, and recovery arithmetic.
  Marketing numbers copied from provider pages are not a capacity plan.

## 3. Data and identity model

### 3.1 Tenants and environments

- A customer organization is a tenant. Each tenant has production and test environments,
  separate API credentials, retention settings within platform limits, members, roles, and
  audit history.
- A person or service account may belong to several tenants. Product data, encryption keys,
  exports, deletion requests, and support access remain scoped to one tenant and environment.
- Tenant suspension stops ingestion and new exports without deleting data. Tenant closure is
  a separately authorized bulk-erasure workflow and must remain restartable for weeks.

### 3.2 Events

- Customers send events over an authenticated HTTPS batch API and upload large historical
  imports as compressed files. SDK retries produce duplicates. Imports may overlap existing
  data and may arrive months late.
- An event includes a tenant/environment, customer event ID when available, event name,
  event time, receive time, source, a subject identifier or anonymous/device identifier,
  and arbitrary properties.
- Producers use at-least-once delivery. Events can arrive duplicated and out of order.
  Customer event IDs are not globally unique and some legacy producers omit them.
- Invalid events are rejected or quarantined with customer-visible reasons. One malformed
  record must not poison an entire import unless the committed import mode says it should.
- Customers can evolve event-property schemas. The plan must define compatibility,
  quarantine, and query behavior rather than pretending arbitrary JSON has no schema.

### 3.3 Identity

- A tenant can identify the same human through account IDs, emails, installation IDs,
  anonymous browser IDs, imported CRM IDs, and later alias/merge calls.
- Identifiers can be reassigned in the outside world: recycled email addresses, shared
  devices, merged customer accounts, and incorrectly linked aliases all occur.
- Customers need an individual activity view and may correct a mistaken identity link.
- An erasure request may begin with any supported identifier. The platform must explain which
  identity graph/version was used, what was included, and how events arriving during or after
  the request are treated.
- After completed erasure, ordinary delayed delivery of historical data must not recreate the
  erased history. A customer may explicitly establish a new consent epoch for genuinely new
  activity; doing so must not restore pre-erasure data or silently reconnect old aliases.

## 4. Product capabilities

### 4.1 Ingestion and observability

- Customers create scoped write keys, rotate and revoke them, view recent ingestion health,
  inspect rejected samples, and see lag from receipt through query availability.
- The system provides backpressure and fair-use controls. One large tenant or import cannot
  starve smaller tenants or the privacy-control plane.
- A customer can safely retry a batch or import. The UI distinguishes received, validating,
  processing, partially accepted, completed, and failed work with durable counts.

### 4.2 Queries, dashboards, and search

- Users can inspect event streams, funnels, retention, grouped counts, and time-series
  dashboards. Saved queries and dashboard definitions are durable and versioned.
- Search supports selected event properties and individual subject histories. Search lag and
  partial results are visible rather than presented as complete truth.
- Interactive query work is isolated from ingestion, erasure, and export work. A pathological
  query cannot consume the privacy workers' capacity or make erasure miss its deadline.
- Cached results must respect tenant, environment, permissions, query parameters, source-data
  version, and erasure changes. A deleted subject must not remain visible through an old cache.

### 4.3 Derived segments and aggregate retention

- Customers define segments such as “active in the last 30 days” or “completed checkout but
  not onboarding.” Segment membership can be exported or queried by customer systems.
- Definitions are versioned. Membership is reproducible against a declared input snapshot,
  and rebuilds do not silently use different logic under the same version.
- Erasure removes covered subject membership and invalidates affected materializations.
- The platform may retain genuinely non-identifying aggregate statistics after erasure, but
  the plan must commit to a concrete release/retention policy. Small cells, rare-event
  combinations, free-text properties, stable hashes, and joinable pseudonyms must not be
  called anonymous without a mechanism that supports the claim.

### 4.4 Exports and access requests

- A tenant member can request a subject-access export or a general dataset export. Exports run
  asynchronously, are encrypted, expire automatically, and are downloaded through narrowly
  scoped authorization.
- Every export is tied to a defined snapshot or consistency point. The product must say what
  happens if erasure, a hold, permission revocation, or tenant closure occurs while an export
  is being built or after it is ready.
- A subject-access response includes the identity basis, covered data, known sources, and
  meaningful processing history without leaking another subject or tenant.
- Export generation and download are auditable. Sensitive values must not leak through job
  arguments, logs, metrics labels, traces, temporary files, or support tooling.

## 5. Erasure and preservation

### 5.1 Erasure requests

- Requests arrive through the customer API, customer UI, tenant-offboarding workflow, and a
  narrow internal support flow. Every request has an idempotency key, authenticated requester,
  tenant/environment, subject basis or tenant scope, reason, jurisdiction/policy basis, and
  immutable request time.
- Duplicate delivery must not start independent deletions. A request has a visible lifecycle:
  accepted, resolving identity, waiting on policy, executing, verifying, completed,
  partially blocked, or failed—or a better state model with equivalent honesty.
- Normal accepted requests should complete within 24 hours and must complete within 30 days
  unless an applicable hold blocks a defined subset. Retries and restarts must converge rather
  than starting over or falsely reporting completion.
- Completion evidence must identify the policy decision, identity-graph version, affected
  systems and object ranges, work attempts, verification results, retained aggregate classes,
  and any held scope. Evidence must not itself retain the personal data that was erased.

### 5.2 Legal holds

- Authorized tenant legal officers can create and release holds. Platform operators cannot
  silently manufacture a customer hold. Emergency platform holds require a separate,
  time-bounded authority and audit path.
- A hold may cover an entire tenant, identified subjects, event types, property classes, and a
  UTC event-time interval. Holds are versioned and immutable after activation; corrections are
  new versions.
- Hold activation and erasure can race. The plan must define an authoritative ordering and
  what happens when work has already deleted some—but not all—covered representations.
- Releasing a hold must resume previously blocked erasure without requiring the data subject
  to submit a second request. Preservation evidence survives without keeping unnecessary
  direct identifiers in general-purpose logs.
- Legal-hold data is isolated from ordinary product mutation and deletion paths, but it remains
  discoverable to authorized legal export workflows.

### 5.3 Immutable raw storage and backups

- Raw ingestion objects and backups are immutable against ordinary application credentials.
  Backup history is retained for 90 days. Daily recovery points and transaction-log/archive
  data must support the RPO and RTO.
- The product does not promise to surgically rewrite every immutable historical backup.
  Instead, it promises that restoring or replaying old data cannot make erased data live or
  accessible again. The plan must specify the enforcement mechanism and recovery ordering.
- A disaster-recovery exercise restores into an isolated environment from a recovery point
  that predates multiple erasures and legal-hold changes. The service may not reopen to
  customers until current privacy control state has been reconciled and non-resurrection has
  been demonstrated.
- Retention expiry, object lifecycle rules, encryption-key destruction, tombstones, and
  restore manifests must have compatible horizons. “The backup expires eventually” is not a
  complete answer to an immediately restored copy.

### 5.4 Late and resurrected data

- Events, dead letters, imports, replicas, and compaction jobs can surface after an erasure.
  Every path that can make data queryable must consult or inherit the relevant privacy state.
- The design must distinguish a genuinely new post-erasure consent epoch from replayed old
  activity. If the available evidence cannot distinguish them, choose and disclose a safe
  product behavior.
- Rebuilding search, analytical tables, or segments from raw inputs must preserve erasure and
  hold semantics without relying on an operator remembering a manual filter.

## 6. Security, administration, and operations

- Authentication supports customer SSO for larger tenants and secure local accounts for small
  tenants. Roles distinguish tenant administration, analysts, developers, billing, privacy,
  legal-hold authority, and export access without creating an unusable matrix.
- Service identities have least privilege. Ingestion, query, export, support, and privacy
  workers do not all share a database superuser or object-store credential.
- Encryption is required in transit and at rest. The plan must define key hierarchy, rotation,
  backup/key interaction, and what cryptographic deletion does and does not prove.
- Support access is time-bounded, approved, tenant-scoped, visible to the customer, and audited.
  Normal support work does not expose raw personal values.
- The operator sees ingestion lag, privacy backlog and oldest age, hold conflicts, failed
  verification, export activity, storage growth, query saturation, replication lag, backup
  freshness, and restore-test status.
- Alerts must lead to a safe operator action. Replaying or force-completing privacy work without
  evidence is not an acceptable runbook.
- Audit data is append-oriented and tamper-evident enough for investigation, while still
  respecting erasure and avoiding personal data copied into prose messages.

## 7. Explicit failure scenarios

Your plan must walk at least these cases step by step, naming durable states, transaction or
coordination boundaries, retry behavior, evidence, and customer-visible outcome:

1. An ingestion worker dies after storing raw bytes but before acknowledging the batch.
2. The same import is processed concurrently by two workers.
3. An alias is added while an erasure request is resolving the identity graph.
4. A mistaken alias is split after one side has already been erased.
5. A legal hold activates while deletion is halfway through its downstream stores.
6. A hold is released after the original erasure request's normal deadline.
7. Search deletion succeeds, the analytical deletion times out, and the worker is killed.
8. Verification receives a timeout after a downstream deletion may have succeeded.
9. A delayed historical import arrives after erasure completion.
10. A segment rebuild reads an old raw object containing an erased subject.
11. An export is generated while erasure begins, then downloaded after erasure completes.
12. A tenant administrator loses export permission while a large export is running.
13. The primary region is destroyed and the newest usable backup predates erasure requests.
14. A cache or replica is unavailable during erasure verification.
15. Tenant closure begins while ingestion, exports, and legal holds remain active.
16. A worker or support user accidentally supplies the correct subject ID with the wrong
    tenant ID.

## 8. What the engineering plan must contain

Produce one self-contained `plan.md` with these sections:

1. **Committed technology decisions.** Languages, services, datastores, object storage,
   analytical engine, queue/workflow mechanism, search strategy, deployment, and observability.
   For each major decision give the choice, strongest rejected alternative, and why.
2. **System architecture.** Named processes/services, trust boundaries, ownership of state,
   communication paths, backpressure, and workload isolation. Include one compact diagram.
3. **Data lifecycle and identity model.** Trace a normal event and every identifier from
   receipt through raw storage, analytical tables, search, cache, segment, export, retention,
   erasure, and restore.
4. **Executable core data model.** Supply migration-order PostgreSQL DDL for the complete
   control plane and representative physical schemas/keys for analytical, search, cache, raw,
   export, and preserved data. Every state and column used later must exist here.
5. **Invariant enforcement map.** Map every promise in §1 and every erasure/hold/restore rule
   to the specific constraint, transaction, version, fence, policy, or credential boundary
   enforcing it, plus the test or operational evidence that proves it. This section is
   weighted most heavily.
6. **Erasure, hold, and non-resurrection protocols.** Give explicit state machines and
   ordering for identity resolution, deletion fan-out, verification, hold conflicts, late
   data, consent epochs, rebuilds, and restore.
7. **Failure walkthroughs.** Walk all sixteen scenarios from §7. Do not combine distinct
   uncertainty windows into “the worker retries.”
8. **Security and privacy model.** Authentication, authorization, tenant enforcement, service
   identities, key hierarchy, secrets, support access, audit treatment, and threat model.
9. **Capacity, performance, and cost.** Multiplied-out ingestion, storage, compaction, query,
   deletion, export, replication, backup, restore, and monthly-cost arithmetic, including peak
   and tenant-offboarding cases.
10. **Testing and release evidence.** Unit/property/integration/chaos/security tests, what runs
    per change versus nightly, real-service validation, restore drills, and measurable launch
    gates. Include tests that attempt to falsify each hard promise.
11. **Delivery and migration phases.** Vertical phases closed by evidence, the first complete
    event-to-query slice, the first complete erase-and-prove slice, production rollout,
    rollback, and operator readiness.
12. **Risk register.** At least twelve concrete risks with early signals, mitigation, and the
    point at which the chosen architecture must change.
13. **Tradeoffs and assumptions.** State the most consequential accepted limitations. Do not
    quietly weaken a promise elsewhere in the document.

## 9. Ground rules

- Make decisions yourself. Do not ask the product manager questions and do not leave option
  menus for the implementation team.
- Keep the plan at or below **12,000 words**. Completeness comes from precise mechanisms, not
  repeated prose.
- Standard-library pseudocode and focused SQL are welcome where they make a boundary precise;
  do not implement the application.
- Use real, currently supported technology and documented service capabilities. Name versions
  or service tiers where their behavior matters.
- DDL advertised as executable must execute in document order. Pseudocode must not reference
  states, tables, credentials, or provider operations absent from the rest of the plan.
- Do not claim perfect exactly-once delivery across systems. Define idempotency, uncertainty,
  reconciliation, and safe terminal states honestly.
- Do not gold-plate. Every component must earn its operational cost for this scale and team.
- The review rewards an admitted capability gap more than a precisely described mechanism that
  does not enforce its claim.

