"""Crash-recovery probes: SIGKILL the live service mid-operation, restart against
the same state dir, and assert the recovery invariants.

Fresh (uncached) payloads keep operations cold/slow so an early signal reliably
lands mid-operation; each iteration uses independent state, and the asserted
invariant holds for EVERY crash landing point, so the tests are valid regardless
of exact scheduling.
"""

import concurrent.futures as cf
import os
import signal
import time

import pytest

from harness import ADMIN, T1, Vault, cleanup, _sha256


@pytest.fixture()
def v():
    vault = Vault().start()
    yield vault
    cleanup(vault)


def _signal_mid(v, delay):
    """SIGKILL the server `delay` seconds from now (if still alive), then restart."""
    time.sleep(delay)
    if v.proc.poll() is None:
        v.kill(signal.SIGKILL)
    v.wait_dead()
    v.restart()


def test_crash_during_chunk_write(v):
    """A maximal chunk write interrupted by SIGKILL; the upload must be resumable
    and no partial chunk may be treated as complete."""
    for k in range(4):
        chunk = os.urandom(16 * 1024 * 1024)            # fresh, uncached, maximal
        st, up = v.req("POST", "/uploads", T1,
                       {"name": f"cw{k}", "total_size": len(chunk),
                        "chunk_size": len(chunk)})
        upload_id = up["upload_id"]

        fut_holder = {}

        def put():
            fut_holder["r"] = v.req("PUT", f"/uploads/{upload_id}/chunks/0", T1,
                                    body=chunk,
                                    headers={"X-Chunk-SHA256": _sha256(chunk)})

        with cf.ThreadPoolExecutor(1) as ex:
            ex.submit(put)
            _signal_mid(v, 0.01)

        # Recovery: re-PUT the chunk (idempotent if it landed, fresh if not) and
        # finalize must succeed with the exact bytes.
        st, _ = v.req("PUT", f"/uploads/{upload_id}/chunks/0", T1,
                      body=chunk, headers={"X-Chunk-SHA256": _sha256(chunk)})
        assert st in (200, 201), (k, st)
        st, out = v.finalize(T1, upload_id, chunk)
        assert st == 200, (k, st, out)
        st, body = v.download(T1, out["artifact_id"])
        assert st == 200 and body == chunk, (k, st)


def test_crash_during_finalize(v):
    """A large finalize interrupted between assembly and commit; afterward there is
    either a complete, readable artifact or none (re-finalizable). Never a half-one."""
    for k in range(4):
        data = os.urandom(16 * 1024 * 1024)             # fresh, uncached
        upload_id = v.upload(T1, f"cf{k}", data, chunk_size=1024 * 1024)

        def fin():
            return v.req("POST", f"/uploads/{upload_id}/finalize", T1,
                         {"sha256": _sha256(data), "size": len(data)})

        with cf.ThreadPoolExecutor(1) as ex:
            ex.submit(fin)
            _signal_mid(v, 0.01)

        st, state = v.req("GET", f"/uploads/{upload_id}", T1)
        assert st == 200
        if state["state"] == "finalized":
            st, body = v.download(T1, state["artifact_id"])
            assert st == 200 and body == data, (k, st)
        else:
            assert state["state"] == "open", (k, state)
            st, out = v.finalize(T1, upload_id, data)
            assert st == 200, (k, st, out)
            st, body = v.download(T1, out["artifact_id"])
            assert st == 200 and body == data, (k, st)


def test_crash_during_gc(v):
    """A GC pass over many collectable blobs, interrupted; referenced bytes must
    survive and live artifacts remain fully readable."""
    live = []
    for i in range(16):
        payload = f"live-{i}-".encode() + os.urandom(512 * 1024)
        live.append((i, payload,
                     v.finalize_upload(T1, f"live{i}", payload, chunk_size=1024 * 1024)))
    # many collectable blobs make the GC's file phase long enough to overlap the signal
    for i in range(700):
        payload = f"dead-{i}-".encode() + os.urandom(512 * 1024)
        aid = v.finalize_upload(T1, f"dead{i}", payload, chunk_size=1024 * 1024)
        st, _ = v.req("DELETE", f"/artifacts/{aid}", T1)
        assert st == 200

    def gc():
        return v.req("POST", "/admin/gc", ADMIN)

    with cf.ThreadPoolExecutor(1) as ex:
        ex.submit(gc)
        _signal_mid(v, 0.005)

    # Recovery: every live artifact is still fully readable with its exact bytes.
    for i, payload, aid in live:
        st, body = v.download(T1, aid)
        assert st == 200 and body == payload, (i, st)

    # A subsequent clean GC completes collection without touching live blobs.
    st, gcres = v.gc()
    assert st == 200
    for i, payload, aid in live:
        st, body = v.download(T1, aid)
        assert st == 200 and body == payload, (i, st)


def test_restart_is_clean_after_plain_crash(v):
    """A SIGKILL at an arbitrary point leaves a clean, restartable service with all
    prior artifacts intact."""
    payload = b"survivor-payload" * 100
    aid = v.finalize_upload(T1, "survivor", payload, chunk_size=512)
    v.kill(signal.SIGKILL)
    v.wait_dead()
    v.restart()
    st, body = v.download(T1, aid)
    assert st == 200 and body == payload
