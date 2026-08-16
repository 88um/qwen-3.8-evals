# Build challenge: plan "Atlas Cutover" — a zero-downtime brownfield migration

You are the newly hired principal engineer of a profitable SaaS company. I am the
product manager. This is **not** a greenfield project: the company runs a large
PostgreSQL monolith that is described, concretely, in the two artifact files
accompanying this brief — `legacy/schema.sql` and `legacy/code-paths.md`. Read them
before writing anything; your plan will be judged against *that* system, not a
generic one.

Your task is not to write code. It is to produce the complete engineering plan for
migrating **orders and inventory** out of the monolith into two separate services,
executed while the business keeps running. The plan follows
`engineering-plan-standard.md` exactly (13 sections, all rules). It will be
adversarially reviewed by engineers who have performed migrations like this and
been burned; every mechanism will be probed, every arithmetic claim recomputed,
and every rollback path walked against the worst-timed failure.

**Output cap: 12,000 tokens.** Verbosity is not coverage; a plan over the cap is
truncated where it stands and graded on what remains.

---

## 1. The situation

The monolith serves a B2B commerce platform. One PostgreSQL 15 cluster, one
application deployed as ~40 identical binaries behind a load balancer. The `orders`
and `inventory` domains (the tables and code paths in the artifacts) must move into
two new services — **Order Service** and **Inventory Service** — each owning its own
PostgreSQL database, because the monolith's write contention on `inventory_levels`
is the company's scaling ceiling.

Numbers that bind every claim in your plan:

- The cluster holds **2 TB**; the tables being migrated total **1.4 TB**
  (`orders` + `order_lines` are 1.1 TB; inventory tables ~300 GB).
- Sustained load: **4,000 writes/sec** across the migrating tables at daily peak,
  ~900 writes/sec at trough (02:00–05:00 UTC). Reads are 10× writes.
- Availability requirement: **99.99% monthly** on order placement and inventory
  reads — a budget of **4 minutes 23 seconds** of order-placement unavailability
  per month, all causes included, migration included.
- The migration must complete inside a **30-day window**; the CFO has tied a
  contract renewal to it.
- During the window, **old and new application versions run simultaneously**:
  deploys are rolling over ~40 binaries taking up to 45 minutes, and one old
  binary is always assumed to be alive until proven drained.
- **Every phase must be reversible.** For each phase your plan defines, the
  reviewers will ask: "we are mid-phase and the new path is misbehaving — walk the
  rollback." A phase without a rollback answer scores zero for that phase, and a
  "rollback" that loses committed customer writes is worse than none.

## 2. The promises

1. **No lost writes.** Every order and inventory mutation committed by either
   version of the application, at any moment during the 30 days, exists exactly
   once in the system of record that survives the migration.
2. **No overselling.** The invariant enforced today by the monolith — available
   quantity never goes below zero (see `place_order` in the artifacts) — must hold
   *continuously* through the migration, including while writes are moving between
   systems. There is no acceptable window of "briefly unenforced."
3. **No flag day.** At no point does the plan require a synchronized cutover where
   correctness depends on everything switching at once. Every switch is gradual,
   observable, and individually reversible.
4. **Order numbers stay unique and monotonic per customer** (the property
   `orders.order_number` provides today — see the artifacts for how it is
   generated, because that mechanism is one of your problems).

## 3. What the plan must cover

Beyond the standard's 13 sections, reviewers will specifically grade:

- **Target architecture and schema** — full DDL for both new services (§3), and
  the explicit ownership boundary: which monolith tables die, which columns move
  where, what replaces the cross-domain foreign keys and the trigger-maintained
  denormalization the artifacts contain.
- **Backfill + change-capture protocol** — how 1.4 TB copies while 4,000 writes/sec
  mutate it. Name the mechanism (and per R10, the exact PostgreSQL facilities —
  publication scope, slot behavior, what logical decoding does and does not emit
  for the tables as defined in `legacy/schema.sql`; check `REPLICA IDENTITY`
  against each table's actual definition). State how a row updated *during* its
  own backfill copy converges, how a row **deleted behind the backfill cursor**
  converges, and how duplicate or reordered CDC events are made harmless — with
  the dedup key named.
- **Read and write cutover sequence** — the ordered steps from "monolith owns
  everything" to "services own everything," each step naming which component reads
  and writes where, what fences out **an old binary that writes to the monolith
  tables after ownership has moved** (the 45-minute rolling-deploy tail makes this
  a certainty, not an edge case), and each step's rollback.
- **Consistency verification** — your plan must define *comparison semantics*
  before it claims "shadow reads match": compared at what isolation, as of what
  moment, keyed how, tolerating what in-flight skew, with mismatches routed where.
  A verification section that does not define when two systems under 4,000
  writes/sec are allowed to differ will be treated as no verification.
- **Rollback after divergence** — the hardest question in the brief, answered
  explicitly: a phase is rolled back *after* the new service has accepted writes
  the monolith never saw. Where do those writes go, what re-injects them, what
  detects collisions with `order_number` sequences that continued advancing in the
  monolith (see the artifacts' generation mechanism), and what is the customer
  impact stated honestly.
- **Capacity and duration arithmetic** (R4/R5) — backfill throughput derivation
  (MB/s achievable against the given hardware and 4,000 writes/sec of competing
  WAL traffic), total backfill duration per table, replication lag budget, dual-
  write overhead if you choose it, and the day-by-day fit inside 30 days with
  slack stated. Reviewers will recompute every line.
- **Fault-injection tests** (§7) — named tests that kill the CDC consumer
  mid-transaction, crash the backfill at a stated cursor, partition a service from
  its database mid-cutover, and roll a deploy back mid-phase; each with the
  observable assertion that proves the promise survived.

## 4. Constraints and non-goals

- Managed PostgreSQL for the new services is available; no other database engine
  is (the team is a PostgreSQL shop and this is not the moment to learn).
- No third-party CDC SaaS: data residency contracts forbid it. Self-hosted open
  tooling or native PostgreSQL facilities only.
- The monolith's code can be modified during the window (you decide the
  modifications and they ship via the same 45-minute rolling deploys), but the
  legacy code paths in the artifact are *live today* and every intermediate state
  must tolerate the newest and the oldest running binary at once.
- §6 (AI strategy): this project ships no AI features; per the standard, one line
  saying so suffices.
- Non-goals: no product feature changes, no schema "improvements" beyond what the
  split itself forces, no multi-region.

## 5. Grading

| What | Weight |
|---|---:|
| §4 Invariant enforcement map (promises 1–4 under concurrency and crash) | 30 |
| §3 Data model + ownership boundary + state machines | 20 |
| §5 Failure walkthroughs and per-phase rollback (10 numbered, worst-window) | 15 |
| §8 Migration sequencing and delivery phases | 10 |
| §9 Security and tenant isolation across the new boundary | 10 |
| §7 Testing and fault-injection evidence | 10 |
| Capacity and duration arithmetic (checked line by line) | 5 |

At least 70% of each category is graded on mechanically checkable claims: DDL that
executes, PostgreSQL capabilities that exist as named, arithmetic that recomputes,
walkthroughs whose steps name real mechanisms against the *provided* legacy
artifacts. A plan that never engages with the specific traps in
`legacy/code-paths.md` — however elegant in the abstract — fails the brief.
