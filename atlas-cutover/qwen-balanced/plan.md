# Atlas Cutover — engineering plan

Migration of the orders and inventory domains out of the PostgreSQL 15 monolith into Order Service (OS) and Inventory Service (IS), each with its own managed PostgreSQL 15 database, executed inside a 30-day window while the business runs. Every mechanism below is checked against `legacy/schema.sql` and `legacy/code-paths.md` as provided.

---

## 1. Technology decisions

### 1.1 Change capture mechanism

**Choice:** Native PostgreSQL 15 logical replication, self-hosted consumer. Publication per source database; slot created at an explicit start LSN via `pg_create_logical_replication_slot(slot, 'pgoutput', false, start_lsn)`; consumer speaks the replication protocol (libpq replication API: `PQreplicationOpen`, `PQstartCopy`) over a self-hosted worker. No third-party CDC SaaS (residency contracts).

**Rejected:** Third-party CDC platform (e.g., a managed change-capture SaaS). Strongest option operationally — managed, battle-tested exactly for this shape. Rejected because the brief forbids it (data residency), and the only capability lost is managed-ops convenience, which the self-hosted worker replaces with ~2k LOC of checkpointed apply logic.

**Why:** Native logical decoding is the only mechanism that (a) emits row-level INSERT/UPDATE/DELETE for the tables as defined in `legacy/schema.sql`, (b) starts at an arbitrary historical LSN so backfill and capture converge, and (c) needs no engine outside PostgreSQL.

Replica-identity check against each table's actual definition (R10):

| Table | PK in artifact | Replica identity (DEFAULT) | Logical decoding emits |
|---|---|---|---|
| `orders` | `id` | PK-based | INSERT/UPDATE/DELETE, keyed |
| `order_lines` | `id` | PK-based | INSERT/UPDATE/DELETE, keyed |
| `inventory_levels` | `(product_id, warehouse_id)` | PK-based | INSERT/UPDATE/DELETE, keyed |
| `reservations` | `id` | PK-based | INSERT/UPDATE/DELETE, keyed |
| `inventory_movements` | **none** | DEFAULT → nothing usable | INSERTs fully; UPDATE/DELETE carry **no decodable key** |
| `order_status_history` | **none** | DEFAULT → nothing usable | INSERTs fully; UPDATE/DELETE undecodable |
| `audit_log` | `id` | PK-based | not captured — stays in monolith (§3) |

`inventory_movements` and `order_status_history` are append-only as written in the artifacts: all five live write paths INSERT (placement, cancel, warehouse import COPY, reconcile writes `inventory_levels` not movements, ORM hook INSERTs history). The consumer therefore applies INSERTs only and treats any UPDATE/DELETE on these two tables as a poison event: halt, alarm, page. Fail-closed, because an undecodable event means the replica has silently diverged.

### 1.2 Backfill mechanism

**Choice:** Consistent snapshot per table via `COPY` inside a `REPEATABLE READ` transaction, with the replication slot created at the snapshot's LSN so the CDC stream contains every mutation from snapshot time onward. Last-writer-wins by (LSN, offset), applied as keyed upserts.

**Rejected:** `pg_dump -Fc` / `pg_restore` parallel restore. Strongest general-purpose option, and it parallelizes. Rejected because restore re-executes rows without WAL ordering: a row updated during its own copy converges only by accident of timing, and there is no LSN ordering to make duplicate/reordered events harmless. Snapshot+slot gives total ordering for free.

**Why:** The brief's convergence questions (row updated during its own copy; row deleted behind the cursor) are answered by LSN ordering, which only the WAL provides.

Convergence semantics (all keyed by natural key):

- **Row updated during its own backfill COPY:** the COPY reads the snapshot value (pre-update); the CDC stream carries the UPDATE at a higher LSN; the consumer's keyed upsert applies the post-update value. Converged.
- **Row deleted behind the backfill cursor:** the CDC stream carries the DELETE; the consumer applies it by key. Converged to absent.
- **Duplicate or reordered CDC events:** consumer checkpoints its applied LSN in its own state table; restart replays from the checkpoint. Replay duplicates are made harmless because every apply is an idempotent upsert keyed by natural key (`orders.id`, `order_lines.id`, `inventory_levels.(product_id, warehouse_id)`, `reservations.id`); for the two keyless append-only tables the **dedup key is the full natural tuple** `(product_id, warehouse_id, delta, reason, order_id, occurred_at)` — an INSERT is applied only if no row with that exact tuple exists; a tuple-count ambiguity (two distinct events, identical tuple) routes to a review queue with an alarm rather than being silently dropped. Reordering within the stream cannot occur (WAL is total-ordered); reordering after consumer restart is absorbed by idempotency.

### 1.3 Write-ownership cutover point

**Choice:** Writes flip to the services only after **drain proof** of the oldest binary: zero connections from the oldest application version in `pg_stat_activity` for 15 continuous minutes, following completion of the rolling deploy. Until then the monolith remains the sole write authority and the services are a converging replica.

**Rejected:** Dual-write (new binaries write both systems simultaneously). Strongest candidate for "no drain dependency" — it removes the need to prove the oldest binary dead. Rejected because two write authorities on the same rows require bidirectional conflict resolution, and the one collision class that cannot be made value-idempotent — two distinct orders racing for the last unit of stock — becomes a live risk: each system's `FOR UPDATE` check sees only its own reserved state, so oversell (promise 2) is structurally possible during the overlap. Single-authority-throughout keeps the oversell invariant enforced by exactly one lock domain at every instant.

**Why:** Promise 2 (continuous no-oversell) outranks the operational convenience of not needing drain proof. Drain proof is achievable inside the window (45-minute deploy tail + connection drain), so the cost of single-authority is schedule slack, not correctness.

### 1.4 Order-number allocation authority

**Choice:** One allocation authority at every instant. Pre-flip: the monolith sequence `order_number_seq` (as today). At backfill, the OS-local sequence `order_number_seq` is initialized to the monolith sequence's next value (`pg_sequence_last_value('order_number_seq') + 1` read at the snapshot). Post-flip: only the OS sequence advances; the monolith sequence is frozen because no monolith writers remain.

**Rejected:** Parallel sequences (monolith keeps allocating for old binaries while OS allocates for new). Strongest if drain proof were impossible. Rejected because interleaved allocation from two counters breaks strict per-customer monotonicity — the property the EDI partners enforce by rejecting non-increasing files (`code-paths.md` §5) — for the entire mixed-binary period.

**Why:** Disjoint number ranges by construction (every post-flip allocation is greater than every monolith allocation ever made, since sequences never reuse values) preserve promise 4 continuously, including across rollback (re-injected orders keep their allocated numbers, which are greater than any monolith number).

Collision detection (defense in depth, required by the brief): (a) `UNIQUE (order_number)` on `orders` in both systems — a colliding re-injection INSERT fails loudly, never silently; (b) rollback verification asserts re-injected rowcount equals exported rowcount; (c) a pre-flip gate check asserts OS sequence next value > monolith sequence last value.

### 1.5 Availability-denormalization replacement

**Choice:** IS maintains `product_availability(product_id, available)` inside the IS database, recomputed in-transaction on every `inventory_levels` mutation — the exact computation of the monolith trigger `refresh_available()` (`COALESCE(SUM(on_hand - reserved), 0)` per product), moved next to the data. Storefront listing reads route to IS post-flip.

**Rejected:** Keep updating monolith `products.cached_available` via cross-DB write from IS. Strongest for read-path continuity (listings keep their current source). Rejected because the update would leave its transaction's database before commit — a crashed or rolled-back IS transaction would leave `cached_available` advanced, and the storefront would display stock that does not exist; the trigger exists today precisely because computation and data share one transaction.

**Why:** The denormalization's purpose is read latency (40k reads/s through the app cache). Its correctness property is transactional atomicity with the stock mutation. Only co-location preserves both. Pre-flip the monolith trigger continues to serve old writes exactly as today; post-flip all writes are in IS, so the monolith column freezes and listings read IS — no divergence window exists because the read switch and write switch land together at drain.

### 1.6 Audit-log delivery

**Choice:** Outbox relay. Every OS order mutation INSERTs its audit entry into a local `audit_outbox` table in the same transaction; a relay worker drains the outbox into monolith `audit_log` with an idempotency guard (`SELECT` existence on `(table_name, row_pk, action, at)` before `INSERT`; outbox row marked sent only after rowcount = 1). `at` is the event time of the mutation, making retries deterministic.

**Rejected:** Synchronous cross-DB audit write inside placement. Strongest for immediacy. Rejected because a failed audit write would either fail the order (availability) or commit the order without its audit entry (compliance) — the outbox makes audit delivery eventually consistent with exponential backoff (base 1 s, cap 60 s, unbounded retries) without coupling to the order transaction.

**Why:** `audit_log` stays in the monolith per the artifact's own comment; the outbox is the minimum mechanism that keeps audit completeness (promise 1's compliance face) without a two-commit atomicity problem.

### 1.7 Distributed placement protocol

**Choice:** Three-phase saga with the stock decision atomic in IS and compensating sweeps for crash gaps:

- **Phase A (IS, one transaction):** `SELECT … FOR UPDATE` on each `(product_id, warehouse_id)` row in sorted order (deadlock-free under concurrency), check `on_hand - reserved >= qty`, `reserved += qty`, INSERT `reservations` row with `order_id = NULL`. Commit. Returns a reservation token (the `reservations.id` values).
- **Phase B (OS, one transaction):** `INSERT orders` (allocating `order_number` from the OS sequence), INSERT `order_lines` (local FK to `orders` preserved), INSERT `audit_outbox` entry. Commit.
- **Phase C (IS, one transaction, delivered by the OS outbox relay with retry):** UPDATE `reservations` SET `order_id` by token; INSERT `inventory_movements` (`delta = -qty`, `reason = 'order'`, `order_id` known). Commit.

Crash between A and B: order absent; reserved incremented with a `reservations` row present (`order_id NULL`). IS-local **abandon sweep** — `reservations` rows with `order_id IS NULL` and `created_at < now() - interval '10 minutes'` — releases the hold (`reserved -= qty`, movements INSERT `reason = 'reconcile'`, reservations DELETE). Conservative: over-reserved during the grace period, never oversold. Crash between B and C: outbox retry delivers C until acked; no gap persists.

**Rejected:** Order-first with compensating order deletion on stock failure. Strongest for keeping Phase A's check and the order INSERT coupled. Rejected because a failed placement would leave an `orders` row committed and then deleted — the storefront's error handling depends on a failed placement leaving **no trace** (`code-paths.md` §1), and a deleted-then-referenced order number would still have advanced the sequence and appeared in audit.

**Why:** Stock decisions serialize on one lock domain (IS) exactly as today's single-transaction `FOR UPDATE` does; the order row exists only after stock is secured; every crash state is either conservative (over-reserved, self-healed) or repaired by retry.

### 1.8 Cancel/shipment delivery

**Choice:** OS transaction updates `orders.status` (conditional `WHERE status IN ('placed','paid')`, preserving today's `NotCancelable` semantics); an outbox command delivers `release(order_id)` to IS, where reserved decrement + movements INSERT (`reason = 'cancel'`) + reservations DELETE are atomic in one IS transaction. `release` is idempotent by construction (a second invocation matches zero reservations rows and changes nothing), so outbox retry is safe. Shipment confirmation routes through IS identically (`reserved -= qty`, `on_hand -= qty`, movements INSERT, reservations DELETE) with the OS status flip separate.

**Rejected:** IS-first release then OS status flip. Rejected because a crash between them leaves stock released for an order not yet canceled — an oversell window, the one direction of asymmetry the invariant forbids. OS-first makes every crash state over-reserved.

**Why:** The invariant is asymmetric: over-reservation self-heals (support cancels the order, or the sweep releases), oversell does not.

---

## 2. System architecture

**Processes.**

- **OS** (new service): owns order creation, order status transitions, order lines, status history, audit outbox + relay, EDI export job, order-number sequence. Reads: its own DB; synchronous point reads of monolith `products.unit_price_cents` during placement (one indexed read per line, preserving today's exact-price semantics).
- **IS** (new service): owns stock decisions — reservation check/increment, release, shipment, warehouse import, nightly reconcile, abandon sweep, `product_availability`.
- **Monolith** (existing ~40 binaries): unchanged until drain; post-drain its migrated tables are frozen replicas, then retired. `customers`, `products`, `warehouses`, `audit_log` remain monolith-resident permanently.
- **CDC consumers** (self-hosted, checkpointed): monolith→OS and monolith→IS during the window; IS→monolith standby consumer created at flip time, used only for rollback re-injection.

**Communication.** OS→IS and OS→monolith price reads: synchronous RPC/SQL, per-operation timeout 2 s, retries 0 in Phase A (a timed-out reservation is surfaced as placement failure and the order is not created — the outbox never retries Phase A, only Phase C). Outbox relays: poll interval 250 ms, exponential backoff on failure (base 1 s, cap 60 s), batch size 200 commands. Internal RPC authenticated by per-service certificates; command payloads HMAC-signed.

**Read routing.** Pre-flip: all reads monolith (as today). Shadow-verification period: new binaries additionally issue each orders/inventory read to the service replica and compare (monolith response is authoritative and returned). Post-flip: orders/inventory reads serve from the services; listings serve `product_availability` from IS. Monolith replica of migrated tables continues converging via CDC until oldest-binary drain, then freezes.

**State machines.** Order status: unchanged from today (`placed → paid → picking → shipped → delivered`; `canceled` from `placed|paid`), guarded by the same conditional UPDATEs. Reservation lifecycle: `order_id NULL` (pre-bind, ≤10 min) → bound → released (cancel/ship) or abandoned (sweep). Replica lifecycle: converging → frozen → retired.

---

## 3. Data model

### 3.1 Order Service database (new)

```sql
CREATE TABLE orders (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_number   BIGINT NOT NULL UNIQUE DEFAULT nextval('order_number_seq'),
    customer_id    BIGINT NOT NULL,            -- FK to monolith customers dropped (cross-DB); validated at the auth layer before placement
    status         TEXT NOT NULL DEFAULT 'placed' CHECK (status IN
                   ('placed','paid','picking','shipped','delivered','canceled')),
    total_cents    INTEGER NOT NULL CHECK (total_cents >= 0),
    placed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX orders_customer ON orders (customer_id, placed_at DESC);
CREATE SEQUENCE order_number_seq START 88000001;  -- re-seeded at backfill to monolith last_value + 1 (§1.4)

CREATE TABLE order_lines (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id     BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,  -- preserved: both tables in OS
    product_id   BIGINT NOT NULL,            -- FK to monolith products dropped; price read validates existence synchronously
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

-- Audit outbox: same-transaction audit capture, relayed to monolith audit_log (§1.6)
CREATE TABLE audit_outbox (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    table_name  TEXT NOT NULL,
    row_pk      TEXT NOT NULL,
    action      TEXT NOT NULL,
    diff        JSONB,
    at          TIMESTAMPTZ NOT NULL,          -- event time of the mutation (deterministic for retries)
    sent_at     TIMESTAMPTZ
);
CREATE INDEX audit_outbox_pending ON audit_outbox (id) WHERE sent_at IS NULL;

-- Command outbox: Phase-C binds, cancel/ship releases, delivered with retry (§1.7/§1.8)
CREATE TABLE command_outbox (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('bind','release','ship')),
    payload     JSONB NOT NULL,
    acked_at    TIMESTAMPTZ
);
CREATE INDEX command_outbox_pending ON command_outbox (id) WHERE acked_at IS NULL;
```

### 3.2 Inventory Service database (new)

```sql
CREATE TABLE inventory_levels (
    product_id   BIGINT NOT NULL,            -- FK to monolith products dropped; pair existence validated by reservations FK below
    warehouse_id BIGINT NOT NULL,
    on_hand      INTEGER NOT NULL CHECK (on_hand >= 0),
    reserved     INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (product_id, warehouse_id),
    CHECK (reserved <= on_hand)
);

CREATE TABLE reservations (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id     BIGINT,                     -- NULL until Phase C binds; FK to OS orders dropped (cross-DB)
    product_id   BIGINT NOT NULL,
    warehouse_id BIGINT NOT NULL,
    qty          INTEGER NOT NULL CHECK (qty > 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (product_id, warehouse_id)
        REFERENCES inventory_levels (product_id, warehouse_id)   -- preserved: both tables in IS
);
CREATE INDEX reservations_order ON reservations (order_id);
CREATE INDEX reservations_unbound_age ON reservations (created_at) WHERE order_id IS NULL;

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

-- Replaces the monolith trigger trg_refresh_available (§1.5): same computation, in-transaction
CREATE TABLE product_availability (
    product_id BIGINT PRIMARY KEY,
    available  INTEGER NOT NULL DEFAULT 0 CHECK (available >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

`product_availability` rows are recomputed inside every IS transaction that mutates `inventory_levels` (the trigger's exact SQL, executed by application code in-transaction rather than by a trigger — triggers are dropped from the new schema; the recomputation is named function `refresh_available_local(product_id)` called by every stock-mutating path).

### 3.3 Ownership boundary

| Monolith table | Fate | Where columns move |
|---|---|---|
| `orders` | dies (frozen replica → retired) | OS `orders` |
| `order_lines` | dies | OS `order_lines` (FK to `orders` preserved) |
| `order_status_history` | dies | OS (append-only; INSERT-only CDC) |
| `order_number_seq` | dies | OS sequence, re-seeded at backfill (§1.4) |
| `inventory_levels` | dies | IS (PK preserved) |
| `reservations` | dies | IS; `order_id` becomes nullable (split-forced: cross-DB FK dropped and saga ordering requires pre-bind NULL) |
| `inventory_movements` | dies | IS; append-only preserved; dedup key = full natural tuple (§1.1) |
| `products.cached_available` | column frozen post-flip | replaced by IS `product_availability` |
| `customers`, `products`, `warehouses`, `audit_log` | **stay** in monolith permanently | — |

Cross-domain FKs dropped: `orders.customer_id→customers`, `order_lines.product_id→products`, `order_lines.warehouse_id→warehouses`, `reservations.order_id→orders`, `inventory_levels.*→products/warehouses`. Each is replaced by the named enforcement in §4. Trigger-maintained denormalization replaced per §1.5.

### 3.4 Monolith-side modifications (ship via rolling deploy)

- New binaries' orders/inventory code paths target OS/IS (config-selected write/read target; hot-reloadable, no restart).
- `audit_log` trigger retained (serves old writes until drain).
- Post-retirement: `REVOKE INSERT, UPDATE, DELETE ON orders, order_lines, order_status_history, inventory_levels, reservations, inventory_movements FROM monolith_app_role;` — the fence that makes any post-retirement write from any surviving old binary fail loudly at the constraint layer, not silently into a dead table.

---

## 4. Invariant enforcement map

| Invariant | Mechanism | Evidence it works |
|---|---|---|
| Available quantity never below zero, continuously (promise 2) | Single lock domain: every stock decision serializes on `SELECT … FOR UPDATE` on `inventory_levels` rows inside one IS transaction; `CHECK (reserved <= on_hand)` and `CHECK (on_hand >= 0)` on `inventory_levels`; sorted row locking prevents deadlock; no dual-write at any instant (§1.3) | Test `T-oversell`: 200 concurrent placements for the last 50 units of one product across mixed binaries; assert exactly 50 succeed, `reserved` ends at `on_hand`, zero CHECK violations; repeat with consumer killed mid-run (FI-1) |
| Order + stock mutations atomic per placement (storefront no-trace semantics) | Saga ordering §1.7: order row committed only after Phase A secured stock; Phase C delivered by outbox retry; abandon sweep heals A/B crash gaps conservatively | Test `T-notrace`: kill OS between Phase A commit and Phase B commit on 1k placements; assert zero `orders` rows without matching secured-then-bound reservations after sweep runs; assert reserved returns to pre-test value |
| No lost writes (promise 1) | WAL-total-ordered CDC with checkpointed idempotent apply (dedup: natural keys; movements/history full-tuple); outbox retry with unbounded backoff for Phase C, releases, audit; `UNIQUE`/PK on every migrated table | Test `T-noloss`: 24 h mixed-binary soak at peak load; assert per-table rowcount+checksum parity between monolith and each replica at a quiesced comparison point (§5 walkthrough 7 defines the comparison); assert outbox depth returns to 0 |
| Order numbers unique + strictly increasing per customer (promise 4) | Single allocation authority at every instant; OS sequence seeded above monolith's last value at backfill; monolith sequence frozen post-drain; `UNIQUE (order_number)` both systems; collision detection per §1.4 | Test `T-monotonic`: per-customer `LAG(order_number) OVER (PARTITION BY customer_id ORDER BY placed_at)` over the full mixed period; assert zero non-positive deltas; assert EDI file acceptance simulation passes on every hourly boundary |
| Audit completeness | Same-transaction `audit_outbox` INSERT + relay with idempotency guard on `(table_name, row_pk, action, at)` | Test `T-audit`: after any OS mutation batch, assert `audit_log` rowcount for the batch equals outbox-completed count, with zero duplicate tuples |
| Replica convergence (mixed-period reads) | Diff-guarded keyed upsert apply; consumer checkpoint LSN; lag alarm at 30 s p99 | Test `T-converge`: inject 100k mixed mutations; assert replica lag p99 < 30 s within 5 min and zero persistent diff-guard rejections |
| EDI continuity | Export re-routed to OS at flip; rollback catch-up export against monolith before restoring OS-only export | Test `T-edi`: replay last 30 days of partner files through the partner's own dedup/monotonicity validator; assert zero rejections |

---

## 5. Failure-mode walkthroughs

**1. Scenario:** CDC consumer killed mid-apply-batch during mixed-period operation.
**What happens:**
1. Worker process dies; open apply transaction rolls back (PostgreSQL transaction abort on connection loss).
2. Supervisor restarts the worker; worker reads its checkpoint LSN from its state table and re-requests the stream from that LSN (`PQstartCopy` with `origin_lsn`).
3. Replayed events re-apply as idempotent upserts (natural-key dedup; movements full-tuple check) — already-applied rows are no-ops.
4. Lag alarm (30 s p99) fires during the gap; replica reads during the gap serve stale-but-consistent data; no write path is affected (writes remain monolith-authoritative pre-flip).
**Evidence:** consumer state table shows checkpoint LSN advanced monotonically with no gaps; replica checksum comparison at the next quiesced point matches; alarm timeline shows one 30 s breach.

**2. Scenario:** Backfill COPY crashes at a stated cursor (order_lines, row ~60% through) with 4,000 writes/s mutating the source.
**What happens:**
1. COPY aborts; the `REPEATABLE READ` transaction ends, releasing the snapshot.
2. Restart re-opens a fresh snapshot at a later LSN and re-COPYs the table from zero; the replication slot, created at the original snapshot LSN, already contains every mutation since then.
3. Re-COPYed rows that changed since the first attempt are overwritten by their CDC values (higher LSN wins, keyed upsert).
**Evidence:** post-restart checksum of the table equals a second independent snapshot taken after convergence; WAL shows no gap between original and restart snapshot LSNs in the slot's stream.

**3. Scenario:** IS database partitioned from IS application mid-cutover (post-flip, services own writes).
**What happens:**
1. Placement Phase A RPC times out (2 s) → placement fails cleanly; no order row created (no-trace semantics preserved).
2. Cancel/ship outbox commands queue; relay backs off (1 s→60 s cap) until IS returns.
3. Availability impact bounded: order-placement error rate rises during the partition; the 263 s monthly budget is checked against the partition duration before/after by the rollback gate — partitions longer than budget remainder trigger rollback to monolith reads/writes pre-emptively.
**Evidence:** placement error-rate metric vs partition window; outbox depth decays to 0 after restore; zero orders without bound reservations.

**4. Scenario:** Rolling deploy rolled back mid-phase (new binaries reverted to old code while old binaries still run).
**What happens:**
1. Reverted binaries resume monolith write path — legitimate pre-flip, since ownership has not moved (flip is gated on drain proof, which a rollback invalidates).
2. Any Phase-C commands already delivered remain valid (idempotent binds); any in-flight outbox commands target reservations that exist regardless of binary version.
3. Drain-proof timer resets; flip schedule recomputed.
**Evidence:** `pg_stat_activity` version histogram shows reverted population; zero service-originated orders with unbound reservations older than 10 min.

**5. Scenario:** Rollback after divergence — new binaries have placed orders the monolith never saw; decision made to roll back (e.g., IS regression).
**What happens:**
1. Write-target config flipped back fleet-wide (rolling, hot-reload); oldest-alive assumption re-asserted.
2. Standby IS→monolith consumer (created at flip-time LSN) replays every post-flip IS/OS mutation into the monolith tables as diff-guarded upserts; orders keep their OS-allocated `order_number` values.
3. Collision detection: `UNIQUE (order_number)` makes any colliding INSERT fail loudly; rollback verification asserts re-injected rowcount = exported rowcount and per-customer monotonicity (OS numbers > every monolith number by §1.4 seeding).
4. EDI catch-up: one-time export run against the monolith for `order_number > high_water` before restoring OS-only export, so partners see rolled-back orders exactly once (their own dedup on `order_number` absorbs the overlap).
5. Customer impact, stated honestly: orders placed during the divergence window succeed normally; a rollback within the window delays their EDI export by at most one hourly cycle; no customer-visible order state changes; stock holds placed via IS are re-created in the monolith before any read serves from it.
**Evidence:** monolith checksum parity at the next quiesced comparison; EDI validator passes on the catch-up file; zero duplicate `order_number` values in either system.

**6. Scenario:** Oldest binary survives the drain window (deploy tail longer than assumed) after flip was executed.
**What happens:**
1. Its writes land in monolith tables post-retirement → rejected by the revoked privileges (§3.4) — loud failure at the constraint layer, no silent loss; its placements fail and the customer sees an error (no retry path writes to a dead table).
2. Pre-emptive fence: drain proof requires 15 min of zero oldest-version connections; the retirement step additionally runs a canary — one deliberate oldest-version write attempt from a synthetic connection, asserted to fail — before `REVOKE` executes.
3. If the canary unexpectedly succeeds (a live oldest binary exists), retirement aborts and rolls back to frozen-replica state (writes re-permitted via `GRANT` restore from the recorded privilege set).
**Evidence:** canary result logged; `pg_stat_activity` oldest-version count = 0 at retirement; privilege audit diff matches the recorded set.

**7. Scenario:** Comparison during active load — when are two systems under 4,000 writes/sec allowed to differ?
**What happens:**
1. Comparisons run only at **quiesced comparison points**: trough window (02:00–05:00 UTC, ~900 writes/s) with a 5-minute write-quiescence prefix (outbox drained to 0, consumer lag < 5 s asserted before sampling).
2. Comparison semantics: `REPEATABLE READ` snapshot on both sides opened within a 2 s interval; compared by natural key; row-level diff of full tuples; toleration: zero — any diff is a defect, routed to the review queue with both tuple images and the WAL LSN of the last applied event.
3. In-flight skew during non-quiesced operation is bounded by consumer lag (30 s p99 alarm) and is never compared.
**Evidence:** comparison report per table: matched/missing-on-each-side/diff counts; every diff row carries an LSN trace; review queue drains to zero before the next phase gate.

**8. Scenario:** Warehouse import (COPY, up to 400k rows) lands while backfill/CDC is active.
**What happens:**
1. COPY generates WAL row changes; logical decoding emits them as individual INSERTs.
2. Consumer applies INSERTs with full-tuple dedup; microsecond-precision `occurred_at` makes tuple collisions between distinct import rows measure-zero; a collision routes to the review queue rather than dropping.
3. The import's `inventory_levels` upserts apply by PK-keyed upsert, LSN-ordered against concurrent backfill.
**Evidence:** post-import `inventory_levels` checksum parity; review queue entries (expected: none) each carry both candidate tuples.

**9. Scenario:** Audit relay dies permanently mid-window.
**What happens:**
1. `audit_outbox` grows at mutation rate (~2.2k/s peak for orders); table sized to 7 days at peak (≈1.5B rows worst case — alarm at 10% of that).
2. Relay restore drains at apply rate; idempotency guard makes the drain re-runnable.
3. No order write is ever blocked by audit delivery (§1.6).
**Evidence:** outbox high-water vs time; post-drain `audit_log` completeness check per batch.

**10. Scenario:** Monolith→services CDC slot is lost (slot dropped by mistake) during mixed period.
**What happens:**
1. Lag alarm fires immediately (no stream progress).
2. Slot recreated at the consumer's last checkpoint LSN via `pg_create_logical_replication_slot(..., start_lsn)`; WAL retention (7 days, assumption A4) covers the gap.
3. Replay converges via idempotent apply.
**Evidence:** slot LSN continuity across the checkpoint; checksum parity at next quiesced point.

---

## 6. AI strategy

This project ships no AI features; no models, prompts, validation, measurement, or cost apply.

---

## 7. Testing and release confidence

Named fault-injection tests (each run in staging against production-shaped load, then once in production at reduced scale before its phase gate):

- **FI-1** — SIGKILL the CDC consumer mid-transaction during a 10k-mutation burst. Assertion: checkpoint LSN resumes monotonically; replay produces zero duplicate rows (natural-key/full-tuple dedup); lag alarm fired exactly once; post-convergence checksum parity.
- **FI-2** — Crash the backfill at a stated cursor (order_lines, 60%) under live write load. Assertion: restart converges to second-snapshot checksum; no row exists whose final value predates its last mutation's LSN.
- **FI-3** — Partition IS from its database mid-cutover (stop the DB instance) for 10 minutes. Assertion: placements fail cleanly with zero orphan orders; outbox depth decays to 0 on restore; availability breach measured and inside the remaining monthly budget.
- **FI-4** — Roll a deploy back mid-phase (revert 50% of binaries to oldest code). Assertion: drain-proof timer resets; zero service-originated orders with unbound reservations older than 10 min; mixed-period invariants (T-oversell, T-monotonic) still pass.
- **FI-5** — Execute the full rollback-after-divergence procedure (§5.5) with 100k post-flip orders. Assertion: monolith parity at next quiesced point; EDI validator passes on catch-up file; zero duplicate `order_number`; per-customer monotonicity intact.
- **FI-6** — Drop the replication slot mid-stream. Assertion: recreation at checkpoint LSN; WAL retention covers gap; parity restored.

Release confidence gates (each phase in §8 closes on its evidence, never on dates): invariant tests T-oversell/T-notrace/T-monotonic/T-audit pass at production scale; FI-1..FI-6 pass; comparison reports (§5.7) show zero unresolved diffs; outbox depths at 0; consumer lag p99 < 30 s over a full peak-to-trough cycle.

---

## 8. Delivery phases

**Phase 0 — Foundations.** Build OS/IS schemas (§3), CDC consumers with checkpointing, outbox relays, comparison tooling, FI harness. *Exit criteria:* FI-1/FI-2 pass in staging; comparison tooling produces a zero-diff report on a static fixture dataset; consumer checkpoint/replay demonstrated.

**Phase 1 — Backfill and warm replica (first end-to-end run of the full data path).** Snapshots + slots per §1.2; consumers running; replicas converging. Justified before any cutover: every later step reads from these replicas, so the data path must exist and be proven convergent first. *Exit criteria:* all migrated tables at checksum parity at a quiesced comparison point; consumer lag p99 < 30 s over a peak-to-trough cycle; FI-2 passed in production at reduced scale.

**Phase 2 — Shadow read verification.** New binaries deployed with read-dual-compare (monolith authoritative). *Exit criteria:* ≥72 h of shadow comparison at full load with zero unresolved diffs (all routed diffs triaged); T-monotonic passes over the shadow period.

**Phase 3 — Drain and write/read flip.** Drain proof (§1.3); then per-binary hot-reload of write/read target to the services; OS sequence authority active; monolith→services CDC continues until frozen. *Exit criteria:* drain-proof record (15 min zero oldest-version connections); placement/cancel/ship/warehouse-import/reconcile all executing in IS/OS at full load; T-oversell and T-notrace pass post-flip; audit relay completeness verified; EDI export re-routed with validator pass.

**Phase 4 — Monolith freeze and retirement.** Monolith migrated tables frozen (CDC halted after final parity); privileges revoked (§3.4) behind the canary fence (§5.6); oldest-binary drain completed. *Exit criteria:* final quiesced parity report; canary-fence record; revoked-privilege audit diff; oldest-version connection count zero for 15 min.

**Phase 5 — Steady state.** Services are the system of record; monolith retains `customers`, `products`, `warehouses`, `audit_log`. *Exit criteria:* 7 days steady-state with all invariant tests green; review queues at zero; outbox depths at 0.

---

## 9. Security and privacy

- Tenant isolation across the new boundary: OS/IS databases are separate managed instances with separate credential scopes; application roles per service (`os_app`, `is_app`); CDC replication users carry replication privilege only and no direct table grants; outbox relays connect with least-privilege roles (OS relay: `INSERT` on monolith `audit_log` only; command relay: `UPDATE/INSERT/DELETE` on the named IS tables only).
- Internal RPC (OS→IS Phase A/C, OS→monolith price reads) over mTLS with per-service certificates; command payloads HMAC-signed; per-operation timeout 2 s.
- Data residency unchanged: all databases in-region, self-hosted tooling only (§1.1).
- Post-retirement fence: revoked DML privileges on migrated monolith tables (§3.4) make any surviving old-binary write fail at the constraint layer.
- Audit: `audit_log` completeness preserved (§1.6); comparison reports and rollback verifications are retained 1 year.

---

## 10. Risk register

| Risk | Likelihood | Impact | Mitigation (mechanism) |
|---|---|---|---|
| Oldest binary outlives drain assumption | medium | post-retirement write attempts | canary fence + revoked privileges (§5.6); loud failure, no silent loss |
| Consumer apply throughput below assumption A3 | medium | catch-up overruns night window | backfill serialized per table; trough-window catch-up; worst case still fits window with ≥20 days slack (§11 arithmetic) |
| Movements full-tuple dedup collision | low | one movement row held in review queue | review queue + alarm; nightly reconcile re-baselines `on_hand` from movements regardless |
| Price-read hop adds placement latency | high | p99 placement latency +~2 ms intra-AZ | measured in staging; if p99 placement latency exceeds today's +10%, fall back to synchronous read batching per transaction (one products read per distinct product) |
| Rollback after heavy divergence | low | EDI export delay ≤1 hourly cycle | catch-up export (§5.5); partner dedup absorbs overlap |
| WAL retention shorter than slot-loss gap | low | slot recreation gap unfillable | assumption A4 (7-day retention); alarm at retention/2 of unconsumed WAL age |

---

## 11. Explicit tradeoffs

1. **Mixed-period replica lag (≤30 s p99).** New-binary shadow reads during Phase 2 can observe up to 30 s of skew vs the monolith. Accepted because the monolith response is authoritative during shadow (no customer sees the skew), and post-flip the skew source (monolith writes) ceases to exist.
2. **Abandon-sweep grace period (10 min).** A crashed placement over-reserves stock for up to 10 min. Accepted because over-reservation is conservative (no oversell) and self-heals; the alternative (shorter grace) risks releasing holds for slow-but-legitimate Phase B/C deliveries under load.
3. **Price staleness: none; latency +~2 ms.** Chose synchronous monolith price reads over a products replica to preserve exact-price semantics; paid in placement latency, gated by the §10 latency check.
4. **Backfill worst case.** At consumer apply throttled to 2k rows/s (A3 floor), catch-up extends to ~10 h. Still inside the window: backfill+catch-up ≤ ~1.5 days, leaving ≥28 days of the 30-day window for verification and cutover. If apply throughput measures below 2k rows/s in staging, backfill tables are split into smaller snapshot chunks to shorten single-snapshot holds.
5. **Rollback EDI delay.** Rolled-back orders' partner export slips by at most one hourly cycle. Accepted: partners dedup on `order_number`; delay, not loss or duplication.
6. **`reservations.order_id` nullable.** Weakens the FK guarantee (a reservations row can transiently reference no order). Accepted: split-forced (§3.3), bounded by the 10-min sweep, and strictly conservative.

---

## 12. Where this is stronger than required

1. **Single lock domain at every instant** — the brief requires the no-oversell invariant to hold continuously; this plan makes dual-write structurally impossible rather than merely conflict-resolved, so the invariant's enforcement does not depend on conflict-resolution correctness during the mixed period.
2. **Drain-proof-gated flip with canary fence** — beyond "assume one old binary alive," retirement is preceded by an active probe (canary write attempt asserted to fail) and rolls back automatically if the probe succeeds.
3. **Full-tuple dedup with review-queue fail-closed** for the two keyless append-only tables — stricter than dropping or guessing on ambiguity.
4. **Rollback with EDI catch-up** — the brief requires rollback to exist; this specifies the partner-feed continuity step most plans omit.

---

## 13. Assumptions

- **A1** — Managed PostgreSQL instances sustain ~350 MB/s single-stream COPY reads (NVMe-backed); effective ~320 MB/s after WAL contention at peak load (peak WAL ≈ 8 MB/s from 4,000 writes/s × ~2 KB mean mutation footprint plus trigger fan-out). Measured in staging before Phase 1; backfill arithmetic below uses these values.
- **A2** — Table-size split within the brief's 1.1 TB orders domain: `orders` ≈ 200 GB, `order_lines` ≈ 900 GB; within ~300 GB inventory domain: `inventory_movements` ≈ 250 GB, `inventory_levels`+`reservations` ≈ 50 GB. Corrected from `pg_total_relation_size` at Phase 0; durations recompute linearly.
- **A3** — Consumer apply throughput ≥ 10k rows/s sustained (2k rows/s floor for worst case). Measured in staging.
- **A4** — Monolith WAL retention ≥ 7 days (slot-loss coverage).
- **A5** — Shipment confirmation (not excerpted in the artifacts) decrements `reserved` and `on_hand` and DELETEs reservations with a movements row; routed through IS atomically per §1.8. If the live path differs, the IS routing shape is unchanged; only the movements `reason` value follows the live path's existing value.
- **A6** — `customers`, `products`, `warehouses` remain monolith-resident permanently; `customer_id` validity enforced at the auth layer pre-placement.
- **A7** — Intra-AZ RPC latency ~1–2 ms; price-read hop costed in §10.
- **A8** — Backfill arithmetic (for the record): `orders` 200 GB ÷ 320 MB/s ≈ 10 min; `order_lines` 900 GB ÷ 320 MB/s ≈ 47 min; `inventory_levels`+`reservations` 50 GB ≈ 3 min; `inventory_movements` 250 GB ≈ 13 min; total COPY ≈ 73 min. Changes accumulated during backfill at peak ≈ 4,000/s × 73 min × ~2 KB ≈ 34 GB of WAL changes; catch-up at 10k rows/s ≈ ≤2 h at trough write rates. Total Phase-1 duration ≤ ~6 h, schedulable inside the 02:00–05:00 UTC trough over ≤2 nights, leaving ≥28 days of window slack. Worst case (A3 floor): catch-up ~10 h; still ≤1.5 days total.
