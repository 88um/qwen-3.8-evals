"""Crash-recovery tests: SIGKILL mid-operation, restart, verify."""

import concurrent.futures
import hashlib
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Server  # noqa: E402

MB = 1024 * 1024


def pattern(seed, size):
    base = bytes((i + seed) % 256 for i in range(4096))
    out = bytearray()
    while len(out) < size:
        out += base
    return bytes(out[:size])


def _swallow(fn):
    """Run fn in a thread; ignore connection errors caused by the kill."""
    box = {}

    def run():
        try:
            box["res"] = fn()
        except Exception as e:
            box["err"] = repr(e)

    t = __import__("threading").Thread(target=run)
    t.start()
    return box, t


class CrashTests(unittest.TestCase):
    def setUp(self):
        self.srv = Server().start()

    def tearDown(self):
        self.srv.close()

    def _kill_and_restart(self, delay):
        time.sleep(delay)
        self.srv.kill9()
        self.srv.start()

    def test_crash_during_chunk_write(self):
        content = pattern(11, 64 * MB)
        st, up = self.srv.req("POST", "/uploads",
                              {"name": "big", "total_size": len(content),
                               "chunk_size": 32 * MB}, "tok-t1")
        uid = up["upload_id"]
        chunk0 = content[:32 * MB]
        chunk1 = content[32 * MB:]

        box, t = _swallow(lambda: self.srv.put_chunk(uid, 0, chunk0))
        self._kill_and_restart(0.12)
        t.join(timeout=30)

        # Upload is resumable: complete chunk 0 (replay or fresh write) + chunk 1.
        st, _ = self.srv.put_chunk(uid, 0, chunk0)
        self.assertEqual(st, 200)
        st, _ = self.srv.put_chunk(uid, 1, chunk1)
        self.assertEqual(st, 200)
        st, fin = self.srv.finalize(uid, content)
        self.assertEqual(st, 200, fin)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(st, 200)
        self.assertEqual(data, content)

    def test_crash_during_finalize(self):
        content = pattern(12, 150 * MB)
        uid, _ = self.srv.upload("f.bin", content, chunk_size=32 * MB)

        box, t = _swallow(lambda: self.srv.finalize(uid, content))
        self._kill_and_restart(0.15)
        t.join(timeout=30)

        # Either the artifact committed (retry returns its id) or it did not
        # (retry completes it). Never a half-one.
        st, fin = self.srv.finalize(uid, content)
        self.assertEqual(st, 200, fin)
        aid = fin["artifact_id"]
        st, data = self.srv.download(aid)
        self.assertEqual(st, 200)
        self.assertEqual(data, content)
        st, lst = self.srv.artifacts()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["artifact_id"], aid)

    def test_crash_during_gc(self):
        live = pattern(13, 4 * MB)
        uid, _ = self.srv.upload("live", live)
        st, fin = self.srv.finalize(uid, live)
        self.assertEqual(st, 200)
        live_aid = fin["artifact_id"]

        fillers = []
        for i in range(20):
            f = pattern(200 + i, 1 * MB)
            fillers.append(f)
            fu, _ = self.srv.upload("filler%d" % i, f)
            st, ff = self.srv.finalize(fu, f)
            self.assertEqual(st, 200)
            self.srv.req("DELETE", "/artifacts/%s" % ff["artifact_id"],
                         token="tok-t1")

        box, t = _swallow(lambda: self.srv.gc())
        self._kill_and_restart(0.08)
        t.join(timeout=30)

        # Live artifact's bytes must be intact.
        st, data = self.srv.download(live_aid)
        self.assertEqual(st, 200)
        self.assertEqual(data, live)

        # GC completes on re-run.
        st, gc = self.srv.gc()
        self.assertEqual(st, 200)
        self.assertGreaterEqual(gc["collected"], 0)

        # Store consistency: metadata <-> disk agree exactly.
        st, blobs = self.srv.blobs()
        self.assertEqual(st, 200)
        listed = {b["content_hash"]: b["size"] for b in blobs}
        disk = self.srv.blob_disk_files()
        self.assertEqual(set(listed), set(disk),
                         "metadata/disk divergence: %r" % (
                             {k: listed.get(k) for k in set(listed) ^ set(disk)},))
        for h, size in listed.items():
            self.assertEqual(disk[h], size)
        # live blob still referenced
        self.assertIn(hashlib.sha256(live).hexdigest(), listed)

    def test_crash_during_chunk_write_repeated(self):
        """Several kills across successive chunk writes; upload always recovers."""
        content = pattern(14, 96 * MB)
        st, up = self.srv.req("POST", "/uploads",
                              {"name": "r", "total_size": len(content),
                               "chunk_size": 32 * MB}, "tok-t1")
        uid = up["upload_id"]
        chunks = [content[i * 32 * MB:(i + 1) * 32 * MB] for i in range(3)]
        done = set()
        for attempt_round in range(3):
            for i in range(3):
                if i in done:
                    continue
                box, t = _swallow(lambda i=i: self.srv.put_chunk(uid, i, chunks[i]))
                self._kill_and_restart(0.1)
                t.join(timeout=30)
                st, _ = self.srv.put_chunk(uid, i, chunks[i])
                self.assertEqual(st, 200)
                done.add(i)
        st, fin = self.srv.finalize(uid, content)
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(data, content)


if __name__ == "__main__":
    unittest.main()
