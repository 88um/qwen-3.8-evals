# NOTES — DST detection with Python zoneinfo

## How nonexistent and ambiguous times are detected

Python's `datetime` has a `fold` attribute (PEP 495) that controls which
UTC offset is used when a local time is ambiguous or falls in a DST gap.
With `zoneinfo.ZoneInfo`:

1. Construct the same naive datetime twice, with `fold=0` and `fold=1`.
2. Convert both to UTC via `.astimezone(timezone.utc)`.
3. Compare the two UTC instants:

   - **Equal** → the local time is **normal** (unambiguous, exists).
   - **Different** → either a gap or an ambiguity. Distinguish by
     round-tripping the fold=0 UTC result back to local:
     - Round-trip matches the original naive time → **ambiguous** (fall-back).
       Both fold values map to valid but different UTC instants.
       `fold=0` gives the earlier UTC instant (pre-transition offset).
     - Round-trip does NOT match → **gap** (spring-forward).
       The local time doesn't exist; `fold=0` applied the pre-gap offset,
       producing a UTC instant whose local equivalent is a different
       wall-clock time.

## Verified behavior (Python 3.13, macOS system tzdata)

```
>>> from datetime import datetime, timezone
>>> from zoneinfo import ZoneInfo
>>> tz = ZoneInfo("America/New_York")

# Spring-forward gap: March 8 2026, 02:30 doesn't exist
>>> dt0 = datetime(2026, 3, 8, 2, 30, tzinfo=tz)
>>> dt1 = datetime(2026, 3, 8, 2, 30, fold=1, tzinfo=tz)
>>> dt0.astimezone(timezone.utc)
datetime.datetime(2026, 3, 8, 7, 30, tzinfo=datetime.timezone.utc)
>>> dt1.astimezone(timezone.utc)
datetime.datetime(2026, 3, 8, 6, 30, tzinfo=datetime.timezone.utc)
# Different → gap (round-trip of 07:30Z gives 03:30 EDT, not 02:30)

# Fall-back ambiguity: Nov 1 2026, 01:30 occurs twice
>>> dt0 = datetime(2026, 11, 1, 1, 30, tzinfo=tz)
>>> dt1 = datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=tz)
>>> dt0.astimezone(timezone.utc)
datetime.datetime(2026, 11, 1, 5, 30, tzinfo=datetime.timezone.utc)
>>> dt1.astimezone(timezone.utc)
datetime.datetime(2026, 11, 1, 6, 30, tzinfo=datetime.timezone.utc)
# Different → ambiguous (round-trip of 05:30Z gives 01:30 EDT = original)

# Normal time: June 2 2026, 08:30
>>> dt0 = datetime(2026, 6, 2, 8, 30, tzinfo=tz)
>>> dt1 = datetime(2026, 6, 2, 8, 30, fold=1, tzinfo=tz)
>>> dt0.astimezone(timezone.utc) == dt1.astimezone(timezone.utc)
True
# Equal → normal
```
