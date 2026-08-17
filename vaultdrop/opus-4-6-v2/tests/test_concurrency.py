import concurrent.futures
import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from helpers import VaultDropInstance, APIClient, upload_artifact


class TestConcurrency(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = VaultDropInstance()
        cls.instance.start()
        cls.t1 = APIClient(cls.instance.port, 'tok-t1')
        cls.admin = APIClient(cls.instance.port, 'tok-admin')

    @classmethod
    def tearDownClass(cls):
        cls.instance.cleanup()

    def test_concurrent_chunk_uploads(self):
        chunk_size = 1024
        num_chunks = 20
        data = os.urandom(chunk_size * num_chunks)

        s, resp = self.t1.create_upload('concurrent_chunks.bin', len(data), chunk_size)
        uid = resp['upload_id']

        def upload_chunk(i):
            c = APIClient(self.instance.port, 'tok-t1')
            start = i * chunk_size
            end = start + chunk_size
            return c.upload_chunk(uid, i, data[start:end])

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
            futures = [pool.submit(upload_chunk, i) for i in range(num_chunks)]
            results = [f.result() for f in futures]

        for s, r in results:
            self.assertEqual(s, 200, f'chunk upload failed: {r}')

        sha = hashlib.sha256(data).hexdigest()
        s, resp = self.t1.finalize(uid, sha, len(data))
        self.assertEqual(s, 200)

        s, body = self.t1.get_artifact(resp['artifact_id'])
        self.assertEqual(s, 200)
        self.assertEqual(body, data)

    def test_concurrent_duplicate_chunks(self):
        data = b'X' * 200
        chunk_size = 100

        s, resp = self.t1.create_upload('dup_chunks.bin', len(data), chunk_size)
        uid = resp['upload_id']

        def upload_chunk_0():
            c = APIClient(self.instance.port, 'tok-t1')
            return c.upload_chunk(uid, 0, data[:100])

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(upload_chunk_0) for _ in range(5)]
            results = [f.result() for f in futures]

        statuses = [s for s, _ in results]
        self.assertTrue(all(s == 200 for s in statuses), f'got statuses: {statuses}')

    def test_concurrent_finalizes_different_uploads(self):
        uploads = []
        for i in range(5):
            data = os.urandom(50)
            sha = hashlib.sha256(data).hexdigest()
            s, resp = self.t1.create_upload(f'cf_{i}.bin', len(data), len(data))
            uid = resp['upload_id']
            self.t1.upload_chunk(uid, 0, data)
            uploads.append((uid, sha, len(data)))

        def do_finalize(args):
            uid, sha, size = args
            c = APIClient(self.instance.port, 'tok-t1')
            return c.finalize(uid, sha, size)

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(do_finalize, u) for u in uploads]
            results = [f.result() for f in futures]

        for s, r in results:
            self.assertEqual(s, 200, f'finalize failed: {r}')

        aids = set(r['artifact_id'] for _, r in results)
        self.assertEqual(len(aids), 5)

    def test_finalize_vs_gc_race(self):
        data = os.urandom(200)
        sha = hashlib.sha256(data).hexdigest()

        aid1 = upload_artifact(self.t1, 'fvg1.bin', data)
        self.t1.delete_artifact(aid1)

        s, resp = self.t1.create_upload('fvg2.bin', len(data), len(data))
        uid2 = resp['upload_id']
        self.t1.upload_chunk(uid2, 0, data)

        def do_gc():
            c = APIClient(self.instance.port, 'tok-admin')
            return c.gc()

        def do_finalize():
            c = APIClient(self.instance.port, 'tok-t1')
            return c.finalize(uid2, sha, len(data))

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            gc_future = pool.submit(do_gc)
            fin_future = pool.submit(do_finalize)
            gc_result = gc_future.result()
            fin_result = fin_future.result()

        fin_status, fin_resp = fin_result
        self.assertEqual(fin_status, 200, f'finalize failed: {fin_resp}')

        s, body = self.t1.get_artifact(fin_resp['artifact_id'])
        self.assertEqual(s, 200)
        self.assertEqual(body, data)

    def test_concurrent_uploads_same_content_different_tenants(self):
        data = os.urandom(100)

        def upload_for_tenant(token, name):
            c = APIClient(self.instance.port, token)
            return upload_artifact(c, name, data)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(upload_for_tenant, 'tok-t1', 'cross1.bin')
            f2 = pool.submit(upload_for_tenant, 'tok-t2', 'cross2.bin')
            aid1 = f1.result()
            aid2 = f2.result()

        self.assertNotEqual(aid1, aid2)

        s1, body1 = APIClient(self.instance.port, 'tok-t1').get_artifact(aid1)
        s2, body2 = APIClient(self.instance.port, 'tok-t2').get_artifact(aid2)
        self.assertEqual(s1, 200)
        self.assertEqual(s2, 200)
        self.assertEqual(body1, data)
        self.assertEqual(body2, data)

        s, blobs = self.admin.admin_blobs()
        content_hash = hashlib.sha256(data).hexdigest()
        matching = [b for b in blobs if b['content_hash'] == content_hash]
        self.assertEqual(len(matching), 1)
        self.assertGreaterEqual(matching[0]['refcount'], 2)

    def test_download_during_gc(self):
        data = os.urandom(500)
        aid = upload_artifact(self.t1, 'dlgc.bin', data)

        def do_gc():
            c = APIClient(self.instance.port, 'tok-admin')
            return c.gc()

        def do_download():
            c = APIClient(self.instance.port, 'tok-t1')
            return c.get_artifact(aid)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(do_gc)
            f2 = pool.submit(do_download)
            gc_result = f1.result()
            dl_result = f2.result()

        dl_status, dl_body = dl_result
        self.assertEqual(dl_status, 200)
        self.assertEqual(dl_body, data)


if __name__ == '__main__':
    unittest.main()
