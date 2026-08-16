#!/usr/bin/env python3
"""Integration tests for the ledger service."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError

PORT = 19100
STATE_DIR = tempfile.mkdtemp(prefix="ledger-test-")
BASE = f"http://127.0.0.1:{PORT}"
proc = None


def setup():
    global proc
    env = {**os.environ, "PORT": str(PORT), "LEDGER_STATE_DIR": STATE_DIR}
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "ledger"), "serve"],
        env=env,
    )
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{BASE}/ledger", timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Server did not start")


def teardown():
    if proc:
        proc.terminate()
        proc.wait()
    shutil.rmtree(STATE_DIR, ignore_errors=True)


def post(path, data):
    body = json.dumps(data).encode()
    req = urllib.request.Request(
        f"{BASE}{path}", data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except HTTPError as e:
        return e.code, json.loads(e.read())


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as resp:
        return resp.status, json.loads(resp.read())


def test_basic_flow():
    code, _ = post("/account", {"account_id": "a1", "initial": 100})
    assert code == 200

    code, _ = post("/account", {"account_id": "a1", "initial": 100})
    assert code == 200

    code, b = get("/account?account_id=a1")
    assert b["balance"] == 100 and b["available"] == 100

    code, _ = post("/topup", {"account_id": "a1", "amount": 50, "key": "k1"})
    assert code == 200
    code, _ = post("/topup", {"account_id": "a1", "amount": 50, "key": "k1"})
    assert code == 200

    _, b = get("/account?account_id=a1")
    assert b["balance"] == 150 and b["available"] == 150

    code, b = post("/hold", {"account_id": "a1", "ref": "r1", "amount": 60})
    assert code == 200 and b["held"] is True
    _, b = get("/account?account_id=a1")
    assert b["balance"] == 150 and b["available"] == 90

    code, _ = post("/charge", {"ref": "r1"})
    assert code == 200
    _, b = get("/account?account_id=a1")
    assert b["balance"] == 90 and b["available"] == 90

    code, _ = post("/charge", {"ref": "r1"})
    assert code == 200
    code, b = post("/release", {"ref": "r1"})
    assert code == 409
    print("  PASS test_basic_flow")


def test_release_flow():
    post("/account", {"account_id": "a3", "initial": 100})
    post("/hold", {"account_id": "a3", "ref": "r3", "amount": 30})
    code, _ = post("/release", {"ref": "r3"})
    assert code == 200
    _, b = get("/account?account_id=a3")
    assert b["balance"] == 100 and b["available"] == 100
    code, _ = post("/release", {"ref": "r3"})
    assert code == 200
    code, _ = post("/charge", {"ref": "r3"})
    assert code == 409
    print("  PASS test_release_flow")


def test_hold_insufficient():
    post("/account", {"account_id": "a2", "initial": 10})
    code, b = post("/hold", {"account_id": "a2", "ref": "r2", "amount": 20})
    assert code == 409 and b["error"] == "insufficient"
    print("  PASS test_hold_insufficient")


def test_duplicate_ref():
    post("/account", {"account_id": "a5", "initial": 100})
    post("/hold", {"account_id": "a5", "ref": "r5", "amount": 10})
    code, b = post("/hold", {"account_id": "a5", "ref": "r5", "amount": 20})
    assert code == 409 and b["error"] == "duplicate_ref"
    code, b = post("/hold", {"account_id": "a5", "ref": "r5", "amount": 10})
    assert code == 200
    print("  PASS test_duplicate_ref")


def test_topup_unknown():
    code, _ = post("/topup", {"account_id": "nope", "amount": 10, "key": "k99"})
    assert code == 404
    print("  PASS test_topup_unknown")


def test_unknown_ref():
    code, _ = post("/charge", {"ref": "no-such-ref"})
    assert code == 404
    code, _ = post("/release", {"ref": "no-such-ref"})
    assert code == 404
    print("  PASS test_unknown_ref")


def test_concurrent_holds():
    post("/account", {"account_id": "a4", "initial": 100})
    results = []

    def do_hold(ref):
        return post("/hold", {"account_id": "a4", "ref": ref, "amount": 80})

    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = [pool.submit(do_hold, f"ch-{i}") for i in range(4)]
        results = [f.result() for f in as_completed(futs)]

    codes = sorted(r[0] for r in results)
    assert codes.count(200) == 1, f"Expected exactly 1 success, got {codes}"
    assert codes.count(409) == 3
    _, b = get("/account?account_id=a4")
    assert b["available"] == 20
    print("  PASS test_concurrent_holds")


def test_charge_vs_release_race():
    post("/account", {"account_id": "a6", "initial": 100})
    post("/hold", {"account_id": "a6", "ref": "race1", "amount": 40})

    with ThreadPoolExecutor(max_workers=2) as pool:
        fc = pool.submit(post, "/charge", {"ref": "race1"})
        fr = pool.submit(post, "/release", {"ref": "race1"})
        rc = fc.result()
        rr = fr.result()

    codes = sorted([rc[0], rr[0]])
    assert codes == [200, 409], f"Expected [200, 409], got {codes}"
    print("  PASS test_charge_vs_release_race")


def test_ledger_integrity():
    _, body = get("/ledger")
    entries = body["entries"]
    seqs = [e["seq"] for e in entries]
    assert seqs == sorted(seqs) and len(seqs) == len(set(seqs))

    for e in entries:
        if e["kind"] in ("hold", "charge", "release"):
            assert e["ref"] is not None
        if e["kind"] == "topup":
            assert e["key"] is not None
    print("  PASS test_ledger_integrity")


if __name__ == "__main__":
    setup()
    try:
        test_basic_flow()
        test_release_flow()
        test_hold_insufficient()
        test_duplicate_ref()
        test_topup_unknown()
        test_unknown_ref()
        test_concurrent_holds()
        test_charge_vs_release_race()
        test_ledger_integrity()
        print("\nAll tests passed.")
    finally:
        teardown()
