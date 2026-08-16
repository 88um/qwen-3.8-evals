"""Functional + chunk-semantics + admin-surface tests."""

import hashlib
import threading

import pytest

from harness import ADMIN, T1, T2, Vault, cleanup, _sha256


@pytest.fixture()
def v():
    vault = Vault().start()
    yield vault
    cleanup(vault)


def test_health_no_auth(v):
    st, _ = v.req("GET", "/health")
    assert st == 200


def test_auth_required(v):
    st, _ = v.req("GET", "/artifacts")
    assert st == 401
    st, _ = v.req("GET", "/artifacts", token="tok-nobody")
    assert st == 401


def test_roundtrip_and_integrity(v):
    data = b"vaultdrop functional payload" * 100
    aid = v.finalize_upload(T1, "f1", data, chunk_size=997)
    st, body = v.download(T1, aid)
    assert st == 200
    assert body == data
    assert hashlib.sha256(body).hexdigest() == _sha256(data)


def test_chunk_idempotent_replay(v):
    data = b"0123456789"
    upload_id = v.upload(T1, "replay", data, chunk_size=10)
    # replay the exact same chunk -> 200
    st, _ = v.req("PUT", f"/uploads/{upload_id}/chunks/0", T1,
                  body=data, headers={"X-Chunk-SHA256": _sha256(data)})
    assert st == 200


def test_chunk_conflicting_replay(v):
    # fresh upload with NO chunks stored yet
    st, up = v.req("POST", "/uploads", T1,
                   {"name": "conflict", "total_size": 10, "chunk_size": 10})
    upload_id = up["upload_id"]
    # first write wins
    st, _ = v.req("PUT", f"/uploads/{upload_id}/chunks/0", T1,
                  body=b"AAAAAAAAAA", headers={"X-Chunk-SHA256": _sha256(b"AAAAAAAAAA")})
    assert st == 201
    # conflicting bytes at same index -> 409, and first bytes are NOT overwritten
    st, _ = v.req("PUT", f"/uploads/{upload_id}/chunks/0", T1,
                  body=b"BBBBBBBBBB", headers={"X-Chunk-SHA256": _sha256(b"BBBBBBBBBB")})
    assert st == 409
    # finalize with the FIRST bytes still succeeds (first-write-wins)
    st, out = v.finalize(T1, upload_id, b"AAAAAAAAAA")
    assert st == 200, out
    st, body = v.download(T1, out["artifact_id"])
    assert st == 200 and body == b"AAAAAAAAAA"


def test_chunk_sha_mismatch_rejected(v):
    data = b"0123456789"
    upload_id = v.upload(T1, "badsha", data, chunk_size=10)
    st, _ = v.req("PUT", f"/uploads/{upload_id}/chunks/0", T1,
                  body=data, headers={"X-Chunk-SHA256": "00" * 32})
    assert st == 422


def test_finalize_rejects_bad_sha(v):
    data = b"0123456789"
    upload_id = v.upload(T1, "badfinalize", data, chunk_size=10)
    st, _ = v.req("POST", f"/uploads/{upload_id}/finalize", T1,
                  {"sha256": "11" * 10 + "22", "size": len(data)})
    assert st == 422
    # no artifact was created
    st, lst = v.req("GET", "/artifacts", T1)
    assert st == 200 and lst == []


def test_finalize_rejects_incomplete(v):
    data = b"0123456789"
    # fresh upload, no chunks sent
    st, up = v.req("POST", "/uploads", T1,
                   {"name": "incomplete", "total_size": len(data), "chunk_size": 10})
    upload_id = up["upload_id"]
    st, _ = v.req("POST", f"/uploads/{upload_id}/finalize", T1,
                  {"sha256": _sha256(data), "size": len(data)})
    assert st == 422


def test_gc_collects_unreferenced_only(v):
    # two distinct artifacts
    a1 = v.finalize_upload(T1, "a1", b"alpha-alpha", chunk_size=8)
    a2 = v.finalize_upload(T1, "a2", b"beta-beta-beta", chunk_size=8)
    # delete a1 -> its blob becomes collectable; a2's blob stays referenced
    st, _ = v.req("DELETE", f"/artifacts/{a1}", T1)
    assert st == 200
    st, gc = v.gc()
    assert st == 200
    assert gc["collected"] == 1
    assert gc["bytes_freed"] == len(b"alpha-alpha")
    # a2 still fully readable
    st, body = v.download(T1, a2)
    assert st == 200 and body == b"beta-beta-beta"


def test_dedup_within_tenant_refcount(v):
    data = b"shared-bytes"
    a1 = v.finalize_upload(T1, "d1", data, chunk_size=8)
    a2 = v.finalize_upload(T1, "d2", data, chunk_size=8)
    assert a1 != a2                       # distinct artifacts
    st, blobs = v.blobs()
    assert st == 200
    row = [b for b in blobs if b["content_hash"] == _sha256(data)]
    assert len(row) == 1 and row[0]["refcount"] == 2


def test_validate_reports_ok(v):
    v.finalize_upload(T1, "val", b"validate-me", chunk_size=8)
    st, out = v.validate()
    assert st == 200
    assert out["ok"] >= 1 and out["mismatch"] == 0 and out["missing"] == 0


def test_admin_requires_admin_token(v):
    st, _ = v.req("GET", "/admin/blobs", T1)
    assert st == 403
    st, _ = v.req("POST", "/admin/gc", T1)
    assert st == 403
