#!/usr/bin/env python3
"""End-to-end crash-safety test.

Runs the shared receiver on a private port, drives `publisher` through
enqueue/status/daemon while repeatedly SIGKILLing the daemon's process group,
then verifies at-most-once and nothing-lost against receiver ground truth.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
RECEIVER = os.path.normpath(os.path.join(ROOT, "..", "..", "receiver.py"))
PUBLISHER = os.path.join(ROOT, "publisher")

N_MESSAGES = 25
KILL_WINDOW_S = 60.0
KILL_INTERVAL_S = 1.5
SETTLE_TIMEOUT_S = 180.0


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_health(url, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/health", timeout=2) as r:
                if r.getcode() == 200:
                    return True
        except Exception:
            time.sleep(0.2)
    return False


def start_daemon(env, log):
    return subprocess.Popen(
        [PUBLISHER, "daemon"],
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def kill_daemon(daemon):
    try:
        os.killpg(os.getpgid(daemon.pid), signal.SIGKILL)
    except ProcessLookupError:
        pass
    daemon.wait()


def enqueue(env, mid, payload):
    p = subprocess.run([PUBLISHER, "enqueue", mid, payload], env=env, capture_output=True)
    assert p.returncode == 0, p.stderr


def status(env):
    p = subprocess.run([PUBLISHER, "status", "--json"], env=env, capture_output=True)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def main():
    port = free_port()
    state_dir = tempfile.mkdtemp(prefix="test-state-", dir=ROOT)
    env = dict(os.environ)
    env.update({
        "PUBLISHER_STATE_DIR": state_dir,
        "RECEIVER_URL": "http://127.0.0.1:%d" % port,
    })

    receiver = subprocess.Popen(
        [sys.executable, RECEIVER],
        env=dict(os.environ, PORT=str(port), SEED=str(int(time.time()))),
    )
    log = open(os.path.join(state_dir, "daemon.log"), "w")
    daemon = None
    try:
        assert wait_health("http://127.0.0.1:%d" % port), "receiver failed to start"

        daemon = start_daemon(env, log)

        ids = ["msg-%03d" % i for i in range(N_MESSAGES)]
        for i, mid in enumerate(ids):
            enqueue(env, mid, "payload %d" % i)
        enqueue(env, ids[0], "duplicate attempt")

        killed = 0
        extra = 0
        deadline = time.time() + KILL_WINDOW_S
        while time.time() < deadline:
            time.sleep(KILL_INTERVAL_S)
            kill_daemon(daemon)
            killed += 1
            if killed % 4 == 0:
                extra += 1
                enqueue(env, "late-%03d" % extra, "late payload %d" % extra)
            time.sleep(0.3)
            daemon = start_daemon(env, log)

        all_ids = ids + ["late-%03d" % i for i in range(1, extra + 1)]

        deadline = time.time() + SETTLE_TIMEOUT_S
        final = None
        while time.time() < deadline:
            final = status(env)
            if all(m["state"] in ("delivered", "failed") for m in final["messages"]):
                break
            time.sleep(2.0)

        kill_daemon(daemon)
        daemon = None

        with urllib.request.urlopen(
            "http://127.0.0.1:%d/dump" % port, timeout=5
        ) as r:
            dump = json.loads(r.read())
        counts = {}
        for a in dump["accepted"]:
            counts[a["message_id"]] = counts.get(a["message_id"], 0) + 1

        assert final is not None
        by_id = {m["id"]: m for m in final["messages"]}
        assert set(by_id) == set(all_ids), set(by_id) ^ set(all_ids)

        failures = []
        for mid in all_ids:
            st = by_id[mid]
            c = counts.get(mid, 0)
            if c >= 2:
                failures.append("%s: DUPLICATE accepted_count=%d" % (mid, c))
            if st["state"] == "delivered" and c != 1:
                failures.append("%s: delivered but accepted_count=%d" % (mid, c))
            if st["state"] == "failed" and (c != 0 or st["attempts"] < 10):
                failures.append(
                    "%s: failed but count=%d attempts=%d" % (mid, c, st["attempts"])
                )
            if st["state"] not in ("delivered", "failed"):
                failures.append("%s: non-terminal state %s" % (mid, st["state"]))

        delivered = sum(1 for m in by_id.values() if m["state"] == "delivered")
        failed = sum(1 for m in by_id.values() if m["state"] == "failed")
        print("kills=%d delivered=%d failed=%d total=%d" % (killed, delivered, failed, len(all_ids)))
        if failures:
            print("FAIL")
            for f in failures:
                print(" -", f)
            return 1
        print("PASS")
        return 0
    finally:
        if daemon is not None:
            kill_daemon(daemon)
        receiver.kill()
        receiver.wait()
        log.close()
        shutil.rmtree(state_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
