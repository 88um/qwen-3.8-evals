import hashlib
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__)))
from helpers import VaultDropInstance, APIClient, upload_artifact


class TestCrashRecovery(unittest.TestCase):
    def test_crash_during_upload_resumable(self):
        inst = VaultDropInstance()
        try:
            inst.start()
            t1 = APIClient(inst.port, 'tok-t1')

            chunk_size = 1024
            num_chunks = 10
            data = os.urandom(chunk_size * num_chunks)

            s, resp = t1.create_upload('crash_upload.bin', len(data), chunk_size)
            uid = resp['upload_id']

            for i in range(5):
                start = i * chunk_size
                end = start + chunk_size
                s, _ = t1.upload_chunk(uid, i, data[start:end])
                self.assertEqual(s, 200)

            inst.kill()
            time.sleep(0.5)
            inst.restart()
            t1 = APIClient(inst.port, 'tok-t1')

            s, info = t1.get_upload(uid)
            self.assertEqual(s, 200)
            self.assertEqual(info['state'], 'uploading')
            self.assertEqual(sorted(info['received']), [0, 1, 2, 3, 4])

            for i in range(5, num_chunks):
                start = i * chunk_size
                end = start + chunk_size
                s, _ = t1.upload_chunk(uid, i, data[start:end])
                self.assertEqual(s, 200)

            sha = hashlib.sha256(data).hexdigest()
            s, resp = t1.finalize(uid, sha, len(data))
            self.assertEqual(s, 200)

            s, body = t1.get_artifact(resp['artifact_id'])
            self.assertEqual(s, 200)
            self.assertEqual(body, data)
        finally:
            inst.cleanup()

    def test_crash_finalized_artifact_survives(self):
        inst = VaultDropInstance()
        try:
            inst.start()
            t1 = APIClient(inst.port, 'tok-t1')

            data = os.urandom(5000)
            aid = upload_artifact(t1, 'durable.bin', data, chunk_size=1000)

            inst.kill()
            time.sleep(0.5)
            inst.restart()
            t1 = APIClient(inst.port, 'tok-t1')

            s, body = t1.get_artifact(aid)
            self.assertEqual(s, 200)
            self.assertEqual(body, data)

            s, items = t1.list_artifacts()
            self.assertEqual(s, 200)
            ids = [i['artifact_id'] for i in items]
            self.assertIn(aid, ids)
        finally:
            inst.cleanup()

    def test_crash_during_finalize_recovery(self):
        inst = VaultDropInstance()
        try:
            inst.start()
            t1 = APIClient(inst.port, 'tok-t1')

            data = os.urandom(2000)
            sha = hashlib.sha256(data).hexdigest()

            s, resp = t1.create_upload('crash_fin.bin', len(data), len(data))
            uid = resp['upload_id']
            s, _ = t1.upload_chunk(uid, 0, data)
            self.assertEqual(s, 200)

            import concurrent.futures
            def do_finalize():
                c = APIClient(inst.port, 'tok-t1')
                try:
                    return c.finalize(uid, sha, len(data))
                except Exception:
                    return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(do_finalize)
                time.sleep(0.1)
                inst.kill()
                try:
                    result = future.result(timeout=5)
                except Exception:
                    result = None

            time.sleep(0.5)
            inst.restart()
            t1 = APIClient(inst.port, 'tok-t1')

            s, info = t1.get_upload(uid)
            self.assertEqual(s, 200)

            if info['state'] == 'finalized':
                aid = info.get('artifact_id')
                if aid is None:
                    upload = t1.get_upload(uid)
                s, body = t1.get_artifact(aid)
                self.assertEqual(s, 200)
                self.assertEqual(body, data)
            else:
                self.assertEqual(info['state'], 'uploading')
                s, resp = t1.finalize(uid, sha, len(data))
                self.assertEqual(s, 200)
                s, body = t1.get_artifact(resp['artifact_id'])
                self.assertEqual(s, 200)
                self.assertEqual(body, data)
        finally:
            inst.cleanup()

    def test_crash_during_gc_no_data_loss(self):
        inst = VaultDropInstance()
        try:
            inst.start()
            t1 = APIClient(inst.port, 'tok-t1')
            admin = APIClient(inst.port, 'tok-admin')

            data_keep = os.urandom(1000)
            data_delete = os.urandom(1000)
            aid_keep = upload_artifact(t1, 'keep.bin', data_keep)
            aid_delete = upload_artifact(t1, 'delete.bin', data_delete)

            t1.delete_artifact(aid_delete)

            import concurrent.futures
            def do_gc():
                c = APIClient(inst.port, 'tok-admin')
                try:
                    return c.gc()
                except Exception:
                    return None

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(do_gc)
                time.sleep(0.05)
                inst.kill()
                try:
                    future.result(timeout=5)
                except Exception:
                    pass

            time.sleep(0.5)
            inst.restart()
            t1 = APIClient(inst.port, 'tok-t1')

            s, body = t1.get_artifact(aid_keep)
            self.assertEqual(s, 200)
            self.assertEqual(body, data_keep)
        finally:
            inst.cleanup()

    def test_multiple_crashes_state_consistent(self):
        inst = VaultDropInstance()
        try:
            inst.start()
            t1 = APIClient(inst.port, 'tok-t1')

            artifacts = []
            for i in range(3):
                data = os.urandom(500)
                aid = upload_artifact(t1, f'multi_{i}.bin', data)
                artifacts.append((aid, data))

            inst.kill()
            time.sleep(0.3)
            inst.restart()
            t1 = APIClient(inst.port, 'tok-t1')

            for i in range(3, 6):
                data = os.urandom(500)
                aid = upload_artifact(t1, f'multi_{i}.bin', data)
                artifacts.append((aid, data))

            inst.kill()
            time.sleep(0.3)
            inst.restart()
            t1 = APIClient(inst.port, 'tok-t1')

            for aid, data in artifacts:
                s, body = t1.get_artifact(aid)
                self.assertEqual(s, 200)
                self.assertEqual(body, data)
        finally:
            inst.cleanup()


if __name__ == '__main__':
    unittest.main()
