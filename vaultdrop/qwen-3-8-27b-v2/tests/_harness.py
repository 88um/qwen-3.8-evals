"""Shared test harness: spawns/steers a real vaultdrop subprocess."""

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(TESTS_DIR)
EXE = os.path.join(ROOT_DIR, "vaultdrop")

TENANTS = {
    "tenants": [
        {"id": "t1", "token": "tok-t1"},
        {"id": "t2", "token": "tok-t2"},
    ],
    "admin_token": "tok-admin",
}


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    """A live vaultdrop serve process over a private state dir."""

    def __init__(self, tenants=None):
        self.state_dir = tempfile.mkdtemp(prefix="vdtest-")
        with open(os.path.join(self.state_dir, "tenants.json"), "w") as f:
            json.dump(tenants or TENANTS, f)
        self.port = _free_port()
        self.proc = None

    # -- lifecycle -----------------------------------------------------------
    def start(self):
        env = dict(os.environ)
        env["PORT"] = str(self.port)
        env["VAULTDROP_STATE_DIR"] = self.state_dir
        self.proc = subprocess.Popen(
            [sys.executable, EXE, "serve"],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 30
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited early: rc=%r" % self.proc.returncode)
            try:
                st, _ = self.req("GET", "/health")
                if st == 200:
                    return self
            except Exception:
                time.sleep(0.05)
        self.kill9()
        raise RuntimeError("server did not become healthy")

    def kill9(self):
        if self.proc is not None and self.proc.poll() is None:
            os.kill(self.proc.pid, 9)
        if self.proc is not None:
            self.proc.wait()

    def restart(self):
        self.kill9()
        return self.start()

    def close(self):
        self.kill9()
        shutil.rmtree(self.state_dir, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *a):
        self.close()

    # -- client ---------------------------------------------------------------
    def _req(self, method, path, body=None, token=None, headers=None, timeout=60):
        url = "http://127.0.0.1:%d%s" % (self.port, path)
        r = urllib.request.Request(url, method=method)
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        if body is not None:
            r.add_header("Content-Length", str(len(body)))
        if token is not None:
            r.add_header("Authorization", "Bearer " + token)
        for k, v in (headers or {}).items():
            r.add_header(k, v)
        try:
            with urllib.request.urlopen(
                r, data=body if body is not None else None, timeout=timeout
            ) as resp:
                data = resp.read()
                return resp.status, data
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def req(self, method, path, body=None, token=None, headers=None, raw=False, timeout=60):
        st, data = self._req(method, path, body, token, headers, timeout)
        if raw:
            return st, data
        try:
            return st, json.loads(data)
        except ValueError:
            return st, data

    def put_chunk(self, uid, index, payload, token="tok-t1"):
        return self.req(
            "PUT", "/uploads/%s/chunks/%d" % (uid, index), payload, token,
            {"X-Chunk-SHA256": hashlib.sha256(payload).hexdigest()},
        )

    def upload(self, name, content, token="tok-t1", chunk_size=1024 * 1024, threads=1):
        """Create an upload and PUT all chunks (optionally concurrently)."""
        st, up = self.req("POST", "/uploads",
                          {"name": name, "total_size": len(content),
                           "chunk_size": chunk_size}, token)
        assert st == 200, (st, up)
        uid = up["upload_id"]
        chunks = []
        for i in range((len(content) + chunk_size - 1) // chunk_size):
            chunks.append(content[i * chunk_size:(i + 1) * chunk_size])
        if threads <= 1:
            for i, c in enumerate(chunks):
                st, _ = self.put_chunk(uid, i, c, token)
                assert st == 200, (st, i)
        else:
            import concurrent.futures
            def put(i):
                return self.put_chunk(uid, i, chunks[i], token)[0]
            with concurrent.futures.ThreadPoolExecutor(threads) as ex:
                sts = list(ex.map(put, range(len(chunks))))
            assert all(s == 200 for s in sts), sts
        return uid, chunks

    def finalize(self, uid, content, token="tok-t1"):
        return self.req("POST", "/uploads/%s/finalize" % uid,
                        {"sha256": hashlib.sha256(content).hexdigest(),
                         "size": len(content)}, token)

    def download(self, aid, token="tok-t1"):
        return self.req("GET", "/artifacts/%s" % aid, token=token, raw=True)

    def gc(self, token="tok-admin"):
        return self.req("POST", "/admin/gc", token=token)

    def blobs(self, token="tok-admin"):
        return self.req("GET", "/admin/blobs", token=token)

    def validate(self, token="tok-admin"):
        return self.req("GET", "/admin/validate", token=token)

    def artifacts(self, token="tok-t1"):
        return self.req("GET", "/artifacts", token=token)

    def blob_disk_files(self):
        """Walk the blob store; return {content_hash: size} of regular files."""
        out = {}
        root = os.path.join(self.state_dir, "blobs")
        if not os.path.isdir(root):
            return out
        for shard in os.listdir(root):
            sdir = os.path.join(root, shard)
            if not os.path.isdir(sdir):
                continue
            for name in os.listdir(sdir):
                p = os.path.join(sdir, name)
                if os.path.isfile(p):
                    out[name] = os.path.getsize(p)
        return out
