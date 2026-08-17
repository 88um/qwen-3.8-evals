import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import shutil

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VAULTDROP = os.path.join(PROJECT_DIR, 'vaultdrop')

DEFAULT_TENANTS = {
    'tenants': [
        {'id': 't1', 'token': 'tok-t1'},
        {'id': 't2', 'token': 'tok-t2'},
    ],
    'admin_token': 'tok-admin',
}


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class VaultDropInstance:
    def __init__(self, tenants=None):
        self.state_dir = tempfile.mkdtemp(prefix='vaultdrop_test_')
        self.port = find_free_port()
        self.process = None
        tenants_config = tenants or DEFAULT_TENANTS
        with open(os.path.join(self.state_dir, 'tenants.json'), 'w') as f:
            json.dump(tenants_config, f)

    def start(self):
        env = os.environ.copy()
        env['PORT'] = str(self.port)
        env['VAULTDROP_STATE_DIR'] = self.state_dir
        self.process = subprocess.Popen(
            [sys.executable, os.path.join(PROJECT_DIR, 'src', 'main.py'), 'serve'],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._wait_ready()

    def _wait_ready(self, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=1)
                conn.request('GET', '/health')
                resp = conn.getresponse()
                if resp.status == 200:
                    conn.close()
                    return
                conn.close()
            except (ConnectionRefusedError, OSError, http.client.HTTPException):
                pass
            time.sleep(0.1)
        raise RuntimeError(f'Server failed to start on port {self.port}')

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()

    def kill(self):
        if self.process and self.process.poll() is None:
            self.process.kill()
            self.process.wait()

    def restart(self):
        self.process = None
        self.start()

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.state_dir, ignore_errors=True)

    @property
    def base_url(self):
        return f'http://127.0.0.1:{self.port}'


class APIClient:
    def __init__(self, port, token=None):
        self.port = port
        self.token = token

    def _conn(self):
        return http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)

    def _headers(self, extra=None):
        h = {}
        if self.token:
            h['Authorization'] = f'Bearer {self.token}'
        if extra:
            h.update(extra)
        return h

    def request(self, method, path, body=None, headers=None, raw_body=None):
        conn = self._conn()
        h = self._headers(headers)
        if body is not None and raw_body is None:
            raw_body = json.dumps(body).encode()
            h['Content-Type'] = 'application/json'
        if raw_body is not None:
            h['Content-Length'] = str(len(raw_body))
        conn.request(method, path, body=raw_body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data

    def request_json(self, method, path, body=None, headers=None, raw_body=None):
        status, data = self.request(method, path, body, headers, raw_body)
        try:
            return status, json.loads(data)
        except json.JSONDecodeError:
            return status, data

    def create_upload(self, name, total_size, chunk_size):
        return self.request_json('POST', '/uploads', {
            'name': name,
            'total_size': total_size,
            'chunk_size': chunk_size,
        })

    def upload_chunk(self, upload_id, index, data, sha256=None):
        import hashlib
        h = {}
        if sha256 is None:
            sha256 = hashlib.sha256(data).hexdigest()
        h['X-Chunk-SHA256'] = sha256
        return self.request_json(
            'PUT', f'/uploads/{upload_id}/chunks/{index}',
            headers=h, raw_body=data,
        )

    def get_upload(self, upload_id):
        return self.request_json('GET', f'/uploads/{upload_id}')

    def finalize(self, upload_id, sha256, size):
        return self.request_json('POST', f'/uploads/{upload_id}/finalize', {
            'sha256': sha256,
            'size': size,
        })

    def get_artifact(self, artifact_id):
        return self.request('GET', f'/artifacts/{artifact_id}')

    def list_artifacts(self):
        return self.request_json('GET', '/artifacts')

    def delete_artifact(self, artifact_id):
        return self.request_json('DELETE', f'/artifacts/{artifact_id}')

    def gc(self):
        return self.request_json('POST', '/admin/gc')

    def admin_blobs(self):
        return self.request_json('GET', '/admin/blobs')

    def admin_validate(self):
        return self.request_json('GET', '/admin/validate')

    def health(self):
        return self.request_json('GET', '/health')


def upload_artifact(client, name, data, chunk_size=None):
    import hashlib
    if chunk_size is None:
        chunk_size = max(1, len(data))
    status, resp = client.create_upload(name, len(data), chunk_size)
    assert status == 200, f'create_upload failed: {status} {resp}'
    upload_id = resp['upload_id']

    num_chunks = (len(data) + chunk_size - 1) // chunk_size if len(data) > 0 else 0
    for i in range(num_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, len(data))
        chunk = data[start:end]
        s, r = client.upload_chunk(upload_id, i, chunk)
        assert s == 200, f'upload_chunk {i} failed: {s} {r}'

    sha256 = hashlib.sha256(data).hexdigest()
    status, resp = client.finalize(upload_id, sha256, len(data))
    assert status == 200, f'finalize failed: {status} {resp}'
    return resp['artifact_id']
