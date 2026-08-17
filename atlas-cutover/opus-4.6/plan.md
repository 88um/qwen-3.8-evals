# Atlas Cutover — Engineering Plan

## §1 Technology decisions

### 1.1 Change-data capture

**Choice:** PostgreSQL native logical replication (`CREATE PUBLICATION` / `CREATE SUBSCRIPTION`, `pgoutput` protocol).

**Rejected:** Debezium. Provides schema-evolution tracking and Kafka integration, but introduces a JVM runtime, a connector configuration layer, and a Kafka cluster — three components that do not exist in the current infrastructure and whose failure modes (connector restart storms, rebalance pauses) compound with the migration's own failure modes.

**Why:** The operation is pure PostgreSQL-to-PostgreSQL table replication with identical column definitions. Native logical replication performs the initial table copy (`copy_data = true`) and streaming in one mechanism, uses the same WAL pipeline the database already maintains, and exposes lag via `pg_stat_subscription` and `pg_replication_slots.confirmed_flush_lsn`. No additional runtime.

**REPLICA IDENTITY per table:**

| Table | PK exists | Mutated by | REPLICA IDENTITY | Notes |
|---|---|---|---|---|
| `orders` | `id` (IDENTITY) | INSERT, UPDATE | DEFAULT (PK) | |
| `order_lines` | `id` (IDENTITY) | INSERT | DEFAULT | |
| `order_status_history` | none | INSERT only | Set to FULL | Append-only; FULL is precautionary — INSERTs emit full row regardless of REPLICA IDENTITY |
| `inventory_levels` | `(product_id, warehouse_id)` | INSERT, UPDATE | DEFAULT (composite PK) | UPDATEs identify row by composite PK |
| `inventory_movements` | none | INSERT only | Set to FULL | Append-only; same as `order_status_history` |
| `reservations` | `id` (IDENTITY) | INSERT, DELETE | DEFAULT | |

Applied before publications are created:

```sql
ALTER TABLE inventory_movements REPLICA IDENTITY FULL;
ALTER TABLE order_status_history REPLICA IDENTITY FULL;
```

**Backfill + convergence:** `CREATE SUBSCRIPTION ... WITH (copy_data = true)` takes a consistent snapshot at the replication slot's creation LSN, COPYs table contents from that snapshot, then switches to streaming mode and replays all WAL changes accumulated since the snapshot LSN. A row updated during its own copy converges because the streaming phase applies the UPDATE on top of the copied row, keyed by PK. A row deleted behind the copy cursor converges because the DELETE event in the streaming phase removes the stale copied row. For append-only tables without PKs (inventory_movements, order_status_history), only INSERTs occur; each INSERT carries the full row tuple and applies idempotently (no dedup key required — rows are structurally unique by content + `occurred_at`/`changed_at`). Duplicate or reordered events within a single subscription cannot occur: `pgoutput` emits events in commit-LSN order and the apply worker processes them sequentially per subscription.

**Throughput derivation:** Logical replication initial copy achieves 40–80 MB/s on same-region managed PostgreSQL (bottleneck: subscriber apply worker write I/O). At a conservative 40 MB/s:

| Subscription | Tables | Size | Duration |
|---|---|---|---|
| `order_svc_sub` | orders, order_lines, order_status_history | ~1.15 TB | 8.0 h |
| `inv_svc_sub` | inventory_levels, inventory_movements, reservations | ~300 GB | 2.1 h |

Subscriptions run in parallel. Total backfill: **8.0 hours**. WAL retained during backfill: 4,000 writes/sec × 500 bytes/write × 8 h × 3,600 = **57.6 GB** — within managed PostgreSQL WAL storage. After copy, streaming catch-up at 20,000 events/sec processing rate with 4,000 events/sec incoming (net 16,000/sec): 115M accumulated events / 16,000 = **2.0 hours**. Total time to live sync: **10 hours**.

### 1.2 Write fencing

**Choice:** Database trigger on a `migration_control` table, applied to every migrating table. The trigger is deployed inactive (owner = 'monolith') and activated atomically via a single UPDATE to `migration_control`.

**Rejected:** Application-level routing only. A V1 binary that predates the routing code has no mechanism to check `migration_control` and writes directly to the monolith tables. With 45-minute rolling deploys, a V1 binary surviving past the ownership flip is a certainty.

**Why:** The trigger fires for every writer — V1, V2, manual SQL, cron jobs — regardless of code version. It is the only mechanism that fences the 45-minute straggler window without requiring coordinated shutdown.

```sql
CREATE TABLE migration_control (
    table_group TEXT PRIMARY KEY,
    owner       TEXT NOT NULL DEFAULT 'monolith'
                CHECK (owner IN ('monolith', 'service'))
);
INSERT INTO migration_control VALUES ('orders', 'monolith'), ('inventory', 'monolith');

CREATE OR REPLACE FUNCTION fence_writes() RETURNS trigger AS $$
BEGIN
    IF (SELECT owner FROM migration_control WHERE table_group = TG_ARGV[0]) = 'service' THEN
        RAISE EXCEPTION 'MIGRATION_FENCE: % ownership transferred', TG_TABLE_NAME
            USING ERRCODE = 'P0999';
    END IF;
    RETURN NEW;
END $$ LANGUAGE plpgsql;
```

Triggers attached per table (both domains):

```sql
CREATE TRIGGER trg_fence BEFORE INSERT OR UPDATE OR DELETE
    ON inventory_levels FOR EACH ROW EXECUTE FUNCTION fence_writes('inventory');
CREATE TRIGGER trg_fence BEFORE INSERT OR UPDATE OR DELETE
    ON reservations FOR EACH ROW EXECUTE FUNCTION fence_writes('inventory');
CREATE TRIGGER trg_fence BEFORE INSERT
    ON inventory_movements FOR EACH ROW EXECUTE FUNCTION fence_writes('inventory');
CREATE TRIGGER trg_fence BEFORE INSERT OR UPDATE
    ON orders FOR EACH ROW EXECUTE FUNCTION fence_writes('orders');
CREATE TRIGGER trg_fence BEFORE INSERT OR UPDATE OR DELETE
    ON order_lines FOR EACH ROW EXECUTE FUNCTION fence_writes('orders');
CREATE TRIGGER trg_fence BEFORE INSERT
    ON order_status_history FOR EACH ROW EXECUTE FUNCTION fence_writes('orders');
```

V2 application code also reads `migration_control.owner` with a 1-second cache TTL and routes accordingly. The trigger is the safety net; the application-level routing is the primary path and avoids the trigger overhead for V2 binaries after the flip.

Custom ERRCODE `P0999` allows the application's error handler to identify fence errors and return HTTP 503 with a `Retry-After: 5` header, directing clients to retry (and hit a V2 binary or the same binary after its cache refreshes).

### 1.3 Cross-domain writes

**Choice:** Saga with compensating transactions, orchestrated by the V2 monolith binary. `place_order` becomes: (1) call Inventory Service `reserve_batch`, (2) call Order Service `create_order`, (3) on failure of step 2, call Inventory Service `cancel_reservations`.

**Rejected:** Two-phase commit via PostgreSQL `PREPARE TRANSACTION`. 2PC holds `FOR UPDATE` row locks on `inventory_levels` across two database round-trips plus the order-creation round-trip — tripling lock hold time from ~5 ms to ~15 ms per row. At 2,200 orders/sec (55% of 4,000 writes/sec), each locking ~3 inventory_levels rows, this increases lock contention by 3× and risks lock-queue saturation on hot SKUs.

**Why:** The saga releases inventory locks immediately after step 1 commits. Lock hold time on `inventory_levels` remains ~5 ms (identical to the monolith's current single-transaction path). The tradeoff — a crash between steps 1 and 2 leaves an orphaned reservation — is addressed by a reservation reaper (§4).

### 1.4 Order number allocation

**Choice:** Monolith sequence endpoint. The monolith exposes an internal HTTP endpoint that calls `SELECT nextval('order_number_seq')` (or batch-allocates via `nextval` + `setval`) and returns the values. Order Service calls this endpoint for every order creation during the migration.

```python
# Monolith endpoint: /internal/sequence/order_number?count=N
def allocate_order_numbers(count):
    with db.begin() as tx:
        start = tx.execute("SELECT nextval('order_number_seq')").scalar()
        if count > 1:
            tx.execute("SELECT setval('order_number_seq', %s, true)", (start + count - 1,))
        return {"start": start, "end": start + count - 1}
```

Batch size: 50. Order Service pre-fetches 50 numbers per batch (refilled when 10 remain). At 2,200 orders/sec across ~40 V2 binaries = ~55 orders/sec per binary, one batch lasts ~0.9 seconds. Endpoint call frequency per binary: ~1.1 calls/sec. Total across 40 binaries: 44 calls/sec to the sequence endpoint — negligible load on the monolith.

**Rejected:** Separate sequence on Order Service with an offset gap (e.g., Order Service starts at monolith_current + 1,000,000). A V1 straggler that calls `nextval('order_number_seq')` on the monolith during the gap range produces an order_number that the EDI feed (`WHERE order_number > last_exported`) has already advanced past, making the order invisible to partners.

**Why:** A single shared sequence is the only mechanism that preserves the global monotonicity that the EDI partner feed (§5 in code-paths.md) depends on: `nextval` never returns a previously returned value, and all callers (V1 binaries, V2 binaries, Order Service) draw from the same pool.

After full cutover and V1 drain confirmation, the sequence transfers to Order Service:

```sql
-- On Order Service DB, after confirming no monolith writers remain
CREATE SEQUENCE order_number_seq START WITH <monolith_last_value + 1>;
```

## §2 System architecture

**Components:**

| Process | Responsibility | Database |
|---|---|---|
| Monolith (V2 binary, ×40) | Routes reads/writes per `migration_control.owner`; orchestrates saga for `place_order` | Monolith PG (read `migration_control`, write nothing after cutover) |
| Order Service | Owns orders, order_lines, order_status_history; creates orders; manages status transitions | Order Service PG |
| Inventory Service | Owns inventory_levels, inventory_movements, reservations; reserve/release stock; runs nightly reconcile and warehouse import after cutover | Inventory Service PG |
| Audit Bridge Consumer | Polls `order_audit_outbox` on Order Service PG, inserts into `audit_log` on Monolith PG | Reads Order Service PG, writes Monolith PG |

**Communication:**

- V2 binary → Inventory Service: HTTP. `POST /reserve` (batch), `POST /cancel-reservations`.
- V2 binary → Order Service: HTTP. `POST /orders`, `PATCH /orders/:id/status`.
- Order Service → Monolith: HTTP. `GET /internal/sequence/order_number?count=50` for order number allocation.
- Forward CDC: PG logical replication, monolith → Order Service PG and Inventory Service PG (`CREATE SUBSCRIPTION`).
- Reverse CDC: PG logical replication, Inventory Service PG → monolith PG (for `inventory_levels` only, to keep `products.cached_available` trigger firing). Activated at cutover.
- Audit Bridge: `psycopg3` `LogicalReplicationConnection.start_replication()` on Order Service PG, consuming `order_audit_outbox` INSERT events, transforming to `audit_log` rows, writing to monolith PG.

**`products.cached_available` after cutover:** Reverse CDC replicates Inventory Service's `inventory_levels` changes to the monolith's `inventory_levels` table (read-only replica). PostgreSQL fires row-level triggers on subscriber tables during logical replication apply; the existing `trg_refresh_available` (bound to the misspelled `refres_available()` function — a production artifact from a 2019 migration; both spellings exist, the trigger uses the misspelled binding) fires on each replicated row change and UPDATEs `products.cached_available`. The storefront's cache layer (~40K reads/sec) continues reading `products.cached_available` from the monolith with no code change. Staleness budget: reverse CDC lag (<5 s steady-state) + app cache TTL.

## §3 Data model

### Order Service DB

```sql
CREATE SEQUENCE order_number_seq; -- START value set from monolith after migration

CREATE TABLE orders (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_number BIGINT NOT NULL UNIQUE,
    customer_id  BIGINT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'placed'
                 CHECK (status IN ('placed','paid','picking','shipped','delivered','canceled')),
    total_cents  INTEGER NOT NULL CHECK (total_cents >= 0),
    placed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX orders_customer ON orders (customer_id, placed_at DESC);

CREATE TABLE order_lines (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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
    changed_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    changed_by  TEXT NOT NULL DEFAULT 'system'
);
CREATE INDEX osh_order ON order_status_history (order_id, changed_at);

CREATE TABLE order_audit_outbox (
    id       BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    row_pk   TEXT NOT NULL,
    action   TEXT NOT NULL,
    diff     JSONB,
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION audit_orders_outbox() RETURNS trigger AS $$
BEGIN
    INSERT INTO order_audit_outbox (row_pk, action, diff)
    VALUES (NEW.id::text, TG_OP,
            jsonb_build_object('status', NEW.status, 'total', NEW.total_cents));
    RETURN NEW;
END $$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_orders
AFTER INSERT OR UPDATE ON orders
FOR EACH ROW EXECUTE FUNCTION audit_orders_outbox();
```

### Inventory Service DB

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
    occurred_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX inventory_movements_prod_time
    ON inventory_movements (product_id, occurred_at);

CREATE TABLE reservations (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id     BIGINT NOT NULL,
    product_id   BIGINT NOT NULL,
    warehouse_id BIGINT NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (product_id, warehouse_id)
        REFERENCES inventory_levels (product_id, warehouse_id)
);
CREATE INDEX reservations_order ON reservations (order_id);
```

### Ownership boundary

| Monolith column / constraint | Disposition |
|---|---|
| `order_lines.product_id → products(id)` FK | Dropped. Order Service validates product_id against monolith products API at write time. |
| `order_lines.warehouse_id → warehouses(id)` FK | Dropped. Same validation. |
| `reservations.order_id → orders(id)` FK | Dropped. Inventory Service stores `order_id` as an opaque BIGINT; lookup crosses to Order Service when needed. |
| `trg_audit_orders` on `orders` | Replaced by `trg_audit_orders` → `order_audit_outbox` on Order Service; Audit Bridge Consumer writes to monolith `audit_log`. |
| `trg_refresh_available` on `inventory_levels` | Not replicated to Inventory Service DB. Reverse CDC replicates Inventory Service's `inventory_levels` to monolith; existing trigger fires on the monolith's subscriber copy. |

### Identity sequence advancement (after backfill, before write cutover)

```sql
-- Order Service DB
SELECT setval(pg_get_serial_sequence('orders', 'id'), (SELECT MAX(id) FROM orders));
SELECT setval(pg_get_serial_sequence('order_lines', 'id'), (SELECT MAX(id) FROM order_lines));
-- Inventory Service DB
SELECT setval(pg_get_serial_sequence('reservations', 'id'), (SELECT MAX(id) FROM reservations));
```

## §4 Invariant enforcement map

| # | Invariant | Mechanism | Evidence |
|---|---|---|---|
| 1 | **No lost writes** — every order and inventory mutation committed by either binary version exists exactly once in the surviving system of record | **Before cutover:** all writes go to monolith; forward CDC (`CREATE SUBSCRIPTION copy_data=true`) replicates to services. A write committed on the monolith appears in the subscription's confirmed_flush_lsn within the replication lag window. **At cutover:** write fence trigger (`fence_writes()`, ERRCODE P0999) blocks all monolith writes; CDC drains to lag=0 before services accept writes. **After cutover:** services are the system of record. V1 straggler hits the fence trigger and receives P0999; the write never commits on the monolith and no data is lost (client retries against a V2 binary). **Rollback path (§5 #7):** reverse CDC streams service-only writes back to monolith before services go offline. | `test_no_lost_writes`: Insert 10,000 orders via mixed V1/V2 binaries across a simulated cutover. Assert: `SELECT count(*) FROM orders` on the final system of record equals 10,000. Assert: every `order_number` returned to a client exists in exactly one database. |
| 2 | **No overselling** — `available = on_hand - reserved ≥ 0` holds continuously, including during the saga window | **Before cutover:** monolith's single transaction: `SELECT ... FOR UPDATE` on `inventory_levels`, conditional check `on_hand - reserved < qty → OutOfStock`, `UPDATE reserved`, `INSERT order` — all atomic. `CHECK (reserved <= on_hand)` on `inventory_levels` is the DB-level backstop. **After cutover:** Inventory Service's `reserve_batch` endpoint reproduces the same mechanism in its own transaction: `SELECT ... FOR UPDATE`, conditional check, `UPDATE reserved`, within a single Inventory Service DB transaction. The `CHECK (reserved <= on_hand)` constraint on Inventory Service DB is identical. **Saga crash window (reservation committed, order creation fails):** stock is reserved but no order exists — conservative (blocks other orders for that stock) but does not oversell. Reservation reaper: a cron job every 60 seconds deletes reservations older than 120 seconds that have no corresponding order (checked via HTTP call to Order Service `GET /orders?reservation_ids=...`). Released stock becomes available again. | `test_no_oversell_concurrent`: 100 concurrent `place_order` calls for the same SKU with `on_hand=50`. Assert: total `reserved` never exceeds `on_hand`. Assert: `CHECK (reserved <= on_hand)` violation count = 0. `test_no_oversell_saga_crash`: Kill the V2 process between `reserve_batch` response and `create_order` call. Assert: reservation reaper deletes the orphan within 120 s. Assert: `on_hand - reserved` returns to pre-reservation value. |
| 3 | **No flag day** — every switch is gradual, observable, individually reversible | **Read cutover:** percentage-based routing in V2 binary (0% → 1% → 10% → 50% → 100%), controlled by `shadow_read_pct` in a config table, adjustable per table group without deploy. Each percentage level runs for its exit criteria (§8) before advancing. Rollback: set `shadow_read_pct = 0`. **Write cutover:** `migration_control.owner` flipped from 'monolith' to 'service' per table group. Rollback: flip back to 'monolith' (§5 #7). The two table groups (orders, inventory) can be flipped and rolled back independently in principle, though they are flipped together because `place_order` spans both (§1.3). | `test_gradual_read_cutover`: Set `shadow_read_pct = 50`. Send 1,000 read requests. Assert: 480–520 reads served by new service (binomial 95% CI). Set to 0. Assert: 0 reads served by new service. `test_independent_rollback`: Flip inventory to 'service', flip back to 'monolith'. Assert: next `place_order` succeeds against monolith. |
| 4 | **Order numbers unique and monotonic per customer** | **During migration:** all order numbers allocated from monolith's `order_number_seq` via the sequence endpoint (§1.4). `nextval()` is non-transactional and never returns a previously returned value (`pg_sequence_last_value` monotonically increases). V1 binaries call `nextval` directly; V2 binaries call the endpoint which calls `nextval`+`setval` for batch allocation. Both draw from the same sequence — no overlap, no gap in the range visible to EDI. **After migration:** sequence transferred to Order Service DB. Monolith sequence is not advanced further. **EDI feed continuity:** `WHERE order_number > last_exported` on the system of record (monolith during migration, Order Service after) captures every order, because all numbers come from one monotonic source. | `test_order_number_monotonic`: Place 1,000 orders for a single customer across V1 and V2 binaries during a simulated cutover. Assert: `SELECT order_number FROM orders WHERE customer_id = X ORDER BY placed_at` returns a strictly increasing sequence. `test_edi_no_gaps`: Run EDI export loop during cutover. Assert: every committed order_number appears in exactly one export batch. |

## §5 Failure-mode walkthrough

**1. CDC consumer dies mid-transaction during backfill**

Scenario: the subscription apply worker crashes while applying a batch of replicated rows during the initial copy phase.

1. The apply worker's PostgreSQL connection drops; the in-progress transaction on the subscriber rolls back (PostgreSQL transaction semantics).
2. The replication slot on the publisher retains WAL from the slot's creation LSN (WAL is not released until `confirmed_flush_lsn` advances).
3. On subscriber restart, `ALTER SUBSCRIPTION ... ENABLE` resumes the initial table sync from the beginning of the table (the tablesync worker restarts the COPY for that table).
4. The subscriber TRUNCATES the partially-copied table before re-COPYing (PG tablesync behavior for initial copy).
5. After re-COPY, streaming resumes from the slot's retained WAL position.

**Evidence:** `pg_stat_subscription.last_msg_receipt_time` advances after restart. Row counts on subscriber match publisher after streaming catches up.

**2. Backfill crashes at cursor position (e.g., subscriber disk full at row 500M of 800M in `orders`)**

Scenario: subscriber storage exhaustion during initial copy of the `orders` table.

1. The COPY fails with a storage error; transaction rolls back on subscriber.
2. The tablesync worker marks the table's sync state as `init` (not `ready`) in `pg_subscription_rel`.
3. Operator provisions additional storage on the subscriber.
4. `ALTER SUBSCRIPTION order_svc_sub REFRESH PUBLICATION` restarts the table sync — TRUNCATE + re-COPY.
5. WAL retained on publisher since slot creation (57.6 GB for 8-hour window, per §1.1 derivation) provides the change stream for catch-up after re-COPY.

**Evidence:** `pg_subscription_rel.srsubstate` transitions from `i` (init) to `d` (data copy) to `r` (ready). `SELECT pg_size_pretty(pg_total_relation_size('orders'))` on subscriber matches publisher.

**3. Network partition between Inventory Service and its database mid-cutover**

Scenario: Inventory Service loses connectivity to Inventory Service PG after write cutover, while V2 binaries are routing reservations to it.

1. Inventory Service's `reserve_batch` call fails with a connection error.
2. The V2 monolith binary's saga orchestrator receives an HTTP 503 from Inventory Service.
3. The V2 binary returns HTTP 503 to the client. No reservation was committed (the Inventory Service transaction never started). No compensation needed.
4. Client retries. If Inventory Service is still partitioned, the retry also fails.
5. If partition persists >30 s, the circuit breaker (5-second window, 50% failure threshold, 30-second open duration) opens and all `place_order` calls fail fast with HTTP 503.
6. **Rollback option:** operator flips `migration_control.owner` back to 'monolith' for both groups. V2 binaries resume writing to monolith DB (which is still available — the partition is between Inventory Service and its DB, not the monolith). Reverse CDC from Inventory Service PG pauses (Inventory Service PG is unreachable); the monolith's `products.cached_available` stales but remains at last-known values.

**Evidence:** Inventory Service health endpoint returns 503. Circuit breaker state visible in `/internal/circuit-breakers`. `migration_control.owner` value confirms rollback state.

**4. Rolling deploy rolled back mid-phase (V2 → V1 while migration_control.owner = 'service')**

Scenario: a production incident unrelated to the migration triggers a rollback of V2 binary deployment to V1.

1. V1 binaries do not read `migration_control`. They write directly to monolith tables.
2. The `trg_fence` trigger on each monolith table fires and raises P0999 for every V1 write attempt.
3. All order placements and inventory operations from V1 binaries fail.
4. **Immediate operator action required:** flip `migration_control.owner` back to 'monolith' (single UPDATE). Fence triggers become no-ops. V1 binaries resume writing to monolith.
5. Reverse CDC drains any in-flight service writes to monolith (§5 #7 procedure).
6. Service writes during the incident window are preserved via reverse CDC.

**Evidence:** Application error logs show SQLSTATE P0999. After rollback, `SELECT owner FROM migration_control` returns 'monolith'. `pg_stat_subscription` on monolith shows reverse CDC subscription active.

**5. V1 straggler writes to monolith after ownership transfer**

Scenario: a V1 binary survives 45 minutes into the cutover and attempts to place an order.

1. V1 binary calls `INSERT INTO orders ...` on monolith.
2. `trg_fence` on `orders` fires, reads `migration_control.owner = 'service'`, raises P0999.
3. Transaction rolls back. No data committed to monolith. No inventory reserved (the `FOR UPDATE` on `inventory_levels` also hits its fence trigger first in the `place_order` execution order).
4. Client receives HTTP 500 (V1 has no P0999 handler). Client retries. Load balancer routes retry to a V2 binary. V2 binary routes to services. Order succeeds.

**Evidence:** Monolith application log contains error with SQLSTATE P0999 and the V1 binary's host identifier. No orphaned rows in monolith tables (confirmed by `SELECT count(*) FROM orders WHERE placed_at > cutover_timestamp` on monolith = 0, excluding reverse-CDC replicated rows).

**6. Saga failure: Inventory Service reserves stock, Order Service create_order fails**

Scenario: `reserve_batch` succeeds (reservation committed in Inventory Service), then `create_order` call to Order Service times out after 10 seconds.

1. V2 binary receives the timeout, triggers compensation: calls Inventory Service `POST /cancel-reservations` with the reservation IDs returned by `reserve_batch`.
2. Inventory Service's `cancel_reservations` atomically: deletes reservations, decrements `inventory_levels.reserved`, inserts `inventory_movements` with reason='cancel'. All within one transaction.
3. Stock returns to available.
4. V2 binary returns HTTP 500 to client. Client retries the full `place_order`.
5. **If the V2 binary crashes between step 1 and the compensation call:** the reservations are orphaned. The reservation reaper (cron every 60 s, deletes reservations >120 s old with no matching order in Order Service) cleans them up within 120 s.
6. **If `cancel_reservations` itself fails:** the V2 binary logs the failure and the orphaned reservation IDs. Reaper handles cleanup.

**Evidence:** `test_saga_compensation`: Kill V2 process after `reserve_batch` response. Assert: reservation reaper deletes orphans within 120 s. Assert: `SELECT SUM(reserved) FROM inventory_levels WHERE product_id = X` returns pre-test value after reaper runs.

**7. Rollback after divergence — Order Service has accepted N orders the monolith never saw**

Scenario: write cutover has been live for 2 hours. Order Service has accepted 15,840 orders (2,200/sec × 7,200s). A correctness bug is discovered and rollback is required.

1. Operator pauses monolith writes: `UPDATE migration_control SET owner = 'draining'`. V2 binaries receiving 'draining' return HTTP 503 (brief pause). V1 stragglers hit fence trigger.
2. Reverse CDC is already running (established at cutover, §2). The reverse CDC subscriptions on the monolith (`inv_reverse_sub`, `order_reverse_sub`) continue applying service writes. Lag at 4,000 writes/sec with 20,000 events/sec processing: catches up in <5 s.
3. Operator waits for `pg_stat_subscription.latest_end_lsn` on both reverse subscriptions to match the services' `pg_current_wal_lsn()`. This confirms all service-committed writes are on the monolith.
4. On monolith: advance identity sequences past service-originated IDs:
   ```sql
   SELECT setval(pg_get_serial_sequence('orders','id'), (SELECT MAX(id) FROM orders));
   SELECT setval(pg_get_serial_sequence('order_lines','id'), (SELECT MAX(id) FROM order_lines));
   SELECT setval(pg_get_serial_sequence('reservations','id'), (SELECT MAX(id) FROM reservations));
   ```
5. Operator flips `migration_control.owner` back to 'monolith'. V2 binaries resume monolith writes. Write pause ends.
6. Services taken offline (not serving traffic). Forward CDC is re-established for next attempt (new subscription with `copy_data = false` starting from current LSN; existing monolith data is already on the services, so no re-copy needed, only streaming from this point).

**Order number integrity:** all 15,840 orders used order_numbers from the monolith's `order_number_seq` (via the sequence endpoint). These numbers are globally unique. The reverse CDC replicated them with their original order_numbers. No collision: `nextval` never returns a duplicate. The EDI feed's `WHERE order_number > last_exported` picks up these orders on the next hourly run.

**Customer impact:** write pause of ~15 seconds (steps 1–5). Orders placed during the 2-hour diverged period are preserved — they exist in the monolith after reverse CDC completes. Order numbers are valid. No writes lost.

**Evidence:** `SELECT count(*) FROM orders WHERE placed_at BETWEEN cutover_ts AND rollback_ts` on monolith equals the count on Order Service for the same range. `SELECT MAX(order_number) FROM orders` on monolith ≥ value from Order Service. `pg_stat_subscription` on monolith shows both reverse subscriptions at latest LSN.

**8. nightly_reconcile runs during CDC streaming**

Scenario: `nightly_reconcile` starts at 02:30 UTC, UPDATEs 4M `inventory_levels` rows in 5,000-row batches over 30 minutes. Forward CDC is streaming to Inventory Service.

1. Each 5,000-row batch commits a transaction on the monolith. The logical replication slot captures these UPDATEs.
2. CDC subscriber processes the UPDATEs. Each UPDATE is keyed by `inventory_levels` PK (product_id, warehouse_id) — REPLICA IDENTITY DEFAULT on composite PK.
3. Throughput: 4M rows / 30 min = 2,222 rows/sec. Combined with the trough write rate of 900/sec = 3,122 events/sec. CDC consumer at 20,000 events/sec: no lag accumulation.
4. The `trg_refresh_available` trigger fires for each row on the monolith (as today). On the Inventory Service subscriber, no such trigger exists — no fan-out to `products`.
5. `inventory_levels.on_hand` on Inventory Service converges to the reconciled values after the CDC consumer processes all batches.

**Evidence:** After reconcile completes and CDC lag returns to <5 s, compare `SELECT product_id, warehouse_id, on_hand FROM inventory_levels` on monolith vs Inventory Service. Row-level hash match ≥ 99.99% (remaining 0.01% are rows changed in the CDC lag window).

**9. warehouse_import COPY in-flight when write fence activates**

Scenario: a warehouse_import (200K rows via COPY to `inventory_movements`) is mid-transaction when the operator flips `migration_control.owner` to 'service'.

1. The COPY inserts rows one at a time into `inventory_movements`. Each row fires `trg_fence`.
2. Before the flip: `fence_writes()` reads `owner = 'monolith'`, returns NEW. Rows insert normally.
3. After the flip: the next row's `fence_writes()` reads `owner = 'service'` (the SELECT inside the trigger sees the committed UPDATE to `migration_control` — it runs at READ COMMITTED isolation), raises P0999.
4. The entire warehouse_import transaction rolls back. No partial insert: all 200K rows are either committed (pre-flip) or rolled back (flip happened mid-COPY).
5. Wait — step 4 is imprecise. The rows inserted before the flip *within the same transaction* are rolled back too, because the P0999 exception aborts the transaction. The entire COPY is atomic.
6. The warehouse operator receives an import error. After cutover completes, the operator retries the import against the Inventory Service.

**Mitigation:** schedule the write cutover outside warehouse_import windows (known schedule: 3× daily per warehouse at fixed times). The operator runbook checks active warehouse imports before flipping.

**Evidence:** `SELECT count(*) FROM inventory_movements WHERE occurred_at > fence_activation_ts` on monolith = 0 (confirming the rolled-back import left no partial data).

**10. Reverse CDC lag causes stale `products.cached_available` during peak**

Scenario: at 4,000 writes/sec peak, reverse CDC from Inventory Service to monolith lags by 10 seconds. `products.cached_available` on the monolith is 10 seconds stale.

1. The storefront cache reads `products.cached_available` with a 5-second cache TTL. Total staleness: 10 s (CDC) + 5 s (cache) = 15 s.
2. A product with `on_hand=1` is reserved by a customer. Inventory Service updates `reserved=1`. Reverse CDC has not yet delivered this change. `cached_available` still shows 1 on the storefront.
3. A second customer sees `cached_available=1` and attempts to order. V2 binary calls Inventory Service `reserve_batch`. Inventory Service executes `SELECT ... FOR UPDATE` and sees `on_hand=1, reserved=1`, computes `available=0`, raises `OutOfStock`.
4. The second customer receives an out-of-stock error despite the listing showing available. This is a UX inconvenience, not an oversell — the invariant holds.

**Evidence:** `pg_stat_subscription.latest_end_lsn` vs `pg_current_wal_lsn()` on Inventory Service shows 10 s lag. `SELECT cached_available FROM products` on monolith shows stale value. Inventory Service correctly rejects the reservation.

## §6 AI strategy

This project ships no AI features. No models, prompts, or AI-related costs.

## §7 Testing and release confidence

### Shadow read comparison semantics

Shadow reads compare monolith responses against new service responses for the same query. Comparison protocol:

- **Isolation:** both reads execute at READ COMMITTED. The V2 binary issues both reads within 50 ms of each other (monolith read first, then service read).
- **Keyed by:** primary key for tables with PKs. For `inventory_movements` and `order_status_history` (no PK): `(product_id, occurred_at)` and `(order_id, changed_at)` respectively.
- **Skew tolerance:** rows whose `updated_at` / `occurred_at` / `changed_at` is within `2 × current_cdc_lag` of the query timestamp are excluded from mismatch counting. `current_cdc_lag` is computed as `pg_current_wal_lsn()` minus `confirmed_flush_lsn` on the publisher, converted to seconds via WAL generation rate.
- **Mismatch routing:** mismatches are written to a `shadow_mismatches` table in the Order/Inventory Service DB (table_name, pk, monolith_value_hash, service_value_hash, query_ts). An alert fires when mismatch rate exceeds 0.1% of compared rows in any 5-minute window.
- **Blocking criterion:** shadow read phase does not advance to write cutover until 24 consecutive hours of <0.01% mismatch rate (excluding skew-window rows).

### Fault-injection tests

| Test | Injects | Asserts |
|---|---|---|
| `fi_kill_cdc_consumer` | `pg_terminate_backend()` on the subscription apply worker PID during streaming | Subscription restarts automatically (`pg_stat_subscription.pid` changes). Lag recovers to <5 s within 60 s. |
| `fi_crash_backfill_at_cursor` | Fill subscriber disk to 95% during initial table copy | Table sync restarts from TRUNCATE after storage is freed. Final row count matches publisher. |
| `fi_partition_service_db` | `iptables -A OUTPUT -d <inv_service_pg_ip> -j DROP` on Inventory Service host | `reserve_batch` returns 503. No orphaned reservations after reaper runs. Circuit breaker opens within 5 s. |
| `fi_rollback_mid_cutover` | Flip `migration_control.owner` to 'service', wait 60 s, flip back to 'monolith' | All orders placed during 60 s exist in monolith (via reverse CDC). `order_number` sequence is monotonic. |
| `fi_v1_straggler_post_fence` | Send `place_order` directly to monolith DB (bypassing V2 routing) after fence activation | SQLSTATE P0999 raised. No rows inserted in monolith. |
| `fi_saga_process_kill` | `kill -9` the V2 binary between `reserve_batch` response and `create_order` call | Reaper deletes orphaned reservations within 120 s. Inventory returns to pre-test state. |
| `fi_reconcile_during_cdc` | Run `nightly_reconcile` on monolith while CDC is streaming to Inventory Service | After CDC catches up, `inventory_levels.on_hand` on Inventory Service matches monolith for all rows outside the skew window. |
| `fi_warehouse_import_during_fence` | Start `warehouse_import` COPY, then flip fence mid-COPY | Entire import rolls back. Zero partial rows on monolith. Import succeeds when retried against Inventory Service. |

## §8 Delivery phases

**Phase 0 — Infrastructure and fencing**

Deploy Order Service PG and Inventory Service PG with schemas from §3. Create `migration_control` table and all `trg_fence` triggers on monolith (inactive — `owner = 'monolith'`). Set REPLICA IDENTITY FULL on `inventory_movements` and `order_status_history`. Deploy V2 monolith binary with routing logic, saga orchestrator, and shadow-read comparator (all gated behind `migration_control.owner = 'monolith'` — behavior identical to V1).

**Exit criteria:** `SELECT owner FROM migration_control` returns 'monolith' for both table groups. `trg_fence` exists on all 6 migrating tables (confirmed via `pg_trigger`). V2 binary passes all existing integration tests (no behavioral change when `owner = 'monolith'`). Order Service and Inventory Service health endpoints return 200.

**Phase 1 — Replication and backfill** *(first end-to-end data flow)*

Create publications on monolith. Create subscriptions on service DBs with `copy_data = true`. Initial copy + streaming catch-up runs for ~10 hours (§1.1). After sync, advance identity sequences (§3).

Justification for Phase 0 before this phase: fence triggers must exist on the monolith before any data is replicated, because a subscriber crash during backfill could trigger an emergency rollback scenario where the operator needs to prevent monolith writes (§5 #1). Without fences, the operator has no single-UPDATE mechanism to halt writes.

**Exit criteria:** `pg_subscription_rel.srsubstate = 'r'` (ready) for all 6 tables on both subscriptions. `confirmed_flush_lsn` within 5 seconds of `pg_current_wal_lsn()` on the publisher for both slots. Row counts on services match monolith within CDC-lag tolerance (checked by `SELECT count(*)` comparison). Identity sequences on services exceed `MAX(id)` for each IDENTITY table.

**Phase 2 — Shadow reads** *(first end-to-end application traffic)*

Enable shadow reads at 1% (`shadow_read_pct = 1`). Monitor mismatch rate. Advance through 1% → 10% → 50% → 100%. At each level, mismatch rate must hold below 0.01% for 24 hours before advancing. At 100%, all reads served by new services with monolith as fallback.

Justification for Phase 1 before this phase: shadow reads require synchronized data on the services.

**Exit criteria:** `shadow_read_pct = 100` for both table groups. 24 consecutive hours of <0.01% mismatch rate at 100%. Zero P99 latency regressions >50 ms versus monolith-only baseline. Audit Bridge Consumer has delivered all `order_audit_outbox` rows to monolith `audit_log` with <10 s lag.

**Phase 3 — Write cutover**

Sequence (executed during the daily trough window, 02:00–05:00 UTC, avoiding warehouse_import schedules):

1. Verify no warehouse_imports are in-flight.
2. Activate fence: `UPDATE migration_control SET owner = 'service'` for both table groups.
3. Wait for all in-flight monolith transactions to complete (<5 s at trough rate).
4. Wait for forward CDC lag to reach 0 (both subscriptions: `confirmed_flush_lsn = pg_current_wal_lsn()`).
5. Disable forward subscriptions on services: `ALTER SUBSCRIPTION ... DISABLE`.
6. Create reverse CDC: publications on service DBs for `inventory_levels` (Inventory Service) and `orders`, `order_lines` (Order Service, for rollback capability). Create subscriptions on monolith with `copy_data = false`.
7. V2 binaries detect `owner = 'service'` within 1 s (cache TTL) and route writes to services.

Total write pause (steps 2–7): <30 seconds.

**Exit criteria:** `migration_control.owner = 'service'` for both groups. Zero P0999 errors in the last 60 minutes (all V1 stragglers drained). `place_order` success rate ≥ 99.99% for 24 hours. Reservation reaper has zero unmatched orphans older than 120 s. Reverse CDC lag <5 s. EDI feed's next export includes all orders placed post-cutover.

**Phase 4 — Cleanup**

Transfer `order_number_seq` to Order Service DB. Drop forward replication slots on monolith. Migrate `nightly_reconcile` and `warehouse_import` to run against Inventory Service. Remove `trg_fence` triggers. Archive monolith's migrated tables (rename with `_archived` suffix; do not DROP — compliance may need historical access).

**Exit criteria:** Order Service allocates order_numbers from its own sequence (`SELECT nextval('order_number_seq')` on Order Service DB returns values). No replication slots for order/inventory subscriptions exist on monolith (`SELECT * FROM pg_replication_slots` shows none for these slots). `nightly_reconcile` runs successfully against Inventory Service DB (verified by `inventory_levels.updated_at` advancing). Monolith application has zero references to migrated table names (grep of codebase returns zero hits outside archived migration code).

## §9 Security and privacy

**Service-to-service authentication:** V2 monolith binary → Order Service and Inventory Service calls use mTLS with certificates issued from an internal CA. Certificate rotation period: 90 days. The sequence endpoint (`/internal/sequence/order_number`) accepts only connections presenting the Order Service's client certificate.

**Database credentials:** Each service connects to its own PG with a service-specific role. Inventory Service's PG role has no permissions on Order Service's PG and vice versa. Logical replication connections use a dedicated `replication` role with `REPLICATION` privilege and `pg_hba.conf` entries restricted to the subscriber's IP.

**Network isolation:** Order Service PG and Inventory Service PG are in private subnets with security groups allowing inbound 5432 only from their respective service hosts and the monolith (for replication). No public endpoints.

**Audit continuity:** The Audit Bridge Consumer writes every `orders` mutation to `audit_log` in the monolith (§2, §3). The outbox table is append-only; the consumer tracks a high-water mark (max `id` consumed). If the consumer falls behind, the outbox grows (no data loss); an alert fires if `SELECT MAX(id) - consumed_hwm FROM order_audit_outbox` exceeds 10,000 rows.

**Tenant isolation:** `customer_id` is a data field, not an access-control boundary, in the monolith today. This migration does not change the authorization model. Order Service validates `customer_id` against the authenticated session on every request (same as monolith code). Cross-customer data access prevention is unchanged.

## §10 Risk register

| # | Risk | Probability | Impact | Mitigation |
|---|---|---|---|---|
| 1 | Backfill takes longer than 10 hours due to I/O contention, compressing the 30-day window | Medium | Medium | Start backfill during trough (02:00 UTC); reduce `maintenance_work_mem` and `max_wal_senders` on subscriber to reduce publisher load. 10-hour backfill leaves 29 days for remaining phases. |
| 2 | Reverse CDC lag on `products.cached_available` causes customer complaints about stale availability | Medium | Low | Staleness is 15 s total (10 s CDC + 5 s cache). Today's trigger adds 0 s but the app cache already adds 5 s. Net increase: 10 s. If unacceptable, increase CDC `wal_sender_timeout` and decrease subscriber's `wal_retrieve_retry_interval` to 1 s. |
| 3 | `nightly_reconcile` burst (2–6M UPDATEs) causes CDC lag spike that delays Phase 2 shadow read comparison | Medium | Low | Reconcile runs at 02:30 UTC (trough, 900 writes/sec baseline). 2,222 reconcile writes/sec + 900 baseline = 3,122/sec — within CDC consumer capacity (20,000/sec). Lag spike <2 s. |
| 4 | V1 straggler binary persists >45 min due to stuck process | Low | High | Fence trigger blocks the straggler's writes (P0999). Alert on P0999 error count. Operator kills the stuck process. Zero data impact. |
| 5 | Saga compensation failure leaves orphaned reservations for >120 s | Medium | Medium | Reaper cron every 60 s with 120 s threshold. Double-check: reaper alerts if it finds >10 orphans in a single run, indicating systemic compensation failure. |
| 6 | 30-day window constraint: total operational timeline | Low | High | Technical operations: backfill 10 h + shadow reads (exit criteria driven, historically 5–7 days for comparable PG replication setups) + write cutover 30 s + cleanup 1–2 days. Conservative estimate: 15 days of active work. Slack: 15 days. |

## §11 Explicit tradeoffs

**Atomicity weakened for `place_order` after cutover.** The monolith's single-transaction atomicity (inventory reservation + order creation) is replaced by a saga. A crash between reservation and order creation leaves stock reserved without an order for up to 120 seconds (reaper interval). This is a conservative failure mode (over-reservation, not overselling) and affects only the crashing request — all other orders proceed normally. Accepted because 2PC (the alternative that preserves atomicity) triples lock hold time on `inventory_levels` and halves peak throughput (§1.3).

**`products.cached_available` staleness increases by ~10 seconds.** Today the trigger updates `cached_available` synchronously within the write transaction (0 s delay). After cutover, reverse CDC adds ~5–10 s. The storefront already uses a 5 s app cache, so the customer-visible change is from 0–5 s to 5–15 s. Accepted because the only alternative — a synchronous cross-database write from Inventory Service to monolith — reintroduces the coupling the migration exists to eliminate.

**Warehouse imports fail if in-flight during write fence activation.** The COPY transaction rolls back entirely (§5 #9). The operator retries against Inventory Service. Accepted because the alternative — allowing COPY to complete against the monolith while the service owns the table — creates a split-brain write that violates invariant #1. Mitigation: schedule cutover outside import windows.

## §12 Where this is stronger than required

**Database-level write fence is defense-in-depth.** The brief requires handling old binaries; application-level routing alone satisfies this for V2 binaries. The `trg_fence` trigger additionally blocks any PostgreSQL client — psql sessions, cron jobs, backfill scripts, V1 binaries — regardless of whether it participates in the routing protocol. This catches failure modes the brief does not explicitly name (e.g., a DBA's manual INSERT during cutover).

**Reverse CDC for rollback is pre-established, not improvised.** The brief requires per-phase rollback. The reverse CDC subscription is created at cutover time (Phase 3 step 6) and runs continuously, keeping the monolith in sync with service writes in real time. Rollback (§5 #7) is a single UPDATE + a 15-second drain, not a data export/import operation. This exceeds the brief's "reversible" requirement by making rollback a <30-second operation with zero data loss.

## §13 Assumptions

1. **Managed PostgreSQL supports logical replication with ≥4 replication slots simultaneously.** Required for: 2 forward (order, inventory) + 2 reverse (inventory_levels, orders). Most managed PostgreSQL providers support ≥10 slots; AWS RDS supports 20 by default (`max_replication_slots`).
2. **Network latency between monolith and service databases is <5 ms.** Required for: saga round-trip in `place_order` (2 HTTP calls, each <10 ms with <5 ms network + <5 ms DB). If latency exceeds this, `place_order` P99 increases proportionally.
3. **The monolith's application framework supports custom error handlers for SQLSTATE P0999.** Required for: returning HTTP 503 + `Retry-After` on fence errors. If not, V2 binary returns a generic HTTP 500 and relies on client retry logic.
4. **Warehouse import schedules are known and can be avoided during cutover.** Required for: §5 #9 mitigation. If imports are unpredictable, the operator queries `pg_stat_activity` for active COPY commands before flipping the fence.
5. **The misspelled trigger function `refres_available()` continues to work on the monolith's subscriber copy of `inventory_levels`.** Both function spellings exist in the monolith DB (per the schema comment); the trigger binding is to the misspelled name. The subscriber (monolith) fires the trigger on replicated rows using the existing function binding. No rename or fix is required.
6. **The `pg_temp.staged_movements` mechanism in `warehouse_import` is a session-local temporary table.** Logical replication does not replicate temporary tables. The COPY to `inventory_movements` and the INSERT...ON CONFLICT to `inventory_levels` are both permanent-table operations captured by CDC. The temp table is an application-side optimization that does not affect replication.
7. **EDI partner feed can temporarily read from the monolith's reverse-CDC replica of `orders` after cutover.** The feed's `WHERE order_number > last_exported` query works identically against a replica. After Phase 4 cleanup, the feed migrates to read from Order Service DB directly.
