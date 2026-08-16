"""Functional and tenant-isolation tests against a live server subprocess."""

import hashlib
import os
import sys
import unittest
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Server  # noqa: E402

CONTENT = bytes(range(256)) * 4096          # 1 MiB, non-repetitive pattern
SHA = hashlib.sha256(CONTENT).hexdigest()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.srv = Server().start()

    def tearDown(self):
        self.srv.close()

    # ------------------------------------------------------------- lifecycle
    def test_full_lifecycle(self):
        uid, _ = self.srv.upload("a.bin", CONTENT)
        st, fin = self.srv.finalize(uid, CONTENT)
        self.assertEqual(st, 200)
        aid = fin["artifact_id"]

        st, data = self.srv.download(aid)
        self.assertEqual(st, 200)
        self.assertEqual(data, CONTENT)

        st, lst = self.srv.artifacts()
        self.assertEqual(st, 200)
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["sha256"], SHA)
        self.assertEqual(lst[0]["size"], len(CONTENT))

        st, _ = self.srv.req("DELETE", "/artifacts/%s" % aid, token="tok-t1")
        self.assertEqual(st, 200)
        st, lst = self.srv.artifacts()
        self.assertEqual(lst, [])

        st, gc = self.srv.gc()
        self.assertEqual(st, 200)
        self.assertEqual(gc["collected"], 1)
        self.assertEqual(gc["bytes_freed"], len(CONTENT))
        st, blobs = self.srv.blobs()
        self.assertEqual(blobs, [])

    def test_chunk_idempotency_and_conflict(self):
        uid, chunks = self.srv.upload("a.bin", CONTENT, chunk_size=1024 * 256)
        # exact replay of every chunk -> 200
        for i, c in enumerate(chunks):
            st, _ = self.srv.put_chunk(uid, i, c)
            self.assertEqual(st, 200)
        # conflicting replay -> 409, and the first-written bytes survive:
        st, _ = self.srv.put_chunk(uid, 0, b"Q" * len(chunks[0]))
        self.assertEqual(st, 409)
        st, fin = self.srv.finalize(uid, CONTENT)
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(data, CONTENT)

    def test_conflicting_replay_concurrent(self):
        uid, chunks = self.srv.upload("a.bin", CONTENT, chunk_size=1024 * 256)
        import concurrent.futures
        rival = bytes((b + 1) % 256 for b in chunks[3])
        def put(i, payload):
            return self.srv.put_chunk(uid, i, payload)[0]
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            futs = [ex.submit(put, 3, chunks[3]) for _ in range(4)]
            futs += [ex.submit(put, 3, rival) for _ in range(4)]
            sts = [f.result() for f in futs]
        self.assertEqual(sorted(sts), [200] * 4 + [409] * 4)
        # whichever bytes committed, finalize against them must verify
        st, _ = self.srv.put_chunk(uid, 3, chunks[3])
        winner_is_original = st == 200
        payload = chunks[3] if winner_is_original else rival
        content = b"".join(chunks[:3] + [payload] + chunks[4:])
        st, fin = self.srv.finalize(uid, content)
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(data, content)

    # ------------------------------------------------------------- validation
    def test_finalize_mismatch(self):
        uid, _ = self.srv.upload("a.bin", CONTENT)
        st, err = self.srv.req("POST", "/uploads/%s/finalize" % uid,
                               {"sha256": SHA, "size": len(CONTENT) + 1},
                               token="tok-t1")
        self.assertEqual(st, 422)
        st, err = self.srv.req("POST", "/uploads/%s/finalize" % uid,
                               {"sha256": "0" * 64, "size": len(CONTENT)},
                               token="tok-t1")
        self.assertEqual(st, 422)
        st, lst = self.srv.artifacts()
        self.assertEqual(lst, [])

    def test_finalize_incomplete(self):
        st, up = self.srv.req("POST", "/uploads",
                              {"name": "a.bin", "total_size": len(CONTENT),
                               "chunk_size": 512 * 1024}, "tok-t1")
        uid = up["upload_id"]
        self.srv.put_chunk(uid, 0, CONTENT[:512 * 1024])
        st, _ = self.srv.finalize(uid, CONTENT)
        self.assertEqual(st, 422)
        st, desc = self.srv.req("GET", "/uploads/%s" % uid, token="tok-t1")
        self.assertEqual(st, 200)
        self.assertEqual(desc["received"], [0])
        self.assertEqual(desc["state"], "active")

    def test_empty_artifact(self):
        st, up = self.srv.req("POST", "/uploads",
                              {"name": "empty", "total_size": 0,
                               "chunk_size": 1024}, "tok-t1")
        st, fin = self.srv.req("POST", "/uploads/%s/finalize" % up["upload_id"],
                               {"sha256": hashlib.sha256(b"").hexdigest(),
                                "size": 0}, "tok-t1")
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual((st, data), (200, b""))

    def test_late_chunk_after_finalize(self):
        uid, _ = self.srv.upload("a.bin", CONTENT)
        st, fin = self.srv.finalize(uid, CONTENT)
        self.assertEqual(st, 200)
        st, _ = self.srv.put_chunk(uid, 0, CONTENT[:1024 * 1024])
        self.assertEqual(st, 409)
        # artifact untouched
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(data, CONTENT)
        # repeat finalize is coherent
        st, fin2 = self.srv.finalize(uid, CONTENT)
        self.assertEqual(st, 200)
        self.assertEqual(fin2["artifact_id"], fin["artifact_id"])

    def test_wire_limits(self):
        # declared chunk_size above the stated limit
        st, _ = self.srv.req("POST", "/uploads",
                             {"name": "a", "total_size": 100,
                              "chunk_size": 32 * 1024 * 1024 + 1}, "tok-t1")
        self.assertEqual(st, 422)
        # total_size above the stated limit
        st, _ = self.srv.req("POST", "/uploads",
                             {"name": "a", "total_size": 10 * 1024 ** 3 + 1,
                              "chunk_size": 1024}, "tok-t1")
        self.assertEqual(st, 422)
        # PUT body above the stated chunk limit -> 413
        st, up = self.srv.req("POST", "/uploads",
                              {"name": "a", "total_size": 32 * 1024 * 1024,
                               "chunk_size": 32 * 1024 * 1024}, "tok-t1")
        big = b"B" * (32 * 1024 * 1024 + 1)
        st, _ = self.srv.req("PUT", "/uploads/%s/chunks/0" % up["upload_id"], big,
                             "tok-t1",
                             {"X-Chunk-SHA256": hashlib.sha256(big).hexdigest()})
        self.assertEqual(st, 413)
        # PUT body longer than expected size -> 422
        st, up = self.srv.req("POST", "/uploads",
                              {"name": "a", "total_size": 1000,
                               "chunk_size": 500}, "tok-t1")
        over = b"C" * 501
        st, _ = self.srv.req("PUT", "/uploads/%s/chunks/0" % up["upload_id"], over,
                             "tok-t1",
                             {"X-Chunk-SHA256": hashlib.sha256(over).hexdigest()})
        self.assertEqual(st, 422)
        # chunk index out of range
        st, _ = self.srv.req("PUT", "/uploads/%s/chunks/7" % up["upload_id"], b"D" * 500,
                             "tok-t1",
                             {"X-Chunk-SHA256": hashlib.sha256(b"D" * 500).hexdigest()})
        self.assertEqual(st, 422)

    def test_admin_validate(self):
        uid, _ = self.srv.upload("a.bin", CONTENT)
        self.srv.finalize(uid, CONTENT)
        st, val = self.srv.validate()
        self.assertEqual(st, 200)
        self.assertEqual(val["matched"], 1)
        self.assertEqual(val["mismatched"], 0)
        self.assertEqual(val["missing"], 0)

    # ------------------------------------------------------------- isolation
    def test_cross_tenant_dedup_isolation(self):
        uid1, _ = self.srv.upload("a.bin", CONTENT)
        st, fin1 = self.srv.finalize(uid1, CONTENT)
        aid1 = fin1["artifact_id"]

        uid2, _ = self.srv.upload("b.bin", CONTENT, token="tok-t2")
        st, fin2 = self.srv.finalize(uid2, CONTENT, token="tok-t2")
        aid2 = fin2["artifact_id"]

        self.assertNotEqual(aid1, aid2)
        st, data = self.srv.download(aid2, token="tok-t2")
        self.assertEqual(data, CONTENT)
        # t2 cannot read, list, or confirm t1's artifact
        st, _ = self.srv.download(aid1, token="tok-t2")
        self.assertEqual(st, 404)
        st, lst = self.srv.artifacts(token="tok-t2")
        self.assertEqual([a["artifact_id"] for a in lst], [aid2])
        # physical dedup did happen (one blob) but is invisible: admin view
        st, blobs = self.srv.blobs()
        self.assertEqual(len(blobs), 1)
        self.assertEqual(blobs[0]["refcount"], 2)

    def test_cross_tenant_id_probing_indistinguishable(self):
        uid1, _ = self.srv.upload("a.bin", CONTENT)
        _, fin1 = self.srv.finalize(uid1, CONTENT)
        probe_real = uuid.uuid4().hex
        for path in ("/artifacts/%s" % fin1["artifact_id"],
                     "/uploads/%s" % uid1,
                     "/artifacts/%s" % probe_real,
                     "/uploads/%s" % probe_real):
            st1, b1 = self.srv.req("GET", path, token="tok-t2", raw=True)
            st2, b2 = self.srv.req("GET", path.replace(
                fin1["artifact_id"], probe_real).replace(uid1, probe_real),
                token="tok-t2", raw=True)
            self.assertEqual(st1, 404)
            self.assertEqual((st1, b1), (st2, b2))

    def test_admin_with_tenant_token(self):
        for method, path in (("POST", "/admin/gc"), ("GET", "/admin/blobs"),
                             ("GET", "/admin/validate")):
            st, _ = self.srv.req(method, path, token="tok-t1")
            self.assertEqual(st, 401, (method, path))
        # tenant surface with admin token
        st, _ = self.srv.req("GET", "/artifacts", token="tok-admin")
        self.assertEqual(st, 401)
        st, _ = self.srv.req("GET", "/artifacts", token="tok-nobody")
        self.assertEqual(st, 401)
        st, _ = self.srv.req("GET", "/artifacts")
        self.assertEqual(st, 401)

    def test_gc_never_collects_referenced(self):
        c1, c2 = CONTENT, bytes((b + 7) % 256 for b in CONTENT)
        uid1, _ = self.srv.upload("a", c1)
        uid2, _ = self.srv.upload("b", c2)
        a1 = self.srv.finalize(uid1, c1)[1]["artifact_id"]
        a2 = self.srv.finalize(uid2, c2)[1]["artifact_id"]
        st, _ = self.srv.req("DELETE", "/artifacts/%s" % a1, token="tok-t1")
        self.assertEqual(st, 200)                          # c1 -> refcount 0
        st, gc = self.srv.gc()
        self.assertEqual(gc["collected"], 1)
        self.assertEqual(gc["bytes_freed"], len(c1))
        st, data = self.srv.download(a2)
        self.assertEqual(data, c2)
        st, gc = self.srv.gc()
        self.assertEqual(gc["collected"], 0)


if __name__ == "__main__":
    unittest.main()
