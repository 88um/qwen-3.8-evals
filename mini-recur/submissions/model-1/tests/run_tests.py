#!/usr/bin/env python3
"""Test runner for the model-1 recur submission.

Runs ../recur against every scenario in cases/ (and the two shared
examples, read-only) and compares parsed JSON structures.
"""
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[3]
RECUR = pathlib.Path(__file__).resolve().parents[1] / "recur"
CASES = pathlib.Path(__file__).resolve().parent / "cases"


def run(scenario_text):
    p = subprocess.run([str(RECUR)], input=scenario_text,
                       capture_output=True, text=True)
    return p


def check(label, scenario_path, expected_path):
    p = run(scenario_path.read_text())
    if p.returncode != 0:
        print(f"FAIL {label}: exit {p.returncode} stderr={p.stderr!r}")
        return False
    try:
        got = json.loads(p.stdout)
    except Exception as e:
        print(f"FAIL {label}: unparseable stdout: {e}: {p.stdout!r}")
        return False
    exp = json.loads(expected_path.read_text())
    if got != exp:
        print(f"FAIL {label}:\n got: {json.dumps(got)}\n exp: {json.dumps(exp)}")
        return False
    print(f"PASS {label}")
    return True


def main():
    ok = True
    for name in ("example-1", "example-2"):
        ok &= check(
            f"shared-{name}",
            ROOT / "examples" / f"{name}.json",
            ROOT / "examples" / f"{name}.expected.json",
        )
    for case in sorted(CASES.glob("*.json")):
        if case.name.endswith(".expected.json"):
            continue
        exp = case.with_name(case.name[:-5] + ".expected.json")
        ok &= check(f"case-{case.stem}", case, exp)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
