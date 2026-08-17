import hashlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from helpers import VaultDropInstance, APIClient, upload_artifact


class TestTenantIsolation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.instance = VaultDropInstance()
        cls.instance.start()
        cls.t1 = APIClient(cls.instance.port, 'tok-t1')
        cls.t2 = APIClient(cls.instance.port, 'tok-t2')
        cls.admin = APIClient(cls.instance.port, 'tok-admin')

    @classmethod
    def tearDownClass(cls):
        cls.instance.cleanup()

    def test_cross_tenant_artifact_download_404(self):
        data = b'tenant1 secret data'
        aid = upload_artifact(self.t1, 'secret.bin', data)

        s, _ = self.t2.get_artifact(aid)
        self.assertEqual(s, 404)

    def test_cross_tenant_artifact_delete_404(self):
        data = b'tenant1 no delete'
        aid = upload_artifact(self.t1, 'nodelete.bin', data)

        s, _ = self.t2.delete_artifact(aid)
        self.assertEqual(s, 404)

        s, body = self.t1.get_artifact(aid)
        self.assertEqual(s, 200)
        self.assertEqual(body, data)

    def test_cross_tenant_upload_status_404(self):
        s, resp = self.t1.create_upload('private.bin', 100, 100)
        uid = resp['upload_id']

        s, _ = self.t2.get_upload(uid)
        self.assertEqual(s, 404)

    def test_cross_tenant_chunk_upload_404(self):
        s, resp = self.t1.create_upload('nochunk.bin', 10, 10)
        uid = resp['upload_id']

        s, _ = self.t2.upload_chunk(uid, 0, b'0123456789')
        self.assertEqual(s, 404)

    def test_cross_tenant_finalize_404(self):
        data = b'no finalize'
        sha = hashlib.sha256(data).hexdigest()
        s, resp = self.t1.create_upload('nofin.bin', len(data), len(data))
        uid = resp['upload_id']
        self.t1.upload_chunk(uid, 0, data)

        s, _ = self.t2.finalize(uid, sha, len(data))
        self.assertEqual(s, 404)

    def test_listing_only_own_artifacts(self):
        data1 = os.urandom(30)
        data2 = os.urandom(30)
        aid1 = upload_artifact(self.t1, 'mine1.bin', data1)
        aid2 = upload_artifact(self.t2, 'mine2.bin', data2)

        s, items = self.t1.list_artifacts()
        self.assertEqual(s, 200)
        ids = [i['artifact_id'] for i in items]
        self.assertIn(aid1, ids)
        self.assertNotIn(aid2, ids)

        s, items = self.t2.list_artifacts()
        self.assertEqual(s, 200)
        ids = [i['artifact_id'] for i in items]
        self.assertIn(aid2, ids)
        self.assertNotIn(aid1, ids)

    def test_cross_tenant_dedup_invisible(self):
        data = os.urandom(64)
        content_hash = hashlib.sha256(data).hexdigest()

        aid1 = upload_artifact(self.t1, 'dedup1.bin', data)
        aid2 = upload_artifact(self.t2, 'dedup2.bin', data)

        self.assertNotEqual(aid1, aid2)

        s, body1 = self.t1.get_artifact(aid1)
        self.assertEqual(s, 200)
        self.assertEqual(body1, data)

        s, body2 = self.t2.get_artifact(aid2)
        self.assertEqual(s, 200)
        self.assertEqual(body2, data)

        s, _ = self.t2.get_artifact(aid1)
        self.assertEqual(s, 404)

        s, _ = self.t1.get_artifact(aid2)
        self.assertEqual(s, 404)

        s, blobs = self.admin.admin_blobs()
        matching = [b for b in blobs if b['content_hash'] == content_hash]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]['refcount'], 2)

    def test_dedup_delete_one_tenant_other_survives(self):
        data = os.urandom(48)
        aid1 = upload_artifact(self.t1, 'shared1.bin', data)
        aid2 = upload_artifact(self.t2, 'shared2.bin', data)

        self.t1.delete_artifact(aid1)
        self.admin.gc()

        s, body = self.t2.get_artifact(aid2)
        self.assertEqual(s, 200)
        self.assertEqual(body, data)

    def test_id_probing_returns_404_not_403(self):
        data = b'probing test'
        aid = upload_artifact(self.t1, 'probe.bin', data)

        s, resp = self.t2.get_artifact(aid)
        self.assertEqual(s, 404)
        import json
        body = json.loads(resp)
        self.assertIn('error', body)

        s2, resp2 = self.t2.get_artifact('art_00000000000000000000000000000000')
        self.assertEqual(s2, 404)

    def test_admin_blobs_no_tenant_identity(self):
        data = os.urandom(32)
        upload_artifact(self.t1, 'admin_check.bin', data)

        s, blobs = self.admin.admin_blobs()
        self.assertEqual(s, 200)
        for blob in blobs:
            self.assertNotIn('tenant_id', blob)
            self.assertIn('content_hash', blob)
            self.assertIn('refcount', blob)

    def test_admin_cannot_use_tenant_endpoints(self):
        s, _ = self.admin.list_artifacts()
        self.assertEqual(s, 401)

        s, _ = self.admin.create_upload('admin.bin', 10, 10)
        self.assertEqual(s, 401)

    def test_tenant_cannot_use_admin_endpoints(self):
        s, _ = self.t1.gc()
        self.assertEqual(s, 401)

        s, _ = self.t1.admin_blobs()
        self.assertEqual(s, 401)

        s, _ = self.t1.admin_validate()
        self.assertEqual(s, 401)


if __name__ == '__main__':
    unittest.main()
