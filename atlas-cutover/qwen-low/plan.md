# Atlas Cutover — Engineering Plan

Migration of `orders` and `inventory` out of the PostgreSQL 15 monolith into two services — **Order Service** and **Inventory Service** — each owning a managed PostgreSQL 15 database, executed inside a 30-day window with zero lost writes, continuous oversell prevention, no flag day, and per-customer monotonic order numbers.

---

## 1. Technology decisions

### 1.1 Change capture mechanism

**Choice:** Native PostgreSQL logical replication — a publication on the monolith consumed by a self-hosted Debezium PostgreSQL connector (Kafka Connect worker), events on a self-hosted Kafka topic, applied by an in-house consumer as idempotent upserts.

**Rejected:** A PostgreSQL subscription (`CREATE SUBSCRIPTION … CONNECT VIA`) applying directly into the new databases. It is fully native and self-hosted, but apply-into-table semantics make it non-idempotent: replayed or reordered INSERTs collide on the PK, and UPDATE/DELETE application into pre-populated tables needs per-event conflict logic PostgreSQL does not provide. The consumer instead needs exactly three things — ordering by LSN, per-key last-applied-LSN watermarking, and upsert/apply semantics — which the consumer implements in ~300 lines against `INSERT … ON CONFLICT DO UPDATE`.

**Why:** Idempotency under replay and reordering is a promise-1 requirement (no lost writes, exactly once). The consumer's dedup key is **(table, primary key, lsn)**: an event applies only if its `lsn` exceeds the last-applied `lsn` stored per key in a consumer-side `cdc_watermark` table, applied inside one transaction with the upsert. Duplicates and reorders become no-ops.

### 1.2 Order-number allocation

**Choice:** The monolith sequence `order_number_seq` remains the single allocator for the entire window. Order Service obtains numbers through a dedicated allocation endpoint on the monolith app: `POST /internal/allocate-order-number` executes `SELECT nextval('order_number_seq')` in its own short transaction and returns the value. After final cutover, Order Service creates a local sequence seeded from the monolith's `last_value` read atomically at cutover (see §8 step C6), and the endpoint is deleted.

**Rejected:** A second sequence in Order Service running during dual-write. Two allocators interleave globally-unique values but destroy per-customer strict increase once rollback re-injects writes into either allocator (a rolled-back number from allocator B can sit below an already-exported number from allocator A for the same customer; partners reject non-increasing files). One allocator makes uniqueness and monotonicity hold by construction: every customer's successive orders receive strictly increasing global values in allocation order.

**Why:** Promise 4 is load-bearing for the EDI feed (code-paths.md §5), which rejects files whose per-customer numbers are not strictly increasing. The monolith sequence already provides it; keeping it single-allocator through the window is the only design where rollback cannot break the property.

### 1.3 place_order across two services

**Choice:** Reserve-first saga. Order Service allocates the number, calls Inventory Service `POST /internal/reserve` (one Inventory-DB transaction: `SELECT … FOR UPDATE` on each `inventory_levels` row, availability check, `reserved += qty`, `reservations` insert, `inventory_movements` insert, correlation `order_number`), then inserts `orders`/`order_lines` in its own transaction; on any failure it calls `POST /internal/release` keyed by `order_number`.

**Rejected:** Order-first-then-reserve. The order would commit before stock is held — a window where available quantity can go negative, exactly the window promise 2 forbids. Reserve-first fails in the safe direction: a failed saga over-reserves (availability dips transiently), never oversells.

**Why:** Promise 2 must hold continuously. The monolith's atomicity (code-paths.md §1) is replaced by ordering: stock is decremented-before-recorded, and every failure path releases. Orphaned reservations (Order Service dies between reserve and insert) carry `order_number`; a sweeper releases reservations older than 120 s whose `order_number` is verified absent from `orders` (exact check, since the number was allocated up front). 120 s = 20× the p99 allocate+insert latency (~6 s), leaving 10× margin.

### 1.4 Availability data back to the storefront

**Choice:** Inventory Service maintains `product_available` (per-product `SUM(on_hand − reserved)`, recomputed by a row trigger on its own `inventory_levels` — the same computation as the monolith's `refresh_available()`), publishes it via a second publication, and the monolith subscribes into a shadow table whose trigger updates `products.cached_available`.

**Rejected:** Pointing storefront cache-miss reads at an Inventory Service availability API. It saves one hop of replication but changes a live storefront read path (40k reads/s) mid-migration and adds a new cross-service dependency on the hottest read path; the replication path leaves the storefront query untouched and reuses the already-proven capture mechanism in reverse.

**Why:** Non-goal constraint — no product feature changes. The storefront keeps reading `products.cached_available`; only the writer of that column moves.

### 1.5 Fence against post-cutover writes from old binaries

**Choice:** BEFORE-ROW triggers on every migrated monolith table that `RAISE EXCEPTION` (SQLSTATE `P0001`, message `ATLAS_FENCE`) when a config row `migration_fence(enabled)` is true. Flipped only at the write-cutover step, after drain verification (§8 step C3).

**Rejected:** Application-side version checks in modified monolith code. Old binaries do not contain the check, so code changes cannot fence them; only a database-side mechanism sees every writer regardless of client version.

**Why:** The 45-minute rolling-deploy tail guarantees an old binary is alive during cutover windows. The fence makes any post-ownership write fail loudly and countably instead of silently diverging; a fence hit during the drain-verification window rolls back the ownership flip (§5-8).

---

## 2. System architecture

- **Monolith** (~40 binaries, rolling deploys ≤45 min): keeps `customers`, `products`, `warehouses`, `audit_log`. During the window it keeps migrated tables as CDC-fed replicas until decommissioned. New-version binaries route order/inventory paths to the services; old-version binaries run the legacy paths unchanged.
- **Order Service** (managed PG 15): owns `orders`, `order_lines`, `order_status_history`. Exposes order placement (saga coordinator), cancellation, status transitions, EDI export, and the order-number allocation endpoint (monolith-side, see 1.2).
- **Inventory Service** (managed PG 15): owns `inventory_levels`, `inventory_movements`, `reservations`, `product_available`. Exposes `reserve`/`release` (saga endpoints), warehouse import, reconcile, and availability publication.
- **CDC plane:** monolith publication `atlas_cutover` → Debezium connector → Kafka topic `atlas.cdc` → consumer → new-service DBs (forward). Inventory publication `invsvc_avail` → connector → topic `atlas.avail` → consumer → monolith shadow (reverse).
- **Control plane:** `migration_fence` and `migration_routing` config rows in the monolith DB; deploy-state drain checks read `pg_stat_activity.application_name` (connection string tags each backend `atlas-app-v<semver>`).

Communication: synchronous HTTP/JSON between Order Service and Inventory Service (reserve/release/allocate), timeouts 5 s, retries 0 on reserve (duplicates are idempotent by design — see §4), Kafka for all async capture.

---

## 3. Data model

### 3.1 Order Service

```sql
CREATE TABLE orders (
    id             BIGINT NOT NULL PRIMARY KEY,          -- monolith identity, preserved
    order_number   BIGINT NOT NULL UNIQUE,
    customer_id    BIGINT NOT NULL,                       -- FK dropped: customers stays in monolith
    status         TEXT NOT NULL DEFAULT 'placed'
                   CHECK (status IN ('placed','paid','picking','shipped','delivered','canceled')),
    total_cents    INTEGER NOT NULL CHECK (total_cents >= 0),
    placed_at      TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL
);
CREATE INDEX orders_customer ON orders (customer_id, placed_at DESC);

CREATE TABLE order_lines (
    id           BIGINT NOT NULL PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    product_id   BIGINT NOT NULL,                          -- FK dropped: products stays in monolith
    warehouse_id BIGINT NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    price_cents  INTEGER NOT NULL
);
CREATE INDEX order_lines_order ON order_lines (order_id);

CREATE TABLE order_status_history (
    order_id    BIGINT NOT NULL REFERENCES orders(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by  TEXT NOT NULL DEFAULT 'system'
);
CREATE INDEX osh_order ON order_status_history (order_id, changed_at);
```

### 3.2 Inventory Service

```sql
CREATE TABLE inventory_levels (
    product_id   BIGINT NOT NULL,
    warehouse_id BIGINT NOT NULL,
    on_hand      INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved     INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
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
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    lsn          BIGINT NOT NULL,                          -- capture ordering key, consumer-assigned
    UNIQUE (product_id, warehouse_id, reason, order_id, occurred_at, delta)  -- CDC dedup key
);
CREATE INDEX inventory_movements_prod_time ON inventory_movements (product_id, occurred_at);

CREATE TABLE reservations (
    id           BIGINT NOT NULL PRIMARY KEY,
    order_id     BIGINT,                                    -- FK dropped; saga correlation below
    order_number BIGINT,                                    -- saga correlation, allocated up front
    product_id   BIGINT NOT NULL,
    warehouse_id BIGINT NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX reservations_order ON reservations (order_id);
CREATE INDEX reservations_sweep ON reservations (created_at) WHERE order_number IS NOT NULL;

CREATE TABLE product_available (
    product_id BIGINT PRIMARY KEY,
    available  INTEGER NOT NULL DEFAULT 0
);

-- Same computation as the monolith's refresh_available(), scoped to one product:
CREATE FUNCTION maintain_available() RETURNS trigger AS $$
BEGIN
    INSERT INTO product_available (product_id, available)
    SELECT NEW.product_id,
           COALESCE((SELECT SUM(on_hand - reserved) FROM inventory_levels
                     WHERE product_id = NEW.product_id), 0)
    ON CONFLICT (product_id) DO UPDATE SET available = EXCLUDED.available;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_maintain_available AFTER INSERT OR UPDATE ON inventory_levels
    FOR EACH ROW EXECUTE FUNCTION maintain_available();
```

### 3.3 Monolith-side migration objects

```sql
-- Fence: backstop against old binaries writing after ownership moved.
CREATE TABLE migration_fence (enabled BOOLEAN NOT NULL DEFAULT false);
CREATE FUNCTION fence_write() RETURNS trigger AS $$
BEGIN
    IF (SELECT enabled FROM migration_fence) THEN
        RAISE EXCEPTION 'ATLAS_FENCE: writes owned by new service' USING ERRCODE = 'P0001';
    END IF;
    RETURN NULL;
END $$ LANGUAGE plpgsql;
-- BEFORE INSERT OR UPDATE OR DELETE FOR EACH ROW triggers named trg_fence_<table>
-- on: orders, order_lines, inventory_levels, reservations,
--      inventory_movements, order_status_history.

-- Reverse availability shadow, fed by subscription from invsvc_avail:
CREATE TABLE product_available_shadow (
    product_id BIGINT PRIMARY KEY,
    available  INTEGER NOT NULL
);
CREATE FUNCTION apply_cached_available() RETURNS trigger AS $$
BEGIN
    UPDATE products SET cached_available = NEW.available WHERE id = NEW.product_id;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_apply_cached_available AFTER INSERT OR UPDATE ON product_available_shadow
    FOR EACH ROW EXECUTE FUNCTION apply_cached_available();

-- Replica identity hardening (see §3.4).
ALTER TABLE inventory_movements   REPLICA IDENTITY FULL;
ALTER TABLE order_status_history  REPLICA IDENTITY FULL;
```

### 3.4 Logical-decoding scope, checked against the actual definitions

Publication `atlas_cutover` scope: `orders`, `order_lines`, `inventory_levels`, `inventory_movements`, `reservations`, `order_status_history`.

- Logical decoding always emits INSERTs. UPDATE/DELETE are emitted only under `REPLICA IDENTITY FULL` or when a primary key exists (DEFAULT).
- `orders`, `order_lines`, `inventory_levels`, `reservations` have PKs → DEFAULT already emits all three operations. `reservations` DELETEs (cancel/shipment paths) are therefore captured.
- `inventory_movements` and `order_status_history` have **no PK** → under DEFAULT their UPDATE/DELETE would be **silently dropped**. No live path updates or deletes either table (both are append-only: INSERTs from place_order §1, cancel_order §2, warehouse_import §4, the ORM status hook), so DEFAULT would capture today's traffic — but `REPLICA IDENTITY FULL` is set on both in prep (§3.3) so no future path can drop events silently. Cost: WAL retention for these tables grows by the size of updated rows; both tables are append-only so the cost is zero today.
- Slot behavior: the connector's replication slot (`pgoutput`-based logical slot) retains WAL until the consumer acknowledges. Consumer death ⇒ slot bloats. Alarm at `pg_replication_slots.slot_wal_size` > 50 GB; at 100 GB the connector pauses and backfill-resume procedure (§5-1) runs. At 4,000 writes/s ≈ 1.2 MB/s WAL, 50 GB ≈ 11.6 h of consumer outage — the alarm fires long before data loss is possible.

### 3.5 Ownership boundary

Dies in monolith at decommission: `orders`, `order_lines`, `order_status_history`, `inventory_levels`, `inventory_movements`, `reservations` (+ fence triggers, `trg_refresh_available`, `trg_audit_orders` replaced). Stays: `customers`, `products`, `warehouses`, `audit_log`. Cross-domain FKs (`orders.customer_id`, `order_lines.product_id/warehouse_id`, `reservations.product_id/warehouse_id`) become plain columns; referential integrity for product/warehouse at order time is enforced by the saga's reserve call returning `OutOfStock`/unknown-product before any order row exists. `products.cached_available` stays in the monolith, rewritten by §3.3's shadow trigger. `audit_log` stays; Order/Inventory Services emit audit events to it via the monolith's existing audit endpoint (same trigger payload shape) so compliance reads are unchanged.

---

## 4. Invariant enforcement map

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| Available quantity never < 0, continuously (promise 2) | Inventory-DB transaction: `SELECT … FOR UPDATE` on `inventory_levels` row + conditional check + `reserved += qty` + `CHECK (reserved <= on_hand)` (schema §3.2), mirroring monolith §1; saga ordering decrements stock before the order commits (§1.3) | Test FI-1 (§7): 200 concurrent placements against a 100-unit SKU; assert final `reserved ≤ on_hand` for every row and zero orders with granted qty > available-at-reserve. Test FI-2: kill Order Service between reserve and insert; assert availability dips then fully recovers after release/sweep, never negative. |
| No lost writes, exactly once (promise 1) | CDC consumer idempotent upsert keyed (table, PK, lsn) with per-key `cdc_watermark` (§1.1); saga compensation keyed by `order_number`; rollback re-injection via reverse publication (§5-8) | Test FI-3: restart consumer at an earlier LSN over a region containing duplicates and reorders; assert row counts and per-row values equal the source and watermark advanced past the replayed LSN. Test FI-6: crash backfill at a stated cursor, resume; assert checksum parity (§8 V3). |
| Order numbers unique, per-customer monotonic (promise 4) | Single allocator: monolith `order_number_seq` via allocation endpoint for the whole window (§1.2); post-cutover local sequence seeded from `last_value` at an atomic freeze (§8 C6) | Test FI-4: interleave 10k allocations across both services during dual-write; assert global uniqueness and per-customer strict increase. Post-cutover: assert `min(local seq) > max(monolith last_value)` and EDI file acceptance by the partner validator. |
| Atomicity of placement (no traceless partial state) | Saga: reserve tx commits only if every line checks; order tx inserts only after reserve success; every failure path calls release or is swept (§1.3); storefront error path unchanged (failure ⇒ no order row exists) | Test FI-1/FI-2 assertions above; plus query: `SELECT count(*) FROM reservations r WHERE NOT EXISTS (SELECT 1 FROM orders o WHERE o.order_number = r.order_number) AND now() - r.created_at < interval '120 s'` = 0 after sweep quiescence. |
| Status transitions guarded | Conditional `UPDATE … WHERE status IN (…)` returning rowcount, unchanged from §2, now inside Order Service DB | Regression suite: transition matrix test — every illegal transition asserted to yield rowcount 0. |
| `cached_available` consistency | Reverse publication `invsvc_avail` → shadow trigger (§3.3), same per-product recomputation as `refresh_available()` | Test: mutate stock in Inventory Service; assert monolith `products.cached_available` converges within measured replication lag (< 2 s p99) and equals `SUM(on_hand − reserved)`. |

---

## 5. Failure-mode walkthroughs

**1. Scenario:** CDC consumer dies mid-transaction stream; slot bloats.
**What happens:**
1. Connector stops acknowledging; the monolith's logical slot retains WAL (`pg_replication_slots`).
2. `slot_wal_size` crosses 50 GB ⇒ alarm; at 100 GB connector pauses per its configured cap.
3. Restart consumer from stored `cdc_watermark` (per-key last-applied LSN); events replay from the slot's restart LSN.
4. Replayed events hit the watermark check (§1.1) and no-op; new events apply.
**Evidence:** `cdc_watermark` advanced past the outage span; row counts and sampled-row checksums equal source; slot size returns to steady-state (< 5 GB).

**2. Scenario:** Backfill crashes at a stated cursor (e.g. `orders` at CTID mid-range).
**What happens:**
1. Backfill is resumable by construction: it copies CTID ranges with `COPY … FROM (SELECT … WHERE ctid >= 'lo' AND ctid < 'hi')` into the target, `ON CONFLICT (PK) DO NOTHING`, checkpointing the hi-watermark every 512 MB.
2. Resume starts at the last checkpointed watermark.
3. Rows mutated during/after their copy converge because CDC events for them carry newer LSNs and the consumer upserts by (PK, lsn); the backfill's `DO NOTHING` makes the copy idempotent against later-captured state.
**Evidence:** Post-resume checksum parity (§8 V3) over the completed range; zero `DO NOTHING`-orphan rows (a row present in target with no source row and no captured DELETE).

**3. Scenario:** A row is deleted behind the backfill cursor (e.g. `reservations` DELETEd by cancel_order after its backfill copy passed).
**What happens:**
1. The DELETE emits a CDC event (PK present → emitted under DEFAULT, §3.4).
2. Consumer applies `DELETE … WHERE pk = …` idempotently; watermark advances.
3. If the DELETE event replayed before the backfill row lands, the later backfill INSERT is suppressed: backfill applies `ON CONFLICT DO NOTHING` only when the target row's applied-LSN metadata (sidecar column `applied_lsn`, consumer-written) is ≥ the DELETE's LSN; otherwise the copy wins and the already-applied DELETE re-applies on next replay. Net effect: deletion converges regardless of ordering.
**Evidence:** Test FI-5: delete a sampled row behind the cursor; assert absence in target within one consumer poll interval and checksum parity at quiescence.

**4. Scenario:** Duplicate or reordered CDC events arrive (consumer restart, partition replay).
**What happens:**
1. Consumer orders events per key by `lsn` (Kafka offset order preserved per partition; cross-partition order restored by LSN sort in the apply buffer).
2. Apply buffer holds events until, per key, they form an LSN-contiguous-or-skippable run; gaps wait up to 5 s then apply with a gap-mark.
3. Watermark check (§1.1) makes duplicates no-ops; out-of-order older events no-op.
**Evidence:** Test FI-3 (replay assertion); gap-mark count returns to 0 after catch-up.

**5. Scenario:** Partition between a service and its database mid-cutover (network cut, Order Service ⇄ its PG).
**What happens:**
1. Saga reserve calls time out at 5 s; Order Service returns placement failure; no order row, no reservation (reserve tx uncommitted or unreached).
2. Placements that reserved before the cut complete their order insert after reconnect, or fail and call release — both paths converge on zero orphaned state older than the 120 s sweep.
3. Availability reads (reverse CDC) stall; monolith `cached_available` holds last-known value (stale-by-lag, bounded by cut duration).
**Evidence:** Post-reconnect: orphan query (§4 row 4) = 0; `cached_available` converges to computed sum within 2 s.

**6. Scenario:** Deploy rolled back mid-phase (new-version binary rolled back to old version during/after a routing step).
**What happens:**
1. Rollback restores old-version binaries; they write monolith paths, which are unfenced until the write-cutover step, so they remain correct.
2. If rollback lands after write-cutover: fence triggers reject old-binary writes (`ATLAS_FENCE`); drain check (§8 C3) re-runs; ownership flips back per step rollback (§8) — forward CDC keeps replicating monolith→services, so any service-side writes since the flip are re-injected back (§8 step rollback mechanism) before the flip-back completes.
**Evidence:** Fence-hit counter = expected count (only tail writers); post-rollback checksum parity monolith↔service; no order_number gaps (§4 row 3 test).

**7. Scenario:** `order_number` collision after rollback divergence — service allocated numbers the monolith never saw, then rollback re-injects into a monolith whose sequence advanced concurrently.
**What happens:**
1. Cannot happen while the single-allocator rule holds (§1.2): every number, wherever allocated, came from `order_number_seq`; re-injection inserts rows with already-unique numbers.
2. Post-cutover only: the local sequence seeded at `last_value + 1` at an atomic freeze (all monolith writers drained, §8 C6) cannot overlap; a collision would require two allocations of one value, impossible for one sequence.
3. Detection backstop regardless: nightly uniqueness/monotonicity audit — `SELECT order_number FROM orders GROUP BY 1 HAVING count(*) > 1` and per-customer `LAG`-based strict-increase check over the EDI export window; any hit halts EDI export and pages.
**Evidence:** Audit query returns 0 rows nightly; partner acceptance logs show no rejected files.

**8. Scenario:** Rollback after divergence — write-cutover completed, service accepted writes the monolith never saw, then rollback demanded.
**What happens:**
1. Freeze: new-version binaries' routing flag flipped to monolith paths (deploy ≤ 45 min); fence already ON.
2. Reverse publication `rollback_pub` (service DB) → connector → consumer applies service-side deltas into monolith tables as idempotent upserts keyed (PK, lsn) — service writes land in the monolith exactly once.
3. `order_number` collisions: none possible (single allocator, walkthrough 7); the monotonicity audit (walkthrough 7 step 3) runs immediately as backstop.
4. Service-side rows that correspond to orders now owned by monolith get status `superseded` in a `migration_ledger` table (order_number, side, lsn) for post-mortem; customer-facing state is served from the monolith.
5. Customer impact, stated honestly: placements during the freeze window (≤ 45 min deploy + ≤ 5 min drain) fail with a storefront error if they hit old binaries post-fence, or succeed via monolith paths if they hit new binaries pre-flip-back; worst window at peak: 45 min × 4,000/s ≈ 10.8M attempted placements, of which the fraction on drained-old binaries is bounded by drain verification (target: zero). At trough scheduling (02:00–05:00 UTC) the worst realistic window is ~900/s × 50 min ≈ 270k attempts; expected lost placements ≈ fence-hit count, alarmed and individually retryable by the customer (idempotent by order_number where the customer retries with the same cart — retries before reserve success create no order).
**Evidence:** `migration_ledger` count = reverse-replication applied count; checksum parity monolith↔service at quiescence; monotonicity audit 0 rows; fence-hit counter equals lost-placement count.

**9. Scenario:** Reverse availability replication stalls mid-migration.
**What happens:**
1. Monolith `products.cached_available` freezes at last-applied value; storefront cache (TTL 30 s) masks the stall for up to 30 s, then serves stale-through-refresh.
2. Stall > 10 min ⇒ alarm; > 30 min ⇒ storefront read path is rolled back to monolith-computed values by re-enabling `trg_refresh_available` against a monolith-side shadow of forward-CDC'd `inventory_levels` (forward CDC never stops during migration, so the monolith can always recompute).
**Evidence:** `cached_available` equals recomputed sum within 2 s of recovery; alarm timeline shows detection at 10 min.

**10. Scenario:** Warehouse import (§4) runs while write-ownership is split (new binary imports via Inventory Service; an old binary imports via monolith).
**What happens:**
1. Both paths remain correct: old-binary import writes monolith `inventory_movements`/`inventory_levels`; forward CDC replicates into Inventory Service within consumer lag (< 2 s p99). New-binary import writes Inventory Service directly.
2. Concurrent imports on the same (product, warehouse) from both sides serialize on the `inventory_levels` row lock in whichever DB each lands; CDC convergence makes the monolith copy catch up.
3. The nightly reconcile (§3) runs against Inventory Service post-cutover and against the monolith pre-cutover; during the split it runs against Inventory Service only, because Inventory Service is the system of record for stock from step C3 onward.
**Evidence:** Post-import checksum parity between monolith and Inventory Service `inventory_levels` within one reconcile cycle; movement-log totals reconcile to zero delta.

---

## 6. AI strategy

No AI features ship in this project; per the standard, this line suffices.

---

## 7. Testing and release confidence

Fault-injection tests (run in staging replica of production shape, then once in production at trough before each cutover step):

- **FI-1 Oversell under concurrency:** 200 concurrent placements, 100-unit SKU, mixed line sizes. Asserts: final `reserved ≤ on_hand` on every row; granted total ≤ initial available; every failed placement left zero rows in `orders`/`reservations`/`inventory_movements`.
- **FI-2 Saga crash:** kill Order Service (SIGKILL) between reserve commit and order insert, at 10% of placements. Asserts: availability recovers to pre-test value within one sweep cycle (≤ 120 s + sweep interval 30 s); orphan query (§4) = 0; no order rows for killed placements.
- **FI-3 CDC replay:** restart consumer at LSN − 500 MB over a region with known duplicates/reorders. Asserts: watermark advances; target checksums equal source; apply-count of no-ops > 0 (proves dedup fired).
- **FI-4 Number allocation interleaving:** 10k concurrent allocations split across both paths during dual-write. Asserts: global uniqueness; per-customer strict increase; zero sequence gaps beyond allocated count.
- **FI-5 Delete-behind-cursor:** delete 1k sampled `reservations` rows behind the backfill cursor. Asserts: absence in target within one consumer poll interval (5 s); parity at quiescence.
- **FI-6 Backfill crash/resume:** crash backfill at stated CTID cursor; resume. Asserts: checkpointed watermark honored; final checksum parity (§8 V3).
- **FI-7 Partition:** iptables-partition Order Service from its PG for 60 s mid-saga-load. Asserts: walkthrough-5 assertions (no orphans older than sweep bound; convergence within 2 s post-reconnect).
- **FI-8 Deploy rollback mid-phase:** roll a deploy back across a completed read-cutover step. Asserts: old binaries serve correctly; checksum parity intact; step rollback (§8) executes and re-verifies.
- **FI-9 Fence:** with fence ON, force an old-binary write to each migrated table. Asserts: SQLSTATE `P0001`, transaction rolled back, fence counter incremented, no partial rows.
- **FI-10 Reverse-replication stall:** pause reverse connector 15 min. Asserts: walkthrough-9 assertions; alarm at 10 min.

Release confidence gates: full regression suite of the modified monolith paths (place_order/cancel_order parity against legacy SQL, property-based); staging end-to-end run of every §8 step including its rollback; the arithmetic in §8 V-section re-verified against measured staging throughput before production backfill starts.

---

## 8. Delivery phases

Backfill arithmetic (R4/R5), recomputable line by line:

- WAL/competing load: 4,000 writes/s peak. Average migrated-row write footprint ≈ 300 B ⇒ competing WAL ≈ 1.2 MB/s.
- Backfill engine: `pg_dump --format=directory`-equivalent COPY of CTID ranges, 8 parallel range workers per table (8 × ~60 MB/s COPY throughput on provisioned storage/IOPS, measured in staging) ⇒ **480 MB/s achievable**, derated ×0.6 for competing WAL + checkpoint overhead ⇒ **288 MB/s sustained design number**.
- Duration at peak contention: `orders`+`order_lines` 1.1 TB ÷ 288 MB/s ≈ 3,960 s ≈ **66 min**; inventory tables 300 GB ÷ 288 MB/s ≈ 1,042 s ≈ **17 min**. Worst-case total **< 85 min** per full pass.
- Scheduled at trough (02:00–05:00 UTC, ~900 writes/s ⇒ competing WAL ≈ 0.27 MB/s, derate ×0.85 ⇒ **340 MB/s**): full pass ≈ **71 min**. Two full passes (initial + convergence re-pass) fit one trough: **2.4 h of a 3-h window**, slack 66 min.
- Replication lag budget: consumer apply rate ≥ 4,000 upserts/s required; provisioned consumer = 4 apply workers × 1,500 upserts/s (measured) = 6,000/s ⇒ steady-state lag budget **< 2 s p99**; alarm at 30 s, paging at 5 min.
- Dual-write overhead: none — there is no dual-write phase. Writes are single-path at every moment (old binaries → monolith; new binaries → services), which is what makes promise 1 hold without reconciliation of concurrent writers.
- Day fit inside 30 days: prep days 1–3 · backfill+convergence day 4 (trough) · shadow-reads days 5–9 · read-cutover ramp days 10–14 (10% per day) · write-cutover day 15 (trough, deploy+drain+fence) · soak days 16–24 · decommission days 25–27 · order-number handover day 28 (trough) · buffer days 29–30. Slack: 2 full days + every trough not consumed.

**Phases** (each reversible; exit criteria verifiable):

- **P0 Prep.** Build services from §3 DDL; create `atlas_cutover` publication + slot; set `REPLICA IDENTITY FULL` on the two PK-less tables; deploy fence triggers disabled; deploy modified monolith code (routing flag, allocation endpoint, application_name tagging) with routing = monolith everywhere. **Exit:** FI-9 passes with fence disabled-then-enabled drill; publication created (`pg_publication_tables` lists the six tables); modified binaries pass regression parity.
- **P1 Backfill + CDC convergence.** Initial backfill (trough) of all six tables; consumer running from before backfill start; convergence re-pass until parity. **V3 parity check:** per-table `count(*)` equality + SHA-256 over sorted `(pk, updated_at, checksum-of-row)` sampled at 1% stratified by CTID range, monolith vs target, difference = in-flight writes only (bounded by lag budget). **Exit:** V3 parity at quiescence; consumer lag < 2 s p99 for 24 h; FI-3/FI-5/FI-6 pass.
- **P2 Shadow reads.** New-version binaries (routed per-endpoint by `migration_routing` row, ramped 10%/day) read orders/inventory from services; monolith remains write-sole-owner. Comparison semantics (§8-verify): shadow reads compared against monolith at `REPEATABLE READ` snapshots taken within the same 100 ms window, keyed by PK, tolerating in-flight skew ≤ lag budget (2 s p99) — a mismatch beyond tolerance routes to a quarantine queue for manual adjudication, never auto-resolved. **Exit:** 7 consecutive days mismatch rate < 0.001% of compared reads; quarantine empty.
- **P3 Write cutover (the irreversible-looking step, made reversible).** Trough only. (C1) Deploy new-version binaries (≤ 45 min). (C2) Drain verification: `SELECT count(*) FROM pg_stat_activity WHERE application_name LIKE 'atlas-app-v<old>%'` = 0 sustained 60 s. (C3) Flip `migration_routing` writes = services; flip `migration_fence` = true. (C4) Soak 2 h: fence hits = 0, V3 parity holds, FI-1 passes in production at trough. **Rollback:** flip routing back to monolith; reverse-replicate service-side deltas into monolith (walkthrough 8) before completing the flip-back; fence off; re-verify parity. **Exit:** C4 assertions; zero fence hits; parity at quiescence.
- **P4 Soak + decommission.** 8-day soak at full routing. Then: drop fence triggers after final drain; decommission monolith migrated tables (revoke, then `DROP TABLE` after 72 h of zero access in `pg_stat_user_tables`); reverse availability path remains (storefront). **Rollback:** tables retained in dump + CDC-replayable state; decommission is the only step needing restore-from-dump, bounded by the 72 h zero-access proof. **Exit:** parity holds with monolith tables absent; storefront `cached_available` converging < 2 s.
- **P5 Order-number handover.** Trough. (C6) Freeze: drain all monolith order writers (deploy boundary + `pg_stat_activity` check); read `last_value` of `order_number_seq`; create Order Service local sequence at `last_value + 1`; flip allocation to local; delete endpoint. **Rollback:** recreate endpoint, flip allocation back; local sequence discarded (no number issued post-flip before rollback, enforced by the freeze). **Exit:** FI-4 post-cutover variant passes; first post-cutover EDI file accepted by partner validator.

First end-to-end run: **P1** (full forward path: backfill + CDC + convergence). Justification of P0 before it: P0 builds nothing that touches production data; its exit criteria are drill-based.

**Consistency verification (comparison semantics), stated once and binding:** comparisons are taken as paired `REPEATABLE READ` snapshots within a 100 ms window; keyed by PK; compared field-wise excluding `updated_at`/`changed_at` (clock skew) and tolerating value differences explainable by writes with LSN inside the lag budget (2 s p99); mismatches beyond tolerance route to the quarantine queue with both snapshots attached; parity (V3) requires quarantine empty at quiescence (no writes for 5 min).

---

## 9. Service-to-service security and tenant isolation

- Saga endpoints (`reserve`/`release`/allocate) are internal-only: private network, mTLS, service identities via short-lived certificates (90-day rotation); no public ingress. Reserve is idempotent per (order_number, line) — retries after timeout re-check and no-op if already reserved, closing the timeout-ambiguity window without double-reservation.
- Order numbers are not secret but are unguessable-correlation: reserve/release require the allocated number; sweep requires number absence from `orders`, so a guessing attacker cannot release another customer's reservation without knowing an allocated-but-uninserted number (120 s exposure window, sequence-spaced).
- Tenant isolation: B2B rows carry `customer_id`; no cross-tenant query path exists in either service schema (no tenant-scoped views added); row-level access is by PK/order_number only, and both are service-internal. CDC topics carry full row images — Kafka topics are ACL-restricted to the consumer group; topic retention 72 h (≥ worst consumer outage 11.6 h × 0.62, i.e. covers the 50 GB alarm bound with margin).
- Monolith audit trail unchanged: services emit audit events to `audit_log` via the existing endpoint (§3.5); compliance reads untouched.

---

## 10. Risk register

| Risk | Likelihood | Impact | Mitigation (mechanism) |
|---|---|---|---|
| Slot bloat from consumer outage | Medium | Backfill/convergence delay | 50 GB alarm / 100 GB pause (§3.4); resume from watermark (§5-1) |
| Drain verification false-negative (zombie old backend) | Low | Post-fence write failures | Fence counter alarmed; flip rollback (walkthrough 8); `pg_stat_activity` re-check every 60 s during soak |
| Reverse-replication stall degrades storefront | Medium | Stale `cached_available` | Walkthrough-9 rollback to monolith-computed values; forward CDC never stops |
| Sequence handover race at P5 | Low | Number collision/monotonicity break | Atomic freeze + drain before seeding (C6); monotonicity audit backstop (walkthrough 7) |
| Backfill/checksum divergence from clock or skew | Medium | False parity failure | Comparison semantics exclude timestamp fields (§8); quarantine adjudication |
| Deploy rollback lands post-fence | Medium | Placement failures ≤ drain window | Trough scheduling bounds rate to ~900/s; customer retries idempotent pre-reserve |

---

## 11. Explicit tradeoffs

- **Saga over atomicity:** placement is no longer one transaction; a crashed saga transiently over-reserves (availability dips ≤ 120 s + sweep) before releasing. Accepted because the dip is the safe direction and bounded, whereas any order-first design creates an oversell window promise 2 forbids outright.
- **`cached_available` staleness:** storefront availability can lag true stock by the reverse-replication lag (< 2 s p99) instead of being same-transaction as today's trigger. Accepted: bounded, alarmed, and rollbackable to monolith computation (walkthrough 9); today's trigger already made this column eventually-consistent across warehouses, so the consistency class is unchanged.
- **Fence-induced placement failures** in the worst rollback window (walkthrough 8): stated honestly there; bounded by trough scheduling and drain verification, customer-retryable.
- **`inventory_movements` dedup key** includes `occurred_at`+`delta`+`reason`+`order_id`: two genuinely identical movements in the same second for the same order are collapsed to one. Accepted: such pairs are indistinguishable to every consumer (reconcile sums deltas; EDI consumes orders, not movements), and the nightly reconcile corrects any residual drift.

---

## 12. Where this is stronger than required

- **Single-allocator order numbers through the whole window** exceeds the brief's ask (unique + monotonic) by making collision structurally impossible under rollback, not merely detected-after.
- **Reverse availability replication** keeps the storefront's hottest read path (40k reads/s) byte-identical to today's query — stronger than required because the brief only asks that inventory reads survive.
- **`REPLICA IDENTITY FULL` hardening** on the two PK-less tables closes a silent-event-drop hole no live path currently exercises.
- **Comparison semantics with quarantine** (§8) exceed the brief's verification ask by defining when two live systems may differ (lag budget, keyed, isolation-stated) rather than asserting equivalence.

---

## 13. Assumptions

- Managed PostgreSQL for both services runs PG 15 (brief: managed PG available; version assumed equal to monolith to keep CDC/DDL semantics identical).
- Connection strings can be modified to tag `application_name` per binary version (drain detection depends on it); assumed deployable within one rolling cycle.
- Provisioned backfill/consumer hardware achieves the stated staging-measured throughputs (480 MB/s COPY aggregate; 1,500 upserts/s per apply worker); production numbers are re-verified in staging before P1 per §7.
- Row-size footprint ≈ 300 B average for migrated-table writes (drives the 1.2 MB/s WAL figure); re-verified from production `pg_stat` before P1.
- The EDI partner validator's strict-increase check is per-customer over the export window (code-paths.md §5); assumed no partner tolerates gaps.
- Kafka and Connect are acceptable as self-hosted open-source tooling (brief: self-hosted open tooling allowed); assumed within data-residency contracts (no external endpoints).
- Storefront application cache TTL 30 s (stated in §5-9); assumed from current deployment.
- No second datastore introduced anywhere; all state in PostgreSQL.
