#!/usr/bin/env python3
from __future__ import annotations

import concurrent.futures
import hashlib
import http.client
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RunningService:
    def __init__(self, state: Path):
        self.state = state
        self.port = free_port()
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        env = os.environ.copy()
        env["VAULTDROP_STATE_DIR"] = str(self.state)
        env["PORT"] = str(self.port)
        self.process = subprocess.Popen(
            [str(ROOT / "vaultdrop"), "serve"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError("service exited during startup")
            try:
                status, _, _ = self.request("GET", "/health")
                if status == 200:
                    return
            except OSError:
                time.sleep(0.03)
        raise RuntimeError("service did not become healthy")

    def stop(self, sig: int = signal.SIGTERM) -> None:
        if self.process is None or self.process.poll() is not None:
            return
        os.kill(self.process.pid, sig)
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)

    def request(
        self,
        method: str,
        path: str,
        body: bytes | dict | None = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 20,
    ) -> tuple[int, bytes, dict[str, str]]:
        request_headers = dict(headers or {})
        if token:
            request_headers["Authorization"] = f"Bearer {token}"
        if isinstance(body, dict):
            body = json.dumps(body).encode()
            request_headers["Content-Type"] = "application/json"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            conn.request(method, path, body=body, headers=request_headers)
            response = conn.getresponse()
            data = response.read()
            return response.status, data, dict(response.getheaders())
        finally:
            conn.close()


class VaultDropTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name)
        (self.state / "tenants.json").write_text(
            json.dumps(
                {
                    "tenants": [
                        {"id": "t1", "token": "tok-t1"},
                        {"id": "t2", "token": "tok-t2"},
                    ],
                    "admin_token": "tok-admin",
                }
            )
        )
        self.service = RunningService(self.state)
        self.service.start()

    def tearDown(self) -> None:
        self.service.stop()
        self.temp.cleanup()

    def json_request(self, *args, **kwargs) -> tuple[int, object]:
        status, raw, _ = self.service.request(*args, **kwargs)
        return status, json.loads(raw)

    def start_upload(self, data: bytes, chunk_size: int, token: str = "tok-t1") -> str:
        status, body = self.json_request(
            "POST",
            "/uploads",
            {"name": "artifact.bin", "total_size": len(data), "chunk_size": chunk_size},
            token,
        )
        self.assertEqual(status, 201, body)
        return body["upload_id"]

    def put(self, upload: str, index: int, data: bytes, token: str = "tok-t1") -> int:
        status, _, _ = self.service.request(
            "PUT",
            f"/uploads/{upload}/chunks/{index}",
            data,
            token,
            {"X-Chunk-SHA256": hashlib.sha256(data).hexdigest()},
        )
        return status

    def finalize(self, upload: str, data: bytes, token: str = "tok-t1") -> tuple[int, object]:
        return self.json_request(
            "POST",
            f"/uploads/{upload}/finalize",
            {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
            token,
        )

    def upload_all(self, data: bytes, chunk_size: int, token: str = "tok-t1") -> tuple[str, str]:
        upload = self.start_upload(data, chunk_size, token)
        for index, offset in enumerate(range(0, len(data), chunk_size)):
            self.assertEqual(self.put(upload, index, data[offset : offset + chunk_size], token), 200)
        status, body = self.finalize(upload, data, token)
        self.assertEqual(status, 200, body)
        return upload, body["artifact_id"]

    def test_concurrent_chunks_conflict_and_finalize(self) -> None:
        chunk_size = 128 * 1024
        chunks = [os.urandom(chunk_size) for _ in range(12)]
        data = b"".join(chunks)
        upload = self.start_upload(data, chunk_size)
        jobs = [(i, chunk) for i, chunk in enumerate(chunks)]
        jobs += [(2, chunks[2]), (7, chunks[7]), (2, chunks[2])]
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            statuses = list(pool.map(lambda item: self.put(upload, *item), jobs))
        self.assertEqual(statuses, [200] * len(jobs))
        self.assertEqual(self.put(upload, 2, b"x" * chunk_size), 409)

        threading_barrier = threading.Barrier(2)

        def race_finalize():
            threading_barrier.wait()
            return self.finalize(upload, data)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: race_finalize(), range(2)))
        self.assertTrue(all(status in (200, 409) for status, _ in results), results)
        self.assertTrue(any(status == 200 for status, _ in results), results)
        status, listing = self.json_request("GET", "/artifacts", token="tok-t1")
        self.assertEqual(status, 200)
        self.assertEqual(len(listing), 1)
        # Once finalized, even an exact late replay is rejected and cannot mutate it.
        self.assertEqual(self.put(upload, 2, chunks[2]), 409)
        self.assertEqual(self.put(upload, 2, b"z" * chunk_size), 409)
        status, downloaded, _ = self.service.request(
            "GET", f"/artifacts/{listing[0]['artifact_id']}", token="tok-t1"
        )
        self.assertEqual((status, downloaded), (200, data))

    def test_tenant_isolation_dedup_and_gc(self) -> None:
        data = os.urandom(300_000)
        _, a1 = self.upload_all(data, 100_000, "tok-t1")
        _, a2 = self.upload_all(data, 100_000, "tok-t2")
        self.assertNotEqual(a1, a2)
        self.assertEqual(self.service.request("GET", f"/artifacts/{a1}", token="tok-t2")[0], 404)
        self.assertEqual(self.json_request("GET", "/artifacts", token="tok-t2")[1][0]["artifact_id"], a2)
        status, blobs = self.json_request("GET", "/admin/blobs", token="tok-admin")
        self.assertEqual(status, 200)
        self.assertEqual(len(blobs), 1)
        self.assertEqual(blobs[0]["refcount"], 2)

        self.assertEqual(self.service.request("DELETE", f"/artifacts/{a1}", token="tok-t1")[0], 200)
        status, gc1 = self.json_request("POST", "/admin/gc", token="tok-admin")
        self.assertEqual(status, 200)
        self.assertEqual(gc1["collected"], 0)
        self.assertEqual(self.service.request("GET", f"/artifacts/{a2}", token="tok-t2")[1], data)
        self.assertEqual(self.service.request("DELETE", f"/artifacts/{a2}", token="tok-t2")[0], 200)
        status, gc2 = self.json_request("POST", "/admin/gc", token="tok-admin")
        self.assertEqual(status, 200)
        self.assertEqual(gc2["collected"], 1)
        self.assertEqual(self.json_request("GET", "/admin/blobs", token="tok-admin")[1], [])

    def test_corrupt_blob_is_never_streamed(self) -> None:
        data = os.urandom(250_000)
        upload, artifact = self.upload_all(data, 125_000)
        self.assertEqual(
            self.json_request("GET", f"/uploads/{upload}", token="tok-t2")[0], 404
        )
        digest = hashlib.sha256(data).hexdigest()
        blob = self.state / "blobs" / digest[:2] / f"{digest}.blob"
        blob.write_bytes(b"!" * len(data))

        status, raw, headers = self.service.request(
            "GET", f"/artifacts/{artifact}", token="tok-t1"
        )
        self.assertEqual(status, 500)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertNotEqual(raw, data)
        status, validation = self.json_request(
            "GET", "/admin/validate", token="tok-admin"
        )
        self.assertEqual(status, 200)
        self.assertEqual(validation["invalid"], 1)

    def test_checksum_mismatch_is_retryable(self) -> None:
        data = b"correct bytes" * 1000
        upload = self.start_upload(data, len(data))
        self.assertEqual(self.put(upload, 0, data), 200)
        status, _ = self.json_request(
            "POST",
            f"/uploads/{upload}/finalize",
            {"sha256": "0" * 64, "size": len(data)},
            "tok-t1",
        )
        self.assertEqual(status, 422)
        status, body = self.finalize(upload, data)
        self.assertEqual(status, 200, body)

    def test_sigkill_mid_chunk_recovers_resumable_upload(self) -> None:
        data = os.urandom(2 * 1024 * 1024)
        upload = self.start_upload(data, len(data))
        sock = socket.create_connection(("127.0.0.1", self.service.port), timeout=3)
        request = (
            f"PUT /uploads/{upload}/chunks/0 HTTP/1.1\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Authorization: Bearer tok-t1\r\n"
            f"X-Chunk-SHA256: {hashlib.sha256(data).hexdigest()}\r\n"
            f"Content-Length: {len(data)}\r\n\r\n"
        ).encode()
        sock.sendall(request)
        sock.sendall(data[: 512 * 1024])
        time.sleep(0.15)
        self.service.stop(signal.SIGKILL)
        sock.close()

        self.service = RunningService(self.state)
        self.service.start()
        status, body = self.json_request("GET", f"/uploads/{upload}", token="tok-t1")
        self.assertEqual(status, 200)
        self.assertEqual(body["received"], [])
        self.assertEqual(body["state"], "uploading")
        self.assertEqual(self.put(upload, 0, data), 200)
        status, finalized = self.finalize(upload, data)
        self.assertEqual(status, 200, finalized)
        artifact = finalized["artifact_id"]

        self.service.stop(signal.SIGKILL)
        self.service = RunningService(self.state)
        self.service.start()
        status, downloaded, _ = self.service.request(
            "GET", f"/artifacts/{artifact}", token="tok-t1"
        )
        self.assertEqual((status, downloaded), (200, data))


if __name__ == "__main__":
    unittest.main(verbosity=2)
