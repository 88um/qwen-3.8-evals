"""Deterministic crash-remnant recovery probes.

Each test SIGKILLs the server, plants the exact filesystem/metadata remnant a
particular crash interleaving would leave, restarts, and asserts the remnant
is swept and the affected operation is resumable/consistent.
"""

import hashlib
import os
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _harness import Server  # noqa: E402

MB = 1024 * 1024
CONTENT = bytes(range(256)) * 2048          # 512 KiB


def pattern(seed, size):
    base = bytes((i + seed) % 256 for i in range(4096))
    out = bytearray()
    while len(out) < size:
        out += base
    return bytes(out[:size])


class RecoveryProbeTests(unittest.TestCase):
    def setUp(self):
        self.srv = Server().start()

    def tearDown(self):
        self.srv.close()

    def _stop(self):
        self.srv.kill9()

    def _db(self):
        return sqlite3.connect(os.path.join(self.srv.state_dir, "vaultdrop.db"))

    def _plant(self, rel, data=b"partial"):
        path = os.path.join(self.srv.state_dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)

    def test_partial_chunk_temp_swept(self):
        st, up = self.srv.req("POST", "/uploads",
                              {"name": "a", "total_size": len(CONTENT),
                               "chunk_size": len(CONTENT)}, "tok-t1")
        uid = up["upload_id"]
        self._stop()
        self._plant("chunks/%s/.tmp/0.deadbeef" % uid, b"partial-bytes")
        self.srv.start()
        tmpd = os.path.join(self.srv.state_dir, "chunks", uid, ".tmp")
        self.assertFalse(os.path.exists(os.path.join(tmpd, "0.deadbeef")))
        # upload fully resumable
        st, _ = self.srv.put_chunk(uid, 0, CONTENT)
        self.assertEqual(st, 200)
        st, fin = self.srv.finalize(uid, CONTENT)
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(data, CONTENT)

    def test_assemble_temp_swept(self):
        self._stop()
        self._plant("tmp/assemble.deadbeef", b"partial-assembly")
        self.srv.start()
        self.assertFalse(
            os.path.exists(os.path.join(self.srv.state_dir, "tmp", "assemble.deadbeef")))

    def test_orphan_blob_bytes_swept(self):
        sha = hashlib.sha256(CONTENT).hexdigest()
        self._stop()
        self._plant("blobs/%s/%s" % (sha[:2], sha), CONTENT)
        self.srv.start()
        self.assertFalse(os.path.exists(
            os.path.join(self.srv.state_dir, "blobs", sha[:2], sha)))
        # content still fully serviceable afterwards
        uid, _ = self.srv.upload("a", CONTENT)
        st, fin = self.srv.finalize(uid, CONTENT)
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(data, CONTENT)

    def test_tombstone_with_bytes_purged(self):
        uid, _ = self.srv.upload("a", CONTENT)
        st, fin = self.srv.finalize(uid, CONTENT)
        self.assertEqual(st, 200)
        st, _ = self.srv.req("DELETE", "/artifacts/%s" % fin["artifact_id"],
                             token="tok-t1")
        self.assertEqual(st, 200)
        sha = hashlib.sha256(CONTENT).hexdigest()
        self._stop()
        # simulate GC having committed the tombstone but crashed before unlink
        c = self._db()
        c.execute("UPDATE blobs SET state='deleted' WHERE content_hash=?", (sha,))
        c.commit()
        c.close()
        self.srv.start()
        st, blobs = self.srv.blobs()
        self.assertEqual(blobs, [])
        self.assertFalse(os.path.exists(
            os.path.join(self.srv.state_dir, "blobs", sha[:2], sha)))
        # resurrectable via a fresh finalize
        uid2, _ = self.srv.upload("b", CONTENT)
        st, fin2 = self.srv.finalize(uid2, CONTENT)
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin2["artifact_id"])
        self.assertEqual(data, CONTENT)

    def test_quarantine_file_swept(self):
        sha = hashlib.sha256(CONTENT).hexdigest()
        self._stop()
        self._plant("blobs/%s/%s.gcq.1234" % (sha[:2], sha), CONTENT)
        self.srv.start()
        self.assertFalse(os.path.exists(
            os.path.join(self.srv.state_dir, "blobs", sha[:2],
                         "%s.gcq.1234" % sha)))

    def test_orphan_chunk_final_swept(self):
        st, up = self.srv.req("POST", "/uploads",
                              {"name": "a", "total_size": len(CONTENT),
                               "chunk_size": len(CONTENT)}, "tok-t1")
        uid = up["upload_id"]
        self._stop()
        self._plant("chunks/%s/0" % uid, b"orphan-chunk")
        self.srv.start()
        self.assertFalse(os.path.exists(
            os.path.join(self.srv.state_dir, "chunks", uid, "0")))
        st, _ = self.srv.put_chunk(uid, 0, CONTENT)
        self.assertEqual(st, 200)
        st, fin = self.srv.finalize(uid, CONTENT)
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(data, CONTENT)

    def test_chunk_row_without_file_dropped(self):
        uid, _ = self.srv.upload("a", CONTENT)
        st, _ = self.srv.put_chunk(uid, 0, CONTENT)
        self.assertEqual(st, 200)
        self._stop()
        os.unlink(os.path.join(self.srv.state_dir, "chunks", uid, "0"))
        self.srv.start()
        st, desc = self.srv.req("GET", "/uploads/%s" % uid, token="tok-t1")
        self.assertEqual(st, 200)
        self.assertEqual(desc["received"], [])
        # resumable via re-PUT
        st, _ = self.srv.put_chunk(uid, 0, CONTENT)
        self.assertEqual(st, 200)
        st, fin = self.srv.finalize(uid, CONTENT)
        self.assertEqual(st, 200)
        st, data = self.srv.download(fin["artifact_id"])
        self.assertEqual(data, CONTENT)

    def test_gc_crash_remnants_swept_and_rerunnable(self):
        """Plant the remnants of a GC killed mid-collection, restart, and verify
        the pass reruns to completion without touching referenced bytes."""
        live = pattern(7, 2 * MB)
        uid, _ = self.srv.upload("live", live)
        st, fin = self.srv.finalize(uid, live)
        self.assertEqual(st, 200)
        live_aid = fin["artifact_id"]
        live_sha = hashlib.sha256(live).hexdigest()

        dead = pattern(8, 1 * MB)
        dead_sha = hashlib.sha256(dead).hexdigest()
        du, _ = self.srv.upload("dead", dead)
        st, df = self.srv.finalize(du, dead)
        self.assertEqual(st, 200)
        self.srv.req("DELETE", "/artifacts/%s" % df["artifact_id"], token="tok-t1")

        self._stop()
        # remnant A: tombstone committed, unlink never ran (bytes still present)
        c = self._db()
        c.execute("UPDATE blobs SET state='deleted' WHERE content_hash=?",
                  (dead_sha,))
        c.commit()
        c.close()
        # remnant B: quarantined bytes, unlink never ran
        self._plant("blobs/%s/%s.gcq.abcd" % (dead_sha[:2], dead_sha), dead)
        self.srv.start()

        st, blobs = self.srv.blobs()
        self.assertEqual(st, 200)
        self.assertEqual([b["content_hash"] for b in blobs], [live_sha])
        disk = self.srv.blob_disk_files()
        self.assertEqual(set(disk), {live_sha})
        # live bytes intact
        st, data = self.srv.download(live_aid)
        self.assertEqual(data, live)
        # GC reruns cleanly
        st, gc = self.srv.gc()
        self.assertEqual(st, 200)
        self.assertEqual(gc["collected"], 0)


if __name__ == "__main__":
    unittest.main()
