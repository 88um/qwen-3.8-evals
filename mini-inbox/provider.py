#!/usr/bin/env python3
"""Fake billing provider for the mini-inbox eval.

Frozen harness component — submissions must NOT modify this file.

It maintains true subscription state for N accounts, mutates it over a seeded
schedule, and delivers webhook events to CONSUMER_URL/webhook with deliberate
chaos: random duplicates and long-delayed (stale) deliveries, out of order.
Failed deliveries (consumer down) are retried until they succeed — events are
never dropped by the provider.

Endpoints:
  GET /subscription?account_id=X   canonical current state — authoritative but
                                   RATE LIMITED (default 20 requests/min
                                   global; over-budget requests get 429).
  GET /health                      200 ok
  GET /truth                       full truth + stats. Requires header
                                   X-Truth-Token: $TRUTH_TOKEN. Off-limits to
                                   submissions (grader only).

Env: PORT, SEED, CONSUMER_URL, N_ACCOUNTS (10), EVENTS (60),
     EVENT_INTERVAL (1.5 s), RATE_LIMIT (20/min), TRUTH_TOKEN.
"""
import heapq
import json
import os
import random
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8090"))
SEED = int(os.environ.get("SEED", "0"))
CONSUMER_URL = os.environ.get("CONSUMER_URL", "").rstrip("/")
N_ACCOUNTS = int(os.environ.get("N_ACCOUNTS", "10"))
EVENTS = int(os.environ.get("EVENTS", "60"))
EVENT_INTERVAL = float(os.environ.get("EVENT_INTERVAL", "1.5"))
RATE_LIMIT = int(os.environ.get("RATE_LIMIT", "20"))
TRUTH_TOKEN = os.environ.get("TRUTH_TOKEN", "")

PLANS = ["basic", "pro", "max"]
STATUSES = ["active", "past_due", "canceled"]

_rng = random.Random(SEED)
_lock = threading.Lock()
_truth = {f"acc-{i}": {"plan": "basic", "status": "active"}
          for i in range(N_ACCOUNTS)}
_stats = {"events_emitted": 0, "deliveries_ok": 0, "delivery_failures": 0,
          "subscription_requests": 0, "rate_429s": 0, "done": False}
_touched = set()  # accounts that had at least one event (graded subset)
_queue = []  # heap of (due_ts, seq, payload)
_queue_seq = 0
_rate_window = []


def enqueue(delay, payload):
    global _queue_seq
    with _lock:
        _queue_seq += 1
        heapq.heappush(_queue, (time.time() + delay, _queue_seq, payload))


def generator():
    for n in range(EVENTS):
        time.sleep(EVENT_INTERVAL)
        with _lock:
            acct = f"acc-{_rng.randrange(N_ACCOUNTS)}"
            if _rng.random() < 0.7:
                _truth[acct]["plan"] = _rng.choice(PLANS)
            else:
                _truth[acct]["status"] = _rng.choice(STATUSES)
            payload = {
                "event_id": f"ev-{n}",
                "account_id": acct,
                "type": "subscription_updated",
                "data": dict(_truth[acct]),  # state as of NOW — goes stale
                "created_at": time.time(),
            }
            _stats["events_emitted"] += 1
            _touched.add(acct)
            r = _rng.random()
        enqueue(_rng.uniform(0, 1), payload)          # normal delivery
        if r < 0.30:
            enqueue(_rng.uniform(2, 8), payload)      # duplicate
        elif r < 0.50:
            enqueue(_rng.uniform(10, 25), payload)    # extra stale straggler


def dispatcher():
    while True:
        with _lock:
            item = _queue[0] if _queue and _queue[0][0] <= time.time() else None
            if item:
                heapq.heappop(_queue)
        if item is None:
            with _lock:
                if (_stats["events_emitted"] >= EVENTS and not _queue):
                    _stats["done"] = True
            time.sleep(0.2)
            continue
        _, _, payload = item
        try:
            req = urllib.request.Request(
                CONSUMER_URL + "/webhook",
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                ok = resp.status == 200
        except OSError:
            ok = False
        with _lock:
            _stats["deliveries_ok" if ok else "delivery_failures"] += 1
        if not ok:
            enqueue(4, payload)  # never dropped — retried until delivered


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _j(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            self._j(200, {"ok": True})
        elif u.path == "/subscription":
            aid = (parse_qs(u.query).get("account_id") or [None])[0]
            now = time.time()
            with _lock:
                _stats["subscription_requests"] += 1
                _rate_window[:] = [t for t in _rate_window if now - t < 60]
                if len(_rate_window) >= RATE_LIMIT:
                    _stats["rate_429s"] += 1
                    self._j(429, {"error": "rate_limited", "retry_after": 5})
                    return
                _rate_window.append(now)
                if aid not in _truth:
                    self._j(404, {"error": "unknown account"})
                else:
                    self._j(200, {"account_id": aid, **_truth[aid]})
        elif u.path == "/truth":
            if not TRUTH_TOKEN or self.headers.get("X-Truth-Token") != TRUTH_TOKEN:
                self._j(403, {"error": "forbidden"})
                return
            with _lock:
                self._j(200, {"truth": {k: dict(v) for k, v in _truth.items()},
                              "touched": sorted(_touched),
                              "stats": dict(_stats)})
        else:
            self._j(404, {"error": "not found"})


def main():
    threading.Thread(target=generator, daemon=True).start()
    threading.Thread(target=dispatcher, daemon=True).start()
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"provider on http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
