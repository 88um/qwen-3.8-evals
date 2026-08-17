import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from helpers import VaultDropInstance, APIClient, upload_artifact


class TestBasicAPI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = VaultDropInstance()
        cls.instance.start()
        cls.t1 = APIClient(cls.instance.port, 'tok-t1')
        cls.t2 = APIClient(cls.instance.port, 'tok-t2')
        cls.admin = APIClient(cls.instance.port, 'tok-admin')
        cls.noauth = APIClient(cls.instance.port)

    @classmethod
    def tearDownClass(cls):
        cls.instance.cleanup()

    def test_health(self):
        s, r = self.noauth.health()
        self.assertEqual(s, 200)
        self.assertEqual(r['status'], 'ok')

    def test_unauthorized(self):
        s, _ = self.noauth.create_upload('x', 10, 10)
        self.assertEqual(s, 401)

    def test_bad_token(self):
        bad = APIClient(self.instance.port, 'bad-token')
        s, _ = bad.create_upload('x', 10, 10)
        self.assertEqual(s, 401)

    def test_upload_and_download(self):
        data = b'hello world this is a test artifact'
        artifact_id = upload_artifact(self.t1, 'test.bin', data, chunk_size=10)
        self.assertTrue(artifact_id.startswith('art_'))

        s, body = self.t1.get_artifact(artifact_id)
        self.assertEqual(s, 200)
        self.assertEqual(body, data)

    def test_empty_artifact(self):
        data = b''
        sha = hashlib.sha256(data).hexdigest()
        s, resp = self.t1.create_upload('empty.bin', 0, 1)
        self.assertEqual(s, 200)
        uid = resp['upload_id']

        s, resp = self.t1.finalize(uid, sha, 0)
        self.assertEqual(s, 200)
        aid = resp['artifact_id']

        s, body = self.t1.get_artifact(aid)
        self.assertEqual(s, 200)
        self.assertEqual(body, data)

    def test_list_artifacts(self):
        data = b'list test data'
        aid = upload_artifact(self.t1, 'listed.bin', data)

        s, items = self.t1.list_artifacts()
        self.assertEqual(s, 200)
        ids = [i['artifact_id'] for i in items]
        self.assertIn(aid, ids)

        found = [i for i in items if i['artifact_id'] == aid][0]
        self.assertEqual(found['name'], 'listed.bin')
        self.assertEqual(found['size'], len(data))
        self.assertEqual(found['sha256'], hashlib.sha256(data).hexdigest())

    def test_delete_artifact(self):
        data = b'delete me'
        aid = upload_artifact(self.t1, 'delete.bin', data)

        s, _ = self.t1.delete_artifact(aid)
        self.assertEqual(s, 200)

        s, _ = self.t1.get_artifact(aid)
        self.assertEqual(s, 404)

        s, _ = self.t1.delete_artifact(aid)
        self.assertEqual(s, 404)

    def test_chunk_idempotent_replay(self):
        data = b'A' * 20
        s, resp = self.t1.create_upload('idem.bin', 20, 10)
        uid = resp['upload_id']
        chunk0 = data[:10]

        s1, _ = self.t1.upload_chunk(uid, 0, chunk0)
        self.assertEqual(s1, 200)

        s2, _ = self.t1.upload_chunk(uid, 0, chunk0)
        self.assertEqual(s2, 200)

    def test_chunk_conflict_replay(self):
        data = b'B' * 20
        s, resp = self.t1.create_upload('conflict.bin', 20, 10)
        uid = resp['upload_id']
        chunk0a = b'0123456789'
        chunk0b = b'9876543210'

        s, _ = self.t1.upload_chunk(uid, 0, chunk0a)
        self.assertEqual(s, 200)

        s, _ = self.t1.upload_chunk(uid, 0, chunk0b)
        self.assertEqual(s, 409)

    def test_finalize_sha_mismatch(self):
        data = b'mismatch test'
        s, resp = self.t1.create_upload('mis.bin', len(data), len(data))
        uid = resp['upload_id']
        self.t1.upload_chunk(uid, 0, data)

        s, resp = self.t1.finalize(uid, 'bad' * 20 + 'aa', len(data))
        self.assertEqual(s, 422)

    def test_finalize_size_mismatch(self):
        data = b'size mismatch'
        sha = hashlib.sha256(data).hexdigest()
        s, resp = self.t1.create_upload('siz.bin', len(data), len(data))
        uid = resp['upload_id']
        self.t1.upload_chunk(uid, 0, data)

        s, resp = self.t1.finalize(uid, sha, len(data) + 1)
        self.assertEqual(s, 422)

    def test_chunk_after_finalize(self):
        data = b'no late chunks'
        aid = upload_artifact(self.t1, 'sealed.bin', data)

        s, resp = self.t1.create_upload('late.bin', 20, 10)
        uid2 = resp['upload_id']
        s, _ = self.t1.upload_chunk(uid2, 0, b'0123456789')
        self.assertEqual(s, 200)

    def test_get_upload_status(self):
        s, resp = self.t1.create_upload('status.bin', 30, 10)
        uid = resp['upload_id']

        s, info = self.t1.get_upload(uid)
        self.assertEqual(s, 200)
        self.assertEqual(info['state'], 'uploading')
        self.assertEqual(info['received'], [])

        self.t1.upload_chunk(uid, 1, b'B' * 10)
        s, info = self.t1.get_upload(uid)
        self.assertEqual(info['received'], [1])

    def test_admin_blobs(self):
        s, blobs = self.admin.admin_blobs()
        self.assertEqual(s, 200)
        self.assertIsInstance(blobs, list)

    def test_admin_gc(self):
        s, result = self.admin.gc()
        self.assertEqual(s, 200)
        self.assertIn('scanned', result)
        self.assertIn('collected', result)
        self.assertIn('bytes_freed', result)

    def test_admin_validate(self):
        s, result = self.admin.admin_validate()
        self.assertEqual(s, 200)
        self.assertIn('total', result)
        self.assertIn('valid', result)

    def test_gc_frees_unreferenced_blob(self):
        data = os.urandom(100)
        aid = upload_artifact(self.t1, 'gc_target.bin', data)
        content_hash = hashlib.sha256(data).hexdigest()

        s, blobs = self.admin.admin_blobs()
        hashes = [b['content_hash'] for b in blobs]
        self.assertIn(content_hash, hashes)

        self.t1.delete_artifact(aid)

        s, result = self.admin.gc()
        self.assertEqual(s, 200)
        self.assertGreaterEqual(result['collected'], 1)

        s, blobs = self.admin.admin_blobs()
        hashes = [b['content_hash'] for b in blobs]
        self.assertNotIn(content_hash, hashes)

    def test_dedup_same_content(self):
        data = os.urandom(50)
        aid1 = upload_artifact(self.t1, 'dup1.bin', data)
        aid2 = upload_artifact(self.t1, 'dup2.bin', data)

        self.assertNotEqual(aid1, aid2)

        content_hash = hashlib.sha256(data).hexdigest()
        s, blobs = self.admin.admin_blobs()
        matching = [b for b in blobs if b['content_hash'] == content_hash]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['refcount'], 2)

    def test_concurrent_finalize_same_upload(self):
        data = b'concurrent finalize test'
        sha = hashlib.sha256(data).hexdigest()
        s, resp = self.t1.create_upload('cf.bin', len(data), len(data))
        uid = resp['upload_id']
        self.t1.upload_chunk(uid, 0, data)

        import concurrent.futures
        def do_finalize():
            c = APIClient(self.instance.port, 'tok-t1')
            return c.finalize(uid, sha, len(data))

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(do_finalize) for _ in range(4)]
            results = [f.result() for f in futures]

        successes = [(s, r) for s, r in results if s == 200]
        conflicts = [(s, r) for s, r in results if s == 409]

        self.assertGreaterEqual(len(successes), 1)
        if len(successes) > 1:
            aids = set(r['artifact_id'] for _, r in successes)
            self.assertEqual(len(aids), 1)

    def test_missing_chunks_finalize(self):
        s, resp = self.t1.create_upload('partial.bin', 30, 10)
        uid = resp['upload_id']
        self.t1.upload_chunk(uid, 0, b'A' * 10)

        sha = hashlib.sha256(b'A' * 10 + b'\x00' * 20).hexdigest()
        s, resp = self.t1.finalize(uid, sha, 30)
        self.assertEqual(s, 400)

    def test_upload_not_found(self):
        s, _ = self.t1.get_upload('up_nonexistent')
        self.assertEqual(s, 404)

    def test_artifact_not_found(self):
        s, _ = self.t1.get_artifact('art_nonexistent')
        self.assertEqual(s, 404)


if __name__ == '__main__':
    unittest.main()
