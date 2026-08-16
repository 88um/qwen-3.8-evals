"""VaultDrop HTTP layer: auth, routing, streaming."""

import json
import os
import re
import sys

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import core


class Auth:
    """Token map loaded once at startup from tenants.json."""

    def __init__(self, tenants, admin_token):
        self.tenant_of = {}
        for t in tenants:
            self.tenant_of[t["token"]] = t["id"]
        self.admin_token = admin_token

    def check(self, header_value):
        """Return ('tenant', tenant_id) | ('admin',) | raise ApiError(401)."""
        if not isinstance(header_value, str):
            raise core.ApiError(401, "missing bearer token")
        m = re.fullmatch(r"Bearer (\S+)", header_value)
        if not m:
            raise core.ApiError(401, "missing bearer token")
        tok = m.group(1)
        if tok == self.admin_token:
            return ("admin",)
        tenant = self.tenant_of.get(tok)
        if tenant is None:
            raise core.ApiError(401, "unknown token")
        return ("tenant", tenant)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "VaultDrop/1.0"
    timeout = 60          # socket timeout: malformed/lying clients cannot hang a worker

    # ------------------------------------------------------------ plumbing
    def log_message(self, fmt, *args):
        sys.stderr.write("[vd] %s %s\n" % (self.address_string(), fmt % args))
        sys.stderr.flush()

    def _send(self, status, body, content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, obj):
        self._send(status, json.dumps(obj).encode())

    def _error(self, status, message):
        self._send_json(status, {"error": message})

    def _auth(self):
        return self.server.auth.check(self.headers.get("Authorization"))

    # ------------------------------------------------------------- routing
    def _route(self, method):
        path = self.path.split("?", 1)[0]
        store = self.server.store
        try:
            if method == "GET" and path == "/health":
                return self._send_json(200, {"status": "ok"})

            kind = self._auth()

            if method == "POST" and path == "/uploads":
                if kind[0] != "tenant":
                    return self._error(401, "tenant token required")
                body = core.read_json_body(self.rfile, self._clen())
                uid = store.create_upload(kind[1], body.get("name"),
                                          body.get("total_size"),
                                          body.get("chunk_size"))
                return self._send_json(200, {"upload_id": uid, "received": []})

            m = re.fullmatch(r"/uploads/([0-9a-f]{32})/chunks/(\d+)", path)
            if method == "PUT" and m:
                if kind[0] != "tenant":
                    return self._error(401, "tenant token required")
                index = int(m.group(2))
                digest = self.headers.get("X-Chunk-SHA256")
                clen = self._clen()
                if clen < 0:
                    return self._error(400, "bad content-length")
                store.put_chunk(kind[1], m.group(1), index, self.rfile, digest, clen)
                return self._send_json(200, {"stored": index})

            m = re.fullmatch(r"/uploads/([0-9a-f]{32})/finalize", path)
            if method == "POST" and m:
                if kind[0] != "tenant":
                    return self._error(401, "tenant token required")
                body = core.read_json_body(self.rfile, self._clen())
                aid = store.finalize(kind[1], m.group(1),
                                     body.get("sha256"), body.get("size"))
                return self._send_json(200, {"artifact_id": aid})

            m = re.fullmatch(r"/uploads/([0-9a-f]{32})", path)
            if method == "GET" and m:
                if kind[0] != "tenant":
                    return self._error(401, "tenant token required")
                return self._send_json(200, store.describe_upload(kind[1], m.group(1)))

            if method == "GET" and path == "/artifacts":
                if kind[0] != "tenant":
                    return self._error(401, "tenant token required")
                rows = store.list_artifacts(kind[1])
                return self._send_json(200, [
                    {"artifact_id": r[0], "name": r[1], "size": r[2], "sha256": r[3]}
                    for r in rows])

            m = re.fullmatch(r"/artifacts/([0-9a-f]{32})", path)
            if method == "GET" and m:
                if kind[0] != "tenant":
                    return self._error(401, "tenant token required")
                return self._download(kind[1], m.group(1))

            m = re.fullmatch(r"/artifacts/([0-9a-f]{32})", path)
            if method == "DELETE" and m:
                if kind[0] != "tenant":
                    return self._error(401, "tenant token required")
                if not store.delete_artifact(kind[1], m.group(1)):
                    return self._error(404, "not found")
                return self._send_json(200, {"deleted": m.group(1)})

            if kind[0] != "admin":
                return self._error(401, "admin token required")
            if method == "POST" and path == "/admin/gc":
                return self._send_json(200, store.gc_pass())
            if method == "GET" and path == "/admin/blobs":
                return self._send_json(200, [
                    {"content_hash": r[0], "refcount": r[1], "size": r[2],
                     "validated": bool(r[3])}
                    for r in store.list_blobs()])
            if method == "GET" and path == "/admin/validate":
                return self._send_json(200, store.validate_pass())

            return self._error(404, "not found")
        except core.ApiError as e:
            return self._error(e.status, e.message)
        except Exception as e:
            sys.stderr.write("[vd] internal error: %r\n" % (e,))
            sys.stderr.flush()
            return self._error(500, "internal error")

    def _clen(self):
        v = self.headers.get("Content-Length")
        try:
            return int(v) if v is not None else 0
        except ValueError:
            return -1

    def _download(self, tenant_id, artifact_id):
        row = self.server.store.get_artifact(tenant_id, artifact_id)
        sha, size = row[4], row[3]
        blob = self.server.store.db.read_one(
            "SELECT path, state FROM blobs WHERE content_hash=?", (sha,))
        if blob is None or blob[1] != "active":
            return self._error(500, "blob unavailable")
        path = blob[0]
        try:
            st = os.stat(path)
        except OSError:
            return self._error(500, "blob bytes unavailable")
        if st.st_size != size:
            return self._error(500, "blob size mismatch")
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        try:
            with open(path, "rb") as f:
                sent = 0
                while sent < size:
                    b = f.read(core.STREAM_BUF)
                    if not b:
                        return              # truncated: client sees short response
                    self.wfile.write(b)
                    sent += len(b)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    # ------------------------------------------------------- verb dispatch
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def do_PUT(self):
        self._route("PUT")

    def do_DELETE(self):
        self._route("DELETE")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, store, auth):
        super().__init__(addr, Handler)
        self.store = store
        self.auth = auth


def serve(store, auth, port):
    srv = Server(("0.0.0.0", port), store, auth)
    sys.stderr.write("[vd] listening on :%d\n" % port)
    sys.stderr.flush()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
