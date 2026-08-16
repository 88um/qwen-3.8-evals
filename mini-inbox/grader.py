#!/usr/bin/env python3
"""Convergence grader for mini-inbox submissions. OPERATOR ONLY.

Usage: python3 grader.py submissions/model-a [...] [--runs N] [--events N]
       [--interval S] [--kills N]

Per run: starts the provider (seeded chaos: duplicates, stale stragglers,
out-of-order delivery), starts the submission's consumer, SIGKILLs the
consumer's process group N times mid-stream, waits for the stream to drain,
then polls the consumer until its /state/all matches the provider's /truth
(token-protected) or the settle deadline passes.

Scoring: 100 − 10 per mismatched/missing account at the deadline. PASS =
exact convergence on every touched account. Canonical-call count and 429s
are reported as design signals but not scored.

Load profile: two small HTTP servers plus the consumer, a few kills, mostly
sleeps. ~3–4 min per run at defaults. Self-nices (GRADER_NICE, default 10).
"""
import argparse
import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROVIDER = HERE / "provider.py"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url, timeout=10, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def wait_health(base, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            http_json(base + "/health", timeout=2)
            return True
        except OSError:
            time.sleep(0.3)
    return False


def kill_group(proc):
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def diff_states(truth, touched, consumer_state):
    accounts = (consumer_state or {}).get("accounts") or {}
    mismatched = []
    for acct in touched:
        got = accounts.get(acct)
        want = truth[acct]
        if not isinstance(got, dict) or got.get("plan") != want["plan"] \
                or got.get("status") != want["status"]:
            mismatched.append(acct)
    return mismatched


def run_once(sub_dir, name, run_idx, opts):
    rng = random.Random(opts.seed + run_idx * 31)
    run_dir = HERE / "results" / name / f"run-{run_idx}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    state_dir = run_dir / "state"
    state_dir.mkdir()
    report = {"submission": name, "run": run_idx, "kills": 0,
              "mismatched": None, "aborted": None, "score": 0,
              "verdict": "FAIL", "settled_after_s": None}

    inbox = Path(sub_dir) / "inbox"
    if not (inbox.is_file() and os.access(inbox, os.X_OK)):
        report["aborted"] = "no executable ./inbox"
        return finish(report, run_dir)

    token = f"tt-{rng.getrandbits(64):016x}"
    p_port, c_port = free_port(), free_port()
    p_base, c_base = f"http://127.0.0.1:{p_port}", f"http://127.0.0.1:{c_port}"
    p_log = open(run_dir / "provider.log", "w")
    c_log = open(run_dir / "consumer.log", "w")

    p_env = os.environ.copy()
    p_env.update(PORT=str(p_port), SEED=str(opts.seed + run_idx * 31),
                 CONSUMER_URL=c_base, TRUTH_TOKEN=token,
                 EVENTS=str(opts.events), EVENT_INTERVAL=str(opts.interval))
    c_env = os.environ.copy()
    c_env.update(PORT=str(c_port), PROVIDER_URL=p_base,
                 INBOX_STATE_DIR=str(state_dir))

    provider = subprocess.Popen([sys.executable, str(PROVIDER)], env=p_env,
                                stdout=p_log, stderr=p_log)
    consumer = None
    truth_hdr = {"X-Truth-Token": token}
    try:
        if not wait_health(p_base):
            report["aborted"] = "provider failed to start"
            return finish(report, run_dir)

        def start_consumer():
            return subprocess.Popen(["./inbox", "serve"], cwd=sub_dir,
                                    env=c_env, stdout=c_log, stderr=c_log,
                                    start_new_session=True)

        consumer = start_consumer()
        if not wait_health(c_base):
            report["aborted"] = "consumer did not come up in 15s"
            return finish(report, run_dir)

        # chaos phase: SIGKILL the consumer opts.kills times mid-stream
        stream_seconds = opts.events * opts.interval + 10
        stream_end = time.time() + stream_seconds
        kills_left = opts.kills
        next_kill = time.time() + rng.uniform(15, 30)
        while time.time() < stream_end:
            if consumer.poll() is not None:
                consumer = start_consumer()
            if kills_left and time.time() >= next_kill:
                kill_group(consumer)
                report["kills"] += 1
                kills_left -= 1
                time.sleep(rng.uniform(1, 2))
                consumer = start_consumer()
                next_kill = time.time() + rng.uniform(15, 30)
            time.sleep(1)

        # wait for the provider to drain its delivery queue
        drain_deadline = time.time() + 120
        truth = None
        while time.time() < drain_deadline:
            truth = http_json(p_base + "/truth", headers=truth_hdr)
            if truth["stats"]["done"]:
                break
            if consumer.poll() is not None:
                consumer = start_consumer()
            time.sleep(3)

        # settle: poll until exact convergence or deadline
        settle_start = time.time()
        settle_deadline = settle_start + opts.settle
        mismatched = None
        while time.time() < settle_deadline:
            if consumer.poll() is not None:
                consumer = start_consumer()
            truth = http_json(p_base + "/truth", headers=truth_hdr)
            try:
                state = http_json(c_base + "/state/all", timeout=5)
            except (OSError, json.JSONDecodeError):
                state = None
            mismatched = diff_states(truth["truth"], truth["touched"], state)
            if not mismatched:
                report["settled_after_s"] = round(time.time() - settle_start, 1)
                break
            time.sleep(8)

        report["mismatched"] = mismatched
        report["provider_stats"] = truth["stats"] if truth else None
        return finish(report, run_dir)
    finally:
        if consumer is not None:
            kill_group(consumer)
        provider.terminate()
        p_log.close()
        c_log.close()


def finish(report, run_dir):
    if report["aborted"]:
        report["score"], report["verdict"] = 0, "ABORTED"
    else:
        misses = len(report["mismatched"] or [])
        report["score"] = max(0, 100 - 10 * misses)
        report["verdict"] = "PASS" if misses == 0 else "FAIL"
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
    p.add_argument("--events", type=int, default=60)
    p.add_argument("--interval", type=float, default=1.5)
    p.add_argument("--kills", type=int, default=3)
    p.add_argument("--settle", type=float, default=150)
    p.add_argument("--seed", type=int, default=2000)
    opts = p.parse_args()

    rows = []
    for sub in opts.submissions:
        sub_dir = Path(sub).resolve()
        for i in range(opts.runs):
            print(f"=== {sub_dir.name} run {i} ===", flush=True)
            r = run_once(sub_dir, sub_dir.name, i, opts)
            rows.append(r)
            stats = r.get("provider_stats") or {}
            print(f"  {r['verdict']}  score={r['score']}  "
                  f"mismatched={r['mismatched'] or 'none'}  "
                  f"kills={r['kills']}  "
                  f"settled_after={r['settled_after_s']}s  "
                  f"canonical_calls={stats.get('subscription_requests')}  "
                  f"429s={stats.get('rate_429s')}"
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
