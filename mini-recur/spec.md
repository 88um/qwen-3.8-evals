# mini-recur — Product Spec

Build a recurrence engine for a social-media scheduler: given weekly posting
rules in a timezone, materialize the exact posting instants over a window —
correctly across DST transitions, rule edits, and timezone changes, without
ever minting a duplicate occurrence or inventing one that shouldn't exist.

This is a pure-correctness task. No daemon, no network, no concurrency. Your
program reads one JSON scenario on stdin and writes one JSON result on stdout.
It is graded against hidden scenarios with exact expected outputs.

## Shared workspace rules — read first

Several independent submissions are built from this same spec, possibly
concurrently on this machine. You have been assigned a **submission ID** (e.g.
`model-a`) in your kickoff instructions.

- Create **every file you write** inside your own submission directory
  (`submissions/<your-id>/`). Nothing outside it — no stray files in the shared
  root.
- Never read, modify, or write another submission's directory, and never modify
  the shared `spec.md` or `examples/`.
- The grading harness (`grader.py`, `make_goldens.py`, anything under
  `goldens/` or `results/`) may be present elsewhere on this machine. It is
  off-limits: do not read, run, or search for it. Build to this spec, not to
  the test — consulting the harness or goldens is a disqualifying protocol
  violation.

## Interface contract

Your submission root must contain an executable named `recur` (shebang script,
any language on a stock macOS dev machine; standard library only — timezone
math must come from your platform's IANA tzdata support, e.g. Python
`zoneinfo`, Node `Intl`/`Temporal`, Go `time`). It is invoked as:

```
./recur < scenario.json > result.json
```

Exit 0 on success. No other output on stdout.

### Input

```json
{
  "timezone": "America/New_York",
  "window": {"start": "2026-03-01T00:00:00Z", "end": "2026-03-15T00:00:00Z"},
  "rules": [
    {"id": "r1", "days": ["sun", "wed"], "time": "02:30"}
  ],
  "edits": [
    {"at": "2026-03-10T00:00:00Z", "kind": "set_time", "rule_id": "r1", "time": "19:30"},
    {"at": "2026-03-12T00:00:00Z", "kind": "set_timezone", "timezone": "Asia/Tokyo"}
  ]
}
```

- `days`: subset of `mon tue wed thu fri sat sun`.
- `time`: 24h `HH:MM` local wall-clock time.
- `edits` are given in ascending `at` (UTC instants) and take effect at exactly
  that instant. `set_time` changes one rule's wall-clock time; `set_timezone`
  changes the timezone governing all rules.

### Output

```json
{
  "occurrences": [
    {"rule_id": "r1", "local_date": "2026-03-04", "local_time": "02:30",
     "instant": "2026-03-04T07:30:00Z", "status": "scheduled"},
    {"rule_id": "r1", "local_date": "2026-03-08", "local_time": "02:30",
     "instant": null, "status": "skipped_dst"}
  ]
}
```

Sorted by firing instant (see below), then `rule_id`. Instants formatted
exactly as `YYYY-MM-DDTHH:MM:SSZ` (UTC, seconds always present).

## Semantics (normative)

Think of it as a simulation that walks the window in instant order and
materializes occurrences first-writer-wins, exactly like a slot ledger with a
unique key on `(rule_id, local_date)`:

1. **Config epochs.** The initial config (timezone + rules) is active from the
   beginning of time; each edit starts a new epoch at its `at` instant. Epoch
   k is active over `[at_k, at_k+1)`.
2. **Candidate slots.** Under a given epoch's config, each rule produces a
   candidate for every local calendar date (in the epoch's timezone) whose
   weekday is in the rule's `days`, at the rule's wall-clock `time`.
3. **Resolving a slot to a firing instant.**
   - Normal times resolve directly.
   - **Nonexistent** times (spring-forward gap, e.g. 02:30 on a US
     spring-forward date): the occurrence is emitted with `"instant": null`
     and `"status": "skipped_dst"`. For ordering, epoch membership, and window
     membership, its *firing instant* is the gap-advanced resolution (the
     instant you get by applying the pre-gap UTC offset — e.g. 02:30
     America/New_York on a gap day fires at 07:30Z).
   - **Ambiguous** times (fall-back repeat, e.g. 01:30 occurring twice): use
     the **earlier** offset (the first occurrence of that wall time).
4. **A candidate is valid only if its firing instant lies inside its own
   epoch's active interval AND inside `[window.start, window.end)`.** This is
   the heart of edit semantics: after a `set_time` edit at instant E, the old
   time's slots fire only if they land before E, and the new time's slots only
   if they land at/after E. A date where the old slot would have fired after E
   and the new slot before E gets **no occurrence at all** — the edit stepped
   over it. Do not invent one from either config.
5. **First-writer-wins dedup.** Process all valid candidates in firing-instant
   order. If the key `(rule_id, local_date)` (the `YYYY-MM-DD` string) already
   has a materialized occurrence with status `scheduled`, drop the candidate
   silently — a rule edit or timezone change must never produce a second
   occurrence for the same rule and local date. `skipped_dst` entries do not
   occupy the key (dedupe them per key too — at most one skip record per
   key — but a later valid candidate for that key may still materialize as
   `scheduled`).
6. **Timezone changes** re-key future dates in the new timezone's local
   calendar; the dedup key comparison is on the date *string*, so a local date
   already materialized under the old timezone blocks a same-dated slot under
   the new one.

Statuses are exactly `scheduled` and `skipped_dst`. Window boundaries:
`instant == window.start` is inside; `instant == window.end` is outside.

## Examples

Two fully worked scenarios with exact expected outputs are in `examples/`.
Your implementation must reproduce them byte-for-byte (modulo JSON whitespace;
the grader compares parsed structures, not text). The hidden grading scenarios
cover the same semantics — including the corners this spec calls out. Include
your own tests and a short `NOTES.md` describing how you detect nonexistent
and ambiguous local times in your language's timezone library — the library's
actual behavior, not assumed behavior; getting this wrong is historically the
most common failure on this task.
