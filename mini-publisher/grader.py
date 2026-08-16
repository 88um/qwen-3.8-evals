#!/usr/bin/env python3
"""Chaos grader for mini-publisher submissions. OPERATOR ONLY — do not give to models.

Usage:
  python3 grader.py submissions/model-a [submissions/model-b ...] [options]

Options:
  --runs N        chaos runs per submission (default 3)
  --messages N    messages per run (default 50)
  --chaos S       chaos-phase seconds with random SIGKILLs (default 480)
  --settle S      max settle seconds for the daemon to finish/reconcile (default 480)
  --seed N        base seed; run i uses seed N + i*17 (default 1000)
  --smoke         quick contract check: 5 msgs, reliable receiver, ~2 kills

Per message, final state is diffed against the receiver's ground truth:
  accepted_count >= 2                      -> DUPLICATE        -20  (the cardinal sin)
  state=delivered but accepted_count == 0  -> FALSE_DELIVERED  -10
  state=failed    but accepted_count >= 1  -> FALSE_FAILED     -10
  missing from status --json               -> LOST_RECORD      -10
  non-terminal after settle                -> STUCK             -5
  state=failed with accepted_count == 0    -> GAVE_UP           -3
  persistent enqueue CLI failure           -> ENQUEUE_FAIL      -5
Score = max(0, 100 - penalties). PASS requires zero duplicates, false claims,
lost records, and stuck messages. Results land in results/<name>/run-<i>/.
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
RECEIVER = HERE / "receiver.py"
TERMINAL = {"delivered", "failed"}
VALID_STATES = {"pending", "delivering", "uncertain", "delivered", "failed"}

PENALTIES = {
    "duplicates": 20,
    "false_delivered": 10,
    "false_failed": 10,
    "lost_records": 10,
    "stuck": 5,
    "gave_up": 3,
    "enqueue_failures": 5,
}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def http_json(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read())


def start_receiver(port, seed, reliable, log):
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["SEED"] = str(seed)
    if reliable:
        env.update(P_ACCEPT="1.0", P_REJECT="0.0", P_TIMEOUT="0.0")
    proc = subprocess.Popen(
        [sys.executable, str(RECEIVER)], env=env, stdout=log, stderr=log
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(50):
        try:
            http_json(base + "/health", timeout=2)
            return proc, base
        except OSError:
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("receiver failed to start")


def start_daemon(sub_dir, env, log):
    return subprocess.Popen(
        ["./publisher", "daemon"],
        cwd=sub_dir,
        env=env,
        stdout=log,
        stderr=log,
        start_new_session=True,  # so SIGKILL hits the whole group (power-loss emulation)
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


def cli(sub_dir, env, *args, timeout=20):
    return subprocess.run(
        ["./publisher", *args],
        cwd=sub_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def try_enqueue(sub_dir, env, mid, counters):
    for _ in range(3):
        try:
            if cli(sub_dir, env, "enqueue", mid, f"payload-{mid}").returncode == 0:
                return True
        except (subprocess.TimeoutExpired, OSError):
            pass
        time.sleep(1)
    counters["enqueue_failures"] += 1
    return False


def get_status(sub_dir, env):
    """Returns {message_id: state} or None if the CLI/JSON contract is broken."""
    for _ in range(3):
        try:
            r = cli(sub_dir, env, "status", "--json")
            if r.returncode == 0:
                data = json.loads(r.stdout)
                return {m["id"]: m.get("state", "?") for m in data["messages"]}
        except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError,
                KeyError, TypeError):
            pass
        time.sleep(2)
    return None


def run_once(sub_dir, name, run_idx, opts):
    rng = random.Random(opts.seed + run_idx * 17)
    run_dir = HERE / "results" / name / f"run-{run_idx}"
    if run_dir.exists():  # stale state from a prior invocation would corrupt the diff
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    state_dir = run_dir / "state"
    state_dir.mkdir()

    report = {
        "submission": name, "run": run_idx, "seed": opts.seed + run_idx * 17,
        "kills": 0, "unexpected_daemon_exits": 0, "aborted": None,
        "counters": {k: 0 for k in PENALTIES}, "settled": False,
        "score": 0, "verdict": "FAIL",
    }
    counters = report["counters"]

    publisher = Path(sub_dir) / "publisher"
    if not (publisher.is_file() and os.access(publisher, os.X_OK)):
        report["aborted"] = "no executable ./publisher in submission dir"
        return finish(report, run_dir, {}, {})

    rec_log = open(run_dir / "receiver.log", "w")
    daemon_log = open(run_dir / "daemon.log", "w")
    receiver = daemon = None
    try:
        receiver, base = start_receiver(
            free_port(), opts.seed + run_idx * 17, opts.smoke, rec_log
        )
        env = os.environ.copy()
        env["PUBLISHER_STATE_DIR"] = str(state_dir)
        env["RECEIVER_URL"] = base

        ids = [f"msg-{i:03d}" for i in range(opts.messages)]
        batch = max(1, opts.messages // 10)
        enq_interval = max(2.0, (opts.chaos * 0.6) / max(1, opts.messages / batch))

        # --- chaos phase: enqueue in waves while SIGKILLing the daemon ---
        deadline = time.time() + opts.chaos
        next_enqueue, sent = 0.0, 0
        kill_at = None
        started_at = 0.0
        fast_exits = 0
        while time.time() < deadline:
            if daemon is None or daemon.poll() is not None:
                if daemon is not None:  # exited on its own
                    report["unexpected_daemon_exits"] += 1
                    fast_exits = fast_exits + 1 if time.time() - started_at < 2 else 0
                    if fast_exits >= 5:
                        report["aborted"] = "daemon crashed within 2s on 5 consecutive starts"
                        return finish(report, run_dir, {}, {})
                daemon = start_daemon(sub_dir, env, daemon_log)
                started_at = time.time()
                kill_at = started_at + rng.uniform(4, 12)
            if time.time() >= next_enqueue and sent < opts.messages:
                for mid in ids[sent : sent + batch]:
                    try_enqueue(sub_dir, env, mid, counters)
                sent += batch
                next_enqueue = time.time() + enq_interval
            if kill_at and time.time() >= kill_at:
                kill_group(daemon)
                report["kills"] += 1
                daemon, kill_at = None, None
                time.sleep(rng.uniform(0.5, 2))
            time.sleep(0.3)
        for mid in ids[sent:]:
            try_enqueue(sub_dir, env, mid, counters)

        # --- settle phase: leave the daemon up, wait for terminal states ---
        settle_deadline = time.time() + opts.settle
        status = None
        while time.time() < settle_deadline:
            if daemon is None or daemon.poll() is not None:
                if daemon is not None:
                    report["unexpected_daemon_exits"] += 1
                daemon = start_daemon(sub_dir, env, daemon_log)
            status = get_status(sub_dir, env)
            if status is not None and all(
                status.get(m) in TERMINAL for m in ids
            ):
                report["settled"] = True
                break
            time.sleep(8)
        if status is None:
            status = get_status(sub_dir, env) or {}

        truth = http_json(base + "/dump", timeout=15)
        accepted_counts = {}
        for a in truth["accepted"]:
            accepted_counts[a["message_id"]] = accepted_counts.get(a["message_id"], 0) + 1
        report["total_deliver_requests"] = len(truth["events"])

        for mid in ids:
            got = accepted_counts.get(mid, 0)
            state = status.get(mid)
            if got >= 2:
                counters["duplicates"] += 1
            if state is None or state not in VALID_STATES:
                counters["lost_records"] += 1
            elif state == "delivered" and got == 0:
                counters["false_delivered"] += 1
            elif state == "failed" and got >= 1:
                counters["false_failed"] += 1
            elif state == "failed":
                counters["gave_up"] += 1
            elif state not in TERMINAL:
                counters["stuck"] += 1
        return finish(report, run_dir, status, accepted_counts)
    finally:
        if daemon is not None:
            kill_group(daemon)
        if receiver is not None:
            receiver.terminate()
        rec_log.close()
        daemon_log.close()


def finish(report, run_dir, status, accepted_counts):
    c = report["counters"]
    if report["aborted"]:
        report["score"] = 0
        report["verdict"] = "ABORTED"
    else:
        report["score"] = max(
            0, 100 - sum(c[k] * PENALTIES[k] for k in PENALTIES)
        )
        clean = all(
            c[k] == 0
            for k in ("duplicates", "false_delivered", "false_failed",
                      "lost_records", "stuck")
        )
        report["verdict"] = "PASS" if clean else "FAIL"
    report["final_status"] = status
    report["accepted_counts"] = accepted_counts
    (run_dir / "report.json").write_text(json.dumps(report, indent=2))
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("submissions", nargs="+")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--messages", type=int, default=50)
    p.add_argument("--chaos", type=float, default=480)
    p.add_argument("--settle", type=float, default=480)
    p.add_argument("--seed", type=int, default=1000)
    p.add_argument("--smoke", action="store_true")
    opts = p.parse_args()
    if opts.smoke:
        opts.runs, opts.messages, opts.chaos, opts.settle = 1, 5, 25, 90

    rows = []
    for sub in opts.submissions:
        sub_dir = Path(sub).resolve()
        name = sub_dir.name
        for i in range(opts.runs):
            print(f"=== {name} run {i} (seed {opts.seed + i * 17}) ===", flush=True)
            r = run_once(sub_dir, name, i, opts)
            rows.append(r)
            c = r["counters"]
            print(
                f"  {r['verdict']}  score={r['score']}  dup={c['duplicates']} "
                f"falseDel={c['false_delivered']} falseFail={c['false_failed']} "
                f"lost={c['lost_records']} stuck={c['stuck']} gaveUp={c['gave_up']} "
                f"enqFail={c['enqueue_failures']} kills={r['kills']} "
                f"settled={r['settled']}"
                + (f"  ABORT: {r['aborted']}" if r["aborted"] else ""),
                flush=True,
            )

    print("\n=== summary (mean score per submission) ===")
    by_name = {}
    for r in rows:
        by_name.setdefault(r["submission"], []).append(r["score"])
    for name, scores in sorted(by_name.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        print(f"  {name}: {sum(scores) / len(scores):.1f}  (runs: {scores})")


if __name__ == "__main__":
    main()
