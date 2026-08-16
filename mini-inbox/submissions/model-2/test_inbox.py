#!/usr/bin/env python3
"""Integration tests for the inbox service."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

INBOX_PORT = 19200
PROVIDER_PORT = 19201
STATE_DIR = tempfile.mkdtemp(prefix="inbox-test-")
inbox_proc = None


class FakeProvider(BaseHTTPRequestHandler):
    truth = {
        "acc-0": {"plan": "pro", "status": "active"},
        "acc-1": {"plan": "max", "status": "canceled"},
    }

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/subscription":
            aid = (parse_qs(u.query).get("account_id") or [None])[0]
            if aid in self.truth:
                body = json.dumps({"account_id": aid, **self.truth[aid]}).encode()
                self.send_response(200)
            else:
                body = json.dumps({"error": "not found"}).encode()
                self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/health":
            body = b'{"ok":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


def start_provider():
    server = ThreadingHTTPServer(("127.0.0.1", PROVIDER_PORT), FakeProvider)
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server


def start_inbox():
    global inbox_proc
    env = {
        **os.environ,
        "PORT": str(INBOX_PORT),
        "INBOX_STATE_DIR": STATE_DIR,
        "PROVIDER_URL": f"http://127.0.0.1:{PROVIDER_PORT}",
    }
    inbox_proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "inbox"), "serve"],
        env=env,
    )
    for _ in range(50):
        try:
            urllib.request.urlopen(
                f"http://127.0.0.1:{INBOX_PORT}/health", timeout=1
            )
            return
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("Inbox did not start")


def post_webhook(event):
    body = json.dumps(event).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{INBOX_PORT}/webhook",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return resp.status


def get_state():
    with urllib.request.urlopen(
        f"http://127.0.0.1:{INBOX_PORT}/state/all", timeout=5
    ) as resp:
        return json.loads(resp.read())


def test_webhook_accept():
    code = post_webhook({
        "event_id": "ev-0", "account_id": "acc-0",
        "type": "subscription_updated",
        "data": {"plan": "basic", "status": "active"},
        "created_at": 1000.0,
    })
    assert code == 200
    state = get_state()
    assert "acc-0" in state["accounts"]
    print("  PASS test_webhook_accept")


def test_duplicate_event():
    post_webhook({
        "event_id": "ev-1", "account_id": "acc-1",
        "type": "subscription_updated",
        "data": {"plan": "pro", "status": "active"},
        "created_at": 2000.0,
    })
    s1 = get_state()
    post_webhook({
        "event_id": "ev-1", "account_id": "acc-1",
        "type": "subscription_updated",
        "data": {"plan": "pro", "status": "active"},
        "created_at": 2000.0,
    })
    s2 = get_state()
    assert s1 == s2
    print("  PASS test_duplicate_event")


def test_stale_event_ignored():
    post_webhook({
        "event_id": "ev-new", "account_id": "acc-0",
        "type": "subscription_updated",
        "data": {"plan": "max", "status": "active"},
        "created_at": 9000.0,
    })
    post_webhook({
        "event_id": "ev-stale", "account_id": "acc-0",
        "type": "subscription_updated",
        "data": {"plan": "basic", "status": "canceled"},
        "created_at": 500.0,
    })
    state = get_state()
    assert state["accounts"]["acc-0"]["plan"] != "basic"
    print("  PASS test_stale_event_ignored")


def test_convergence():
    post_webhook({
        "event_id": "ev-conv", "account_id": "acc-0",
        "type": "subscription_updated",
        "data": {"plan": "basic", "status": "canceled"},
        "created_at": 99000.0,
    })
    time.sleep(8)
    state = get_state()
    assert state["accounts"]["acc-0"]["plan"] == "pro"
    assert state["accounts"]["acc-0"]["status"] == "active"
    print("  PASS test_convergence")


if __name__ == "__main__":
    provider = start_provider()
    start_inbox()
    try:
        test_webhook_accept()
        test_duplicate_event()
        test_stale_event_ignored()
        test_convergence()
        print("\nAll tests passed.")
    finally:
        if inbox_proc:
            inbox_proc.terminate()
            inbox_proc.wait()
        provider.shutdown()
        shutil.rmtree(STATE_DIR, ignore_errors=True)
