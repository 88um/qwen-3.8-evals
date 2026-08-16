#!/usr/bin/env python3
"""Self-contained tests for the model-1 inbox consumer.

Spins up its own provider instance (the frozen provider.py, unmodified) and
its own consumer on ephemeral ports, passing all ports/URLs via env. Only
allowed endpoints are used: the consumer's own endpoints and the provider's
/subscription and /health. /truth is never touched.

Tests:
  1. test_webhook_ack_latency  — webhook acked within the 3 s contract.
  2. test_429_backoff          — canonical 429s are backed off, then converged.
  3. test_convergence          — exact post-settle convergence under hostile
                                  delivery; zero consumer-side 429s; sane call count.
  4. test_crash_safety         — SIGKILL mid-stream + restart still converges.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

ROOT = os.path.dirname(os.path.abspath(__file__))
INBOX = os.path.normpath(os.path.join(ROOT, "..", "inbox"))
PROVIDER = os.path.normpath(os.path.join(ROOT, "..", "..", "..", "provider.py"))

SEED = 7
EVENTS = 60
INTERVAL = 0.15
N_ACCOUNTS = 5
SETTLE_WAIT = 95.0      # fixed post-stream settle before verification
COMPARE_SPACING = 3.2   # pace our own verification fetches like a good citizen
ACCT_RE = re.compile(r"account=(acc-\d+)")


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def get_json(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


def post_json(url, obj, timeout=5):
    req = urllib.request.Request(
        url, data=json.dumps(obj).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode())


class Consumer:
    def __init__(self, state_dir, provider_url):
        self.state_dir = state_dir
        self.provider_url = provider_url
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.logfile = tempfile.mktemp(prefix="consumer-", suffix=".log")
        self.proc = None

    def start(self):
        env = dict(os.environ)
        env.update({"PORT": str(self.port),
                    "PROVIDER_URL": self.provider_url,
                    "INBOX_STATE_DIR": self.state_dir})
        fd = os.open(self.logfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        self.proc = subprocess.Popen(
            [sys.executable, INBOX, "serve"], env=env,
            stdout=subprocess.DEVNULL, stderr=fd)
        os.close(fd)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if get_json(self.url + "/health")[0] == 200:
                    return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("consumer did not start")

    def log(self):
        try:
            with open(self.logfile) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def stop(self, kill=False):
        if self.proc is None or self.proc.poll() is not None:
            return
        if kill:
            os.kill(self.proc.pid, signal.SIGKILL)
        else:
            self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except Exception:
            os.kill(self.proc.pid, signal.SIGKILL)
            self.proc.wait()


class Provider:
    def __init__(self, consumer_url, seed, events, interval, n_accounts):
        self.consumer_url = consumer_url
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.seed = seed
        self.events = events
        self.interval = interval
        self.n_accounts = n_accounts
        self.logfile = tempfile.mktemp(prefix="provider-", suffix=".log")
        self.proc = None

    def start(self):
        env = dict(os.environ)
        env.update({"PORT": str(self.port),
                    "CONSUMER_URL": self.consumer_url,
                    "SEED": str(self.seed),
                    "EVENTS": str(self.events),
                    "EVENT_INTERVAL": str(self.interval),
                    "N_ACCOUNTS": str(self.n_accounts)})
        env.pop("TRUTH_TOKEN", None)
        fd = os.open(self.logfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        self.proc = subprocess.Popen(
            [sys.executable, PROVIDER], env=env, stdout=fd, stderr=fd)
        os.close(fd)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                if get_json(self.url + "/health")[0] == 200:
                    return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError("provider did not start")

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGKILL)
            self.proc.wait()


class MockCanonical:
    """Tiny canonical endpoint: first two calls 429 (retry_after=5), then truth."""

    def __init__(self, truth):
        self.truth = truth
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.calls = []
        self._server = None

    def start(self):
        calls, truth = self.calls, self.truth

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                if u.path != "/subscription":
                    body = b"{}"
                    code = 404
                else:
                    calls.append(time.time())
                    if len(calls) < 3:
                        code = 429
                        body = json.dumps({"error": "rate_limited", "retry_after": 5}).encode()
                    else:
                        code = 200
                        body = json.dumps(truth).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def stop(self):
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def compare_state(consumer_url, provider_url, accounts):
    """Paced comparison of consumer /state/all against canonical truth."""
    _, state = get_json(consumer_url + "/state/all")
    mine = state.get("accounts", {})
    results = {}
    for aid in sorted(accounts):
        truth = None
        for _ in range(10):
            try:
                code, body = get_json(f"{provider_url}/subscription?account_id={aid}")
            except Exception:
                code, body = None, None
            if code == 200:
                truth = body
                break
            if code == 429:
                time.sleep(float((body or {}).get("retry_after", 5)))
                continue
            time.sleep(3)
        time.sleep(COMPARE_SPACING)
        got = mine.get(aid)
        ok = (truth is not None and got is not None
              and got.get("plan") == truth.get("plan")
              and got.get("status") == truth.get("status"))
        results[aid] = {"ok": ok, "mine": got, "truth": truth}
    return results


def touched_accounts(*logs):
    out = set()
    for text in logs:
        out.update(ACCT_RE.findall(text))
    return sorted(out)


def test_webhook_ack_latency():
    state_dir = tempfile.mkdtemp(prefix="inbox-state-")
    consumer = Consumer(state_dir, "http://127.0.0.1:1")  # dead provider: ack must not care
    consumer.start()
    try:
        t0 = time.time()
        code, _ = post_json(consumer.url + "/webhook", {
            "event_id": "ev-test-1", "account_id": "acc-0",
            "type": "subscription_updated",
            "data": {"plan": "pro", "status": "active"},
            "created_at": time.time()})
        elapsed = time.time() - t0
        assert code == 200, f"webhook returned {code}"
        assert elapsed < 3.0, f"webhook ack took {elapsed:.2f}s"
        # duplicate delivery must be idempotent and still fast
        t0 = time.time()
        code, _ = post_json(consumer.url + "/webhook", {
            "event_id": "ev-test-1", "account_id": "acc-0",
            "type": "subscription_updated",
            "data": {"plan": "pro", "status": "active"},
            "created_at": time.time()})
        assert code == 200 and time.time() - t0 < 3.0
    finally:
        consumer.stop()
    print(f"  ack latency ok")


def test_429_backoff():
    truth = {"account_id": "acc-7", "plan": "pro", "status": "active"}
    mock = MockCanonical(truth)
    mock.start()
    state_dir = tempfile.mkdtemp(prefix="inbox-state-")
    consumer = Consumer(state_dir, mock.url)
    consumer.start()
    try:
        post_json(consumer.url + "/webhook", {
            "event_id": "ev-test-2", "account_id": "acc-7",
            "type": "subscription_updated",
            "data": {"plan": "basic", "status": "active"},
            "created_at": time.time()})
        deadline = time.time() + 45
        got = None
        while time.time() < deadline:
            _, state = get_json(consumer.url + "/state/all")
            got = state.get("accounts", {}).get("acc-7")
            if got is not None:
                break
            time.sleep(1)
        assert got is not None, "consumer never converged after backoffs"
        assert got == {k: truth[k] for k in ("plan", "status")}, f"state {got} != truth"
        assert len(mock.calls) >= 3, f"expected >=3 canonical calls, saw {len(mock.calls)}"
        gap1 = mock.calls[1] - mock.calls[0]
        gap2 = mock.calls[2] - mock.calls[1]
        assert gap1 >= 4.0, f"first backoff gap {gap1:.2f}s < 4s"
        assert gap2 >= 4.0, f"second backoff gap {gap2:.2f}s < 4s"
        print(f"  backoff gaps ok ({gap1:.1f}s, {gap2:.1f}s), converged")
    finally:
        consumer.stop()
        mock.stop()


def run_stream(kill_at):
    """Run one full hostile stream; optionally SIGKILL+restart the consumer.
    Returns (consumer_logs, compare_results)."""
    state_dir = tempfile.mkdtemp(prefix="inbox-state-")
    consumer = Consumer(state_dir, "")  # url filled once provider port known
    consumer_port = consumer.port
    provider = Provider(f"http://127.0.0.1:{consumer_port}", SEED, EVENTS,
                        INTERVAL, N_ACCOUNTS)
    consumer.provider_url = provider.url
    consumer.start()
    provider.start()
    stream_start = time.time()
    logs = []
    try:
        if kill_at is not None:
            time.sleep(kill_at)
            logs.append(consumer.log())
            consumer.stop(kill=True)
            consumer = Consumer(state_dir, provider.url)
            consumer.start()
        deadline = stream_start + SETTLE_WAIT
        while time.time() < deadline:
            time.sleep(2)
        logs.append(consumer.log())
        accounts = touched_accounts(*logs)
        assert accounts, "no webhooks were ever delivered"
        results = compare_state(consumer.url, provider.url, accounts)
        return logs, results
    finally:
        consumer.stop()
        provider.stop()


def check_convergence(name, logs, results):
    bad = {a: r for a, r in results.items() if not r["ok"]}
    assert not bad, f"{name}: mismatched accounts: {json.dumps(bad, indent=1)}"
    four29 = sum(text.count("429 on") for text in logs)
    assert four29 == 0, f"{name}: consumer hit {four29} 429s (pacing failure)"
    calls = sum(len(re.findall(r"reconciled #|429 on|canonical fetch|provider unreachable", t))
                for t in logs)
    assert calls < 60, f"{name}: {calls} canonical calls (not budget-sane)"
    print(f"  converged on {len(results)} accounts, {calls} canonical calls, zero consumer-side 429s")


def test_convergence():
    logs, results = run_stream(kill_at=None)
    check_convergence("convergence", logs, results)


def test_crash_safety():
    logs, results = run_stream(kill_at=5.0)
    check_convergence("crash-safety", logs, results)


def main():
    tests = [test_webhook_ack_latency, test_429_backoff,
             test_convergence, test_crash_safety]
    failed = []
    for t in tests:
        name = t.__name__
        print(f"{name} ...", flush=True)
        t0 = time.time()
        try:
            t()
            print(f"  PASS ({time.time() - t0:.0f}s)\n", flush=True)
        except AssertionError as e:
            failed.append((name, str(e)))
            print(f"  FAIL: {e}\n", flush=True)
        except Exception as e:
            failed.append((name, repr(e)))
            print(f"  ERROR: {e!r}\n", flush=True)
    if failed:
        print("FAILED:", ", ".join(n for n, _ in failed))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
