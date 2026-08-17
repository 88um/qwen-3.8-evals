import json
import math
import os
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_ARTIFACT_SIZE = 10 * 1024 * 1024 * 1024  # 10 GiB
MAX_CHUNK_SIZE = 64 * 1024 * 1024  # 64 MiB


class VaultDropHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    ROUTES = [
        ('POST', re.compile(r'^/uploads$'), 'handle_create_upload'),
        ('PUT', re.compile(r'^/uploads/([^/]+)/chunks/(\d+)$'), 'handle_upload_chunk'),
        ('GET', re.compile(r'^/uploads/([^/]+)$'), 'handle_get_upload'),
        ('POST', re.compile(r'^/uploads/([^/]+)/finalize$'), 'handle_finalize'),
        ('GET', re.compile(r'^/artifacts$'), 'handle_list_artifacts'),
        ('GET', re.compile(r'^/artifacts/([^/]+)$'), 'handle_get_artifact'),
        ('DELETE', re.compile(r'^/artifacts/([^/]+)$'), 'handle_delete_artifact'),
        ('POST', re.compile(r'^/admin/gc$'), 'handle_gc'),
        ('GET', re.compile(r'^/admin/blobs$'), 'handle_admin_blobs'),
        ('GET', re.compile(r'^/admin/validate$'), 'handle_admin_validate'),
        ('GET', re.compile(r'^/health$'), 'handle_health'),
    ]

    def log_message(self, format, *args):
        pass

    @property
    def db(self):
        return self.server.db

    @property
    def store(self):
        return self.server.store

    def _auth(self):
        header = self.headers.get('Authorization', '')
        if not header.startswith('Bearer '):
            return None, None
        token = header[7:]
        tenant_id = self.server.token_to_tenant.get(token)
        if tenant_id:
            return 'tenant', tenant_id
        if token == self.server.admin_token:
            return 'admin', None
        return None, None

    def _read_json(self):
        length = int(self.headers.get('Content-Length', 0))
        if length <= 0:
            return {}
        data = self.rfile.read(length)
        return json.loads(data)

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, msg):
        self.close_connection = True
        self._send_json(status, {'error': msg})

    def _dispatch(self, method):
        path = self.path.split('?')[0]
        for route_method, pattern, handler_name in self.ROUTES:
            if route_method != method:
                continue
            m = pattern.match(path)
            if m:
                handler = getattr(self, handler_name)
                try:
                    handler(*m.groups())
                except Exception as e:
                    self._send_error(500, str(e))
                return
        self._send_error(404, 'not found')

    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PUT(self):
        self._dispatch('PUT')

    def do_DELETE(self):
        self._dispatch('DELETE')

    def handle_health(self):
        self._send_json(200, {'status': 'ok'})

    def handle_create_upload(self):
        role, tenant_id = self._auth()
        if role != 'tenant':
            self._send_error(401, 'unauthorized')
            return
        body = self._read_json()
        name = body.get('name', '')
        total_size = body.get('total_size')
        chunk_size = body.get('chunk_size')
        if not name or total_size is None or chunk_size is None:
            self._send_error(400, 'missing required fields: name, total_size, chunk_size')
            return
        if not isinstance(total_size, int) or total_size < 0:
            self._send_error(400, 'total_size must be a non-negative integer')
            return
        if not isinstance(chunk_size, int) or chunk_size <= 0:
            self._send_error(400, 'chunk_size must be a positive integer')
            return
        if total_size > MAX_ARTIFACT_SIZE:
            self._send_error(400, f'total_size exceeds maximum of {MAX_ARTIFACT_SIZE} bytes')
            return
        if chunk_size > MAX_CHUNK_SIZE:
            self._send_error(400, f'chunk_size exceeds maximum of {MAX_CHUNK_SIZE} bytes')
            return
        num_chunks = math.ceil(total_size / chunk_size) if total_size > 0 else 0
        upload_id = 'up_' + uuid.uuid4().hex
        self.db.create_upload(upload_id, tenant_id, name, total_size, chunk_size, num_chunks)
        self._send_json(200, {'upload_id': upload_id, 'received': []})

    def handle_upload_chunk(self, upload_id, index_str):
        role, tenant_id = self._auth()
        if role != 'tenant':
            self._send_error(401, 'unauthorized')
            return

        index = int(index_str)
        upload = self.db.get_upload(upload_id)
        if upload is None or upload['tenant_id'] != tenant_id:
            self._send_error(404, 'not found')
            return
        if upload['state'] != 'uploading':
            self._send_error(409, 'upload is not in uploading state')
            return
        if index < 0 or index >= upload['num_chunks']:
            self._send_error(400, f'chunk index out of range [0, {upload["num_chunks"]})')
            return

        content_length = self.headers.get('Content-Length')
        if content_length is None:
            self._send_error(411, 'Content-Length required')
            return
        content_length = int(content_length)

        if content_length > MAX_CHUNK_SIZE:
            self._send_error(413, f'chunk body exceeds maximum of {MAX_CHUNK_SIZE} bytes')
            return

        total_size = upload['total_size']
        cs = upload['chunk_size']
        if index == upload['num_chunks'] - 1:
            expected_size = total_size - index * cs
        else:
            expected_size = cs

        if content_length != expected_size:
            self._send_error(400, f'expected {expected_size} bytes for chunk {index}, got {content_length}')
            return

        expected_hash = self.headers.get('X-Chunk-SHA256', '')

        tmp, written, actual_hash, status = self.store.write_chunk(
            upload_id, index, self.rfile, expected_size, expected_hash
        )

        if status == 'hash_mismatch':
            self._send_error(400, f'chunk SHA-256 mismatch: expected {expected_hash}, got {actual_hash}')
            return
        if status == 'size_mismatch':
            self._send_error(400, f'expected {expected_size} bytes, received {written}')
            return

        self.store.chunk_locks.acquire(upload_id, index)
        try:
            existing = self.db.get_chunk(upload_id, index)
            if existing:
                os.unlink(tmp)
                if existing['sha256'] == actual_hash:
                    self._send_json(200, {'status': 'ok'})
                else:
                    self._send_error(409, 'chunk already uploaded with different content')
                return

            self.store.place_chunk(tmp, upload_id, index)
            inserted = self.db.insert_chunk(upload_id, index, actual_hash, written)
            if not inserted:
                existing = self.db.get_chunk(upload_id, index)
                if existing and existing['sha256'] == actual_hash:
                    self._send_json(200, {'status': 'ok'})
                else:
                    self._send_error(409, 'chunk already uploaded with different content')
                return
            self._send_json(200, {'status': 'ok'})
        finally:
            self.store.chunk_locks.release(upload_id, index)

    def handle_get_upload(self, upload_id):
        role, tenant_id = self._auth()
        if role != 'tenant':
            self._send_error(401, 'unauthorized')
            return
        upload = self.db.get_upload(upload_id)
        if upload is None or upload['tenant_id'] != tenant_id:
            self._send_error(404, 'not found')
            return
        received = self.db.get_received_chunks(upload_id)
        resp = {
            'upload_id': upload_id,
            'received': received,
            'state': upload['state'],
        }
        if upload['artifact_id']:
            resp['artifact_id'] = upload['artifact_id']
        self._send_json(200, resp)

    def handle_finalize(self, upload_id):
        role, tenant_id = self._auth()
        if role != 'tenant':
            self._send_error(401, 'unauthorized')
            return

        body = self._read_json()
        expected_sha256 = body.get('sha256', '').lower()
        expected_size = body.get('size')

        if not expected_sha256 or expected_size is None:
            self._send_error(400, 'missing required fields: sha256, size')
            return

        result = self.db.try_set_finalizing(upload_id, tenant_id)
        if result == 'not_found':
            self._send_error(404, 'not found')
            return
        if result == 'already_finalized':
            aid = self.db.get_upload_artifact(upload_id)
            self._send_json(200, {'artifact_id': aid})
            return
        if result == 'conflict':
            self._send_error(409, 'upload is already being finalized')
            return

        upload = self.db.get_upload(upload_id)
        received = self.db.get_received_chunks(upload_id)

        if len(received) != upload['num_chunks']:
            self.db.rollback_finalizing(upload_id)
            missing = set(range(upload['num_chunks'])) - set(received)
            self._send_error(400, f'missing chunks: {sorted(missing)[:20]}')
            return

        if upload['num_chunks'] == 0:
            import hashlib
            content_hash = hashlib.sha256(b'').hexdigest()
            actual_size = 0
            tmp_blob = None
        else:
            try:
                tmp_blob, actual_size, content_hash = self.store.assemble_blob(
                    upload_id, upload['num_chunks']
                )
            except Exception as e:
                self.db.rollback_finalizing(upload_id)
                self._send_error(500, f'assembly failed: {e}')
                return

        if actual_size != expected_size:
            self.db.rollback_finalizing(upload_id)
            if tmp_blob:
                try:
                    os.unlink(tmp_blob)
                except OSError:
                    pass
            self._send_error(422, f'size mismatch: expected {expected_size}, got {actual_size}')
            return

        if content_hash != expected_sha256:
            self.db.rollback_finalizing(upload_id)
            if tmp_blob:
                try:
                    os.unlink(tmp_blob)
                except OSError:
                    pass
            self._send_error(422, f'sha256 mismatch: expected {expected_sha256}, got {content_hash}')
            return

        artifact_id = 'art_' + uuid.uuid4().hex

        self.store.blob_locks.acquire(content_hash)
        try:
            if tmp_blob:
                self.store.ensure_blob_file(tmp_blob, content_hash)
            else:
                bdir = self.store._blob_dir(content_hash)
                os.makedirs(bdir, exist_ok=True)
                bp = self.store.blob_path(content_hash)
                if not os.path.exists(bp):
                    with open(bp, 'wb') as f:
                        f.flush()
                        os.fsync(f.fileno())
                    from store import _fsync_dir
                    _fsync_dir(bdir)

            self.db.commit_finalize(
                upload_id, artifact_id, tenant_id,
                upload['name'], actual_size, content_hash
            )

            if not os.path.exists(self.store.blob_path(content_hash)):
                self.db.rollback_finalizing(upload_id)
                self._send_error(500, 'blob file missing after commit')
                return
        except Exception as e:
            self.db.rollback_finalizing(upload_id)
            self._send_error(500, f'finalize failed: {e}')
            return
        finally:
            self.store.blob_locks.release(content_hash)

        self.store.cleanup_chunks(upload_id)
        self._send_json(200, {'artifact_id': artifact_id})

    def handle_list_artifacts(self):
        role, tenant_id = self._auth()
        if role != 'tenant':
            self._send_error(401, 'unauthorized')
            return
        rows = self.db.list_artifacts(tenant_id)
        result = [
            {
                'artifact_id': r['id'],
                'name': r['name'],
                'size': r['size'],
                'sha256': r['content_hash'],
            }
            for r in rows
        ]
        self._send_json(200, result)

    def handle_get_artifact(self, artifact_id):
        role, tenant_id = self._auth()
        if role != 'tenant':
            self._send_error(401, 'unauthorized')
            return
        artifact = self.db.get_artifact(artifact_id)
        if artifact is None or artifact['tenant_id'] != tenant_id:
            self._send_error(404, 'not found')
            return

        blob_path = self.store.blob_path(artifact['content_hash'])
        if not os.path.exists(blob_path):
            self._send_error(500, 'blob file missing')
            return

        size = artifact['size']
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.end_headers()

        with open(blob_path, 'rb') as f:
            remaining = size
            while remaining > 0:
                chunk = f.read(min(65536, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def handle_delete_artifact(self, artifact_id):
        role, tenant_id = self._auth()
        if role != 'tenant':
            self._send_error(401, 'unauthorized')
            return
        deleted = self.db.delete_artifact(artifact_id, tenant_id)
        if not deleted:
            self._send_error(404, 'not found')
            return
        self._send_json(200, {'status': 'deleted'})

    def handle_gc(self):
        role, _ = self._auth()
        if role != 'admin':
            self._send_error(401, 'unauthorized')
            return

        candidates = self.db.get_gc_candidates()
        scanned = len(candidates)
        collected = 0
        bytes_freed = 0

        for row in candidates:
            content_hash = row['content_hash']
            size = row['size']

            self.store.blob_locks.acquire(content_hash)
            try:
                deleted = self.db.gc_delete_blob(content_hash)
                if deleted:
                    if not self.db.blob_exists(content_hash):
                        self.store.delete_blob_file(content_hash)
                        collected += 1
                        bytes_freed += size
            finally:
                self.store.blob_locks.release(content_hash)

        self._send_json(200, {
            'scanned': scanned,
            'collected': collected,
            'bytes_freed': bytes_freed,
        })

    def handle_admin_blobs(self):
        role, _ = self._auth()
        if role != 'admin':
            self._send_error(401, 'unauthorized')
            return
        rows = self.db.list_blobs()
        result = [
            {
                'content_hash': r['content_hash'],
                'refcount': r['refcount'],
                'size': r['size'],
                'validated': r['validated'],
            }
            for r in rows
        ]
        self._send_json(200, result)

    def handle_admin_validate(self):
        role, _ = self._auth()
        if role != 'admin':
            self._send_error(401, 'unauthorized')
            return

        rows = self.db.list_blobs()
        total = len(rows)
        valid = 0
        invalid = 0
        errors = 0

        for row in rows:
            content_hash = row['content_hash']
            actual = self.store.validate_blob(content_hash)
            if actual is None:
                errors += 1
                self.db.set_blob_validated(content_hash, False)
            elif actual == content_hash:
                valid += 1
                self.db.set_blob_validated(content_hash, True)
            else:
                invalid += 1
                self.db.set_blob_validated(content_hash, False)

        self._send_json(200, {
            'total': total,
            'valid': valid,
            'invalid': invalid,
            'errors': errors,
        })


class VaultDropServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, port, state_dir, tenants_config, db, store):
        self.db = db
        self.store = store
        self.token_to_tenant = {}
        self.admin_token = tenants_config.get('admin_token', '')
        for t in tenants_config.get('tenants', []):
            self.token_to_tenant[t['token']] = t['id']
        super().__init__(('0.0.0.0', port), VaultDropHandler)
