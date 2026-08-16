#!/usr/bin/env python3
"""Concurrency grader for mini-ledger submissions. OPERATOR ONLY.

Usage: python3 grader.py submissions/model-a [...] [--runs N]

Load profile: deliberately light — a few dozen short HTTP requests, ≤12
threads alive for under a second at a time, one SIGKILL/restart, ~45s per run.
Self-nices (GRADER_NICE, default 10) so the machine stays responsive.

Phases:
  P1  20 barrier-synced duplicate-hold pairs (same ref, two threads)
  P2  12-thread drain race on a 5-credit account (only 5 may win)
  P3  charge-vs-release races on held refs (exactly one terminal each)
  P4  verbatim replay storm (must append nothing, must return success)
  P5  SIGKILL the server's process group, restart, verify durability,
      then a few more ops must work
Verification replays the final ledger dump prefix-by-prefix against every
invariant in spec.md. PASS requires zero violations.
"""
import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent

PENALTIES = {
    "negative_available": 20, "dup_hold": 20, "dup_terminal": 20,
    "terminal_before_hold": 20, "append_only": 20, "durability_loss": 20,
    "replay_appended": 10, "replay_rejected": 5, "false_claim": 10,
    "balance_mismatch": 10, "bad_seq": 10, "dup_topup_key": 10,
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def req(base, method, path, body=None, timeout=10):
    r = urllib.request.Request(
        base + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except json.JSONDecodeError:
            return e.code, {}
    except (OSError, json.JSONDecodeError):
        return None, {}


def race(*thunks):
    """Run thunks concurrently, released simultaneously by a barrier."""
    barrier = threading.Barrier(len(thunks))
    results = [None] * len(thunks)

    def wrap(i, fn):
        barrier.wait()
        results[i] = fn()

    threads = [threading.Thread(target=wrap, args=(i, f))
               for i, f in enumerate(thunks)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return results


def start_server(sub_dir, env, log):
    return subprocess.Popen(
        ["./ledger", "serve"], cwd=sub_dir, env=env, stdout=log, stderr=log,
        start_new_session=True,
    )


def kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def wait_up(base, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        status, _ = req(base, "GET", "/ledger", timeout=3)
        if status == 200:
            return True
        time.sleep(0.3)
    return False


def replay_verify(entries, counters):
    """Replay the dump prefix-by-prefix; returns final per-account balances."""
    balance, open_holds, seen_refs, seen_terms, topup_keys = {}, {}, {}, {}, set()
    last_seq = 0
    for e in entries:
        if not isinstance(e.get("seq"), int) or e["seq"] <= last_seq:
            counters["bad_seq"] += 1
        last_seq = e.get("seq") if isinstance(e.get("seq"), int) else last_seq
        kind, acct, ref, amt = (e.get("kind"), e.get("account_id"),
                                e.get("ref"), e.get("amount") or 0)
        if kind == "account_open":
            balance.setdefault(acct, 0)
            balance[acct] += amt
        elif kind == "topup":
            if e.get("key") in topup_keys:
                counters["dup_topup_key"] += 1
            topup_keys.add(e.get("key"))
            balance[acct] = balance.get(acct, 0) + amt
        elif kind == "hold":
            if ref in seen_refs:
                counters["dup_hold"] += 1
            seen_refs[ref] = (acct, amt)
            open_holds[ref] = (acct, amt)
        elif kind in ("charge", "release"):
            if ref in seen_terms:
                counters["dup_terminal"] += 1
            elif ref not in seen_refs:
                counters["terminal_before_hold"] += 1
            seen_terms[ref] = kind
            held = open_holds.pop(ref, None)
            if kind == "charge" and held:
                balance[held[0]] = balance.get(held[0], 0) - held[1]
        # the invariant: available never negative at any prefix
        for a in balance:
            avail = balance[a] - sum(
                amt for (acct2, amt) in open_holds.values() if acct2 == a
            )
            if avail < 0:
                counters["negative_available"] += 1
                balance[a] += 10**9  # count once per event, don't cascade
    avail = {
        a: balance[a] - sum(amt for (a2, amt) in open_holds.values() if a2 == a)
        for a in balance
    }
    return balance, avail, seen_refs, seen_terms


def run_once(sub_dir, name, run_idx, opts):
    run_dir = HERE / "results" / name / f"run-{run_idx}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    state_dir = run_dir / "state"
    state_dir.mkdir()
    counters = {k: 0 for k in PENALTIES}
    report = {"submission": name, "run": run_idx, "counters": counters,
              "aborted": None, "score": 0, "verdict": "FAIL"}

    ledger_bin = Path(sub_dir) / "ledger"
    if not (ledger_bin.is_file() and os.access(ledger_bin, os.X_OK)):
        report["aborted"] = "no executable ./ledger"
        return finish(report, run_dir)

    log = open(run_dir / "server.log", "w")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(PORT=str(port), LEDGER_STATE_DIR=str(state_dir))
    server = start_server(sub_dir, env, log)
    snapshots = []

    def snap():
        status, body = req(base, "GET", "/ledger")
        if status == 200:
            snapshots.append(body.get("entries", []))
        return snapshots[-1] if snapshots else []

    try:
        if not wait_up(base):
            report["aborted"] = "server did not come up in 15s"
            return finish(report, run_dir)

        accounts = [f"a{i}" for i in range(6)]
        for a in accounts:
            req(base, "POST", "/account", {"account_id": a, "initial": 10})
        for i, a in enumerate(accounts):
            req(base, "POST", "/topup",
                {"account_id": a, "amount": 3, "key": f"t-{i}"})
        req(base, "POST", "/account", {"account_id": "drain", "initial": 5})
        snap()

        # P1: barrier-synced duplicate-hold pairs
        held_refs = []
        for i in range(20):
            ref, acct = f"dup-{i}", accounts[i % 6]
            body = {"account_id": acct, "ref": ref, "amount": 2}
            results = race(lambda b=body: req(base, "POST", "/hold", b),
                           lambda b=body: req(base, "POST", "/hold", b))
            if any(r and r[0] == 200 for r in results):
                held_refs.append(ref)

        # P2: drain race — 12 racing 1-credit holds against 5 credits
        drain_results = race(*[
            (lambda j=j: req(base, "POST", "/hold",
                             {"account_id": "drain", "ref": f"drain-{j}",
                              "amount": 1}))
            for j in range(12)
        ])
        drain_wins = {f"drain-{j}" for j, r in enumerate(drain_results)
                      if r and r[0] == 200}
        snap()

        # P3: charge-vs-release races on the first 10 held refs
        race_outcomes = {}
        for ref in held_refs[:10]:
            ch, rl = race(
                lambda r=ref: req(base, "POST", "/charge", {"ref": r}),
                lambda r=ref: req(base, "POST", "/release", {"ref": r}),
            )
            race_outcomes[ref] = {
                k for k, r in (("charge", ch), ("release", rl))
                if r and r[0] == 200
            }
        snap()

        # P4: verbatim replay storm — success required, zero appends allowed
        before = len(snap())
        replays = [("POST", "/topup",
                    {"account_id": accounts[1], "amount": 3, "key": "t-1"})]
        for ref in held_refs[10:14]:
            acct = accounts[int(ref.split("-")[1]) % 6]
            replays.append(("POST", "/hold",
                            {"account_id": acct, "ref": ref, "amount": 2}))
        charged = [r for r, k in race_outcomes.items() if k == {"charge"}]
        if charged:
            replays.append(("POST", "/charge", {"ref": charged[0]}))
        for method, path, body in replays:
            status, _ = req(base, method, path, body)
            if status != 200:
                counters["replay_rejected"] += 1
        after = len(snap())
        if after > before:
            counters["replay_appended"] += after - before

        # P5: SIGKILL the whole group, restart, durability + liveness
        pre_kill = snap()
        kill_group(server)
        server = start_server(sub_dir, env, log)
        if not wait_up(base):
            report["aborted"] = "server did not restart after SIGKILL"
            return finish(report, run_dir)
        post_kill = snap()
        if post_kill[: len(pre_kill)] != pre_kill:
            counters["durability_loss"] += 1
        req(base, "POST", "/topup",
            {"account_id": "drain", "amount": 2, "key": "t-post"})
        req(base, "POST", "/hold",
            {"account_id": "drain", "ref": "post-kill", "amount": 1})
        req(base, "POST", "/charge", {"ref": "post-kill"})

        # final verification
        final = snap()
        for i in range(1, len(snapshots)):
            a, b = snapshots[i - 1], snapshots[i]
            if b[: len(a)] != a:
                counters["append_only"] += 1
        balance, avail, seen_refs, seen_terms = replay_verify(final, counters)
        hold_refs_in_ledger = {e["ref"] for e in final if e.get("kind") == "hold"}
        for j in range(12):  # drain: 200 <-> ledger entry, 409 <-> none
            ref = f"drain-{j}"
            if (ref in drain_wins) != (ref in hold_refs_in_ledger):
                counters["false_claim"] += 1
        for ref, winners in race_outcomes.items():
            terms = {seen_terms[ref]} if ref in seen_terms else set()
            if winners != terms or len(winners) != 1:
                counters["false_claim"] += 1
        for a in list(balance):
            status, body = req(base, "GET", f"/account?account_id={a}")
            if status != 200 or body.get("balance") != balance[a] \
                    or body.get("available") != avail[a]:
                counters["balance_mismatch"] += 1
        report["entries_total"] = len(final)
        report["drain_wins"] = len(drain_wins)
        return finish(report, run_dir)
    finally:
        kill_group(server)
        log.close()


def finish(report, run_dir):
    c = report["counters"]
    if report["aborted"]:
        report["score"], report["verdict"] = 0, "ABORTED"
    else:
        report["score"] = max(0, 100 - sum(c[k] * PENALTIES[k] for k in c))
        report["verdict"] = "PASS" if all(v == 0 for v in c.values()) else "FAIL"
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    return report


def main():
    try:
        os.nice(int(os.environ.get("GRADER_NICE", "10")))
    except OSError:
        pass
    p = argparse.ArgumentParser()
    p.add_argument("submissions", nargs="+")
    p.add_argument("--runs", type=int, default=2)
    opts = p.parse_args()
    rows = []
    for sub in opts.submissions:
        sub_dir = Path(sub).resolve()
        for i in range(opts.runs):
            print(f"=== {sub_dir.name} run {i} ===", flush=True)
            r = run_once(sub_dir, sub_dir.name, i, opts)
            rows.append(r)
            nonzero = {k: v for k, v in r["counters"].items() if v}
            print(f"  {r['verdict']}  score={r['score']}  "
                  f"violations={nonzero or 'none'}"
                  + (f"  ABORT: {r['aborted']}" if r["aborted"] else ""),
                  flush=True)
    print("\n=== summary ===")
    by = {}
    for r in rows:
        by.setdefault(r["submission"], []).append(r["score"])
    for name, scores in sorted(by.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        print(f"  {name}: {sum(scores) / len(scores):.1f}  (runs: {scores})")


if __name__ == "__main__":
    main()
