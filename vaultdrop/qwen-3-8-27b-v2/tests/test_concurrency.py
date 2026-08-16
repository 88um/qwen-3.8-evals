"""Concurrency tests: chunk races, finalize races, finalize-vs-GC."""

import concurrent.futures
import hashlib
import os
import sys
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


class ChunkRaceTests(unittest.TestCase):
    def setUp(self):
        self.srv = Server().start()

    def tearDown(self):
        self.srv.close()

    def test_concurrent_overlapping_chunks(self):
        content = pattern(1, 8 * MB)
        st, up = self.srv.req("POST", "/uploads",
                              {"name": "big", "total_size": len(content),
                               "chunk_size": 1 * MB}, "tok-t1")
        uid = up["upload_id"]
        chunks = [content[i * MB:(i + 1) * MB] for i in range(8)]

        # 16 threads, each PUTs two chunks; indices overlap heavily.
        jobs = []
        for i in range(16):
            jobs += [(i % 8, chunks[i % 8]), ((i + 3) % 8, chunks[(i + 3) % 8])]

        def put(job):
            idx, payload = job
            return self.srv.put_chunk(uid, idx, payload)[0]

        with concurrent.futures.ThreadPoolExecutor(16) as ex:
            sts = list(ex.map(put, jobs))
        self.assertEqual(set(sts), {200})
        self.assertEqual(len(set(sts)), 1)

        st, fin = self.srv.finalize(uid, content)
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(data, content)
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         hashlib.sha256(content).hexdigest())

    def test_out_of_order_arrival(self):
        content = pattern(2, 5 * MB)
        st, up = self.srv.req("POST", "/uploads",
                              {"name": "ooo", "total_size": len(content),
                               "chunk_size": 1 * MB}, "tok-t1")
        uid = up["upload_id"]
        order = [4, 0, 3, 1, 2]
        for i in order:
            st, _ = self.srv.put_chunk(uid, i, content[i * MB:(i + 1) * MB])
            self.assertEqual(st, 200)
        st, fin = self.srv.finalize(uid, content)
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(data, content)


class FinalizeRaceTests(unittest.TestCase):
    def setUp(self):
        self.srv = Server().start()

    def tearDown(self):
        self.srv.close()

    def test_concurrent_finalize_same_upload(self):
        content = pattern(3, 4 * MB)
        uid, _ = self.srv.upload("r.bin", content, chunk_size=1 * MB)

        def fin():
            return self.srv.finalize(uid, content)

        with concurrent.futures.ThreadPoolExecutor(2) as ex:
            r1, r2 = list(ex.map(lambda _: fin(), range(2)))
        self.assertEqual(r1[0], 200)
        self.assertEqual(r2[0], 200)
        self.assertEqual(r1[1]["artifact_id"], r2[1]["artifact_id"])
        st, lst = self.srv.artifacts()
        self.assertEqual(len(lst), 1)
        self.assertEqual(lst[0]["artifact_id"], r1[1]["artifact_id"])
        st, data = self.srv.download(r1[1]["artifact_id"])
        self.assertEqual(data, content)

    def test_concurrent_finalize_distinct_content_overlaps(self):
        n = 4
        contents = [pattern(10 + i, 8 * MB) for i in range(n)]
        uids = []
        for i, c in enumerate(contents):
            uids.append(self.srv.upload("d%d" % i, c, chunk_size=1 * MB)[0])

        def fin(j):
            return self.srv.finalize(uids[j], contents[j])

        with concurrent.futures.ThreadPoolExecutor(n) as ex:
            res = list(ex.map(fin, range(n)))
        self.assertTrue(all(r[0] == 200 for r in res), res)
        aids = [r[1]["artifact_id"] for r in res]
        self.assertEqual(len(set(aids)), n)
        for aid, c in zip(aids, contents):
            st, data = self.srv.download(aid)
            self.assertEqual(data, c)
        st, lst = self.srv.artifacts()
        self.assertEqual(len(lst), n)

    def test_finalize_vs_gc_race(self):
        """A blob whose refcount is momentarily zero must survive a concurrent
        finalize that is about to reference it (promise 4)."""
        content = pattern(5, 2 * MB)
        sha = hashlib.sha256(content).hexdigest()

        # Prime the blob, then drop its only reference.
        uid0, _ = self.srv.upload("prime", content)
        st, fin0 = self.srv.finalize(uid0, content)
        self.assertEqual(st, 200)
        st, _ = self.srv.req("DELETE", "/artifacts/%s" % fin0["artifact_id"],
                             token="tok-t1")
        self.assertEqual(st, 200)

        rounds = 12
        for r in range(rounds):
            uid, _ = self.srv.upload("race%d" % r, content)

            def fin():
                return self.srv.finalize(uid, content)

            with concurrent.futures.ThreadPoolExecutor(2) as ex:
                ff = ex.submit(fin)
                gf = ex.submit(self.srv.gc)
                fres, gres = ff.result(), gf.result()
            self.assertEqual(fres[0], 200, (r, fres))
            self.assertEqual(gres[0], 200, (r, gres))
            aid = fres[1]["artifact_id"]
            st, data = self.srv.download(aid)
            self.assertEqual(st, 200, (r, st))
            self.assertEqual(data, content, r)
            self.assertEqual(hashlib.sha256(data).hexdigest(), sha)
            st, _ = self.srv.req("DELETE", "/artifacts/%s" % aid, token="tok-t1")
            self.assertEqual(st, 200)

        # store consistent at the end
        st, blobs = self.srv.blobs()
        self.assertEqual(st, 200)
        for b in blobs:
            self.assertGreaterEqual(b["refcount"], 0)

    def test_gc_during_live_download(self):
        content = pattern(6, 4 * MB)
        uid, _ = self.srv.upload("live", content)
        st, fin = self.srv.finalize(uid, content)
        aid = fin["artifact_id"]
        # unreferenced filler blobs so GC has work to do
        for i in range(5):
            filler = pattern(100 + i, 256 * 1024)
            fu, _ = self.srv.upload("filler%d" % i, filler)
            st, f = self.srv.finalize(fu, filler)
            self.assertEqual(st, 200)
            self.srv.req("DELETE", "/artifacts/%s" % f["artifact_id"],
                         token="tok-t1")

        def dl():
            return self.srv.download(aid)

        with concurrent.futures.ThreadPoolExecutor(2) as ex:
            df = ex.submit(dl)
            gres = self.srv.gc()
            dres = df.result()
        self.assertEqual(gres[0], 200)
        self.assertEqual(gres[1]["collected"], 5)
        self.assertEqual(dres[0], 200)
        self.assertEqual(dres[1], content)


if __name__ == "__main__":
    unittest.main()
