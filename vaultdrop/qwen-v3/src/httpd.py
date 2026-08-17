"""HTTP surface: routing, authentication, streaming.

Tenant isolation at the wire: every resource lookup is scoped to the calling
tenant's id (a single indexed equality predicate), so a foreign or unknown id
yields the same 404 as a nonexistent one. Admin endpoints answer 404 to tenant
tokens and 401 to unknown tokens. All byte movement is streamed in fixed-size
blocks; no request ever buffers more than one block plus small metadata.
"""

import json
import os
import re
import sys
import threading
import traceback

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import blobs
import config
import db
import uploads

HEX32 = re.compile(r"^[0-9a-f]{32}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
MAX_JSON_BODY = 1024 * 1024

_registry = {"tenants": {}, "admin": None, "lock": threading.Lock()}


def load_tenant_registry():
    """Reads $VAULTDROP_STATE_DIR/tenants.json as the source of truth."""
    tenants = {}
    admin = None
    try:
        with open(config.TENANTS_PATH) as f:
            doc = json.load(f)
        for t in doc.get("tenants", []):
            if isinstance(t, dict) and isinstance(t.get("id"), str) and isinstance(t.get("token"), str):
                tenants[t["token"]] = t["id"]
        if isinstance(doc.get("admin_token"), str):
            admin = doc["admin_token"]
    except (OSError, ValueError):
        pass
    with _registry["lock"]:
        _registry["tenants"] = tenants
        _registry["admin"] = admin


def _authenticate(authorization):
    """Returns ('tenant', tenant_id) | ('admin', None) | ('invalid', None)."""
    if not authorization or not authorization.startswith("Bearer "):
        return ("invalid", None)
    token = authorization[len("Bearer "):].strip()
    with _registry["lock"]:
        tenants, admin = _registry["tenants"], _registry["admin"]
    if admin is not None and token == admin:
        return ("admin", None)
    tenant = tenants.get(token)
    if tenant is not None:
        return ("tenant", tenant)
    return ("invalid", None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = config.HTTP_TIMEOUT_S
    server_version = "VaultDrop"

    # --- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status, message):
        self._json(status, {"error": message})

    def _drain(self, limit):
        """Discard up to `limit` bytes of unread request body."""
        drained = 0
        while drained < limit:
            b = self.rfile.read(min(config.IO_BLOCK, limit - drained))
            if not b:
                break
            drained += len(b)
        return drained

    def _reject_with_body(self, content_length, consumed, status, message):
        """Drain the unread body remainder before responding so keep-alive
        framing stays intact whenever the remainder is bounded; otherwise
        drain a bounded prefix and close the connection."""
        remaining = max(0, content_length - consumed)
        cap = config.MAX_CHUNK_SIZE + config.IO_BLOCK
        if remaining > cap:
            self._drain(cap)
            self.close_connection = True
        else:
            self._drain(remaining)
        self._json(status, {"error": message})

    def _read_json_body(self):
        length = self.headers.get("Content-Length")
        if length is None:
            return None, None
        try:
            length = int(length)
        except ValueError:
            return None, None
        if length < 0 or length > MAX_JSON_BODY:
            self._drain(min(length, MAX_JSON_BODY + config.IO_BLOCK))
            self.close_connection = True  # remainder beyond the drain cap
            return None, length
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw), length
        except ValueError:
            return None, length

    def _route(self, method):
        try:
            self._dispatch(method)
        except BrokenPipeError:
            self.close_connection = True
        except Exception:
            traceback.print_exc(file=sys.stderr)
            try:
                self._error(500, "internal error")
            except Exception:
                self.close_connection = True

    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")

    # --- dispatch -----------------------------------------------------------

    def _dispatch(self, method):
        path = self.path.split("?", 1)[0]
        segs = [s for s in path.split("/") if s != ""]

        if segs == ["health"]:
            return self._json(200, {"status": "ok"})

        kind, identity = _authenticate(self.headers.get("Authorization"))

        # Admin surface: tenant tokens get 404 (no existence leaks), unknown
        # tokens get 401.
        if segs and segs[0] == "admin":
            if kind == "invalid":
                return self._error(401, "unauthorized")
            if kind == "tenant":
                return self._error(404, "not found")
            return self._admin(method, segs)

        if kind != "tenant":
            return self._error(401, "unauthorized")
        tenant_id = identity
        conn = db.get_conn()

        if method == "POST" and segs == ["uploads"]:
            return self._post_uploads(conn, tenant_id)
        if method == "PUT" and len(segs) == 4 and segs[0] == "uploads" and segs[2] == "chunks":
            return self._put_chunk(conn, tenant_id, segs[1], segs[3])
        if method == "GET" and len(segs) == 2 and segs[0] == "uploads":
            return self._get_upload(conn, tenant_id, segs[1])
        if method == "POST" and len(segs) == 3 and segs[0] == "uploads" and segs[2] == "finalize":
            return self._post_finalize(conn, tenant_id, segs[1])
        if method == "GET" and segs == ["artifacts"]:
            return self._json(200, uploads.list_artifacts(conn, tenant_id))
        if method == "GET" and len(segs) == 2 and segs[0] == "artifacts":
            return self._get_artifact(conn, tenant_id, segs[1])
        if method == "DELETE" and len(segs) == 2 and segs[0] == "artifacts":
            status, payload = uploads.delete_artifact(conn, tenant_id, segs[1])
            if status == 204:
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self._json(status, payload)
            return
        return self._error(404, "not found")

    # --- tenant endpoints ----------------------------------------------------

    def _post_uploads(self, conn, tenant_id):
        doc, _ = self._read_json_body()
        if doc is None or not isinstance(doc, dict):
            return self._error(400, "invalid JSON body")
        try:
            upload_id = uploads.start_upload(
                conn,
                tenant_id,
                doc.get("name"),
                doc.get("total_size"),
                doc.get("chunk_size"),
            )
        except uploads.UploadError as e:
            return self._error(e.status, e.message)
        self._json(200, {"upload_id": upload_id, "received": []})

    def _put_chunk(self, conn, tenant_id, upload_id, index_seg):
        length_hdr = self.headers.get("Content-Length")
        if length_hdr is None:
            self.close_connection = True
            return self._error(400, "Content-Length required")
        try:
            content_length = int(length_hdr)
        except ValueError:
            self.close_connection = True
            return self._error(400, "bad Content-Length")
        if content_length < 0:
            self.close_connection = True
            return self._error(400, "bad Content-Length")
        if not HEX32.match(upload_id or ""):
            self._reject_with_body(content_length, 0, 404, "not found")
            return
        if index_seg is None or not index_seg.isdigit():
            self._reject_with_body(content_length, 0, 404, "not found")
            return
        index = int(index_seg)
        declared_sha = self.headers.get("X-Chunk-SHA256")
        try:
            uploads.store_chunk(
                conn, tenant_id, upload_id, index, self.rfile, declared_sha,
                content_length,
            )
        except uploads.UploadError as e:
            self._reject_with_body(content_length, e.consumed, e.status,
                                   e.message)
            return
        self._json(200, {"stored": index})

    def _get_upload(self, conn, tenant_id, upload_id):
        if not HEX32.match(upload_id or ""):
            return self._error(404, "not found")
        info = uploads.get_upload(conn, tenant_id, upload_id)
        if info is None:
            return self._error(404, "not found")
        self._json(200, info)

    def _post_finalize(self, conn, tenant_id, upload_id):
        if not HEX32.match(upload_id or ""):
            return self._error(404, "not found")
        doc, _ = self._read_json_body()
        if doc is None or not isinstance(doc, dict):
            return self._error(400, "invalid JSON body")
        sha = doc.get("sha256")
        if isinstance(sha, str):
            sha = sha.lower()
        status, payload = uploads.finalize(
            conn, tenant_id, upload_id, sha, doc.get("size")
        )
        self._json(status, payload)

    def _get_artifact(self, conn, tenant_id, artifact_id):
        if not HEX32.match(artifact_id or ""):
            return self._error(404, "not found")
        art = uploads.get_artifact(conn, tenant_id, artifact_id)
        if art is None:
            return self._error(404, "not found")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(art["size"]))
        self.end_headers()
        try:
            with open(art["path"], "rb") as f:
                while True:
                    block = f.read(config.IO_BLOCK)
                    if not block:
                        break
                    self.wfile.write(block)
        except OSError:
            self.close_connection = True

    # --- admin endpoints -------------------------------------------------------

    def _admin(self, method, segs):
        conn = db.get_conn()
        if method == "POST" and segs == ["admin", "gc"]:
            self._json(200, blobs.gc_pass(conn))
            return
        if method == "GET" and segs == ["admin", "blobs"]:
            rows = [
                {
                    "content_hash": r["hash"],
                    "refcount": r["refcount"],
                    "size": r["size"],
                    "validated": bool(r["validated"]),
                }
                for r in conn.execute(
                    "SELECT hash, refcount, size, validated FROM blobs ORDER BY hash"
                )
            ]
            self._json(200, rows)
            return
        if method == "GET" and segs == ["admin", "validate"]:
            self._json(200, blobs.validate_pass(conn))
            return
        self._error(404, "not found")


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(port):
    load_tenant_registry()
    httpd = Server(("0.0.0.0", port), Handler)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
