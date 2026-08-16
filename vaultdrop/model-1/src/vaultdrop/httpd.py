"""HTTP API: routing, authentication, and the read-boundary integrity check.

Runs in the foreground on $PORT via ThreadingHTTPServer (one thread per
connection). Every tenant lookup is tenant-scoped; artifact downloads verify the
stored bytes against the declared digest at the read boundary before sending a
single byte (promise 2).
"""

import hashlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import config, db, fs, store, uploads
from .tenants import TenantRegistry


def _segments(path: str) -> list[str]:
    return [s for s in urlsplit(path).path.split("/") if s]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    cfg: config.Config = None
    registry: TenantRegistry = None

    # --- plumbing -----------------------------------------------------------

    def log_message(self, fmt, *args):          # silence request logging
        pass

    def _principal(self):
        h = self.headers.get("Authorization", "")
        token = h[len("Bearer "):].strip() if h.startswith("Bearer ") else None
        return self.registry.resolve(token)

    def _send_json(self, status: int, obj) -> None:
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_bytes(self, status: int, data: bytes,
                    ctype: str = "application/octet-stream") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> bytes:
        n = int(self.headers.get("Content-Length", 0) or 0)
        return self.rfile.read(n) if n > 0 else b""

    def _json_body(self) -> dict:
        raw = self._body()
        if not raw:
            return {}
        return json.loads(raw)

    # --- verbs ----------------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    # --- routing --------------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        try:
            self._route(method)
        except json.JSONDecodeError:
            self._send_json(400, {"error": "invalid json"})
        except Exception as e:                       # noqa: BLE001 - report, don't leak state
            self._send_json(500, {"error": f"internal: {type(e).__name__}"})

    def _route(self, method: str) -> None:
        segs = _segments(self.path)

        if segs == ["health"]:
            return self._send_json(200, {"status": "ok"})

        principal = self._principal()
        if principal is None:
            return self._send_json(401, {"error": "unauthorized"})

        # --- admin surface ----------------------------------------------------
        if segs[0] == "admin":
            if principal.role != "admin":
                return self._send_json(403, {"error": "admin required"})
            if method == "POST" and segs == ["admin", "gc"]:
                return self._send_json(200, store.gc_pass(self.cfg))
            if method == "GET" and segs == ["admin", "blobs"]:
                return self._admin_blobs()
            if method == "GET" and segs == ["admin", "validate"]:
                return self._send_json(200, store.validate_pass(self.cfg))
            return self._send_json(404, {"error": "not found"})

        # --- tenant surface ---------------------------------------------------
        if principal.role != "tenant":
            return self._send_json(403, {"error": "tenant required"})
        tid = principal.tenant_id

        if method == "POST" and segs == ["uploads"]:
            b = self._json_body()
            status, obj = uploads.start_upload(
                self.cfg, tid,
                str(b.get("name", "")),
                int(b.get("total_size", -1)),
                int(b.get("chunk_size", 0)),
            )
            return self._send_json(status, obj)

        if method == "PUT" and len(segs) == 4 and segs[0] == "uploads" and segs[2] == "chunks":
            try:
                index = int(segs[3])
            except ValueError:
                return self._send_json(400, {"error": "bad chunk index"})
            body = self._body()
            claimed_sha = self.headers.get("X-Chunk-SHA256")
            status, obj = uploads.put_chunk(self.cfg, tid, segs[1], index, body, claimed_sha)
            return self._send_json(status, obj)

        if method == "GET" and len(segs) == 2 and segs[0] == "uploads":
            state = uploads.get_upload_state(self.cfg, tid, segs[1])
            if state is None:
                return self._send_json(404, {"error": "not found"})
            return self._send_json(200, state)

        if method == "POST" and len(segs) == 3 and segs[0] == "uploads" and segs[2] == "finalize":
            b = self._json_body()
            status, obj = uploads.finalize(
                self.cfg, tid, segs[1],
                str(b.get("sha256", "")),
                int(b.get("size", -1)),
            )
            return self._send_json(status, obj)

        if method == "GET" and segs == ["artifacts"]:
            rows = uploads.list_artifacts(self.cfg, tid)
            return self._send_json(
                200,
                [{"artifact_id": r["artifact_id"], "name": r["name"],
                  "size": r["size"], "sha256": r["sha256"]} for r in rows],
            )

        if method == "GET" and len(segs) == 2 and segs[0] == "artifacts":
            return self._download(tid, segs[1])

        if method == "DELETE" and len(segs) == 2 and segs[0] == "artifacts":
            ok = uploads.delete_artifact(self.cfg, tid, segs[1])
            if not ok:
                return self._send_json(404, {"error": "not found"})
            return self._send_json(200, {"status": "deleted"})

        return self._send_json(404, {"error": "not found"})

    # --- handlers -------------------------------------------------------------

    def _admin_blobs(self) -> None:
        conn = db.open(self.cfg)
        try:
            rows = conn.execute(
                "SELECT content_hash, size, refcount, validated"
                " FROM blobs ORDER BY content_hash"
            ).fetchall()
        finally:
            conn.close()
        self._send_json(
            200,
            [{"content_hash": r["content_hash"], "refcount": r["refcount"],
              "size": r["size"], "validated": r["validated"]} for r in rows],
        )

    def _download(self, tenant_id: str, artifact_id: str) -> None:
        row = uploads.get_artifact(self.cfg, tenant_id, artifact_id)
        if row is None:
            return self._send_json(404, {"error": "not found"})
        bp = config.blob_path(self.cfg, row["blob_hash"])
        try:
            data = fs.read_all(bp)
        except FileNotFoundError:
            return self._send_json(500, {"error": "blob missing"})
        # Read-boundary integrity: never serve bytes that do not match the digest.
        if hashlib.sha256(data).hexdigest() != row["sha256"] or len(data) != row["size"]:
            return self._send_json(500, {"error": "blob integrity"})
        self._send_bytes(200, data)


def make_server(cfg: config.Config, registry: TenantRegistry) -> ThreadingHTTPServer:
    Handler.cfg = cfg
    Handler.registry = registry
    return ThreadingHTTPServer(("0.0.0.0", cfg.port), Handler)
