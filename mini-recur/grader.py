#!/usr/bin/env python3
"""Grader for mini-recur submissions. OPERATOR ONLY — do not give to models.

Usage: python3 grader.py submissions/model-a [submissions/model-b ...]

Runs ./recur once per hidden golden scenario and compares parsed output
structurally (order-insensitive). Score = scenarios passed / total * 100.
Near-zero system load: one short-lived process per scenario, no daemons.
"""
import argparse
import json
import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent


def canon(result):
    occs = result["occurrences"]
    return sorted(
        (o["rule_id"], o["local_date"], o["local_time"],
         o["instant"], o["status"]) for o in occs
    )


def grade(sub_dir, goldens):
    recur = Path(sub_dir) / "recur"
    if not (recur.is_file() and os.access(recur, os.X_OK)):
        return {"score": 0, "passed": [], "failed": ["<no executable ./recur>"]}
    passed, failed = [], []
    for g in goldens:
        try:
            r = subprocess.run(
                ["./recur"], cwd=sub_dir, input=json.dumps(g["input"]),
                capture_output=True, text=True, timeout=30,
            )
            ok = r.returncode == 0 and canon(json.loads(r.stdout)) == canon(
                g["expected"]
            )
        except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError,
                TypeError, ValueError):
            ok = False
        (passed if ok else failed).append(g["name"])
    return {
        "score": round(100 * len(passed) / len(goldens)),
        "passed": passed,
        "failed": failed,
    }


def main():
    try:
        os.nice(int(os.environ.get("GRADER_NICE", "10")))
    except OSError:
        pass
    p = argparse.ArgumentParser()
    p.add_argument("submissions", nargs="+")
    opts = p.parse_args()

    goldens = [
        json.loads(f.read_text())
        for f in sorted((HERE / "goldens").glob("*.json"))
    ]
    if not goldens:
        raise SystemExit("no goldens — run make_goldens.py first")

    results_dir = HERE / "results"
    results_dir.mkdir(exist_ok=True)
    for sub in opts.submissions:
        sub_dir = Path(sub).resolve()
        r = grade(sub_dir, goldens)
        (results_dir / f"{sub_dir.name}.json").write_text(json.dumps(r, indent=2))
        print(f"{sub_dir.name}: {r['score']}/100  "
              f"passed={len(r['passed'])}/{len(goldens)}  "
              f"failed={r['failed'] or '—'}")


if __name__ == "__main__":
    main()
