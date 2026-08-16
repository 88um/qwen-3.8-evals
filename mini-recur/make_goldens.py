#!/usr/bin/env python3
"""Golden-scenario generator for mini-recur. OPERATOR ONLY — never show contestants.

Implements the spec semantics exactly (this is the ground truth) and writes:
  goldens/*.json            hidden grading scenarios {name, input, expected}
  examples/example-*.json   two contestant-visible worked examples
Run once: python3 make_goldens.py
"""
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
FAR_PAST = datetime(1970, 1, 1, tzinfo=timezone.utc)
FAR_FUTURE = datetime(2100, 1, 1, tzinfo=timezone.utc)


def parse_utc(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def fmt_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve(tz, d, hhmm):
    """Resolve local wall time -> ('scheduled'|'skipped_dst', firing_instant)."""
    h, m = map(int, hhmm.split(":"))
    naive = datetime(d.year, d.month, d.day, h, m)
    dt0 = naive.replace(tzinfo=tz, fold=0)
    u0 = dt0.astimezone(timezone.utc)
    back = u0.astimezone(tz)
    if (back.year, back.month, back.day, back.hour, back.minute) != (
        d.year, d.month, d.day, h, m,
    ):
        return "skipped_dst", u0  # nonexistent; u0 is the gap-advanced instant
    # ambiguous -> fold=0 is the earlier offset, which is what the spec wants
    return "scheduled", u0


def evaluate(scenario):
    win_start = parse_utc(scenario["window"]["start"])
    win_end = parse_utc(scenario["window"]["end"])

    # config epochs
    epochs = []  # (start, end, tzname, {rule_id: time})
    tzname = scenario["timezone"]
    times = {r["id"]: r["time"] for r in scenario["rules"]}
    days = {r["id"]: set(r["days"]) for r in scenario["rules"]}
    starts = [FAR_PAST] + [parse_utc(e["at"]) for e in scenario.get("edits", [])]
    configs = [(tzname, dict(times))]
    for e in scenario.get("edits", []):
        tzname_n, times_n = configs[-1][0], dict(configs[-1][1])
        if e["kind"] == "set_time":
            times_n[e["rule_id"]] = e["time"]
        elif e["kind"] == "set_timezone":
            tzname_n = e["timezone"]
        else:
            raise ValueError(e["kind"])
        configs.append((tzname_n, times_n))
    for i, start in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else FAR_FUTURE
        epochs.append((start, end, configs[i][0], configs[i][1]))

    # candidates valid within their epoch and the window
    candidates = []
    for ep_start, ep_end, ep_tz, ep_times in epochs:
        tz = ZoneInfo(ep_tz)
        lo = (win_start.astimezone(tz).date() - timedelta(days=2))
        hi = (win_end.astimezone(tz).date() + timedelta(days=2))
        d = lo
        while d <= hi:
            for rid, hhmm in ep_times.items():
                if DAYS[d.weekday()] in days[rid]:
                    status, firing = resolve(tz, d, hhmm)
                    if ep_start <= firing < ep_end and win_start <= firing < win_end:
                        candidates.append(
                            (firing, rid, d.isoformat(), hhmm, status)
                        )
            d += timedelta(days=1)

    # first-writer-wins on (rule_id, local_date); skips don't occupy the key
    candidates.sort(key=lambda c: (c[0], c[1], c[2]))
    scheduled_keys, skip_keys, out = set(), set(), []
    for firing, rid, dstr, hhmm, status in candidates:
        key = (rid, dstr)
        if key in scheduled_keys:
            continue
        if status == "skipped_dst":
            if key in skip_keys:
                continue
            skip_keys.add(key)
            out.append((firing, {"rule_id": rid, "local_date": dstr,
                                 "local_time": hhmm, "instant": None,
                                 "status": "skipped_dst"}))
        else:
            scheduled_keys.add(key)
            out.append((firing, {"rule_id": rid, "local_date": dstr,
                                 "local_time": hhmm, "instant": fmt_utc(firing),
                                 "status": "scheduled"}))
    out.sort(key=lambda p: (p[0], p[1]["rule_id"], p[1]["local_date"]))
    return {"occurrences": [o for _, o in out]}


SCENARIOS = [
    ("basic-weekly", {
        "timezone": "America/New_York",
        "window": {"start": "2026-06-01T00:00:00Z", "end": "2026-06-15T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["mon", "thu"], "time": "09:00"},
                  {"id": "r2", "days": ["sat"], "time": "18:45"}],
        "edits": [],
    }),
    ("spring-gap", {
        "timezone": "America/New_York",
        "window": {"start": "2026-03-01T00:00:00Z", "end": "2026-03-15T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["sun", "wed"], "time": "02:30"}],
        "edits": [],
    }),
    ("fallback-earlier-offset", {
        "timezone": "America/New_York",
        "window": {"start": "2026-10-25T00:00:00Z", "end": "2026-11-05T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["sun"], "time": "01:30"}],
        "edits": [],
    }),
    ("berlin-spring-gap", {
        "timezone": "Europe/Berlin",
        "window": {"start": "2026-03-25T00:00:00Z", "end": "2026-04-02T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["sun", "mon"], "time": "02:30"},
                  {"id": "r2", "days": ["sun"], "time": "12:00"}],
        "edits": [],
    }),
    ("edit-future-time", {
        "timezone": "America/New_York",
        "window": {"start": "2026-06-01T00:00:00Z", "end": "2026-06-22T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["wed"], "time": "19:00"}],
        "edits": [{"at": "2026-06-08T00:00:00Z", "kind": "set_time",
                   "rule_id": "r1", "time": "07:15"}],
    }),
    # Wed 2026-06-10: old 19:00 EDT fires 23:00Z (after E, invalid under old
    # epoch); new 07:00 EDT fires 11:00Z (before E=16:00Z, invalid under new
    # epoch) -> the edit steps over the date entirely.
    ("edit-step-over", {
        "timezone": "America/New_York",
        "window": {"start": "2026-06-01T00:00:00Z", "end": "2026-06-22T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["wed"], "time": "19:00"}],
        "edits": [{"at": "2026-06-10T16:00:00Z", "kind": "set_time",
                   "rule_id": "r1", "time": "07:00"}],
    }),
    # Edit at 19:00Z (15:00 EDT), after the day's 09:00 fired; new 19:30 slot
    # that same date is valid under the new epoch but the key is taken.
    ("edit-same-day-dedup", {
        "timezone": "America/New_York",
        "window": {"start": "2026-06-01T00:00:00Z", "end": "2026-06-22T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["wed"], "time": "09:00"}],
        "edits": [{"at": "2026-06-10T19:00:00Z", "kind": "set_time",
                   "rule_id": "r1", "time": "19:30"}],
    }),
    ("tz-change-mid-window", {
        "timezone": "America/New_York",
        "window": {"start": "2026-06-01T00:00:00Z", "end": "2026-06-15T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["mon", "fri"], "time": "10:00"}],
        "edits": [{"at": "2026-06-06T00:00:00Z", "kind": "set_timezone",
                   "timezone": "Asia/Tokyo"}],
    }),
    # Mon 10:00 NY fires 14:00Z; tz flips to LA at 15:00Z the same day; Mon
    # 10:00 LA = 17:00Z is valid under the new epoch but (r1, date) is taken.
    ("tz-remint-trap", {
        "timezone": "America/New_York",
        "window": {"start": "2026-06-01T00:00:00Z", "end": "2026-06-12T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["mon"], "time": "10:00"}],
        "edits": [{"at": "2026-06-08T15:00:00Z", "kind": "set_timezone",
                   "timezone": "America/Los_Angeles"}],
    }),
    # 2026-06-04 is a Thursday; 20:00 EDT = 2026-06-05T00:00:00Z == window
    # start (included). 2026-06-18 20:00 EDT = 2026-06-19T00:00:00Z == window
    # end (excluded).
    ("window-boundaries", {
        "timezone": "America/New_York",
        "window": {"start": "2026-06-05T00:00:00Z", "end": "2026-06-19T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["thu"], "time": "20:00"}],
        "edits": [],
    }),
]

EXAMPLES = [
    ("example-1", {
        "timezone": "America/New_York",
        "window": {"start": "2026-06-01T00:00:00Z", "end": "2026-06-08T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["tue", "sat"], "time": "08:30"}],
        "edits": [],
    }),
    ("example-2", {
        "timezone": "America/New_York",
        "window": {"start": "2026-03-06T00:00:00Z", "end": "2026-03-12T00:00:00Z"},
        "rules": [{"id": "r1", "days": ["sun", "tue"], "time": "02:30"}],
        "edits": [],
    }),
]


def main():
    goldens = HERE / "goldens"
    examples = HERE / "examples"
    goldens.mkdir(exist_ok=True)
    examples.mkdir(exist_ok=True)
    for name, scenario in SCENARIOS:
        expected = evaluate(scenario)
        (goldens / f"{name}.json").write_text(
            json.dumps({"name": name, "input": scenario, "expected": expected},
                       indent=2))
        print(f"golden {name}: {len(expected['occurrences'])} occurrences")
    for name, scenario in EXAMPLES:
        (examples / f"{name}.json").write_text(json.dumps(scenario, indent=2))
        (examples / f"{name}.expected.json").write_text(
            json.dumps(evaluate(scenario), indent=2))
        print(f"example {name}: {len(evaluate(scenario)['occurrences'])} occurrences")


if __name__ == "__main__":
    main()
