# Atlas Cutover — Plan Evaluation (Reviewer: GPT / Codex)

> Provenance: produced by Codex CLI, model `gpt-5.6-sol`, reasoning effort `high`, run blind
> (prompt forbade reading `analysis-fable.md` or any other review file). Report reproduced
> verbatim below; reconciliation lives in `analysis-fable.md`.


Review performed blind. I read only the required baseline artifacts and the four submitted plans; no analysis/review files were opened or searched. No repository files were modified.

Scoring: `Correctness = max(0, 100 − confirmed weights)`, with register-claimed failures doubled. `Total = 0.7 × Correctness + 0.3 × Judgment`.

## 1. `qwen-low`

### Findings ledger

| ID | Grade | Severity | Doubled? | Trace / refutation |
|---|---|---:|:---:|---|
| L-01 | CONFIRMED | CRITICAL −25 | Yes → −50 | The inactive fence destroys ordinary writes. §3.3 lines 160–168 says `fence_write()` raises only when enabled, then always executes `RETURN NULL`. For a `BEFORE ROW` trigger, returning null suppresses the row operation. Trace: 1. P0 installs the triggers with `enabled=false` (§8 P0). 2. Legacy `place_order` issues `UPDATE inventory_levels`. 3. The trigger does not raise and returns null. 4. PostgreSQL skips the update. The same happens to subsequent inserts; DELETE also receives `NEW=NULL`. This breaks the §4 “No lost writes, exactly once” claim before cutover begins. |
| L-02 | CONFIRMED | CRITICAL −25 | Yes → −50 | There is no forward-CDC drain-to-zero gate. §8 P3 C3 flips routing/fence immediately after process drain; it never waits for the last monolith commit to be applied. Trace: 1. Source and target both show `reserved=4`, `on_hand=5`. 2. A final monolith transaction reserves one unit and commits `reserved=5`; its CDC event is delayed. 3. C3 starts service writes against stale `reserved=4`. 4. The service reserves another unit and commits `reserved=5`. 5. The delayed absolute-value CDC update applies `reserved=5`. Two orders hold the final unit, but the row records one reservation. This breaks both register claims “No lost writes” and “Available quantity never <0.” |
| L-03 | CONFIRMED | CRITICAL −25 | Yes → −50 | Rollback writes cannot land through the proposed fence. Walkthrough 8 lines 270–277 keeps the fence ON while `rollback_pub` applies service deltas into monolith tables. The fence has no apply-role bypass. Trace: 1. A service order commits. 2. Rollback consumer inserts it into monolith `orders`. 3. `trg_fence_orders` observes enabled=true and raises `P0001`. 4. Apply transaction aborts; lag never reaches zero. Disabling the fence does not help because L-01 then suppresses the write. The §4 rollback/no-loss mechanism is false. |
| L-04 | CONFIRMED | CRITICAL −25 | Yes → −50 | The single sequence does not make EDI high-water export safe. §1.2 allocates the number in a separate transaction before reserve/order commit, while §4 claims per-customer monotonicity “by construction.” Trace: 1. Request A obtains number 100 and stalls. 2. Same-customer request B obtains 101 and commits. 3. The hourly feed exports 101 and advances `last_exported=101`. 4. A commits order 100. 5. Every later `WHERE order_number > 101` omits A permanently. One allocator prevents duplicate allocation; it does not order allocation by commit. |
| L-05 | CONFIRMED | CRITICAL −25 | Yes → −50 | The `inventory_movements` dedup key destroys legitimate source rows. §3.2 lines 113–123 declares `UNIQUE(product_id,warehouse_id,reason,order_id,occurred_at,delta)`; §11 line 373 admits identical movements are collapsed. `now()` is transaction-stable, and `warehouse_import` COPY can contain duplicate CSV lines with null `order_id`. Trace: 1. One import transaction supplies two identical rows. 2. Both receive the same `occurred_at`. 3. The source retains both because it has no uniqueness constraint. 4. Target backfill/CDC retains one or fails on the constraint. 5. `nightly_reconcile` sums the incomplete movement log and rebuilds the wrong `on_hand`; it cannot “correct” the missing delta. This directly breaks the §4 exactly-once claim. |
| L-06 | CONFIRMED | CRITICAL −25 | Yes → −50 | Reserve-first compensation mishandles an ambiguous order commit. §1.3 says “on any failure” call release. Trace: 1. Inventory reserve commits. 2. Order DB commits the order. 3. The connection drops before Order Service receives commit confirmation. 4. The failure handler releases the reservation. 5. The committed order remains, and the released unit can be sold again. The sweep’s later absence check does not protect the immediate release. This breaks the register’s no-oversell and placement-atomicity claims. |
| L-07 | CONFIRMED | HIGH −12 | No | Availability compliance is omitted from the claims register and contradicted by walkthrough 8. Lines 272–276 allow a ≤45-minute rolling freeze plus ≤5-minute drain while the fence is ON, estimating 270,000 trough attempts. That exceeds the entire 263-second monthly budget. The walkthrough also says new binaries can succeed through monolith paths during this window, but the enabled fence rejects those writes. This is a concealment finding for the hard availability requirement. |
| L-08 | CONFIRMED | LOW −2 | No | §8 line 326 says two 71-minute passes consume “2.4 h” and leave 66 minutes in a three-hour trough. Two passes are about 142 minutes = 2.37 hours, leaving about 38 minutes; even rounded to 2.4 hours, slack is 36 minutes, not 66. |
| L-P01 | PLAUSIBLE | — | No | The assumed 480 MB/s aggregate source COPY rate may be overly optimistic under production random I/O and checkpoint pressure. It is explicitly gated on staging measurement, so the stated assumption alone does not confirm a defect. |
| L-R01 | REJECTED | — | No | The plan correctly identifies that PK-less append-only tables still emit INSERTs and that `REPLICA IDENTITY FULL` is required only if UPDATE/DELETE must be usable. That facility claim holds. |

Confirmed weight: `50+50+50+50+50+50+12+2 = 314`.

**Correctness: 0/100**

**Judgment: 35/100.** The plan is organized and engages with most legacy traps, but central mechanisms contradict their own SQL: the fence blocks or suppresses all traffic, rollback cannot traverse it, and the availability discussion openly exceeds the budget. Tests repeatedly assert outcomes that the supplied artifacts cannot produce.

**Total: `0.7×0 + 0.3×35 = 10.5`**

---

## 2. `qwen-balanced`

### Findings ledger

| ID | Grade | Severity | Doubled? | Trace / refutation |
|---|---|---:|:---:|---|
| B-01 | CONFIRMED | CRITICAL −25 | Yes → −50 | The CDC API does not exist as named. §1.1 lines 11–15 calls `pg_create_logical_replication_slot(slot,'pgoutput',false,start_lsn)` and libpq `PQreplicationOpen`/`PQstartCopy`. PostgreSQL 15’s function signature is `(slot_name, plugin [, temporary, twophase])`; it cannot create a slot at a historical LSN. Libpq exposes replication through a replication connection plus `START_REPLICATION`/COPY-both, not those functions. [PostgreSQL 15 slot signature](https://www.postgresql.org/docs/15/functions-admin.html), [replication protocol](https://www.postgresql.org/docs/15/protocol-replication.html). Thus the §4 no-loss mechanism cannot start as specified. |
| B-02 | CONFIRMED | CRITICAL −25 | Yes → −50 | Phase 3 creates two stock lock domains while claiming “no dual-write at any instant.” §8 line 371 says targets change by “per-binary hot-reload”; no DB fence exists until Phase 4 retirement. Trace: 1. Both DBs show one available unit. 2. Binary A has reloaded and reserves it in IS. 3. Binary B has not reloaded and independently reserves it in monolith. 4. Both orders commit. 5. Forward value replication cannot represent the sum of the increments. This breaks the register’s doubled no-oversell claim. |
| B-03 | CONFIRMED | CRITICAL −25 | Yes → −50 | Even assuming an instantaneous routing flip, there is no gate that drains forward CDC to zero before service writes start. Phase 3 checks neither publisher current LSN nor consumer applied LSN. The same worst-timed interleave as L-02 permits a final monolith absolute-value update to overwrite a later service update, breaking no-loss and no-oversell. |
| B-04 | CONFIRMED | CRITICAL −25 | Yes → −50 | The OS sequence is seeded at backfill, not at flip. §1.4 lines 53–61 initializes it from the monolith’s snapshot value while Phases 1–2 continue monolith allocations for days. Trace: 1. OS is seeded to 101. 2. Monolith allocates 101–500 during shadowing. 3. At flip, OS next value is still 101. 4. Its first insert collides or reuses an already-exported range. The “pre-flip gate asserts next > monolith last” detects failure but no mechanism advances the sequence. The register’s monotonicity mechanism is false. |
| B-05 | CONFIRMED | CRITICAL −25 | Yes → −50 | Rollback claims more reverse coverage than architecture defines. §2 line 110 creates only an “IS→monolith standby consumer”; walkthrough 5 line 299 suddenly asserts OS and IS mutations are replayed. No OS reverse publication or scope exists. Trace: 1. OS commits an order after flip. 2. Rollback starts. 3. The only defined reverse consumer has no OS source. 4. The order never reaches monolith. The §4 no-loss and EDI-continuity claims fail. |
| B-06 | CONFIRMED | CRITICAL −25 | Yes → −50 | The abandon sweep can release stock backing a committed order. §1.7 permits Phase-C delivery with unbounded retry but releases every unbound reservation after ten minutes solely from local `order_id IS NULL`. Trace: 1. Phase A reserves. 2. Phase B commits the order and command outbox. 3. IS or its relay is unavailable for >10 minutes. 4. The IS-local sweep releases/deletes the reservation. 5. The delayed bind later finds no reservation, while the order remains committed and the unit may be sold. This breaks the doubled no-oversell/no-trace claims. |
| B-07 | CONFIRMED | CRITICAL −25 | Yes → −50 | Full-tuple dedup cannot distinguish replay from legitimate duplication. §1.2 line 43 and walkthrough 8 rely on `(product_id,warehouse_id,delta,reason,order_id,occurred_at)`. Duplicate warehouse-import rows in one transaction legitimately share the full tuple because `now()` is transaction-stable. A “review queue” cannot infer whether the second identical event is a replay or a second source row, so the §4 exactly-once mechanism necessarily either drops valid data or duplicates a replay. |
| B-08 | CONFIRMED | HIGH −12 | No | Availability is absent from the claims register. FI-3 deliberately partitions IS for ten minutes and expects the result to be “inside the remaining monthly budget” (§7 line 354), but post-flip placement fails throughout; 600 seconds exceeds the entire 263-second budget before any other incident. |
| B-09 | CONFIRMED | MEDIUM −5 | No | “Quiesced comparison” is not mechanically established. §5.7 says comparisons occur during a trough still carrying ~900 writes/s; opening two independent snapshots within two seconds with zero tolerated difference cannot compare one logical instant. “Outbox drained” and lag <5 seconds do not stop a source commit between the snapshots, so a correct replica can fail the stated gate. |
| B-R01 | REJECTED | — | No | Inside one IS reserve transaction, sorted `FOR UPDATE` locking plus the unchanged `reserved <= on_hand` constraint correctly prevents two IS transactions from overselling. The confirmed break arises from cutover and sweep paths, not that local transaction. |
| B-R02 | REJECTED | — | No | The plan correctly states that logical decoding emits INSERTs for the two PK-less append-only tables under the legacy workload. |

Confirmed weight: `50+50+50+50+50+50+50+12+5 = 367`.

**Correctness: 0/100**

**Judgment: 42/100.** The document is readable, structurally complete, and makes useful distinctions between local locking and cross-system behavior. Operational realism is substantially weakened by invented PostgreSQL/libpq APIs, a non-atomic per-binary write flip, an unusable sequence handoff, and rollback coverage that appears only in walkthrough prose.

**Total: `0.7×0 + 0.3×42 = 12.6`**

---

## 3. `qwen-max`

### Findings ledger

| ID | Grade | Severity | Doubled? | Trace / refutation |
|---|---|---:|:---:|---|
| M-01 | CONFIRMED | CRITICAL −25 | Yes → −50 | The write fence does not parse. §3.4 lines 214–220 opens `IF ... THEN` but never supplies `END IF;`. Therefore `CREATE FUNCTION write_fence()` fails and no trigger can reference it. A zombie old binary can continue committing after the ownership flip, breaking the register’s exactly-once claim. |
| M-02 | CONFIRMED | CRITICAL −25 | Yes → −50 | Forward inventory publication omits `reservations`. §3.4 line 211 defines `pub_is` as only `inventory_levels, inventory_movements`, despite Phase 1 claiming equality for all six tables. Trace: 1. A live legacy reservation exists and contributes to `reserved`. 2. Its row is never copied to IS. 3. Post-cutover cancel/shipment cannot locate it. 4. The stock hold and movement lifecycle diverge permanently. This breaks the no-loss and atomicity register rows. |
| M-03 | CONFIRMED | CRITICAL −25 | Yes → −50 | Target DDL cannot allocate service-originated IDs or order numbers. §3 declares `orders.id`, `order_lines.id`, and `reservations.id` as plain `BIGINT PRIMARY KEY`, with no identity/default; it defines no OS `order_number_seq`. Line 124 claims local identity sequences are seeded, but those sequences/defaults do not exist. §4.1 step 1 then performs an insert without naming an `id` while expecting `RETURNING id,order_number`. Post-cutover placement fails immediately, invalidating multiple register claims. |
| M-04 | CONFIRMED | CRITICAL −25 | Yes → −50 | REV is started against already-populated monolith tables without `copy_data=false`. PostgreSQL `CREATE SUBSCRIPTION` defaults to copying existing rows. Trace: 1. Monolith already contains every backfilled order. 2. Phase 3 starts REV (§8 line 311). 3. Initial sync INSERTs the same PK rows. 4. Duplicate-key errors stop the subscription; PK-less tables would instead duplicate. The standing rollback/no-loss mechanism never becomes ready. [PostgreSQL 15 `CREATE SUBSCRIPTION`](https://www.postgresql.org/docs/15/sql-createsubscription.html). |
| M-05 | CONFIRMED | CRITICAL −25 | Yes → −50 | The plan’s own initial-copy triggers collapse legitimate append rows. §3.1/§3.2 adds full-tuple `BEFORE INSERT` dedup triggers. PostgreSQL initial table synchronization behaves like COPY and fires row triggers. Two identical warehouse-import rows with transaction-stable `occurred_at` cause the second trigger invocation to return null, so source count 2 becomes target count 1. This breaks the doubled exactly-once claim. [PostgreSQL 15 initial-sync trigger behavior](https://www.postgresql.org/docs/15/logical-replication-architecture.html). |
| M-06 | CONFIRMED | HIGH −12 | Yes → −24 | Streaming apply does not fire the ordinary subscriber triggers on which three mechanisms depend. PostgreSQL apply runs with `session_replication_role=replica`; ordinary triggers require explicit `ENABLE REPLICA` or `ENABLE ALWAYS`. The plan supplies neither. Consequently streaming FWD updates do not maintain `availability_available`, REV does not re-fire `trg_audit_orders`, and streaming dedup triggers do not run as claimed in §4. [PostgreSQL 15 apply semantics](https://www.postgresql.org/docs/15/logical-replication-architecture.html). |
| M-07 | CONFIRMED | CRITICAL −25 | Yes → −50 | Continuous native FWD+REV on the same tables has no loop filter in PostgreSQL 15. §2 lines 58–69 republishes subscriber tables in both directions, but PG15 `CREATE SUBSCRIPTION` has no `origin=none` option; replication origins must be filtered by a custom output mechanism. Trace: 1. Monolith change applies to service. 2. Service publication emits the applied change. 3. REV applies it to monolith. 4. FWD sees it again, producing conflicts/cycles. The §4 exactly-once and standing-rollback claims fail. [PG15 replication origins](https://www.postgresql.org/docs/15/replication-origins.html). |
| M-08 | CONFIRMED | HIGH −12 | No | The 30-day schedule cannot satisfy its own gates. Phase 4 occupies D20–26 for its seven-day soak. Phase 5 begins D27–28 but requires checksum equality “sustained 7 days” (§8 line 313); that gate cannot finish by D30. The claimed two-day contingency reserve does not exist. |
| M-09 | CONFIRMED | LOW −2 | No | Inventory duration arithmetic is wrong: `300,000 MB / 45 MB/s = 6,667 s = 1.85 h`, not 1.2 h (§8 line 315). Parallel wall time remains dominated by the 6.8-hour order copy, but the individual line does not recompute. |
| M-P01 | PLAUSIBLE | — | No | The comparison envelope attributes mismatches using commit timestamps without naming how tuple-level changes are associated with those commits. This is under-specified, but a harness could retain protocol metadata, so no correctness weight is assigned. |
| M-R01 | REJECTED | — | No | Native initial synchronization correctly handles a row updated during its copy and a PK-backed reservation deleted behind the cursor: snapshot value first, then transactional WAL tail. |
| M-R02 | REJECTED | — | No | The post-cutover order-first saga does not itself permit `reserved > on_hand`; the IS transaction’s lock/check still rejects contention. Its confirmed weakness is failed-placement trace semantics, which §11 discloses. |

Confirmed weight: `50+50+50+50+50+24+50+12+2 = 338`.

**Correctness: 0/100**

**Judgment: 46/100.** This is the clearest plan and it explicitly discusses sequence non-replication, replica identities, legacy trigger fan-out, comparison semantics, and availability arithmetic. However, its core architecture depends on invalid DDL, missing target sequences, omitted tables, default initial copies into populated targets, default-disabled subscriber triggers, and unfiltered PG15 bidirectional replication.

**Total: `0.7×0 + 0.3×46 = 13.8`**

---

## 4. `opus-4-6`

### Findings ledger

| ID | Grade | Severity | Doubled? | Trace / refutation |
|---|---|---:|:---:|---|
| V-01 | CONFIRMED | CRITICAL −25 | Yes → −50 | The fence silently disables every legacy DELETE while inactive. §1.2 lines 58–65 always returns `NEW`; for a `BEFORE DELETE` trigger, `NEW` is null, which suppresses deletion. Trace: 1. Phase 0 installs the trigger on `reservations` with owner=`monolith`. 2. `cancel_order` decrements `reserved` and inserts a cancel movement. 3. `DELETE FROM reservations` fires the inactive fence and returns null. 4. The reservation survives although the transaction commits. This immediately violates the register’s no-loss/exactly-once claim. |
| V-02 | CONFIRMED | CRITICAL −25 | Yes → −50 | The batch sequence endpoint allocates overlapping ranges under concurrency and also creates EDI holes. §1.4 lines 101–109 performs `nextval`, then a separate `setval`. Trace: 1. A gets 101. 2. B gets 102 before A’s `setval`. 3. A sets sequence to 150 and returns 101–150. 4. B sets it to 151 and returns 102–151. The batches overlap on 49 values. Independently, an order using 151 may commit/export before one using 101, after which the lower order is permanently below the EDI high-water. This breaks the doubled monotonicity/uniqueness claim. |
| V-03 | CONFIRMED | CRITICAL −25 | Yes → −50 | Reverse CDC omits required migrated tables. Phase 3 line 470 publishes only IS `inventory_levels` and OS `orders,order_lines`; it omits `inventory_movements`, `reservations`, and `order_status_history`. Trace: 1. Service placement commits reservation and movement rows. 2. Rollback copies the level value and order rows but not those source-of-truth rows. 3. Monolith `nightly_reconcile` later rebuilds `on_hand` from its stale movement log. 4. Inventory diverges and the surviving system lacks committed mutations. The §4 rollback/no-loss claim is false. |
| V-04 | CONFIRMED | CRITICAL −25 | Yes → −50 | Saga timeout compensation can release a committed order’s stock. §5 scenario 6 treats every Order Service timeout as failure and immediately cancels reservations. Trace: 1. `reserve_batch` commits. 2. Order Service commits `create_order`. 3. Its response is lost and the caller times out. 4. V2 calls `cancel_reservations`. 5. The order remains committed without stock, and the unit can be sold again. This breaks the register’s doubled no-oversell claim. |
| V-05 | CONFIRMED | HIGH −12 | No | The reverse availability mechanism is definitively false. §2 line 144 and §13 assumption 5 say logical replication fires `trg_refresh_available` on the monolith subscriber. PostgreSQL apply uses `session_replication_role=replica`, so this ordinary trigger does not fire unless explicitly enabled as REPLICA/ALWAYS; the plan never does so. `products.cached_available` freezes after cutover. [PostgreSQL 15 apply semantics](https://www.postgresql.org/docs/15/logical-replication-architecture.html). |
| V-06 | CONFIRMED | CRITICAL −25 | Yes → −50 | Rollback cannot enter its first state. §5 scenario 7 step 1 executes `UPDATE migration_control SET owner='draining'`, but §1.2 DDL constrains owner to `('monolith','service')`. PostgreSQL rejects the UPDATE. The claimed pause never activates, V2 behavior for `draining` is unreachable, and the no-loss rollback sequence cannot proceed as written. |
| V-07 | CONFIRMED | MEDIUM −5 | No | The tablesync recovery API is wrong. Walkthrough 2 says `ALTER SUBSCRIPTION ... REFRESH PUBLICATION` restarts synchronization of the failed existing table. PG15 documents that REFRESH starts synchronization for newly added tables; previously subscribed tables are not copied again. The plan needs a separate resynchronization/recreation procedure. [PostgreSQL 15 `ALTER SUBSCRIPTION`](https://www.postgresql.org/docs/15/sql-altersubscription.html). |
| V-08 | CONFIRMED | LOW −2 | No | Walkthrough 7 states `2,200/sec × 7,200 s = 15,840 orders`; the result is 15,840,000, a factor-of-1,000 error. Standing REV may keep the live backlog small, but the stated rollback-scale evidence and row-count example are wrong. |
| V-P01 | PLAUSIBLE | — | No | The plan never verifies or enables `wal_level=logical`, whose change requires restart. The current cluster setting is absent from the brief, so this is a real omitted operational dependency but not a confirmed outage. [PostgreSQL 15 logical-replication configuration](https://www.postgresql.org/docs/15/logical-replication-config.html). |
| V-R01 | REJECTED | — | No | Phase 3’s conceptual forward handoff ordering is the strongest of the four plans: fence source writes, wait for in-flight transactions, drain forward CDC to zero, disable forward replication, establish reverse capture, then enable service writes. The confirmed defects are in its fence implementation and reverse scope, not this ordering. |
| V-R02 | REJECTED | — | No | Native logical subscription apply is transactional and maintains apply progress; ordinary crash recovery does not require full-tuple dedup for append-only rows. Legitimately identical source rows can therefore survive forward replication when no dedup constraint is added. |
| V-R03 | REJECTED | — | No | Because forward subscriptions are disabled before reverse subscriptions are created with `copy_data=false`, this plan does not create the continuous FWD/REV loop found in `qwen-max`. |

Confirmed weight: `50+50+50+50+12+50+5+2 = 269`.

**Correctness: 0/100**

**Judgment: 54/100.** This plan has the best source-fence/drain/CDC/service-start ordering and provides the most concrete legacy-path walkthroughs. Its score remains limited by a destructive DELETE-trigger bug, a fundamentally unsafe batched sequence allocator, incomplete reverse replication, an impossible rollback state, and incorrect subscriber-trigger semantics.

**Total: `0.7×0 + 0.3×54 = 16.2`**

---

## Final ranking

| Rank | Plan | Correctness | Judgment | Total |
|---:|---|---:|---:|---:|
| 1 | `opus-4-6` | 0 | 54 | **16.2** |
| 2 | `qwen-max` | 0 | 46 | **13.8** |
| 3 | `qwen-balanced` | 0 | 42 | **12.6** |
| 4 | `qwen-low` | 0 | 35 | **10.5** |

All four reach the correctness floor independently; the ranking is therefore determined by the capped judgment layer.

## Cross-cutting observations

- Every plan breaks at least two claims-register promises, so the protocol’s doubling rule drives all correctness scores to zero.
- A sequence orders allocations, not transaction commits. None of the plans supplies an EDI-safe commit/publication barrier; `opus-4-6` additionally makes overlapping allocations possible through concurrent `nextval`/`setval` batching.
- Full-row tuples are not valid identities for the two PK-less append logs. Transaction-stable timestamps and duplicate import lines make legitimate rows identical.
- Native subscriber apply does not fire ordinary triggers. Plans relying on availability, audit, fences, or dedup triggers under apply must name `ENABLE REPLICA`/`ENABLE ALWAYS` and then analyze the consequences.
- Reverse replication must include `inventory_movements` and `reservations`; otherwise rollback may look row-equivalent briefly but `nightly_reconcile` later reconstructs inventory from an incomplete source of truth.
- Only `opus-4-6` specifies the required forward-CDC drain-to-zero ordering. Its implementation defects prevent that otherwise sound handoff from succeeding.

## Consolidated REJECTED attacks

- PK-less tables do emit INSERTs under logical decoding; missing replica identity matters for usable UPDATE/DELETE old tuples, not INSERT capture.
- A single IS transaction using sorted `SELECT … FOR UPDATE`, availability check, update, and `reserved <= on_hand` correctly prevents local concurrent oversell.
- Native initial snapshot plus WAL tail correctly converges PK-backed updates during copy and deletes behind the cursor.
- `qwen-max`’s order-first saga does not itself let the inventory row cross its constraint; its weakness is the disclosed canceled/orphan trace and surrounding replication machinery.
- Native subscription crash recovery is transactional; `opus-4-6` does not need content-based dedup merely to survive an ordinary apply-worker restart.
- `opus-4-6`’s fence → in-flight drain → forward lag zero → reverse setup → service-start ordering withstands the forward-clobber attack at the design level.
