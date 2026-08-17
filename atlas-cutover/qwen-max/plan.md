# Atlas Cutover — engineering plan

Migration of the orders and inventory domains out of the monolith into Order Service (OS) and Inventory Service (IS), executed under continuous operation. Written to `engineering-plan-standard.md` (13 sections). Mechanism claims reference the provided artifacts (`legacy/schema.sql`, `legacy/code-paths.md`) or named PostgreSQL 15 facilities.

## 1. Technology decisions

### 1.1 Change capture: native logical replication, both directions

**Choice:** PostgreSQL 15 logical replication (`wal_level = logical`, `CREATE PUBLICATION`/`CREATE SUBSCRIPTION`, `pgoutput`/`pg_logicalreplication` apply worker) in two directions: forward (FWD) monolith→services, reverse (REV) services→monolith. Self-hosted; no third-party CDC.

**Rejected:** Self-hosted open-source CDC wrapper (Debezium-style). Strongest alternative — same native decoding core plus packaging. Rejected because the brief forbids CDC SaaS and the native subscription already provides initial snapshot, LSN-ordered WAL tail, and applied-position state management; the wrapper adds a component whose failure modes we own without gaining a capability.

**Why:** every convergence claim below is expressed in mechanisms the subscription protocol itself provides — origin-LSN ordering, position-advance-at-apply-commit, snapshot-then-tail sync.

**Named capabilities and limits (R10):**
- Logical decoding emits row changes (INSERT/UPDATE/DELETE) for tables in the publication scope, ordered by WAL LSN, preserving intra-transaction statement order. Reordering is not possible in-stream; the consumer applies in stream order.
- It does **not** emit sequence advancement. `order_number_seq` state is invisible to decoding; sequence state moves only by explicit `setval` handoff (§1.3).
- It does **not** emit changes to tables outside the publication scope. The artifacts' trigger fan-outs — `trg_refresh_available`→`products.cached_available` (schema.sql:149) and `trg_audit_orders`→`audit_log` (schema.sql:150) — are therefore invisible to every subscriber; both tables stay monolith-side and are re-fed by design (§3.4).
- Initial sync: first connect streams a snapshot of each published table, then the WAL tail from the snapshot position. A row updated during its own snapshot copy appears at the scanned value, then its UPDATE event rides the tail and converges by idempotent apply (§5.2). A row deleted behind the copy cursor converges via its tail DELETE event (§5.3).
- `REPLICA IDENTITY` checked against each artifact definition: `orders`, `order_lines`, `reservations`, `inventory_levels` carry primary keys → `DEFAULT` identity suffices. `inventory_movements` and `order_status_history` have **no primary key** (schema.sql:46-108) → publishing them requires `ALTER TABLE … REPLICA IDENTITY FULL` (which also makes UPDATE/DELETE emit the old tuple). Both ALTERs ship in Phase 0.

### 1.2 Write cutover: drain-then-flip with a DB-level write fence

**Choice:** writes move to the services only after every old binary is proven drained and monolith deploys are frozen; afterwards a BEFORE trigger fence on the monolith's migrated tables makes any post-cutover write from an old binary fail loudly (transaction rolls back, nothing commits).

**Rejected:** Bidirectional value-based CDC on `inventory_levels` (zombie writes forwarded by FWD, service writes mirrored back). Strongest because it keeps the zombie tail fully functional. Rejected because CDC delivers resulting **values**, not increments: an old-binary `reserved += qty` (code-paths.md §1) applied value-wise in the service overwrites concurrent service-side increments — lost updates under exactly the concurrency the brief assumes. Increments commute; values do not.

**Why:** promise 1 (no lost writes) outranks keeping the zombie tail functional. The fence converts the zombie case from silent corruption into a loud, rolled-back, availability-budgeted failure (§5.5).

### 1.3 Order-number allocation: single sequencer, explicit handoff at flip

**Choice:** during transition, the monolith `order_number_seq` (schema.sql:61) remains the only allocator. At the write-cutover flip — when no old binary can allocate — OS's local sequence is initialized once from the monolith sequence's current value plus a margin of 1,000, and the monolith sequence is fenced. Rollback jumps the monolith sequence past the service high-water mark (§5.7).

**Rejected:** Disjoint pre-allocated number ranges per system. Strongest because it removes cross-system coupling. Rejected because it breaks promise 4: a customer's orders alternate monolith-range/service-range numbers, and the EDI feed (code-paths.md §5) rejects files whose numbers are not strictly increasing per customer.

**Why:** strict per-customer monotonicity is load-bearing for nine external partners; a single time-ordered allocator is the only mechanism that preserves it under concurrent placement from both binaries.

### 1.4 Service databases: managed PostgreSQL 15, one per service

**Choice:** two managed PostgreSQL 15 instances, sized per §8 arithmetic.

**Rejected:** Shared service database. Strongest because it halves the ops surface. Rejected because the brief's stated scaling ceiling is write contention on `inventory_levels`; co-locating order writes and inventory writes in one cluster re-creates the ceiling inside the new boundary.

**Why:** the split exists to separate the two write populations; the databases separate with them.

### 1.5 Denormalization replacement: service-side availability aggregate, trigger-fed

**Choice:** IS maintains `availability_available(product_id, available_qty)` in its own database, kept current by an AFTER INSERT/UPDATE trigger on its own `inventory_levels` (same shape as `refres_available`, schema.sql:149). Storefront availability reads (~40k/s through the app cache) cut over to this table.

**Rejected:** Service pushes deltas to monolith `products.cached_available`. Strongest because it leaves the storefront read path untouched. Rejected because it recreates a cross-cluster write dependency on the hot path — the exact coupling the split removes.

**Why:** the denormalization's purpose is read speed; moving the read to the owning service serves the purpose without the coupling. Monolith `products.cached_available` keeps being maintained by its own trigger on the monolith's (REV-fed) `inventory_levels` replica, so old binaries' storefront reads stay correct through the window.

## 2. System architecture

**Processes.** OS owns order lifecycle post-cutover (`orders`, `order_lines`, `order_status_history`, reference slices). IS owns `inventory_levels`, `inventory_movements`, `reservations`, `availability_available`. The monolith keeps its ~40-binary topology; modified paths ship via the same 45-minute rolling deploys. Because the newest and oldest binaries coexist all window, every monolith modification is additive-branching (version-selected path), never a rewrite of the legacy path.

**Communication.** All inter-system movement is logical replication: FWD publications `pub_os`/`pub_is` (monolith→OS/IS) and REV publications from each service DB (→monolith). No application-to-application write RPC exists in steady state; order-number allocation is local to whichever system owns writes at the moment (§1.3).

**Ownership by phase.**

| Phase | Order writes | Inventory writes | Order reads | Availability reads |
|---|---|---|---|---|
| 0-1 | monolith | monolith | monolith | monolith `products.cached_available` |
| 2 (read cutover) | monolith | monolith | new binaries→OS replica | new binaries→IS `availability_available` |
| 3-4 (write cutover, post-drain) | OS | IS | OS | IS |
| 5 (decommission) | OS | IS | OS | IS; monolith migrated tables persist as fenced, REV-fed audit replicas |

REV runs continuously from Phase 3 through Phase 5. It is simultaneously the rollback feed and the audit feed (§3.4).

## 3. Data model

### 3.1 Order Service DDL

```sql
CREATE TABLE orders (
    id             BIGINT PRIMARY KEY,            -- monolith identity value via CDC
    order_number   BIGINT NOT NULL UNIQUE,
    customer_id    BIGINT NOT NULL,               -- integrity: application-enforced (§3.3)
    status         TEXT NOT NULL DEFAULT 'placed' CHECK (status IN
                   ('placed','paid','picking','shipped','delivered','canceled')),
    total_cents    INTEGER NOT NULL CHECK (total_cents >= 0),
    placed_at      TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX orders_customer ON orders (customer_id, placed_at DESC);

CREATE TABLE order_lines (
    id           BIGINT PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id   BIGINT NOT NULL,
    warehouse_id BIGINT NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    price_cents  INTEGER NOT NULL
);
CREATE INDEX order_lines_order ON order_lines (order_id);

CREATE TABLE order_status_history (
    order_id    BIGINT NOT NULL,
    from_status TEXT,
    to_status   TEXT NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL,
    changed_by  TEXT NOT NULL DEFAULT 'system'
);
CREATE INDEX osh_order ON order_status_history (order_id, changed_at);

-- Replay-dedup suppression for the append-only table (§5.4); key = full natural row
CREATE FUNCTION osh_dedup() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM order_status_history h
               WHERE h.order_id = NEW.order_id
                 AND h.from_status IS NOT DISTINCT FROM NEW.from_status
                 AND h.to_status = NEW.to_status
                 AND h.changed_at = NEW.changed_at
                 AND h.changed_by = NEW.changed_by) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_osh_dedup BEFORE INSERT ON order_status_history
FOR EACH ROW EXECUTE FUNCTION osh_dedup();
```

`orders.id`/`order_lines.id` arrive as monolith identity values via CDC; OS-side inserts allocate from local identity sequences seeded past the monolith high-water mark at flip (one-time `setval`, same discipline as §1.3).

### 3.2 Inventory Service DDL

```sql
CREATE TABLE inventory_levels (
    product_id   BIGINT NOT NULL,
    warehouse_id BIGINT NOT NULL,
    on_hand      INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved     INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    updated_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (product_id, warehouse_id),
    CHECK (reserved <= on_hand)
);

CREATE TABLE inventory_movements (
    product_id   BIGINT NOT NULL,
    warehouse_id BIGINT NOT NULL,
    delta        INTEGER NOT NULL,
    reason       TEXT NOT NULL CHECK (reason IN
                 ('order','cancel','import','adjustment','reconcile')),
    order_id     BIGINT,
    occurred_at  TIMESTAMPTZ NOT NULL
);
CREATE INDEX inventory_movements_prod_time ON inventory_movements (product_id, occurred_at);

CREATE TABLE reservations (
    id           BIGINT PRIMARY KEY,
    order_id     BIGINT NOT NULL,                 -- cross-system reference: saga-enforced (§4)
    product_id   BIGINT NOT NULL,
    warehouse_id BIGINT NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX reservations_order ON reservations (order_id);

CREATE TABLE availability_available (            -- replaces products.cached_available (§1.5)
    product_id    BIGINT PRIMARY KEY,
    available_qty INTEGER NOT NULL DEFAULT 0
);
CREATE OR REPLACE FUNCTION refresh_available() RETURNS trigger AS $$
BEGIN
    INSERT INTO availability_available (product_id, available_qty)
    VALUES (NEW.product_id,
            COALESCE((SELECT SUM(on_hand - reserved) FROM inventory_levels
                      WHERE product_id = NEW.product_id), 0))
    ON CONFLICT (product_id) DO UPDATE SET available_qty = EXCLUDED.available_qty;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_refresh_available
AFTER INSERT OR UPDATE ON inventory_levels
FOR EACH ROW EXECUTE FUNCTION refresh_available();

-- Replay-dedup suppression; key = full natural row (§5.4)
CREATE FUNCTION mov_dedup() RETURNS trigger AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM inventory_movements m
               WHERE m.product_id = NEW.product_id
                 AND m.warehouse_id = NEW.warehouse_id
                 AND m.delta = NEW.delta AND m.reason = NEW.reason
                 AND m.order_id IS NOT DISTINCT FROM NEW.order_id
                 AND m.occurred_at = NEW.occurred_at) THEN
        RETURN NULL;
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_mov_dedup BEFORE INSERT ON inventory_movements
FOR EACH ROW EXECUTE FUNCTION mov_dedup();
```

The `inventory_levels` CHECK constraints are byte-identical to schema.sql:32-40 — the oversell invariant's structural half moves with the table.

### 3.3 Ownership boundary

**Monolith migrated tables post-Phase-5:** none dropped — they persist as fenced, REV-fed, read-only audit replicas (§3.4). All columns of the six migrated tables move.

**Stay:** `customers`, `products`, `warehouses`, `audit_log`, `order_number_seq` (monolith copy fenced post-flip, retained indefinitely; OS copy created at flip, §1.3).

**Cross-domain foreign keys and replacements:**
- `orders.customer_id→customers`: application-enforced referential integrity in OS insert paths plus the replicated `customers` slice.
- `order_lines.product_id→products`, `order_lines.warehouse_id→warehouses`: replicated slices; OS validates every line against them before insert.
- `reservations.(product_id,warehouse_id)→inventory_levels`: replaced by saga ordering — the IS-side step's `FOR UPDATE` on the level row (code-paths.md §1 shape) is itself the existence check; a reservation never lands without its level row being locked and validated in-transaction.

**Replicated reference slices** (FWD-replicated, fenced read-only via the write fence): OS receives `customers(id, external_ref, tier)`, `products(…)`, `warehouses(id, code, region)`; IS receives `warehouses`. `products.cached_available` replicates into the slice but is unused by service reads — superseded by `availability_available` (§1.5).

### 3.4 Monolith-side modifications (rolling deploys, additive-branching)

1. Phase 0: `ALTER TABLE inventory_movements REPLICA IDENTITY FULL;` `ALTER TABLE order_status_history REPLICA IDENTITY FULL;` create `pub_os` (orders, order_lines, order_status_history), `pub_is` (inventory_levels, inventory_movements), `pub_refs` (customers, products, warehouses). Scope excludes `audit_log` and every non-migrated table, so trigger fan-outs never cross the boundary.
2. Phase 3 fence (installed by the migration role, not by app deploy):
```sql
CREATE FUNCTION write_fence() RETURNS trigger AS $$
BEGIN
    IF current_user NOT IN ('atlas_cdc', 'migration_ctl') THEN
        RAISE EXCEPTION 'atlas write fence: ownership moved';
    RETURN NULL;
END $$ LANGUAGE plpgsql;
-- BEFORE INSERT OR UPDATE OR DELETE triggers on all six migrated tables
```
`RETURN NULL` suppresses the local write, so REV-applied rows (role `atlas_cdc`) are the only writes that land; old-binary writes raise before any row change, roll back the caller's transaction, and commit nothing. The same fence is installed on service-side slice tables, making slices structurally read-only.
3. Phase 5: `trg_audit_orders` (schema.sql:150) keeps firing as-is on the REV-fed replica rows; `audit_log` schema untouched. The misspelled pair from schema.sql:136-138 (`refres_available` bound by `trg_refresh_available`) is replicated verbatim — both spellings created, trigger bound to the misspelled one — because fixing the typo is outside the split's force (non-goal) and changing the binding changes which function fires.

**State machines.** Order status: `placed→paid→picking→shipped→delivered`, `placed|paid→canceled`, enforced by the guarded UPDATE already in code-paths.md §2 (`WHERE status IN ('placed','paid')`); no new transitions. Inventory: `reserved` increments/decrements and `on_hand` rebuilds, each inside a single transaction per code-paths.md §1-§4.

## 4. Invariant enforcement map

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| No oversell, continuously | Check-and-increment never separated: `SELECT … FOR UPDATE` + conditional check + `reserved += qty` + `reserved <= on_hand` CHECK (schema.sql:39) in one IS transaction; post-cutover placement is saga-ordered order-first (§4.1), so the stock mutation always lands inside its own locked-verified transaction | `t-oversell-01`: 64 concurrent placements against a level row with available = qty+1; assert exactly one succeeds and `reserved <= on_hand` holds at every commit point (verified by `inventory_movements` SUM). Run pre- and post-cutover. |
| Atomicity of order+stock mutations | Pre-cutover: one monolith transaction (code-paths.md §1). Post-cutover: saga with compensated orphans (§4.1); CDC applies each origin transaction's rows in one consumer transaction, so cross-system delivery is all-or-nothing per origin transaction | `t-atomic-02`: kill the IS consumer mid-`place_order` event batch (fault `f-01`); assert no partial order exists in OS/IS and the next apply batch completes the row set. |
| Order numbers unique, strictly increasing per customer | Single-sequencer discipline (§1.3): one allocator at any instant; flip-time `setval` handoff with margin 1,000; rollback-time jump past the service high-water mark; `UNIQUE` on `orders.order_number` as backstop | `t-mono-03`: interleave placements across the flip and a rollback; assert per-customer numbers strictly increase and zero unique violations in either DB; replay the EDI strictly-increasing check (code-paths.md §5) over the exported ranges. |
| Exactly-once per committed write | Origin-transaction atomic apply (position advances at apply-commit); replayed events suppressed by dedup triggers keyed on natural row/PK (§3.1/§3.2) or idempotent by PK; rollback convergence verified by row-count + checksum comparison (§5.7) | `t-once-04`: crash-restart the consumer at a forced position reset; assert zero duplicate rows in `inventory_movements`/`order_status_history` (dedup triggers fired) and matching row counts/checksums against origin. |
| Availability budget (4 min 23 s/month) | Migration-attributable unavailability is limited to one managed-PG restart (§8 arithmetic, worst case 188 s); drain/flip/fence/rollback are LB-weighted and trigger-based — zero-downtime by construction | `t-avail-05`: measure order-placement error rate across the restart and the flip; assert total migration-attributable unavailability < 263 s in the test month. |

**§4.1 Post-cutover placement saga** (the only cross-system transaction shape introduced; cancel/shipment saga-identically):
1. OS transaction: validate lines against `products`/`warehouses` slices, compute `total_cents`, `INSERT INTO orders … RETURNING id, order_number` (OS-local sequence, §1.3), insert `order_lines`.
2. IS transaction: `SELECT … FOR UPDATE` the level rows, availability check, `reserved += qty`, `inventory_movements` insert (delta −qty, reason `'order'`), `reservations` insert.
Failure between steps: the order row exists without reservations — an **orphan**. The orphan compensator (OS worker, 30 s scan interval) selects `orders` rows with `status='placed'`, `placed_at < now() − interval '2 min'`, and no matching `reservations` row in IS, then flips them to `canceled` via guarded UPDATE (`WHERE status = 'placed'`) plus an `order_status_history` entry (`changed_by='orphan_compensator'`). The guard makes compensation exactly-once. Oversell analysis: between steps the reserved value is unchanged, so concurrent placers see unchanged availability; contention loses at step 2's locked check and compensates. The reserved increment never lands outside its locked-verified transaction.

## 5. Failure-mode walkthroughs

**1. Scenario:** Backfill crashes at a stated cursor mid-snapshot of `orders`.
**What happens:** 1. Publisher snapshot scan dies with the connection; WAL tail never starts. 2. Operator restarts the subscription; the protocol resumes from the last confirmed position — snapshot restarts for incomplete tables, already-completed tables skip to the tail. 3. Tail replays every change since the snapshot position; apply is idempotent (§4 row 4).
**Evidence:** `pg_stat_replication` shows the subscription back in `streaming`; row count + checksum of `orders` (OS) equals origin; zero dedup-trigger suppressions beyond the replayed window.

**2. Scenario:** A row is updated during its own snapshot copy.
**What happens:** 1. Snapshot scan reads the pre-update value. 2. The UPDATE's WAL record sits after the snapshot position and rides the tail. 3. Apply executes the UPDATE by PK after the snapshot INSERT commits; latest value wins.
**Evidence:** post-convergence checksum equality; the tail's apply log shows the UPDATE applied after the INSERT for that PK.

**3. Scenario:** A `reservations` row is deleted behind the backfill cursor.
**What happens:** 1. Snapshot scan copies the row. 2. The DELETE record (PK-only under `DEFAULT` replica identity) rides the tail. 3. Apply deletes by PK after the INSERT commits.
**Evidence:** post-convergence row count equality; apply log shows DELETE-by-PK following the INSERT.

**4. Scenario:** The CDC consumer is killed mid-transaction (fault `f-01`).
**What happens:** 1. Apply worker dies; its open consumer transaction rolls back — no partial origin transaction lands. 2. Restart resumes from the last applied+committed position; the interrupted origin transaction's events replay as a unit. 3. Any replayed INSERT of an already-present row is suppressed by the dedup trigger (natural-row key, §3.1/§3.2); replayed UPDATE/DELETE is idempotent by PK.
**Evidence:** apply log shows one atomic apply of the replayed transaction; zero duplicate natural rows; checksum equality with origin.

**5. Scenario:** A zombie old binary writes to monolith migrated tables after the flip.
**What happens:** 1. Its statement hits the Phase-3 BEFORE fence trigger (current_user ≠ `atlas_cdc`/`migration_ctl`). 2. Trigger raises; the caller's transaction aborts before any row change. 3. Storefront error path returns failure; nothing commits; LB health-check cycle (≤5 min) retires the binary.
**Evidence:** fence exception count in monolith logs matches zombie write attempts; zero committed rows attributable to non-`atlas_cdc` roles post-flip (`pg_stat_activity` history + audit trail).

**6. Scenario:** REV replication stalls during a rollback.
**What happens:** 1. Rollback's convergence gate (REV lag = 0) does not pass; rollback pauses at the gate. 2. Writes already routed to monolith commit normally — monolith is authoritative again. 3. REV resumes; the gate passes only when the monolith replica converges on all service-side writes.
**Evidence:** rollback runbook shows the gate check output; post-resume checksum equality; zero writes lost (service-side writes present in monolith exactly once).

**7. Scenario:** Rollback after divergence — OS/IS accepted writes the monolith never saw.
**What happens:** 1. Write routing flips back to monolith (LB weights); service writes stop at the LB, in-flight saga steps complete or compensate. 2. REV continues; monolith replica converges on every service-side write (row-count + checksum gate). 3. Monolith `order_number_seq` jumps: `setval('order_number_seq', greatest(monolith last_value, OS sequence last_value) + 1000)` — numbers allocated during the active period exist in monolith via REV, so no collision is possible; the UNIQUE constraint is the backstop detector if the discipline was ever broken. 4. Fence drops; monolith deploys unfreeze.
**Evidence:** checksum equality pre-unfence; per-customer monotonicity check passes over the exported ranges; zero unique violations in either DB. Customer impact, stated honestly: rollback window = drain tail (≤45 min) + REV convergence (lag-bounded); during it, placements served by monolith commit normally; active-period orders are present exactly once with their allocated numbers.

**8. Scenario:** IS is partitioned from its database mid-cutover.
**What happens:** 1. Saga step-2 transactions fail at connect; step-1 OS inserts commit, creating orphans. 2. Orphan compensator (§4.1) cancels them after the 2 min age threshold; customers see placement failures and retry. 3. Partition heals; retries succeed. No reserved increment ever lands without its locked-verified step-2 transaction.
**Evidence:** orphan count rises then drains to zero; zero `inventory_levels` mutations without matching `inventory_movements` rows; availability budget impact within §8 arithmetic.

**9. Scenario:** A deploy rolls back mid-Phase-2 (read-redirect binary reverts to legacy).
**What happens:** 1. LB weight for the reverted binary's read-redirect path drops to zero (the rollout controller versions each binary's LB weights). 2. Its reads return to monolith — authoritative, correct. 3. No write path is affected (writes remain monolith-owned until Phase 3).
**Evidence:** per-endpoint comparison harness shows the reverted binary's samples matching monolith at zero skew; zero mismatches attributed to it.

**10. Scenario:** Flip attempted while an old binary is still undrained (gate race).
**What happens:** 1. Flip gate checks drain proof: sustained 60 min of zero connections from old-binary client-port ranges in `pg_stat_activity`, plus deploy-freeze assertion. 2. Gate fails → flip does not execute; no fence installed; monolith remains authoritative. 3. Drain completes; gate passes on retry.
**Evidence:** gate check log shows the failing predicate with the offending connection; post-pass, zero fence exceptions from legitimate traffic.

## 6. AI strategy

No AI features ship in this project; per the standard, nothing in this section applies.

## 7. Testing and release confidence

**Comparison semantics (defined before any "shadow reads match" claim).** The verification harness samples 1,000 keys/s — hot-key-weighted (top-500 products by write rate) plus random — and queries both systems per key at **READ COMMITTED** isolation, keyed by natural PK (`inventory_levels` PK, `orders.order_number`) and compared **as of the sample instant**. Two systems under 4,000 writes/sec are allowed to differ only within the in-flight skew envelope: a comparison is *tolerated* iff the divergence is attributable to origin events with commit timestamps in `(T_sample − δ, T_sample]`, where `δ = max(measured FWD/REV lag over the trailing 60 s) + 1 s`, measured from `pg_stat_replication` sent/flushed positions. Any mismatch outside the envelope routes to the quarantine queue (table, key, both values, timestamps) and pages a human after 5 min sustained. A phase may not advance on harness silence alone — it advances on the envelope-bounded mismatch rate stated in §8 exit criteria.

**Fault-injection tests** (each executed in staging against a scaled replica of the artifacts' schema, then re-executed in production at reduced scale during its phase's soak):
- `f-01` kill the CDC consumer mid-transaction → walkthrough 4; asserts atomic apply, dedup suppression of the replayed unit, checksum convergence.
- `f-02` crash the backfill at a stated cursor → walkthrough 1; asserts resume-from-confirmed-position and snapshot+tail convergence.
- `f-03` partition a service from its database mid-cutover → walkthrough 8; asserts orphan creation, compensator drain to zero, zero unlocked reserved mutations.
- `f-04` roll a deploy back mid-phase → walkthrough 9 (read) and 5 (write fence); asserts version-gated LB weights and loud fence rollbacks with zero committed zombie writes.
- `f-05` stall REV during a rollback → walkthrough 6; asserts the convergence gate holds the rollback until lag = 0.
- `f-06` attempt the flip with an undrained binary → walkthrough 10; asserts the drain-proof predicate blocks the flip.

**Load and convergence gates.** Apply-capacity benchmark: sustained ≥5,000 events/s per consumer (25% margin over the 4,000 writes/s peak) before Phase-1 exit. Oversell/monotonicity/atomicity tests `t-oversell-01`/`t-mono-03`/`t-atomic-02` (§4) run at every phase boundary, pre- and post-cutover.

## 8. Delivery phases

Phases close on evidence, never on dates. The day-by-day fit below is capacity arithmetic the brief explicitly requires; every gate remains evidence-based, and the schedule slack absorbs gate extensions.

| Phase | Built / changed | Exit criteria (verifiable) |
|---|---|---|
| 0 Prep | Service DBs provisioned; replica-identity ALTERs; `pub_os`/`pub_is`/`pub_refs`; apply-capacity benchmark; one monolith restart (`wal_level = logical`, trough) | Benchmark ≥5,000 events/s sustained 10 min · publications exist with correct scopes · restart completed within budget · zero error-rate regression on storefront paths |
| 1 Backfill + tail | OS/IS subscriptions start (snapshot + WAL tail) | Row-count + checksum equality of all six tables against origin at converged position · envelope-bounded mismatch rate = 0 outside `(T−δ, T]` for 24 h · lag ≤2 s at peak |
| 2 Read cutover (gradual) | Per-endpoint LB-weighted read redirects in new binaries; comparison harness live | Each endpoint step: 72 h soak with zero out-of-envelope mismatches attributed to that endpoint · every step individually reversible (weight flip-back verified) |
| 3 Write cutover | Deploy freeze; drain (proof: 60 min sustained zero old-binary connections in `pg_stat_activity`); flip; fence installed; REV started; sequence handoff (`setval` + margin 1,000) | Drain-proof predicate logged true · flip executed · zero fence exceptions from legitimate traffic in 24 h · REV lag ≤2 s at peak · `t-mono-03` passes across the flip |
| 4 Soak | Nothing new; verification continuous | 7 consecutive days: zero out-of-envelope mismatches, zero fence hits, lag within budget, orphan-compensator rate at contention baseline, EDI strictly-increasing check passes hourly |
| 5 Decommission | Monolith migrated tables re-designated fenced audit replicas; REV continues indefinitely; monolith sequence retained (fenced) | Audit-replica integrity: `audit_log` entries continue at pre-migration rate for orders mutations · checksum equality sustained 7 days · rollback drill (`f-05`-scale) executed successfully once |

**Capacity and duration arithmetic.** Backfill: snapshot streaming planning value 50 MB/s sustained on commodity NVMe+10GbE, derated for WAL contention. WAL contention at peak: 4,000 writes/s × ~400 B average row ≈ 1.6 MB/s; trough (02:00–05:00 UTC, 900 writes/s) ≈ 0.4 MB/s. Parallel OS+IS backfill (shared publisher resources, parallel derate factor 0.7, sustained-average load over a 12 h night window ≈1,500 writes/s → effective ≈45 MB/s each): `orders`+`order_lines` 1.1 TB = 1.1×10⁶ MB → ≈24,400 s ≈ **6.8 h**; inventory tables ~300 GB = 3×10⁵ MB → ≈1.2 h; parallel wall-clock ≈6.8 h against a 12 h window → **≈5 h slack** per backfill night. REV's flip-time snapshot re-applies convergent upserts (monolith already holds the rows) — same order of cost, scheduled in trough, non-blocking. Replication lag budget: apply capacity ≥5,000 events/s vs peak event rate 4,000/s → steady-state lag ≤2 s at peak; alarm at 60 s; comparison envelope δ = measured lag + 1 s. Rollback convergence: backlog drains at net ≥1,000 events/s (5,000 apply − 4,000 incoming) → a 100k-row active-period backlog converges in ≤100 s. Availability (brief's budget: 4 min 23 s = 263 s/month): worst-case migration-attributable unavailability = one restart × 90 s + zombie-window fence failures bounded by (100/4,000)×300 s = 7.5 s of failed placements (one undrained binary's share, ≤5 min LB health-check cycle) = **97.5 s**; adding the restart-rollback case (second restart) → **187.5 s < 263 s**, margin ≈75 s. Day-by-day fit inside 30 days: D1-2 prep/restart · D3-4 backfill night + convergence · D5-11 shadow-read soak · D12-17 gradual read cutover + soaks · D18 deploy freeze + drain · D19 flip/fence/REV/sequence handoff · D20-26 soak/verification · D27-28 decommission verification · **D29-30 explicit slack/contingency reserve**. If any evidence-gated soak extends, the 2-day reserve absorbs it; if exhausted, decommission defers and the contract-renewal exposure is disclosed to the PM before the window closes.

## 9. Security and privacy

**Tenant isolation across the new boundary.** Customer isolation is query-structural in OS: every order read carries a `customer_id` predicate or an `order_number` high-water mark (the EDI shape, code-paths.md §5); no OS endpoint resolves orders by bare `id` without a customer scope or support-role assertion. Reference slices are structurally read-only via the write fence (§3.4-2). Replication endpoints run dedicated roles: publisher-side `atlas_pub` holds replication privilege only; consumer-side `atlas_cdc` holds INSERT/UPDATE/DELETE on the replicated tables only, no DDL, no sequence access. Replication ports are firewalled to the two peer cluster CIDRs only; subscription conninfo lives in the managed secret store, rotated every 90 days. All data movement stays in-region (data-residency contracts; no third-party CDC). Audit: `audit_log` continues via REV re-fire of `trg_audit_orders` (§3.4-3), so compliance reads see an unbroken trail across the boundary.

## 10. Risk register

| Risk | Mitigation (mechanism) | Gate |
|---|---|---|
| Drain-proof false positive (zombie survives) | Write fence (§3.4-2) fails zombie writes loudly; LB health-check cycle ≤5 min retires it; availability impact sized (§8) | Walkthrough 5 evidence before Phase-3 exit |
| REV stall during rollback | Convergence gate holds rollback until lag = 0; monolith authoritative meanwhile | `f-05` assertion |
| Orphan storm under contention | Orphan compensator bounded by guarded UPDATE; oversell analysis (§4.1) shows reserved never unlocked without its locked-verified transaction | `t-oversell-01` at every phase boundary |
| Backfill duration overrun | 5 h slack per backfill night; trough scheduling; parallel derate measured, not assumed | Phase-1 exit criteria |
| Comparison-envelope miscue (false alarm → premature rollback) | Envelope defined numerically (§7); rollback itself is safe and convergent (§5.7) | Quarantine-queue review procedure |
| Sequence-discipline breach | Single-allocator gating (drain proof precedes handoff); UNIQUE backstop detector; rollback jump | `t-mono-03` + walkthrough 10 |
| Fence misconfiguration blocks REV | Fence predicate whitelists `atlas_cdc`/`migration_ctl`; REV-lag alarm at 60 s surfaces any suppression of legitimate flow | Phase-3 exit criteria |
| Deploy-freeze violation (new old binary mid-drain) | Freeze asserted by rollout controller as a flip-gate predicate; violated freeze aborts the drain attempt | Walkthrough 10 predicate |

## 11. Explicit tradeoffs

1. **Placement atomicity degrades** from single-transaction all-or-nothing (code-paths.md §1) to the order-first saga with compensated orphans (§4.1). Orphan window ≈ inter-transaction gap (ms-scale); compensation lands within 2 min + 30 s scan. Customer impact: rare, retriable placement failures under contention or crash; no committed-write loss; disclosed here because the storefront's "failed placement leaves no trace" assumption is weakened to "failed placement leaves a compensated, canceled trace."
2. **Cancel/shipment atomicity degrades** similarly, ordered conservatively (status flip precedes reserved decrement); failure mode is delayed stock release, detectable via `inventory_movements` divergence, never oversell.
3. **Zombie-window writes fail loudly** rather than being captured and forwarded; availability impact sized in §8 (≤7.5 s of failed placements worst case) — chosen over bidirectional value sync, which loses committed increments (§1.2).
4. **EDI exports delay** by REV lag δ during convergence windows; partners' high-water marks tolerate the gap; strictly-increasing order preserved by the single-allocator discipline.
5. **Monolith retains ~1.4 TB of audit replicas indefinitely** (capacity cost) — chosen over breaking the compliance `audit_log` feed, which no named mechanism could re-establish post-decommission.

## 12. Where this is stronger than required

1. **Standing rollback**: REV runs indefinitely, so rollback is available at any point after the flip, not merely per-phase. Brief requires per-phase reversibility; this provides permanent reversibility at the cost of one retained replica set.
2. **Affirmative replay-dedup**: dedup triggers (§3.1/§3.2) make crash-replay and operator position-reset duplicates harmless by suppression, beyond naming the dedup key — the brief asks for the key; this ships the suppression.
3. **Collision prevention, not just detection**: the sequence handoff with margin plus rollback jump makes `order_number` collisions structurally impossible under the discipline; the UNIQUE constraint operates as backstop detector.
4. **Audit continuity through decommission**: REV re-fire of `trg_audit_orders` keeps the compliance trail unbroken across the boundary — the brief is silent on post-decommission audit; this keeps it intact.

## 13. Assumptions

1. Managed-PG instance class for the services is comparable to the monolith's (NVMe + 10 GbE); snapshot streaming planning value 50 MB/s sustained, derated to ≈45 MB/s under the stated contention — to be measured, not assumed, in Phase-0 benchmarking.
2. Consumer apply capacity ≥5,000 events/s sustained (benchmark gate, Phase-0 exit).
3. Managed-PG restart duration ≤90 s; LB health-check cycle ≤5 min.
4. Drain-proof threshold: 60 min sustained zero connections from old-binary client-port ranges in `pg_stat_activity`.
5. Comparison envelope δ = measured lag + 1 s; storefront cache TTL absorbs ≤2 s lag; order-status read tolerance ≤5 s.
6. Shipment-confirmation shape (brief silent): reserved decrement + `inventory_movements` insert (reason `'reconcile'` per the existing enum — no enum extension) + reservations delete, saga-ified exactly as cancel (§4.1 ordering).
7. Zombie write-share bound = peak/40 (one binary of ~40).
8. No multi-region (brief); data residency satisfied by in-region self-hosted replication only.
