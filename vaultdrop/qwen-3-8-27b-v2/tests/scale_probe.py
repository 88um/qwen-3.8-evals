"""Scale probe (not a unittest): metadata responsiveness at 10k blobs,
concurrent-finalize overlap, and RSS during a multi-hundred-MiB round trip."""

import concurrent.futures
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "tests"))
from _harness import Server  # noqa: E402

MB = 1024 * 1024


def pattern(seed, size):
    base = bytes((i + seed) % 256 for i in range(4096))
    out = bytearray()
    while len(out) < size:
        out += base
    return bytes(out[:size])


def main():
    srv = Server().start()
    try:
        n = 10000
        import struct
        contents = [struct.pack(">I", i) + pattern(i, 4092) for i in range(n)]

        def make(i):
            uid, _ = srv.upload("s%d" % i, contents[i], chunk_size=4096)
            st, fin = srv.finalize(uid, contents[i])
            assert st == 200, (i, st, fin)
            return fin["artifact_id"]

        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            list(ex.map(make, range(n)))
        print("populate %d artifacts: %.1fs" % (n, time.time() - t0))

        t0 = time.time()
        st, lst = srv.artifacts()
        dt = time.time() - t0
        assert st == 200 and len(lst) == n, (st, len(lst))
        print("list %d artifacts: %.2fs" % (n, dt))
        assert dt < 5, "listing too slow"

        t0 = time.time()
        st, blobs = srv.blobs()
        dt = time.time() - t0
        assert st == 200 and len(blobs) == n
        print("admin blob listing: %.2fs" % dt)
        assert dt < 5

        # drop half the references so GC has candidates
        aids = [a["artifact_id"] for a in lst]
        def drop(i):
            return srv.req("DELETE", "/artifacts/%s" % aids[i], token="tok-t1")[0]
        with concurrent.futures.ThreadPoolExecutor(8) as ex:
            sts = list(ex.map(drop, range(0, n, 2)))
        assert all(s == 200 for s in sts)

        t0 = time.time()
        st, gc = srv.gc()
        dt = time.time() - t0
        print("gc over %d candidates: %.2fs -> %r" % (n // 2, dt, gc))
        assert st == 200 and gc["collected"] == n // 2
        assert dt < 5, "gc too slow"

        t0 = time.time()
        st, val = srv.validate()
        dt = time.time() - t0
        print("validate over %d blobs: %.2fs -> %r" % (n // 2, dt, val))
        assert st == 200 and val["mismatched"] == 0 and val["missing"] == 0
        assert dt < 5, "validate too slow"

        # concurrent finalize overlap: 4 x 64 MiB distinct content
        big = [pattern(500 + i, 64 * MB) for i in range(4)]
        uids = [srv.upload("big%d" % i, c, chunk_size=16 * MB)[0] for i, c in enumerate(big)]

        def fin(j):
            return srv.finalize(uids[j], big[j])

        t0 = time.time()
        with concurrent.futures.ThreadPoolExecutor(4) as ex:
            res = list(ex.map(fin, range(4)))
        t_par = time.time() - t0
        assert all(r[0] == 200 for r in res)
        for j, r in enumerate(res):
            st, data = srv.download(r[1]["artifact_id"])
            assert data == big[j]

        t0 = time.time()
        for j in range(4):
            srv.finalize(uids[j], big[j])   # already-finalized: metadata path
        t_meta = time.time() - t0
        print("4x64MiB concurrent finalize: %.2fs (metadata repeat path %.2fs)"
              % (t_par, t_meta))

        # RSS during a 256 MiB download
        content = pattern(777, 256 * MB)
        uid, _ = srv.upload("rss", content, chunk_size=32 * MB)
        st, fin = srv.finalize(uid, content)
        assert st == 200
        aid = fin["artifact_id"]
        pid = srv.proc.pid
        peaks = []
        stop = []
        def sample():
            while not stop:
                out = os.popen("ps -o rss= -p %d" % pid).read().strip()
                if out:
                    peaks.append(int(out))
                time.sleep(0.01)
        th = threading.Thread(target=sample)
        th.start()
        st, data = srv.download(aid)
        stop.append(1)
        th.join()
        assert data == content
        print("256MiB round trip: peak RSS %.1f MiB" % (max(peaks) / 1024.0))
        assert max(peaks) / 1024.0 < 512, "memory ceiling blown"

        print("SCALE OK")
    finally:
        srv.close()


if __name__ == "__main__":
    main()
