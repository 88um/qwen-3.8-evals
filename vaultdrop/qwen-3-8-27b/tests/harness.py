"""Test harness: drives a real `vaultdrop serve` subprocess against a temp state dir.

Tests exercise the actual executable over HTTP (stdlib urllib only) and, for crash
tests, SIGKILL the process mid-operation and restart it against the same state dir.
Invariants are asserted post-recovery and are written to hold regardless of exactly
where a crash lands.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # model-1/
LAUNCHER = ROOT / "vaultdrop"

T1 = "tok-t1"
T2 = "tok-t2"
ADMIN = "tok-admin"

DEFAULT_TENANTS = {
    "tenants": [{"id": "t1", "token": T1}, {"id": "t2", "token": T2}],
    "admin_token": ADMIN,
}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Vault:
    def __init__(self):
        self.state = Path(tempfile.mkdtemp(prefix="vdtest-"))
        self.port = _free_port()
        self.proc = None
        self._log = open(self.state / "serve.log", "wb")

    # --- lifecycle ----------------------------------------------------------

    def start(self, tenants=None):
        tenants = tenants or DEFAULT_TENANTS
        (self.state / "tenants.json").write_text(json.dumps(tenants))
        env = dict(os.environ, VAULTDROP_STATE_DIR=str(self.state), PORT=str(self.port))
        self.proc = subprocess.Popen(
            [str(LAUNCHER), "serve"], env=env,
            stdout=self._log, stderr=subprocess.STDOUT,
        )
        self._wait_healthy()
        return self

    def restart(self):
        """Stop (if running) and start again against the SAME state dir."""
        self.stop()
        self.start()
        return self

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None

    def kill(self, sig=signal.SIGKILL):
        assert self.proc is not None and self.proc.poll() is None
        self.proc.send_signal(sig)

    def wait_dead(self, timeout=10):
        assert self.proc is not None
        self.proc.wait(timeout=timeout)

    def _wait_healthy(self, timeout=20):
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(self.url("/health"), timeout=1) as r:
                    if r.status == 200:
                        return
            except Exception as e:            # noqa: BLE001 - retry until up
                last = e
            time.sleep(0.05)
        self._log.flush()
        raise RuntimeError(f"server not healthy: {last}\n{self.state/'serve.log'}")

    # --- http ---------------------------------------------------------------

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def req(self, method, path, token=None, body=None, headers=None, raw=False):
        hdrs = dict(headers or {})
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            if isinstance(body, dict):
                hdrs.setdefault("Content-Type", "application/json")
        r = urllib.request.Request(self.url(path), data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                b = resp.read()
                return resp.status, (b if raw else (json.loads(b) if b else None))
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, json.loads(b)
            except Exception:            # noqa: BLE001
                return e.code, b

    # --- convenience --------------------------------------------------------

    def upload(self, token, name, data, chunk_size):
        st, up = self.req("POST", "/uploads", token,
                          {"name": name, "total_size": len(data), "chunk_size": chunk_size})
        assert st == 201, (st, up)
        upload_id = up["upload_id"]
        n = 0
        for i in range(0, len(data), chunk_size):
            chunk = data[i:i + chunk_size]
            st, _ = self.req("PUT", f"/uploads/{upload_id}/chunks/{n}", token,
                             body=chunk,
                             headers={"X-Chunk-SHA256": _sha256(chunk)})
            assert st in (200, 201), (st, _)
            n += 1
        return upload_id

    def finalize(self, token, upload_id, data):
        st, out = self.req("POST", f"/uploads/{upload_id}/finalize", token,
                           {"sha256": _sha256(data), "size": len(data)})
        return st, out

    def finalize_upload(self, token, name, data, chunk_size):
        upload_id = self.upload(token, name, data, chunk_size)
        st, out = self.finalize(token, upload_id, data)
        assert st == 200, (st, out)
        return out["artifact_id"]

    def download(self, token, artifact_id):
        return self.req("GET", f"/artifacts/{artifact_id}", token, raw=True)

    def gc(self):
        return self.req("POST", "/admin/gc", ADMIN)

    def blobs(self):
        return self.req("GET", "/admin/blobs", ADMIN)

    def validate(self):
        return self.req("GET", "/admin/validate", ADMIN)


def _sha256(b: bytes) -> str:
    import hashlib
    return hashlib.sha256(b).hexdigest()


def cleanup(vault: Vault):
    vault.stop()
    # best-effort state-dir removal
    import shutil
    try:
        shutil.rmtree(vault.state, ignore_errors=True)
    except Exception:            # noqa: BLE001
        pass
