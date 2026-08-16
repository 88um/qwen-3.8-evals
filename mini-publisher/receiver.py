#!/usr/bin/env python3
"""Fake delivery receiver for the mini-publisher eval.

Frozen harness component — submissions must NOT modify this file.

Endpoints:
  POST /deliver          {"message_id": str, "payload": str}
                         Outcomes (random, seeded): accept (200), reject (500),
                         hang-then-drop without accepting, or accept-then-hang.
                         The receiver does NOT deduplicate: every accepted
                         request is recorded, including repeats of the same id.
  GET  /audit?message_id=X   -> {"message_id": X, "accepted_count": N}
                         Authoritative, reliable, never flaky.
  GET  /dump             -> full ground truth (grader; same info as /audit).
  GET  /health           -> 200 "ok"
  POST /reset            -> clears all state (dev convenience).

Env: PORT (8077), SEED, P_ACCEPT (0.55), P_REJECT (0.20), P_TIMEOUT (0.10),
     HANG_SECONDS (20). Remaining probability mass is accept-then-hang.
"""
import json
import os
import random
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("PORT", "8077"))
SEED = os.environ.get("SEED")
P_ACCEPT = float(os.environ.get("P_ACCEPT", "0.55"))
P_REJECT = float(os.environ.get("P_REJECT", "0.20"))
P_TIMEOUT = float(os.environ.get("P_TIMEOUT", "0.10"))
HANG_SECONDS = float(os.environ.get("HANG_SECONDS", "20"))

_rng = random.Random(int(SEED) if SEED is not None else None)
_lock = threading.Lock()
_accepted = []  # every accepted delivery: {message_id, payload, mode, ts}
_events = []    # every /deliver request: {message_id, outcome, ts}


def _pick_outcome():
    with _lock:
        r = _rng.random()
    if r < P_ACCEPT:
        return "accept"
    if r < P_ACCEPT + P_REJECT:
        return "reject"
    if r < P_ACCEPT + P_REJECT + P_TIMEOUT:
        return "hang_no_accept"
    return "accept_then_hang"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence default request logging
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _hang_and_drop(self):
        time.sleep(HANG_SECONDS)
        self.close_connection = True
        try:
            self.connection.close()
        except OSError:
            pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            self._json(200, {"ok": True})
        elif url.path == "/audit":
            qs = parse_qs(url.query)
            mid = (qs.get("message_id") or [None])[0]
            if mid is None:
                self._json(400, {"error": "message_id required"})
                return
            with _lock:
                count = sum(1 for a in _accepted if a["message_id"] == mid)
            self._json(200, {"message_id": mid, "accepted_count": count})
        elif url.path == "/dump":
            with _lock:
                self._json(200, {"accepted": list(_accepted), "events": list(_events)})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/reset":
            with _lock:
                _accepted.clear()
                _events.clear()
            self._json(200, {"ok": True})
            return
        if url.path != "/deliver":
            self._json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length))
            mid = data["message_id"]
            payload = data["payload"]
            if not isinstance(mid, str) or not isinstance(payload, str) or not mid:
                raise ValueError
        except (ValueError, KeyError, json.JSONDecodeError):
            self._json(400, {"error": "body must be JSON {message_id, payload}"})
            return

        outcome = _pick_outcome()
        now = time.time()
        with _lock:
            _events.append({"message_id": mid, "outcome": outcome, "ts": now})
            if outcome in ("accept", "accept_then_hang"):
                _accepted.append(
                    {"message_id": mid, "payload": payload, "mode": outcome, "ts": now}
                )

        if outcome == "accept":
            self._json(200, {"receipt": str(uuid.uuid4())})
        elif outcome == "reject":
            self._json(500, {"error": "transient upstream failure"})
        else:  # hang_no_accept or accept_then_hang: no response, dropped socket
            self._hang_and_drop()


def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    server.daemon_threads = True
    print(f"receiver listening on http://127.0.0.1:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
