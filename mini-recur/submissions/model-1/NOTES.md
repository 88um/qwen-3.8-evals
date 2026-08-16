# model-1 — NOTES

## Timezone library and DST resolution

Language: Python 3 (stdlib only). Timezone math: `zoneinfo` (stdlib since
3.9), backed by the platform's IANA tzdata (`/usr/share/zoneinfo` on this
macOS). Verified against Python 3.14.4's zoneinfo.

### How nonexistent and ambiguous local times are detected

A candidate slot is a naive local datetime `N` (date + wall time) under the
epoch's timezone `tz`. Resolution is:

```python
u = N.replace(tzinfo=tz).astimezone(UTC)          # zoneinfo's choice of instant
if u.astimezone(tz).replace(tzinfo=None) != N:     # round-trip inequality
    status = "skipped_dst"                          # nonexistent wall time
else:
    status = "scheduled"                            # normal or ambiguous-earliest
```

`u` is used as the ordering/epoch/window membership instant in both cases.

This is not assumed behavior — it was verified against the library source
(`zoneinfo/_zoneinfo.py`) and empirically probed:

- `ZoneInfo.utcoffset(dt)` for a datetime carrying wall-clock fields with
  `fold=0` calls `_find_trans`, which bisects `_trans_local[0]` — the list of
  transition wall-times shifted by the *smaller* (earlier-side) offset. For a
  fold transition that entry is the end-of-repeat wall time, so any wall time
  inside the repeat bisects to the **pre-fold (earlier) offset**. For a gap
  transition that entry is the gap-end wall time, so any wall time inside the
  gap bisects to the **pre-gap offset**.
- Consequences (all empirically confirmed):
  - Normal times resolve directly.
  - Ambiguous times (fall-back repeat) resolve to the **earliest**
    occurrence — exactly the spec's mandated choice.
  - Nonexistent times (spring-forward gap) resolve to `N - pre_gap_offset`,
    i.e. exactly the spec's **gap-advanced** ordering instant.
  - The round-trip inequality distinguishes nonexistent from existent: for a
    gap time the pre-gap-offset instant always lands strictly inside/after the
    gap, so converting it back yields a different wall time; for normal and
    ambiguous-earliest times it round-trips exactly.
- Probes run (Python 3.14.4, system tzdata): NY gap day 2026-03-08 —
  `01:59`→scheduled 06:59Z, `02:00`/`02:30`/`02:59`→skipped (gap-advanced
  07:00Z/07:30Z/07:59Z), `03:00`/`03:01`→scheduled (EDT); Sydney fold
  2026-04-05 — repeated `02:00`/`02:01`→earliest occurrence (AEDT); Sydney
  gap 2026-10-04 — `02:00`→skipped (gap-advanced 16:00Z); fixed-offset zone
  sanity. All matched the spec semantics.

The `_TZStr` (TZ-string) fallback path was also reviewed: with `fold=0` it
selects the earlier offset for ambiguous times and the pre-gap offset for gap
times under both DST polarities, so the same two-line resolution holds for
zones defined by TZ strings, not only by recorded transitions.

Leap seconds are ignored by zoneinfo conversions; inputs are second-granular
`...Z` instants, so no leap-second ambiguity enters.

## Algorithm

1. Build config epochs: initial config active from beginning of time; each
   edit starts a new epoch at its `at` instant; epoch intervals are
   `[at_k, at_k+1)`.
2. Per epoch, intersect the epoch interval with `[window.start,
   window.end)`; iterate local calendar dates (in the epoch's timezone) over
   that interval with a ±3-day margin, filter by weekday, resolve each slot,
   and keep candidates whose resolved instant falls inside the intersection.
3. Sort all valid candidates by (firing instant, rule_id) — for
   `skipped_dst` records the firing instant is the gap-advanced instant.
4. First-writer-wins dedup on `(rule_id, local_date)`: an existing
   `scheduled` record drops later candidates for the key; at most one
   `skipped_dst` record per key; a later `scheduled` candidate for a key that
   only holds a skip record still materializes (the skip record is retained —
   the spec's rule 5 forbids dropping previously materialized records and
   explicitly permits the later scheduled occurrence).

## Tests

`tests/run_tests.py` runs the two shared examples plus hand-derived
corner cases in `tests/cases/`: ambiguous-earliest, set_time step-over
(no occurrence), timezone-change date-string collision (dedup), skip-skip
dedup (at most one skip per key), skip-then-scheduled (both records),
window-boundary inclusivity (`start` inclusive, `end` exclusive), and
epoch-boundary exactness (edit exactly at a candidate's instant).
