"""Cross-tenant isolation (promise 1): dedup invisibility and ID probing."""

import hashlib

import pytest

from harness import T1, T2, Vault, cleanup, _sha256


@pytest.fixture()
def v():
    vault = Vault().start()
    yield vault
    cleanup(vault)


def test_cross_tenant_dedup_invisible(v):
    data = b"identical-bytes-both-tenants"
    a1 = v.finalize_upload(T1, "x1", data, chunk_size=16)
    a2 = v.finalize_upload(T2, "x2", data, chunk_size=16)

    # each tenant has its own artifact id
    assert a1 != a2

    # each can download its own
    st1, b1 = v.download(T1, a1)
    st2, b2 = v.download(T2, a2)
    assert st1 == 200 and b1 == data
    assert st2 == 200 and b2 == data

    # t2 CANNOT download t1's artifact
    st, _ = v.download(T2, a1)
    assert st == 404

    # listings are disjoint: neither sees the other's artifact
    st, l1 = v.req("GET", "/artifacts", T1)
    st2, l2 = v.req("GET", "/artifacts", T2)
    ids1 = {r["artifact_id"] for r in l1}
    ids2 = {r["artifact_id"] for r in l2}
    assert a1 in ids1 and a2 not in ids1
    assert a2 in ids2 and a1 not in ids2

    # dedup happened at the byte layer (one blob, refcount 2) but exposes only a count
    st, blobs = v.blobs()
    row = [b for b in blobs if b["content_hash"] == _sha256(data)]
    assert len(row) == 1 and row[0]["refcount"] == 2
    # no tenant identity leaks through the admin blob view
    for b in blobs:
        assert set(b) == {"content_hash", "refcount", "size", "validated"}


def test_cross_tenant_id_probing_indistinguishable(v):
    data = b"probe-me"
    a1 = v.finalize_upload(T1, "p1", data, chunk_size=8)
    upload_id = v.upload(T1, "p2", b"other", chunk_size=8)

    # truly unknown ids
    unknown_art = "art_" + "0" * 32
    unknown_up = "up_" + "0" * 32

    # t2 probing t1's artifact id -> 404, same as unknown
    st_foreign, _ = v.download(T2, a1)
    st_unknown, _ = v.download(T2, unknown_art)
    assert st_foreign == 404 == st_unknown

    # t2 probing t1's upload id -> 404, same as unknown
    st_f_up, _ = v.req("GET", f"/uploads/{upload_id}", T2)
    st_u_up, _ = v.req("GET", f"/uploads/{unknown_up}", T2)
    assert st_f_up == 404 == st_u_up

    # finalize against a foreign upload -> 404
    st, _ = v.req("POST", f"/uploads/{upload_id}/finalize", T2,
                  {"sha256": _sha256(b"other"), "size": 5})
    assert st == 404

    # delete of a foreign artifact -> 404
    st, _ = v.req("DELETE", f"/artifacts/{a1}", T2)
    assert st == 404
    # t1's artifact still intact after the foreign delete attempt
    st, body = v.download(T1, a1)
    assert st == 200 and body == data


def test_upload_state_private(v):
    data = b"state-probe"
    upload_id = v.upload(T1, "sp", data, chunk_size=8)
    st, state = v.req("GET", f"/uploads/{upload_id}", T1)
    assert st == 200 and state["state"] == "open"
    st, _ = v.req("GET", f"/uploads/{upload_id}", T2)
    assert st == 404
