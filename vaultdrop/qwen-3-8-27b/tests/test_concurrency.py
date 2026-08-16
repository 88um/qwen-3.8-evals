"""Concurrency probes: overlapping chunks, racing finalizes, finalize-vs-GC.

Each test asserts an invariant that must hold for EVERY interleaving, so the tests
are correct regardless of exact scheduling. Where a race is the point (finalize vs
GC), the scenario is repeated to make overlap likely.
"""

import concurrent.futures as cf
import hashlib

import pytest

from harness import ADMIN, T1, T2, Vault, cleanup, _sha256


@pytest.fixture()
def v():
    vault = Vault().start()
    yield vault
    cleanup(vault)


def _chunks(data, size):
    return [data[i:i + size] for i in range(0, len(data), size)]


def test_concurrent_overlapping_chunks(v):
    data = bytes(range(256)) * 40            # 10240 bytes
    cs = 512
    parts = _chunks(data, cs)
    upload_id = v.upload(T1, "conc", b"", cs) if False else None
    # start an upload for the real payload
    st, up = v.req("POST", "/uploads", T1,
                   {"name": "conc", "total_size": len(data), "chunk_size": cs})
    upload_id = up["upload_id"]

    # Each chunk index is PUT by TWO threads simultaneously (duplicates), and the
    # indices are issued out of order. All must land consistently.
    def put(index):
        chunk = parts[index]
        return v.req("PUT", f"/uploads/{upload_id}/chunks/{index}", T1,
                     body=chunk, headers={"X-Chunk-SHA256": _sha256(chunk)})[0]

    jobs = []
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        order = list(range(len(parts)))
        order.sort(key=lambda i: (i * 37) % len(parts))      # scrambled issue order
        for idx in order:
            jobs.append(ex.submit(put, idx))
            jobs.append(ex.submit(put, idx))                 # duplicate
        codes = [f.result() for f in cf.as_completed(jobs)]
    assert all(c in (200, 201) for c in codes), codes

    st, out = v.finalize(T1,upload_id, data)
    assert st == 200, out
    st, body = v.download(T1, out["artifact_id"])
    assert st == 200 and body == data


def test_concurrent_finalize_single_artifact(v):
    data = b"racing-finalize-content" * 50
    upload_id = v.upload(T1, "race", data, chunk_size=256)

    results = []

    def do_finalize():
        return v.req("POST", f"/uploads/{upload_id}/finalize", T1,
                     {"sha256": _sha256(data), "size": len(data)})

    with cf.ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(do_finalize) for _ in range(2)]
        results = [f.result() for f in futs]

    # Exactly one artifact exists afterward.
    st, lst = v.req("GET", "/artifacts", T1)
    matching = [r for r in lst if r["sha256"] == _sha256(data)]
    assert len(matching) == 1, lst
    the_id = matching[0]["artifact_id"]

    # Both callers got a coherent answer: the winner's id (or a clear conflict),
    # never a second artifact id.
    returned_ids = [out["artifact_id"] for st, out in results if st == 200]
    assert all(i == the_id for i in returned_ids), results
    assert all(st in (200, 409) for st, _ in results), results
    assert len(returned_ids) >= 1

    # The single artifact is fully readable.
    st, body = v.download(T1, the_id)
    assert st == 200 and body == data


def test_finalize_vs_gc(v):
    """A finalize about to reference a refcount-0 blob races a GC pass. The bytes
    must survive for the finalize (promise 4). Repeated to make overlap likely."""
    for it in range(4):
        payload = f"gc-race-{it}-".encode() + bytes(range(256)) * 8
        # t1 stores then deletes -> blob for `payload` drops to refcount 0.
        a1 = v.finalize_upload(T1, f"seed{it}", payload, chunk_size=1024)
        st, _ = v.req("DELETE", f"/artifacts/{a1}", T1)
        assert st == 200

        # t2 uploads the same bytes (its own chunks).
        upload_id = v.upload(T2, f"victim{it}", payload, chunk_size=1024)

        def do_finalize():
            return v.req("POST", f"/uploads/{upload_id}/finalize", T2,
                         {"sha256": _sha256(payload), "size": len(payload)})

        def do_gc():
            return v.req("POST", "/admin/gc", ADMIN)

        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            fa = ex.submit(do_finalize)
            fg = ex.submit(do_gc)
            fin = fa.result()
            gc = fg.result()

        # The finalize must succeed and its bytes must survive the GC.
        assert fin[0] == 200, (it, fin)
        aid = fin[1]["artifact_id"]
        st, body = v.download(T2, aid)
        assert st == 200 and body == payload, (it, st)

        # The blob now has exactly t2's single reference.
        st, blobs = v.blobs()
        row = [b for b in blobs if b["content_hash"] == _sha256(payload)]
        assert len(row) == 1 and row[0]["refcount"] == 1, (it, row)
