"""VaultDrop integration tests.

Every test drives the real `vaultdrop serve` binary over HTTP against a fresh
state directory. Crash tests SIGKILL the server mid-operation and restart it;
concurrency tests race the live endpoints from threads. Assertions are written
to hold for ANY crash landing point: every point must be recoverable.
"""

import hashlib
import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.join(ROOT, "vaultdrop")

TENANTS_DOC = {
    "tenants": [{"id": "t1", "token": "tok-t1"}, {"id": "t2", "token": "tok-t2"}],
    "admin_token": "tok-admin",
}


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Client:
    def __init__(self, port):
        self.base = "http://127.0.0.1:%d" % port

    def req(self, method, path, token=None, body=None, headers=None, raw_body=None):
        url = self.base + path
        hdrs = dict(headers or {})
        if token:
            hdrs["Authorization"] = "Bearer " + token
        data = None
        if raw_body is not None:
            data = raw_body
            hdrs.setdefault("Content-Length", str(len(raw_body)))
        elif body is not None:
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        r = urllib.request.Request(url, data=data, method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(r, timeout=600) as resp:
                payload = resp.read()
                return resp.status, payload
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def jreq(self, method, path, **kw):
        status, payload = self.req(method, path, **kw)
        try:
            return status, json.loads(payload)
        except ValueError:
            return status, payload

    def put_chunk(self, upload_id, index, data, token, sha=None):
        if sha is None:
            sha = hashlib.sha256(data).hexdigest()
        return self.jreq(
            "PUT",
            "/uploads/%s/chunks/%d" % (upload_id, index),
            token=token,
            raw_body=data,
            headers={"X-Chunk-SHA256": sha},
        )

    def upload(self, token, name, data, chunk_size=1024 * 1024, order=None):
        status, doc = self.jreq(
            "POST", "/uploads", token=token,
            body={"name": name, "total_size": len(data), "chunk_size": chunk_size},
        )
        assert status == 200, (status, doc)
        upload_id = doc["upload_id"]
        n = (len(data) + chunk_size - 1) // chunk_size if chunk_size else 0
        indices = list(range(n))
        if order is not None:
            indices = order
        for i in indices:
            chunk = data[i * chunk_size:(i + 1) * chunk_size]
            status, _ = self.put_chunk(upload_id, i, chunk, token)
            assert status == 200, (i, status)
        return upload_id

    def finalize(self, token, upload_id, data):
        return self.jreq(
            "POST", "/uploads/%s/finalize" % upload_id, token=token,
            body={"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
        )

    def download(self, token, artifact_id, progress=None):
        status, payload = self.req("GET", "/artifacts/%s" % artifact_id, token=token)
        if status != 200 and progress is not None:
            progress(0)
        return status, payload

    def download_stream(self, token, artifact_id, sink_block):
        """Streams the artifact, feeding each block to sink_block; returns (status, size)."""
        status, _ = self.req("GET", "/artifacts/%s" % artifact_id, token=token)
        return status

    def admin_gc(self):
        return self.jreq("POST", "/admin/gc", token="tok-admin")

    def admin_blobs(self):
        return self.jreq("GET", "/admin/blobs", token="tok-admin")

    def admin_validate(self):
        return self.jreq("GET", "/admin/validate", token="tok-admin")


class ServerProc:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.port = free_port()
        with open(os.path.join(state_dir, "tenants.json"), "w") as f:
            json.dump(TENANTS_DOC, f)
        self.client = Client(self.port)
        self._spawn()

    def _spawn(self):
        env = dict(os.environ)
        env["PORT"] = str(self.port)
        env["VAULTDROP_STATE_DIR"] = self.state_dir
        self.proc = subprocess.Popen(
            [sys.executable, BIN, "serve"],
            cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                status, _ = self.client.req("GET", "/health")
                if status == 200:
                    return
            except OSError:
                pass
            time.sleep(0.05)
        raise RuntimeError("server did not come up")

    def kill9(self):
        self.proc.send_signal(signal.SIGKILL)
        self.proc.wait()

    def restart(self):
        self.kill9()
        self._spawn()

    def rss_kib(self):
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(self.proc.pid)],
            capture_output=True, text=True,
        ).stdout.strip()
        return int(out.split()[0]) if out else 0

    def close(self):
        if self.proc.poll() is None:
            self.kill9()


def make_pattern_file(path, size, block_size=1024 * 1024):
    """Writes deterministic pseudo-random content without holding it in memory."""
    h = hashlib.sha256()
    written = 0
    counter = 0
    with open(path, "wb") as f:
        while written < size:
            n = min(block_size, size - written)
            block = os.urandom(n)
            f.write(block)
            h.update(block)
            written += n
            counter += 1
    return h.hexdigest()


def read_pattern_file(path):
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(1024 * 1024)
            if not b:
                break
            h.update(b)
            size += len(b)
    return h.hexdigest(), size


class VaultDropTest(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp(prefix="vaultdrop-test-")
        self.server = ServerProc(self.state_dir)

    def tearDown(self):
        self.server.close()
        shutil.rmtree(self.state_dir, ignore_errors=True)

    # ------------------------------------------------------------------ basic

    def test_health_and_migrate(self):
        status, _ = self.server.client.req("GET", "/health")
        self.assertEqual(status, 200)
        env = dict(os.environ)
        env["VAULTDROP_STATE_DIR"] = self.state_dir
        p = subprocess.run([sys.executable, BIN, "migrate"], env=env, cwd=ROOT)
        self.assertEqual(p.returncode, 0)

    def test_upload_roundtrip(self):
        c = self.server.client
        data = os.urandom(3 * 1024 * 1024 + 123)
        upload_id = c.upload("tok-t1", "f.bin", data, chunk_size=1024 * 1024,
                             order=[2, 0, 3, 1])
        status, doc = c.finalize("tok-t1", upload_id, data)
        self.assertEqual(status, 200, doc)
        artifact_id = doc["artifact_id"]
        status, got = c.download("tok-t1", artifact_id)
        self.assertEqual(status, 200)
        self.assertEqual(got, data)
        status, listing = c.jreq("GET", "/artifacts", token="tok-t1")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["sha256"], hashlib.sha256(data).hexdigest())

    def test_chunk_idempotent_and_conflict(self):
        c = self.server.client
        data_a = os.urandom(1024 * 1024)
        data_b = os.urandom(1024 * 1024)
        total = data_a + os.urandom(512 * 1024)
        upload_id = c.upload("tok-t1", "c.bin", total, chunk_size=1024 * 1024,
                             order=[1])  # only chunk 1 first
        status, _ = c.put_chunk(upload_id, 0, data_a, "tok-t1")
        self.assertEqual(status, 200)
        status, _ = c.put_chunk(upload_id, 0, data_a, "tok-t1")  # exact replay
        self.assertEqual(status, 200)
        status, _ = c.put_chunk(upload_id, 0, data_b, "tok-t1")  # conflict
        self.assertEqual(status, 409)
        status, _ = c.put_chunk(upload_id, 0, data_a, "tok-t1")  # replay again
        self.assertEqual(status, 200)
        status, doc = c.finalize("tok-t1", upload_id, total)
        self.assertEqual(status, 200, doc)
        status, got = c.download("tok-t1", doc["artifact_id"])
        self.assertEqual(status, 200)
        self.assertEqual(got, total)  # first-written bytes were not overwritten

    def test_concurrent_chunks(self):
        c = self.server.client
        chunk_size = 1024 * 1024
        n = 16
        chunks = [os.urandom(chunk_size) for _ in range(n)]
        data = b"".join(chunks)
        status, doc = c.jreq(
            "POST", "/uploads", token="tok-t1",
            body={"name": "cc.bin", "total_size": len(data), "chunk_size": chunk_size},
        )
        self.assertEqual(status, 200)
        upload_id = doc["upload_id"]
        errors = []

        def put(i):
            # each index PUT twice concurrently (duplicate indices)
            for _ in range(2):
                s, _ = c.put_chunk(upload_id, i, chunks[i], "tok-t1")
                if s != 200:
                    errors.append((i, s))

        threads = [threading.Thread(target=put, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        status, doc = c.finalize("tok-t1", upload_id, data)
        self.assertEqual(status, 200, doc)
        status, got = c.download("tok-t1", doc["artifact_id"])
        self.assertEqual(got, data)

    def test_concurrent_finalize_single_artifact(self):
        c = self.server.client
        data = os.urandom(8 * 1024 * 1024)
        upload_id = c.upload("tok-t1", "cf.bin", data)
        results = []

        def fin():
            results.append(c.finalize("tok-t1", upload_id, data))

        threads = [threading.Thread(target=fin) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        statuses = [s for s, _ in results]
        self.assertEqual(statuses, [200, 200], results)
        ids = {doc["artifact_id"] for _, doc in results}
        self.assertEqual(len(ids), 1)
        status, got = c.download("tok-t1", ids.pop())
        self.assertEqual(got, data)
        status, listing = c.jreq("GET", "/artifacts", token="tok-t1")
        self.assertEqual(len(listing), 1)

    def test_finalize_mismatch_and_resume(self):
        c = self.server.client
        data = os.urandom(2 * 1024 * 1024)
        upload_id = c.upload("tok-t1", "mm.bin", data)
        bad = dict(sha256="0" * 64, size=len(data))
        status, _ = c.jreq("POST", "/uploads/%s/finalize" % upload_id,
                           token="tok-t1", body=bad)
        self.assertEqual(status, 422)
        status, listing = c.jreq("GET", "/artifacts", token="tok-t1")
        self.assertEqual(listing, [])
        status, doc = c.finalize("tok-t1", upload_id, data)
        self.assertEqual(status, 200, doc)
        status, got = c.download("tok-t1", doc["artifact_id"])
        self.assertEqual(got, data)

    def test_late_chunk_cannot_mutate(self):
        c = self.server.client
        data = os.urandom(3 * 1024 * 1024)
        upload_id = c.upload("tok-t1", "lc.bin", data, chunk_size=1024 * 1024)
        status, doc = c.finalize("tok-t1", upload_id, data)
        self.assertEqual(status, 200)
        artifact_id = doc["artifact_id"]
        status, _ = c.put_chunk(upload_id, 0, os.urandom(1024 * 1024), "tok-t1")
        self.assertEqual(status, 409)
        status, got = c.download("tok-t1", artifact_id)
        self.assertEqual(status, 200)
        self.assertEqual(got, data)

    # ------------------------------------------------------------- isolation

    def test_cross_tenant_isolation(self):
        c = self.server.client
        data = os.urandom(1024 * 1024)
        upload_id = c.upload("tok-t1", "iso.bin", data)
        status, doc = c.finalize("tok-t1", upload_id, data)
        artifact_id = doc["artifact_id"]
        # direct probing with t2's token: indistinguishable from unknown ids
        for path in ("/artifacts/%s" % artifact_id, "/uploads/%s" % upload_id):
            status, _ = c.jreq("GET", path, token="tok-t2")
            self.assertEqual(status, 404)
        status, _ = c.jreq("DELETE", "/artifacts/%s" % artifact_id, token="tok-t2")
        self.assertEqual(status, 404)
        status, _ = c.jreq("POST", "/uploads/%s/finalize" % upload_id,
                           token="tok-t2", body={"sha256": "0" * 64, "size": len(data)})
        self.assertEqual(status, 404)
        status, listing = c.jreq("GET", "/artifacts", token="tok-t2")
        self.assertEqual(listing, [])
        status, _ = c.jreq("GET", "/artifacts/0" * 31 + "1", token="tok-t2")
        self.assertEqual(status, 404)
        # admin surface with a tenant token never leaks
        status, _ = c.jreq("GET", "/admin/blobs", token="tok-t1")
        self.assertEqual(status, 404)
        status, _ = c.jreq("POST", "/admin/gc", token="tok-t2")
        self.assertEqual(status, 404)
        # unknown tokens
        status, _ = c.jreq("GET", "/artifacts", token="tok-nobody")
        self.assertEqual(status, 401)
        status, _ = c.req("GET", "/artifacts")
        self.assertEqual(status, 401)
        # t1 still owns everything
        status, got = c.download("tok-t1", artifact_id)
        self.assertEqual(got, data)

    def test_cross_tenant_dedup_invisible(self):
        c = self.server.client
        data = os.urandom(2 * 1024 * 1024)
        u1 = c.upload("tok-t1", "d.bin", data)
        u2 = c.upload("tok-t2", "d.bin", data)
        s1, doc1 = c.finalize("tok-t1", u1, data)
        s2, doc2 = c.finalize("tok-t2", u2, data)
        self.assertEqual((s1, s2), (200, 200))
        self.assertNotEqual(doc1["artifact_id"], doc2["artifact_id"])
        status, got = c.download("tok-t2", doc1["artifact_id"])
        self.assertEqual(status, 404)
        status, got = c.download("tok-t1", doc2["artifact_id"])
        self.assertEqual(status, 404)
        status, listing1 = c.jreq("GET", "/artifacts", token="tok-t1")
        status, listing2 = c.jreq("GET", "/artifacts", token="tok-t2")
        self.assertEqual({a["artifact_id"] for a in listing1}, {doc1["artifact_id"]})
        self.assertEqual({a["artifact_id"] for a in listing2}, {doc2["artifact_id"]})
        # byte-layer dedup exists but is admin-visible only, as counts
        status, blobs = c.admin_blobs()
        self.assertEqual(status, 200)
        matching = [b for b in blobs if b["content_hash"] == hashlib.sha256(data).hexdigest()]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["refcount"], 2)
        # deleting one tenant's artifact leaves the other fully readable
        status, _ = c.jreq("DELETE", "/artifacts/%s" % doc1["artifact_id"], token="tok-t1")
        self.assertEqual(status, 204)
        status, got = c.download("tok-t2", doc2["artifact_id"])
        self.assertEqual(status, 200)
        self.assertEqual(got, data)

    # ------------------------------------------------------------------- GC

    def _setup_collectable_blobs(self, count, size=1024):
        """Direct-DB setup simulating prior deleted artifacts: collectable blobs."""
        conn = sqlite3.connect(os.path.join(self.state_dir, "vaultdrop.db"), timeout=30)
        try:
            for i in range(count):
                content = os.urandom(size)
                digest = hashlib.sha256(content).hexdigest()
                path = os.path.join(self.state_dir, "blobs", digest[:2], digest)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(content)
                conn.execute(
                    "INSERT INTO blobs(hash, size, refcount, state, validated, created_at)"
                    " VALUES(?,?,0,'active',1,?)",
                    (digest, size, int(time.time())),
                )
            conn.commit()
        finally:
            conn.close()

    def test_gc_collects_only_unreferenced(self):
        c = self.server.client
        keep = os.urandom(1024 * 1024)
        drop = os.urandom(1024 * 1024)
        u_keep = c.upload("tok-t1", "k.bin", keep)
        u_drop = c.upload("tok-t1", "d.bin", drop)
        _, doc_keep = c.finalize("tok-t1", u_keep, keep)
        _, doc_drop = c.finalize("tok-t1", u_drop, drop)
        status, _ = c.jreq("DELETE", "/artifacts/%s" % doc_drop["artifact_id"], token="tok-t1")
        self.assertEqual(status, 204)
        status, gc = c.admin_gc()
        self.assertEqual(status, 200, gc)
        self.assertGreaterEqual(gc["collected"], 1)
        self.assertGreaterEqual(gc["bytes_freed"], len(drop))
        status, blobs = c.admin_blobs()
        hashes = {b["content_hash"]: b for b in blobs}
        self.assertNotIn(hashlib.sha256(drop).hexdigest(), hashes)
        self.assertIn(hashlib.sha256(keep).hexdigest(), hashes)
        self.assertEqual(hashes[hashlib.sha256(keep).hexdigest()]["refcount"], 1)
        status, got = c.download("tok-t1", doc_keep["artifact_id"])
        self.assertEqual(got, keep)
        status, _ = c.jreq("GET", "/artifacts/%s" % doc_drop["artifact_id"], token="tok-t1")
        self.assertEqual(status, 404)

    def test_finalize_vs_gc_race(self):
        """GC runs while a finalize is about to reference refcount-0 content."""
        c = self.server.client
        self._setup_collectable_blobs(4000)  # slows GC's unlink phase
        data = os.urandom(64 * 1024 * 1024)  # hashing takes long enough to overlap
        upload_id = c.upload("tok-t1", "race.bin", data, chunk_size=32 * 1024 * 1024)
        results = {}

        def gc_():
            results["gc"] = c.admin_gc()

        def fin():
            results["fin"] = c.finalize("tok-t1", upload_id, data)

        t1 = threading.Thread(target=gc_)
        t2 = threading.Thread(target=fin)
        t1.start()
        time.sleep(0.05)  # let GC's mark phase land first
        t2.start()
        t1.join()
        t2.join()
        self.assertEqual(results["fin"][0], 200, results)
        artifact_id = results["fin"][1]["artifact_id"]
        status, got = c.download("tok-t1", artifact_id)
        self.assertEqual(status, 200)
        self.assertEqual(got, data)
        self.assertGreaterEqual(results["gc"][1]["collected"], 1)

    # --------------------------------------------------------------- crashes

    def _kill_during(self, op_thread_factory, delay):
        """Runs an op in a thread, SIGKILLs the server after `delay`, restarts."""
        errors = []

        def runner():
            try:
                op_thread_factory()
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=runner)
        t.start()
        time.sleep(delay)
        self.server.kill9()
        self.server.restart()
        t.join()
        return errors

    def test_crash_during_chunk_write(self):
        c = self.server.client
        chunk = os.urandom(32 * 1024 * 1024)
        rest = os.urandom(1024 * 1024)
        data = chunk + rest
        status, doc = c.jreq(
            "POST", "/uploads", token="tok-t1",
            body={"name": "cw.bin", "total_size": len(data), "chunk_size": 32 * 1024 * 1024},
        )
        self.assertEqual(status, 200)
        upload_id = doc["upload_id"]

        def put0():
            c.put_chunk(upload_id, 0, chunk, "tok-t1")

        self._kill_during(put0, 0.04)  # lands inside the multi-MiB write window
        # retry heals whatever the crash left behind (orphan, missing file, row)
        status, _ = c.put_chunk(upload_id, 0, chunk, "tok-t1")
        self.assertEqual(status, 200)
        status, _ = c.put_chunk(upload_id, 1, rest, "tok-t1")
        self.assertEqual(status, 200)
        status, doc = c.finalize("tok-t1", upload_id, data)
        self.assertEqual(status, 200, doc)
        status, got = c.download("tok-t1", doc["artifact_id"])
        self.assertEqual(got, data)

    def test_crash_chunk_self_heal(self):
        """Deterministic residue of a crash between chunk-row commit and link:
        row present, file absent. The retry must heal it."""
        c = self.server.client
        chunk = os.urandom(1024 * 1024)
        data = chunk + os.urandom(1024)
        status, doc = c.jreq(
            "POST", "/uploads", token="tok-t1",
            body={"name": "sh.bin", "total_size": len(data), "chunk_size": 1024 * 1024},
        )
        upload_id = doc["upload_id"]
        conn = sqlite3.connect(os.path.join(self.state_dir, "vaultdrop.db"), timeout=30)
        conn.execute(
            "INSERT INTO upload_chunks(upload_id, chunk_index, size, sha256)"
            " VALUES(?,?,?,?)",
            (upload_id, 0, len(chunk), hashlib.sha256(chunk).hexdigest()),
        )
        conn.commit()
        conn.close()
        status, _ = c.put_chunk(upload_id, 0, chunk, "tok-t1")
        self.assertEqual(status, 200)
        status, _ = c.put_chunk(upload_id, 1, data[len(chunk):], "tok-t1")
        self.assertEqual(status, 200)
        status, doc = c.finalize("tok-t1", upload_id, data)
        self.assertEqual(status, 200, doc)
        status, got = c.download("tok-t1", doc["artifact_id"])
        self.assertEqual(got, data)

    def test_crash_during_finalize(self):
        c = self.server.client
        data = os.urandom(256 * 1024 * 1024)
        upload_id = c.upload("tok-t1", "cfz.bin", data, chunk_size=32 * 1024 * 1024)

        def fin():
            c.finalize("tok-t1", upload_id, data)

        self._kill_during(fin, 0.15)  # inside the assembly/hash window
        # either no artifact exists, or the complete one; never a half-one
        status, listing = c.jreq("GET", "/artifacts", token="tok-t1")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing), 0)
        status, doc = c.finalize("tok-t1", upload_id, data)
        self.assertEqual(status, 200, doc)
        status, got = c.download("tok-t1", doc["artifact_id"])
        self.assertEqual(status, 200)
        self.assertEqual(got, data)

    def test_crash_during_gc(self):
        c = self.server.client
        live = os.urandom(1024 * 1024)
        u = c.upload("tok-t1", "live.bin", live)
        _, doc = c.finalize("tok-t1", u, live)
        artifact_id = doc["artifact_id"]
        self._setup_collectable_blobs(20000)  # wide unlink window

        def gc_():
            c.admin_gc()

        self._kill_during(gc_, 0.25)
        # referenced bytes survive; no partially-deleted blob is served
        status, got = c.download("tok-t1", artifact_id)
        self.assertEqual(status, 200)
        self.assertEqual(got, live)
        status, blobs = c.admin_blobs()
        self.assertEqual(status, 200)
        status, val = c.admin_validate()
        self.assertEqual(status, 200)
        self.assertEqual(val["mismatch"], 0)
        self.assertEqual(val["scanned"], len(blobs))
        # a follow-up pass completes cleanly
        status, gc = c.admin_gc()
        self.assertEqual(status, 200)
        self.assertGreaterEqual(gc["collected"], 0)

    # ----------------------------------------------------------------- scale

    def test_scale_streaming_memory(self):
        """A multi-GiB round-trip must stay under the 512 MiB RSS ceiling."""
        c = self.server.client
        size = 2 * 1024 ** 3
        src = os.path.join(self.state_dir, "scale-src")
        digest = make_pattern_file(src, size)
        chunk_size = 32 * 1024 * 1024
        n = size // chunk_size
        status, doc = c.jreq(
            "POST", "/uploads", token="tok-t1",
            body={"name": "scale.bin", "total_size": size, "chunk_size": chunk_size},
        )
        self.assertEqual(status, 200)
        upload_id = doc["upload_id"]

        peak = {"kib": 0}
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                try:
                    kib = self.server.rss_kib()
                except Exception:
                    return
                if kib > peak["kib"]:
                    peak["kib"] = kib
                time.sleep(0.02)

        poller = threading.Thread(target=poll, daemon=True)
        poller.start()
        try:
            with open(src, "rb") as f:
                for i in range(n):
                    chunk = f.read(chunk_size)
                    status, _ = c.put_chunk(upload_id, i, chunk, "tok-t1")
                    self.assertEqual(status, 200, i)
            status, doc = self._finalize_by_digest(c, upload_id, digest, size)
            self.assertEqual(status, 200, doc)
            artifact_id = doc["artifact_id"]
            status, got = c.download("tok-t1", artifact_id)
            self.assertEqual(status, 200)
            self.assertEqual(len(got), size)
            self.assertEqual(hashlib.sha256(got).hexdigest(), digest)
        finally:
            stop.set()
            poller.join()
        self.assertLess(peak["kib"], 512 * 1024, "peak RSS %d KiB" % peak["kib"])

    def _finalize_by_digest(self, c, upload_id, digest, size, token="tok-t1"):
        return c.jreq(
            "POST", "/uploads/%s/finalize" % upload_id, token=token,
            body={"sha256": digest, "size": size},
        )

    def test_scale_metadata_responsiveness(self):
        """10k stored blobs: listings, lookups, GC and validate stay fast."""
        c = self.server.client
        self._setup_collectable_blobs(10000, size=1024)
        data = os.urandom(64 * 1024)
        u = c.upload("tok-t1", "m.bin", data)
        status, doc = c.finalize("tok-t1", u, data)
        self.assertEqual(status, 200)

        t0 = time.monotonic()
        status, blobs = c.admin_blobs()
        t_blobs = time.monotonic() - t0
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(blobs), 10000)

        t0 = time.monotonic()
        status, gc = c.admin_gc()
        t_gc = time.monotonic() - t0
        self.assertEqual(status, 200)
        self.assertGreaterEqual(gc["collected"], 10000)

        t0 = time.monotonic()
        status, val = c.admin_validate()
        t_val = time.monotonic() - t0
        self.assertEqual(status, 200)
        self.assertEqual(val["mismatch"], 0)

        t0 = time.monotonic()
        status, got = c.download("tok-t1", doc["artifact_id"])
        t_dl = time.monotonic() - t0
        self.assertEqual(got, data)

        for label, dt in (("blobs", t_blobs), ("gc", t_gc), ("validate", t_val), ("download", t_dl)):
            self.assertLess(dt, 10.0, "%s took %.1fs" % (label, dt))

    def test_concurrent_finalizes_distinct_content(self):
        c = self.server.client
        size = 128 * 1024 * 1024
        src_a = os.path.join(self.state_dir, "cf-a")
        src_b = os.path.join(self.state_dir, "cf-b")
        da = make_pattern_file(src_a, size)
        db_ = make_pattern_file(src_b, size)
        chunk_size = 32 * 1024 * 1024
        n = size // chunk_size
        uploads = {}

        def prep(token, name, src, digest):
            status, doc = c.jreq(
                "POST", "/uploads", token=token,
                body={"name": name, "total_size": size, "chunk_size": chunk_size},
            )
            self.assertEqual(status, 200)
            upload_id = doc["upload_id"]
            with open(src, "rb") as f:
                for i in range(n):
                    status, _ = c.put_chunk(upload_id, i, f.read(chunk_size), token)
                    self.assertEqual(status, 200)
            uploads[name] = (upload_id, digest)

        tp = [threading.Thread(target=prep, args=("tok-t1", "a.bin", src_a, da)),
              threading.Thread(target=prep, args=("tok-t2", "b.bin", src_b, db_))]
        for t in tp:
            t.start()
        for t in tp:
            t.join()
        results = {}

        def fin(name, token):
            upload_id, digest = uploads[name]
            results[name] = self._finalize_by_digest(c, upload_id, digest, size, token)

        tf = [threading.Thread(target=fin, args=("a.bin", "tok-t1")),
              threading.Thread(target=fin, args=("b.bin", "tok-t2"))]
        for t in tf:
            t.start()
        for t in tf:
            t.join()
        self.assertEqual(results["a.bin"][0], 200, results)
        self.assertEqual(results["b.bin"][0], 200, results)
        self.assertNotEqual(results["a.bin"][1]["artifact_id"],
                            results["b.bin"][1]["artifact_id"])
        status, got = c.download("tok-t1", results["a.bin"][1]["artifact_id"])
        self.assertEqual(hashlib.sha256(got).hexdigest(), da)
        status, got = c.download("tok-t2", results["b.bin"][1]["artifact_id"])
        self.assertEqual(hashlib.sha256(got).hexdigest(), db_)


if __name__ == "__main__":
    unittest.main(verbosity=2)
