# Atlas Cutover — Plan Evaluation (Reviewer: Fable)

Hostile review of the four plans in `atlas-cutover/` (`qwen-low`, `qwen-balanced`,
`qwen-max`, `opus-4-6`) against `product.md`, `legacy/schema.sql`,
`legacy/code-paths.md`, and `engineering-plan-standard.md`, scored per the
adapted rules of `review-protocol.md`.

**Protocol adaptation (stated up front).** `review-protocol.md` governs working
software; these are plans, so nothing can be built and run. I applied the
protocol's scoring machinery to what *is* mechanically checkable in a plan, per
`product.md` §5 ("70% of each category is graded on mechanically checkable
claims"): DDL that parses and does what it says, PostgreSQL facilities that
exist as named (R10), arithmetic that recomputes, and walkthroughs whose steps
survive an adversarial interleave against the provided artifacts. The plan's §4
invariant-enforcement map is treated as the **claims register**: a named
mechanism shown false by trace is a confirmed finding at double weight; a
finding is CONFIRMED only when I can cite the exact plan text and give a
step-numbered trace or a definitive facility-level refutation; otherwise it is
recorded as PLAUSIBLE and moves no score. Correctness = max(0, 100 − Σ
confirmed weights), 70% of total; judgment layer ≤30%.

Severity weights per protocol §4: CRITICAL −25, HIGH −12, MEDIUM −5, LOW −2;
×2 when the broken mechanism is claimed in the plan's invariant map.

---

## FINAL RECONCILED RANKING (after two-reviewer reconciliation — see §Reconciliation at end)

| Rank | Plan | Reconciled correctness | Σ confirmed weights | Avg judgment | Total (0.7·C + 0.3·J) |
|---:|---|---:|---:|---:|---:|
| 1 | **qwen-balanced** | 9 | 91 | 58.0 | **23.7** |
| 2 | **opus-4-6** | 0 | 167 | 63.5 | **19.1** |
| 3 | **qwen-max** | 0 | 128 | 59.0 | **17.7** |
| 4 | **qwen-low** | 4 | 96 | 48.5 | **17.4** |

The section below is my original independent (pre-reconciliation) review, preserved
unmodified per the protocol's log-everything rule. The second reviewer's blind
report is `analysis-gpt.md`. Reconciliation — which findings were accepted,
re-graded, or refuted, and why the ranking moved — is the final section of this
file.

## Initial verdict and ranking (independent pass, pre-reconciliation)

| Rank | Plan | Correctness (of 100) | Judgment (of 100) | Total (0.7·C + 0.3·J) |
|---:|---|---:|---:|---:|
| 1 | **qwen-balanced** | 50 | 74 | **57.2** |
| 2 | **qwen-max** | 40 | 72 | **49.6** |
| 3 | **qwen-low** | 36 | 62 | **43.8** |
| 4 | **opus-4-6** | 24 | 73 | **38.7** |

No plan survives this brief unwounded. The spread is driven almost entirely by
confirmed mechanical findings; on judgment quality alone the four are within a
few points of each other, and opus-4-6 — last on total — is arguably the most
operationally literate document of the four. That is exactly the failure mode
the protocol exists to catch: fluency is not correctness.

---

## Cross-cutting findings (affect multiple plans)

**X1 — The subscriber-trigger trap.** PostgreSQL logical-replication apply
workers run with `session_replication_role = replica`, so ordinary (`ENABLE
TRIGGER`, the default) triggers on subscriber tables **do not fire** during
apply; only `ENABLE REPLICA` / `ENABLE ALWAYS` triggers do. Neither opus-4-6 nor
qwen-max names `ALTER TABLE … ENABLE ALWAYS TRIGGER`, yet both build
load-bearing machinery on subscriber-side trigger re-fire (opus-4-6:
`cached_available`; qwen-max: `cached_available` *and* the compliance
`audit_log` feed). qwen-low escapes because its appliers are ordinary Kafka
consumers (normal sessions — triggers fire); qwen-balanced escapes by not using
subscriber triggers at all.

**X2 — No forward-CDC drain gate at the write flip.** At the moment new-path
writes begin, in-flight monolith-origin CDC events (lag up to seconds, or a
backlog) can still land in the service database and overwrite service-side
writes to the same rows — `inventory_levels` counters are exactly the rows both
sides touch. The clobbered write is a silently lost committed mutation
(promise 1) and can under-count `reserved` (promise-2 exposure). **Only
opus-4-6 closes this**, with an explicit ordered flip (fence → wait for
in-flight tx → wait CDC lag = 0 → `ALTER SUBSCRIPTION … DISABLE` → route).
qwen-low, qwen-balanced, and qwen-max all flip without a named
lag-drained-to-zero gate; each takes a confirmed finding for it.

**X3 — `wal_level = logical` restart unbudgeted.** Logical decoding requires
`wal_level = logical`; if the monolith runs the default `replica`, enabling it
is a cluster restart — real minutes against a 263 s/month budget. Only
qwen-max names and budgets this restart (90 s, in its availability arithmetic).
qwen-low, qwen-balanced, and opus-4-6 create publications/slots without ever
establishing the precondition: LOW each.

**X4 — REPLICA IDENTITY accuracy (a discriminator, mostly judgment).** The
truth for the two PK-less tables (`inventory_movements`,
`order_status_history`): INSERTs always decode; an UPDATE/DELETE on a published
table without replica identity **fails on the publisher with an error** — it is
neither "silently dropped" (qwen-low's claim, wrong) nor "emitted without a
key" (qwen-balanced's framing, wrong). opus-4-6's statement ("FULL is
precautionary — INSERTs emit full row regardless") is the most accurate of the
four. All four correctly set FULL as hardening; no live path updates/deletes
these tables, so this is graded LOW/judgment, not correctness — but it
measures who actually knows the facility.

---

## qwen-low — findings ledger

Architecture: Debezium + Kafka forward/reverse CDC, monolith allocation
endpoint for order numbers all window, reserve-first saga, fence triggers,
reverse availability replication into a shadow table.

| ID | Grade | Sev | ×2 | Finding |
|---|---|---|---|---|
| L1 | CONFIRMED | HIGH | ✓ (−24) | **No forward-CDC drain gate at P3 (X2).** §8 C3 flips `migration_routing` and the fence in one step; the consumer ("forward CDC never stops during migration", walkthrough 9) keeps applying. Trace: (1) C3 at T0: fence on, routing → services; (2) T0+0.5 s new binary reserves stock on IS row R; (3) T0+1 s consumer applies a pre-fence monolith UPDATE to R (its LSN > per-key watermark — service writes never advance the monolith-LSN watermark) and overwrites `reserved`. Lost committed write; under-counted reservation. Claims-register row 2 asserts this CDC path is exactly-once → doubled. |
| L2 | CONFIRMED | HIGH (−12) | | **The fence trigger suppresses all writes when the fence is *off*.** §3.3 `fence_write()`: `IF enabled THEN RAISE … END IF; RETURN NULL;`. A BEFORE ROW trigger returning NULL cancels the row operation. These triggers deploy in P0 (fence disabled) on all six live monolith tables → every INSERT/UPDATE/DELETE on the hot path is silently skipped from deployment. (`place_order`'s `INSERT … RETURNING` would fail loudly; the `inventory_levels` UPDATE would vanish silently.) Correct form: `RETURN NEW` (and `RETURN OLD` for DELETE). P0's own exit drill (FI-9) only tests fence-ON, so the plan's gate does not catch it. |
| L3 | CONFIRMED | MEDIUM | ✓ (−10) | **Delete-behind-cursor convergence fails in the stated ordering.** Walkthrough 3 step 3: backfill suppresses its INSERT "only when the target row's applied-LSN metadata … is ≥ the DELETE's LSN". After the consumer applies the DELETE, the row is absent — there is no row to carry `applied_lsn`; backfill's `ON CONFLICT DO NOTHING` finds no conflict and re-inserts → resurrection. The claimed fallback ("the DELETE re-applies on next replay") requires a replay that normal operation never performs. Their own FI-5/V3 parity would detect it pre-cutover (narrow blast radius), but the claimed mechanism is false as written → doubled. |
| L4 | CONFIRMED | MEDIUM (−5) | | **Movements dedup key collapses legitimately identical rows.** Dedup UNIQUE `(product_id, warehouse_id, reason, order_id, occurred_at, delta)`: `now()` is transaction-stable, so two identical order lines in one `place_order`, or duplicate rows in one `warehouse_import` COPY (50k–400k-row files), produce byte-identical tuples → collapsed to one. §11's defense is wrong on the artifact: `nightly_reconcile` (code-paths §3) treats movements as *source of truth* and rebuilds `on_hand` from `SUM(delta)` — it propagates the lost delta, it cannot correct it. |
| L5 | CONFIRMED | MEDIUM (−5) | | **Rollback re-injection is blocked by the plan's own fence.** P3 rollback and walkthrough 8: reverse consumer applies service deltas into monolith tables "before completing the flip-back" while "fence already ON". The reverse consumer is an ordinary session; the fence trigger raises `ATLAS_FENCE` for it — no whitelist role/mechanism is named (contrast qwen-max's `atlas_cdc` whitelist). As written the re-injection cannot land until the fence drops, at which point monolith writers race it. |
| L6 | CONFIRMED | LOW (−2) | | "Services emit audit events to `audit_log` via the monolith's **existing** audit endpoint" (§3.5): no such endpoint exists in the artifacts — auditing is `trg_audit_orders`, a DB trigger. The endpoint must be built; unnamed, unbudgeted, failure semantics unstated. |
| L7 | CONFIRMED | LOW (−2) | | `CREATE SUBSCRIPTION … CONNECT VIA` (§1.1 rejected alt) is not PostgreSQL syntax (R10). |
| L8 | CONFIRMED | LOW (−2) | | wal_level restart unaddressed (X3). |
| L9 | CONFIRMED | LOW (−2) | | Allocate-then-reserve-then-insert widens the allocation-to-commit window from today's ~ms (inside one tx) to the full saga latency; orders whose number is below the EDI high-water at the hourly export and commit after it are never exported. Pre-existing race, widened ~100× without disclosure in §11. |
| L10 | PLAUSIBLE | — | | 288–340 MB/s sustained backfill *write* throughput into fresh managed-PG instances (including index maintenance), and "two full 1.4 TB passes inside one 3-h trough," is at the edge of believable even on NVMe; the plan does gate on staging measurement, so recorded as plausible-overclaim only. |
| L11 | PLAUSIBLE | — | | REPLICA IDENTITY misstatement ("silently dropped", §3.4) — see X4; harmless here because FULL is set, so graded into judgment. |

**Correctness: 100 − 64 = 36.**
**Judgment: 62.** Coherent architecture and the comparison-semantics section
(§8, quarantine, lag-budget tolerance) is genuinely good; single-allocator
choice is clean. Docked for fabricated artifacts (L6, L7), fantasy-adjacent
throughput, and a heavy dependency surface (Kafka + Connect + Debezium + custom
apply buffer + sidecar LSN columns) whose failure modes the plan only partly
owns. **Total: 0.7·36 + 0.3·62 = 43.8.**

---

## qwen-balanced — findings ledger

Architecture: custom self-hosted consumer over native logical decoding,
snapshot-per-table backfill, reserve-first (stock-first) three-phase saga with
outboxes, drain-then-hot-reload flip, REVOKE fence at retirement only.

| ID | Grade | Sev | ×2 | Finding |
|---|---|---|---|---|
| B1 | CONFIRMED | HIGH | ✓ (−24) | **The flip itself violates the plan's central claim.** §4 row 1 claims "no dual-write at any instant"; §8 Phase 3 executes the flip as "per-binary hot-reload of write/read target" after drain proof. During the propagation window (unspecified; the plan's own config-poll cadence elsewhere is 250 ms–seconds, across ~40 binaries), some binaries write the monolith and some write the services: two `FOR UPDATE` lock domains on `inventory_levels` simultaneously — precisely the oversell topology §1.3 rejected dual-write to avoid. Interleave: (1) binary A (flipped) reserves last unit on IS; (2) binary B (not yet flipped) reserves the same unit on the monolith — each domain's check sees only its own `reserved`; (3) both commit; (4) FWD replication then value-overwrites IS with the monolith row, erasing A's increment (no FWD-drain gate is named either — X2). No DB-level fence exists at flip (the REVOKE fence is Phase 4/retirement). Claimed invariant broken → doubled. |
| B2 | CONFIRMED | MEDIUM | ✓ (−10) | **Sequence seeded at the wrong moment.** §1.4 and §3.1 (twice): OS `order_number_seq` is seeded to monolith `last_value + 1` **at backfill**. The monolith then allocates for the entire Phase 1–3 span (~2,200 orders/s at peak — order 10⁸ numbers), so at flip the OS sequence sits far *below* the monolith's high-water. The plan's own gate (§1.4c: OS next value > monolith last value) then fails permanently as designed — the cutover cannot legally proceed without an ad-hoc re-seed the plan never specifies (the seed must be read at flip, under the drain freeze). Claimed promise-4 mechanism false as stated → doubled; base MEDIUM rather than HIGH because the gate converts runtime collision into a blocked flip. |
| B3 | CONFIRMED | MEDIUM (−5) | | **Slot-at-LSN facility does not exist.** §1.1: `pg_create_logical_replication_slot(slot, 'pgoutput', false, start_lsn)` — the PG15 signature is `(name, plugin, temporary, twophase)`; there is no start-LSN parameter, and a logical slot cannot be created at an arbitrary historical LSN (decoding state is built at creation). Walkthrough 10's recovery ("slot recreated at the consumer's last checkpoint LSN") is therefore impossible; a dropped slot means re-backfill regardless of 7-day WAL retention. The legitimate native pattern (create slot → use its exported snapshot for COPY) is the reverse of what §1.2 describes. R10 violation on a load-bearing recovery path. |
| B4 | CONFIRMED | MEDIUM (−5) | | **Bind-after-sweep race loses the stock hold for a committed order.** Interleave: (1) Phase A reserves, `order_id NULL`; (2) Phase B commits the order — customer sees success; (3) IS partition ≥10 min (the exact duration of their own FI-3); (4) abandon sweep releases the hold and writes a `reconcile` movement; (5) other orders consume the stock; (6) Phase C retries, binds zero reservations rows, yet inserts its `-qty` order movement → a committed order with no held stock plus a movement-log double-count that `nightly_reconcile` will propagate into `on_hand`. The sweep never checks OS for a bound order before releasing. |
| B5 | CONFIRMED | LOW (−2) | | `PQreplicationOpen` / `PQstartCopy` (§1.1) are not libpq functions (replication uses `START_REPLICATION` on a replication connection; COPY-both protocol). R10. |
| B6 | CONFIRMED | LOW (−2) | | wal_level restart unaddressed (X3). |
| B7 | CONFIRMED | LOW (−2) | | §1.5 makes storefront-listing correctness depend on the read switch and write switch "landing together at drain" — a synchronized-switch dependency in tension with promise 3, and internally inconsistent with the per-binary rolling reload the same flip uses (B1). |
| B8 | PLAUSIBLE | — | | The flip-time "standby IS→monolith consumer" implies service-side slots retaining WAL from flip onward; if the standby does not continuously apply, slot-retained WAL grows unbounded on managed instances for the whole post-flip period. Not enough text to confirm. |
| B9 | REJECTED | — | | I attacked the stock-first saga ordering (§1.7/§1.8) for oversell windows and found none: every crash state is over-reserved, never over-sold; cancel ordering (status flip before release) is likewise conservative. The asymmetry argument (§1.8) is correct. |

**Correctness: 100 − 50 = 50.**
**Judgment: 74.** The best-written document: strongest R1 discipline (though
§1.1's rejected alternative — a SaaS the brief forbids — is a strawman), honest
and complete assumptions (A1–A8), realistic arithmetic (checked: 900 GB ÷ 320
MB/s ≈ 47 min ✓; catch-up bounds ✓), fail-closed instincts (poison events,
review queues), and the only plan with a considered EDI catch-up on rollback.
**Total: 0.7·50 + 0.3·74 = 57.2.**

---

## qwen-max — findings ledger

Architecture: native logical replication both directions (FWD + standing REV),
drain-then-flip with role-whitelisted trigger fence, order-first saga with
orphan compensator, monolith tables retained as REV-fed audit replicas.

| ID | Grade | Sev | ×2 | Finding |
|---|---|---|---|---|
| M1 | CONFIRMED | HIGH | ✓ (−24) | **No FWD drain gate at flip (X2).** §8 Phase 3: drain proof → flip → fence → REV start; FWD is never stopped or drained-to-zero before services accept writes (its stop is not in any phase step; §2 says the monolith replica "continues converging … then freezes," with no gate tied to the flip). Native apply has no watermark: an in-flight monolith UPDATE to an `inventory_levels` row applies by PK *after* the first service-side reservation on that row and overwrites it. Lost committed write on the hottest rows at the highest-stakes moment; claims-register row 4 ("exactly-once per committed write") → doubled. |
| M2 | CONFIRMED | MEDIUM | ✓ (−10) | **The fence artifact fails as DDL and as described semantics.** §3.4-2 `write_fence()` is missing `END IF` — `CREATE FUNCTION` does not parse (the brief grades "DDL that executes"). Repaired as intended, the whitelisted path falls through to `RETURN NULL`, which in a BEFORE ROW trigger *cancels the row change* — the prose claims the opposite ("REV-applied rows … are the only writes that land"). In reality REV rows land only because of a third, unnamed mechanism: native apply skips ordinary triggers entirely (X1). The named mechanism is false twice over; the invariant survives by accident, so MEDIUM base, doubled (register row 4 / §5.7 rollback convergence names it). Also: whitelisted `migration_ctl` *interactive* sessions get their writes silently swallowed by `RETURN NULL`. |
| M3 | CONFIRMED | HIGH (−12) | | **Audit continuity claim is false (X1).** §3.4-3: "`trg_audit_orders` keeps firing as-is on the REV-fed replica rows." It does not — subscriber apply skips ordinary triggers; `ENABLE ALWAYS` is never named. Every post-flip order mutation is silently absent from the compliance `audit_log`, the one feed the artifacts say compliance reads. §12.4 claims this as a strength. Same falsehood underlies §1.5's claim that the monolith's own trigger keeps maintaining `cached_available` on the REV-fed replica. Silent, load-bearing, cited three times. |
| M4 | CONFIRMED | MEDIUM (−5) | | **FWD/REV overlap loops.** PG15 `pgoutput` has no origin filtering (publication `origin = none` arrives in PG16). While FWD's publication/slot exists, REV-applied rows on the monolith re-enter FWD and are re-sent to the services, where native apply of a duplicate INSERT errors and the subscription halt/retry-loops (dedup triggers cover only the two append-only tables — and per X1 they don't fire under apply anyway). No step drops FWD at flip. |
| M5 | CONFIRMED | MEDIUM (−5) | | **Dedup triggers silently swallow legitimate rows in live traffic.** `mov_dedup`/`osh_dedup` are permanent BEFORE INSERT triggers on the service tables, not CDC-scoped: post-cutover, duplicate import lines or duplicate order lines with transaction-stable timestamps are silently dropped from `inventory_movements` — the reconcile job's source of truth (same artifact logic as L4). |
| M6 | CONFIRMED | LOW (−2) | | Drain proof by "old-binary client-port ranges in `pg_stat_activity`" (§13.4): ephemeral client ports do not encode application versions; no tagging mechanism (e.g., `application_name`) is named. |
| M7 | CONFIRMED | LOW (−2) | | "REV's flip-time snapshot re-applies convergent upserts" (§8): native subscription apply has no upsert; `copy_data = true` into populated tables duplicate-errors, `copy_data = false` takes no snapshot. Facility as named doesn't exist. |
| M8 | PLAUSIBLE | — | | Order-first saga: an orphaned order that reaches `paid` (payment async, inside the 2-min window) escapes the compensator's `status='placed'` guard — a paid order with no stock hold. Timing-dependent on payment flow the artifacts don't show. |
| M9 | REJECTED | — | | I attacked §4.1's oversell analysis (order committed before stock): between steps the `reserved` value is untouched, contenders serialize at step 2's locked check, and the increment never lands outside its locked transaction. The no-oversell reasoning holds; the (disclosed) casualty is the no-trace property, honestly surfaced in §11.1. |

**Correctness: 100 − 60 = 40.**
**Judgment: 72.** The deepest artifact engagement of the four: only plan to
catch and preserve the misspelled-trigger wart, only plan to name that logical
decoding doesn't emit sequence state, only plan to budget the `wal_level`
restart, and the availability arithmetic against 263 s is real and recomputes
(90 + 90 + 7.5 = 187.5 s ✓). Docked for §8 as an unreadable wall, and for a
pattern of nearly-right facility claims (M2, M4, M7) that a plan this
sophisticated should not contain. **Total: 0.7·40 + 0.3·72 = 49.6.**

---

## opus-4-6 — findings ledger

Architecture: pure native logical replication (`copy_data = true`),
`migration_control` fence + application routing, saga orchestrated by V2
monolith binary, batched order-number endpoint, pre-established reverse CDC,
archived (not dropped) monolith tables.

| ID | Grade | Sev | ×2 | Finding |
|---|---|---|---|---|
| V1 | CONFIRMED | HIGH | ✓ (−24) | **Batched allocation breaks promise 4 twice.** (a) The endpoint (§1.4) is `nextval` then `setval(start+count−1)` in separate statements: two concurrent calls interleave (A:`nextval`→100 … B:`nextval`→101 … A:`setval` 149, B:`setval` 150) → overlapping ranges 101–149 handed to two binaries → duplicate allocations at 44 calls/s. (b) Worse and unconditional: per-binary prefetched batches (≈0.9 s of skew at peak) mean commit order routinely diverges from number order — a customer's consecutive orders on two binaries can get *decreasing* numbers, and any order committing with a number below the EDI high-water after the hourly export runs is **never exported** (the plan's own §1.4-Rejected paragraph explains this failure — for someone else's design). Register row 4 claims "no overlap, no gap visible to EDI" → doubled. Today's window for this race is ~ms inside one transaction; batching widens it ~1000×. |
| V2 | CONFIRMED | HIGH (−12) | | **Subscriber-trigger claim is false (X1).** §2: "PostgreSQL fires row-level triggers on subscriber tables during logical replication apply" — by default it does not; only `ENABLE REPLICA/ALWAYS` triggers fire, and no `ALTER TABLE … ENABLE ALWAYS TRIGGER trg_refresh_available` is ever named (Assumption 5 re-asserts the error). Consequence: after cutover, `products.cached_available` — the 40k reads/s storefront column — silently freezes at its pre-cutover values, indefinitely, with no alarm named on the delta. |
| V3 | CONFIRMED | HIGH | ✓ (−24) | **Rollback restores levels but not the movement log → reconcile corrupts stock.** Reverse CDC (§8 Phase 3 step 6) carries only `inventory_levels`, `orders`, `order_lines` — not `inventory_movements` (nor `reservations`, `order_status_history`). Walk the worst window: rollback after 2 h of service ownership (their own walkthrough-7 scenario) completes at, say, 01:00; at 02:30 the `nightly_reconcile` cron (code-paths §3) rebuilds `on_hand = GREATEST(SUM(delta),0)` from a monolith movement log that is missing every service-period `-qty` order movement → `on_hand` silently *inflated* above reality → oversell exposure (promise 2) plus a silently corrupted source-of-truth ledger. Register row 1's rollback path claims "reverse CDC streams service-only writes back" → doubled. |
| V4 | CONFIRMED | HIGH (−12) | | **Identity sequences re-seeded days too early.** §3/§8: `setval(max(id))` runs at Phase 1 exit ("after backfill, before write cutover"), but forward CDC keeps inserting monolith rows through Phases 2–3 (subscriptions disabled only in Phase 3 step 5). At the flip, the services' first `GENERATED ALWAYS` inserts collide with replicated ids that advanced past the Phase-1 seed → PK violations on every placement at cutover until an operator re-runs `setval` — a guaranteed order-placement outage at the worst possible moment, against a 263 s budget the plan has already spent ~30 s of. Phase 1's exit criterion ("sequences exceed MAX(id)") is checked at the only time it is vacuously true. |
| V5 | CONFIRMED | LOW (−2) | | Walkthrough 1/2: subscriber "TRUNCATES the partially-copied table before re-COPYing" — not a PG mechanism (tablesync COPY runs in one transaction; abort discards it and the worker restarts the copy). Outcome right, facility wrong (R10). |
| V6 | CONFIRMED | LOW (−2) | | wal_level restart unaddressed (X3). |
| V7 | PLAUSIBLE | — | | Phase 3's deliberate ~15–30 s global write pause is disclosed and inside budget, but it is a synchronized all-at-once switch in tension with promise 3 ("no flag day"); recorded for reconciliation rather than scored, since each step is individually reversible and observable. |
| V8 | REJECTED | — | | I attacked the cutover ordering (fence → in-flight drain → CDC lag 0 → disable subs → route) for the X2 clobber and could not break it — opus-4-6 is the only plan that closes that window. Likewise walkthrough 9 (COPY atomicity under mid-flight fence) and walkthrough 10 (stale `cached_available` cannot cause oversell) are correct. |

**Correctness: 100 − 76 = 24.**
**Judgment: 73.** The most operationally fluent plan: most accurate
REPLICA IDENTITY analysis (X4), correct exactly-once reasoning via apply-worker
position tracking, the only correct write-flip ordering, concrete runbook
detail (P0999/Retry-After, circuit breaker numbers, import-window check),
realistic 40 MB/s backfill arithmetic (recomputes exactly), and honest
tradeoffs. But four independent HIGH-grade confirmed defects — two of them
doubled register claims — sit on promises 1, 2, and 4. **Total: 0.7·24 +
0.3·73 = 38.7.**

---

## Reconciliation notes for the second reviewer

- The scores are dominated by five doubled findings (L1, B1, B2, V1, V3, M1,
  M2). If you show any of these REJECTED — a repro-level argument that the
  interleave cannot occur or the facility does exist as named — the ranking
  between adjacent plans (max/low, and balanced/max) can flip; please attack
  them specifically.
- X1 (subscriber triggers) and X2 (flip-time CDC drain) are facility-level
  claims about PostgreSQL 15; they are checkable against documentation and
  should be confirmed or refuted, not averaged.
- Judgment scores are offered for averaging per protocol §5, not argument.
- Per the workspace blinded-eval policy: directory names are treated as opaque
  labels; no identity speculation, no mapping persisted.

---

# Reconciliation (Reviewer 1: Fable · Reviewer 2: GPT — `analysis-gpt.md`)

Reviewer 2 reviewed blind (its prompt forbade opening this file). Per protocol
§6, correctness findings are checked, not voted on; judgment is averaged, never
argued; surviving severity disagreements are reported side by side.

## R2 findings accepted (I missed these; verified against plan text)

| R2 ID | Plan | Verified fact | My reconciled grade |
|---|---|---|---|
| M-02 | qwen-max | §3.4-1: `pub_is` = (`inventory_levels`, `inventory_movements`) — **`reservations` is in no publication**, while Phase 1's exit claims checksum parity for "all six tables." Live reservations never reach IS; post-cutover cancel/shipment of pre-flip orders find no rows; `reserved` leaks permanently. | HIGH ×2 = −24 (R2: CRIT ×2 — split reported) |
| M-03 | qwen-max | §3.1 DDL: `orders.id` / `order_lines.id` / `reservations.id` are bare `BIGINT PRIMARY KEY` — no identity, no default, and no OS `order_number_seq` DDL exists despite §1.3/§3.1 prose seeding "local identity sequences." §4.1's `INSERT … RETURNING id, order_number` fails on first post-cutover placement. R2 violation (data model means DDL); loud at first insert. | MEDIUM ×2 = −10 (R2: CRIT ×2) |
| M-08 | qwen-max | Phase 4 soak occupies D20–26; Phase 5 (D27–28) requires "checksum equality sustained 7 days" → completes D34 at the earliest, outside the 30-day window; the claimed 2-day reserve is negative. | MEDIUM −5 (R2: HIGH) |
| M-09 | qwen-max | 300,000 MB ÷ 45 MB/s = 6,667 s ≈ **1.85 h**, not the stated "≈1.2 h". | LOW −2 (agreed) |
| B-05* | qwen-balanced | §2 defines only an "**IS**→monolith standby consumer"; walkthrough 5 asserts it "replays every post-flip **IS/OS** mutation." No OS→monolith reverse channel exists in the architecture — rollback as componentized loses every post-flip order. Internal inconsistency on the rollback path. | MEDIUM ×2 = −10 (R2: CRIT ×2 — I grade lower because walkthrough intent is unambiguous and the gap is a component-list omission, not a designed exclusion; contrast V3) |
| B-06/L-06/V-04 | balanced, low, v2 | **Ambiguous-commit compensation releases stock for a committed order.** All three saga plans call release/cancel-reservations on *any* failure including a response timeout after the order transaction actually committed → committed order without its stock hold; the unit resells; the order is unfulfillable. qwen-balanced additionally reaches the same state via its 10-min abandon sweep during an IS outage of exactly its own FI-3 duration (supersedes my B4, which was the sweep half of this finding). qwen-low's sweeper checks `orders` before releasing, but its §1.3 immediate-failure release does not. | HIGH ×2 = −24 each (registers claim placement atomicity / conservative healing; R2: CRIT ×2) |
| V-01 | opus-4-6 | §1.2 fence returns `NEW` unconditionally; triggers are `BEFORE INSERT OR UPDATE OR DELETE` on `inventory_levels`, `reservations`, `order_lines`. For DELETE, `NEW` is NULL → **every reservations DELETE (cancel_order ~4,000×/day, shipment) is silently suppressed from Phase 0 onward**, fence active or not. Traced consequence: `reserved` counters stay correct (the UPDATE half works), so no stock error — the damage is committed DELETE mutations silently lost (promise 1) and a `reservations` table that diverges and replicates its phantoms to IS. | HIGH ×2 = −24 (R2: CRIT ×2 — R2's trace stops at "reservation survives" without following the counters; blast radius is phantom rows, not oversell) |
| V-06 | opus-4-6 | Walkthrough 7 step 1: `UPDATE migration_control SET owner='draining'` violates §1.2's `CHECK (owner IN ('monolith','service'))` → the rollback procedure's first statement fails; the pause semantics V2 binaries need for the drain step are unreachable as written. | MEDIUM ×2 = −10 (R2: CRIT ×2 — loud, but the workaround forfeits the pause that orders reverse-CDC catch-up) |
| V-07 | opus-4-6 | `ALTER SUBSCRIPTION … REFRESH PUBLICATION` starts sync only for **newly added** tables; it does not re-copy a previously subscribed table after a failed sync. Recovery facility wrong as named (R10). | MEDIUM −5 (agreed) |
| V-08 | opus-4-6 | Walkthrough 7: "2,200/sec × 7,200 s = 15,840 orders" — off by ×1,000 (15.84 M). | LOW −2 (agreed) |
| L-07/B-08 | low, balanced | Availability-budget engagement is missing (qwen-low: no 263 s arithmetic anywhere, and walkthrough-8 freeze windows dwarf the budget → concealment-class) / deficient (qwen-balanced: FI-3 stops a production DB for 10 min "at reduced scale" — 600 s of placement failure against a 263 s budget; partial credit for walkthrough 3's budget gate). | qwen-low HIGH −12; qwen-balanced MEDIUM −5 |
| L-08 | qwen-low | "2.4 h of a 3-h window, slack 66 min": 2 × 71 min = 142 min → slack is 38 min. | LOW −2 (agreed) |

\* Also L-04 (EDI commit-order skew) accepted for qwen-low but re-graded: the
allocation-order-vs-commit-order race **exists in the monolith today** (numbers
allocate at INSERT inside a ~ms transaction; the hourly export can already
permanently skip an order). qwen-low's allocate-then-saga widens the window
~1000×, and its register claims monotonicity "by construction" → MEDIUM ×2 =
−10 (R2: CRIT ×2 — split; grading a plan CRITICAL for amplifying a defect the
brief's own artifact ships is miscalibrated, but the amplification is real).

## R2 findings refuted or materially re-traced

- **M-05 (dedup triggers collapse rows during initial copy): trace refuted.**
  Both the tablesync worker and the apply worker run with
  `session_replication_role = replica`; qwen-max's ordinary `BEFORE INSERT`
  dedup triggers do **not** fire during initial copy or streaming apply. The
  finding survives only via my F-M4 path (live post-cutover traffic, normal
  sessions) at MEDIUM. R2's own M-06 correctly states the apply rule — M-05
  contradicts it.
- **B-07 (full-tuple dedup ambiguity = CRITICAL): severity refuted.**
  qwen-balanced's handling is fail-closed — the ambiguous event is *held in a
  review queue with an alarm*, disclosed in §12.3. Nothing is silently dropped
  or duplicated. That is the best handling of this trap in the field; LOW −2
  for the residual undecidability, not CRITICAL.
- **B-01 (invented CDC APIs = CRITICAL): severity split.** The named calls do
  not exist (confirmed, my B3/B5), but the *design* — slot first, COPY under
  the slot's exported snapshot — is exactly what `CREATE_REPLICATION_SLOT …
  EXPORT_SNAPSHOT` provides, so the mechanism-in-spirit is real; the
  unimplementable part is walkthrough 10's slot-recreation-at-LSN. Held at
  MEDIUM + LOW.
- **L-01/M-01 severity (fence defects = CRITICAL ×2):** both fences are broken
  as written (agreed, confirmed), but both fail **loudly** — qwen-low's makes
  every placement error immediately (RETURNING returns no row) and sits behind
  a P0 staging gate; qwen-max's fails at `CREATE FUNCTION`. The protocol
  reserves CRITICAL for silent-loss classes. Held at HIGH −12 (L) and
  MEDIUM ×2 (M, with the semantic contradiction). Splits reported.
- **Blanket CRITICAL inflation:** R2 grades 17 findings CRITICAL-doubled,
  flooring all four plans and — by its own admission — deciding its entire
  ranking on the capped judgment layer. That inverts the protocol's design
  (findings are supposed to discriminate). My reconciled grades apply the §4
  severity definitions (CRITICAL = silent data loss/corruption class; loud,
  gate-catchable, or pre-existing-defect-amplifying failures grade lower).

## My findings R2 did not surface (stand as confirmed, unrefuted)

L1 (FWD-drain clobber — R2 confirmed the same for low/balanced as L-02/B-03 and
credited v2's ordering, but missed it for **qwen-max**: M1 stands), L3
(delete-behind-cursor resurrection), L5 (rollback blocked by own fence — R2's
L-03 is the same finding independently confirmed), V4/F-V4 (identity reseed at
Phase 1 goes stale by Phase 3 — guaranteed PK collisions at cutover), V5
(tablesync TRUNCATE facility), X3 (`wal_level` restart, low/balanced; R2 flagged
it PLAUSIBLE for v2 only), B7 (synchronized read/write listing switch), M6
(port-range drain proof), M7 (REV bootstrap upsert/copy_data — R2's M-04 is the
same finding; merged at MEDIUM ×2).

## Reconciled ledgers (deduplicated union, my calibrated severities)

- **qwen-low** (Σ 96 → C = 4): FWD-drain clobber H×2 24 · fence RETURN NULL H 12
  · delete-resurrection M×2 10 · movements-dedup collapse M×2 10 · rollback
  blocked by fence M×2 10 (staging-drilled per §7 → not HIGH) · EDI commit-order
  M×2 10 · availability concealment H 12 · audit endpoint L 2 · CONNECT VIA L 2
  · wal_level L 2 · slack arithmetic L 2.
- **qwen-balanced** (Σ 91 → C = 9): flip dual-write + no drain gate H×2 24 ·
  sequence seeded at backfill M×2 10 · ambiguous-commit/sweep release H×2 24 ·
  OS reverse channel missing M×2 10 · slot-at-LSN M 5 · PQ* names L 2 ·
  comparison zero-tolerance M 5 · FI-3 budget M 5 · wal_level L 2 · sync switch
  L 2 · dedup ambiguity L 2.
- **qwen-max** (Σ 128 → C = 0): FWD-drain clobber H×2 24 · `reservations`
  unpublished H×2 24 · FWD/REV loop (duplicating PK-less rows silently) H×2 24
  · subscriber-trigger audit/availability H 12 · fence parse+semantics M×2 10 ·
  REV bootstrap M×2 10 · missing identity/sequence DDL M×2 10 · live dedup
  collapse M 5 · schedule overrun M 5 · port-range drain L 2 · 1.85 h arithmetic
  L 2.
- **opus-4-6** (Σ 167 → C = 0): reverse CDC omits movements CRIT×2 50 (both
  reviewers; silent post-rollback stock corruption via nightly_reconcile — the
  one finding we agree merits CRITICAL: designed-in omission, silent, on the
  brief's hardest question) · batch allocator H×2 24 · fence DELETE suppression
  H×2 24 · ambiguous-commit release H×2 24 · subscriber-trigger
  cached_available H 12 · identity reseed timing H 12 · 'draining' CHECK M×2 10
  · REFRESH PUBLICATION M 5 · TRUNCATE L 2 · wal_level L 2 · 15,840 arithmetic
  L 2.

## Judgment (averaged per §5, not argued)

| Plan | R1 | R2 | Avg |
|---|---:|---:|---:|
| qwen-low | 62 | 35 | 48.5 |
| qwen-balanced | 74 | 42 | 58.0 |
| qwen-max | 72 | 46 | 59.0 |
| opus-4-6 | 73 | 54 | 63.5 |

## Final reconciled scores

| Rank | Plan | C | J avg | Total | Movement vs my initial |
|---:|---|---:|---:|---:|---|
| 1 | qwen-balanced | 9 | 58.0 | **23.7** | — |
| 2 | opus-4-6 | 0 | 63.5 | **19.1** | ▲ from 4th |
| 3 | qwen-max | 0 | 59.0 | **17.7** | ▼ from 2nd |
| 4 | qwen-low | 4 | 48.5 | **17.4** | ▼ from 3rd |

**Why the ranking moved.** Pooling two hostile reviews roughly doubled each
plan's confirmed-finding surface; three of four plans hit the correctness
floor, so — per the protocol's own arithmetic — discrimination below the floor
shifts to the (averaged) judgment layer, where opus-4-6's operational fluency
finally counts for something and qwen-low's fabricated artifacts cost it. Both
reviewers independently ranked qwen-balanced's correctness first, and it is the
only plan above the floor; its #1 is the most robust conclusion in this file.
The 2–4 ordering is floor-compressed (span 1.7 points) and sensitive to
severity calibration: by raw Σ (magnitude of confirmed defect weight:
balanced 91 < low 96 < max 128 < v2 167) the sub-floor order would instead be
low > max > v2. Both orderings are reported per §6; the protocol-computed total
is official.

**Surviving disagreements (reported, not forced):** R2 grades CRITICAL-doubled
where I grade HIGH/MEDIUM on eleven findings (see splits above); the specific
unresolved severity axis is whether loud, gate-catchable defects belong in the
CRITICAL band. Neither reviewer's correctness-first ranking of qwen-balanced is
in dispute; R2's judgment-only ranking (v2 > max > balanced > low) and my
initial correctness-led ranking (balanced > max > low > v2) reconcile to the
table above.
