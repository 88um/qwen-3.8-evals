import os
import hashlib
import threading
import uuid

BUFFER_SIZE = 65536


def _fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class BlobLocks:
    def __init__(self):
        self._locks = {}
        self._meta = threading.Lock()

    def acquire(self, content_hash):
        with self._meta:
            if content_hash not in self._locks:
                self._locks[content_hash] = threading.Lock()
            lock = self._locks[content_hash]
        lock.acquire()

    def release(self, content_hash):
        with self._meta:
            lock = self._locks.get(content_hash)
        if lock:
            lock.release()


class ChunkLocks:
    def __init__(self):
        self._locks = {}
        self._meta = threading.Lock()

    def acquire(self, upload_id, index):
        key = (upload_id, index)
        with self._meta:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            lock = self._locks[key]
        lock.acquire()

    def release(self, upload_id, index):
        key = (upload_id, index)
        with self._meta:
            lock = self._locks.get(key)
        if lock:
            lock.release()


class Store:
    def __init__(self, state_dir):
        self.state_dir = state_dir
        self.chunks_dir = os.path.join(state_dir, 'chunks')
        self.blobs_dir = os.path.join(state_dir, 'blobs')
        self.tmp_dir = os.path.join(state_dir, 'tmp')
        self.blob_locks = BlobLocks()
        self.chunk_locks = ChunkLocks()
        os.makedirs(self.chunks_dir, exist_ok=True)
        os.makedirs(self.blobs_dir, exist_ok=True)
        os.makedirs(self.tmp_dir, exist_ok=True)

    def _blob_dir(self, content_hash):
        return os.path.join(self.blobs_dir, content_hash[:2])

    def blob_path(self, content_hash):
        return os.path.join(self._blob_dir(content_hash), content_hash)

    def blob_exists(self, content_hash):
        return os.path.exists(self.blob_path(content_hash))

    def _chunk_dir(self, upload_id):
        return os.path.join(self.chunks_dir, upload_id)

    def chunk_path(self, upload_id, index):
        return os.path.join(self._chunk_dir(upload_id), str(index))

    def temp_path(self):
        return os.path.join(self.tmp_dir, uuid.uuid4().hex)

    def write_chunk(self, upload_id, index, rfile, expected_size, expected_hash):
        cdir = self._chunk_dir(upload_id)
        os.makedirs(cdir, exist_ok=True)

        tmp = self.temp_path()
        hasher = hashlib.sha256()
        written = 0
        try:
            with open(tmp, 'wb') as f:
                remaining = expected_size
                while remaining > 0:
                    to_read = min(BUFFER_SIZE, remaining)
                    data = rfile.read(to_read)
                    if not data:
                        break
                    f.write(data)
                    hasher.update(data)
                    written += len(data)
                    remaining -= len(data)
                f.flush()
                os.fsync(f.fileno())
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        actual_hash = hasher.hexdigest()
        if expected_hash and actual_hash != expected_hash.lower():
            os.unlink(tmp)
            return None, written, actual_hash, 'hash_mismatch'

        if written != expected_size:
            os.unlink(tmp)
            return None, written, actual_hash, 'size_mismatch'

        return tmp, written, actual_hash, 'ok'

    def place_chunk(self, tmp_path, upload_id, index):
        final = self.chunk_path(upload_id, index)
        os.rename(tmp_path, final)
        _fsync_dir(self._chunk_dir(upload_id))

    def assemble_blob(self, upload_id, num_chunks):
        tmp = self.temp_path()
        hasher = hashlib.sha256()
        total = 0
        with open(tmp, 'wb') as f:
            for i in range(num_chunks):
                cp = self.chunk_path(upload_id, i)
                with open(cp, 'rb') as cf:
                    while True:
                        data = cf.read(BUFFER_SIZE)
                        if not data:
                            break
                        f.write(data)
                        hasher.update(data)
                        total += len(data)
            f.flush()
            os.fsync(f.fileno())
        return tmp, total, hasher.hexdigest()

    def place_blob(self, tmp_path, content_hash):
        bdir = self._blob_dir(content_hash)
        os.makedirs(bdir, exist_ok=True)
        final = self.blob_path(content_hash)
        if not os.path.exists(final):
            os.rename(tmp_path, final)
            _fsync_dir(bdir)
            return True
        else:
            os.unlink(tmp_path)
            return False

    def ensure_blob_file(self, tmp_path, content_hash):
        bdir = self._blob_dir(content_hash)
        os.makedirs(bdir, exist_ok=True)
        final = self.blob_path(content_hash)
        if not os.path.exists(final):
            os.rename(tmp_path, final)
            _fsync_dir(bdir)
        else:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def delete_blob_file(self, content_hash):
        path = self.blob_path(content_hash)
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False

    def stream_blob(self, content_hash):
        path = self.blob_path(content_hash)
        with open(path, 'rb') as f:
            while True:
                data = f.read(BUFFER_SIZE)
                if not data:
                    break
                yield data

    def cleanup_chunks(self, upload_id):
        cdir = self._chunk_dir(upload_id)
        if not os.path.exists(cdir):
            return
        for name in os.listdir(cdir):
            try:
                os.unlink(os.path.join(cdir, name))
            except OSError:
                pass
        try:
            os.rmdir(cdir)
        except OSError:
            pass

    def cleanup_tmp(self):
        if not os.path.exists(self.tmp_dir):
            return
        for name in os.listdir(self.tmp_dir):
            try:
                os.unlink(os.path.join(self.tmp_dir, name))
            except OSError:
                pass

    def validate_blob(self, content_hash):
        path = self.blob_path(content_hash)
        if not os.path.exists(path):
            return None
        hasher = hashlib.sha256()
        with open(path, 'rb') as f:
            while True:
                data = f.read(BUFFER_SIZE)
                if not data:
                    break
                hasher.update(data)
        return hasher.hexdigest()

    def list_blob_files(self):
        result = []
        if not os.path.exists(self.blobs_dir):
            return result
        for prefix in os.listdir(self.blobs_dir):
            pdir = os.path.join(self.blobs_dir, prefix)
            if not os.path.isdir(pdir):
                continue
            for name in os.listdir(pdir):
                result.append(name)
        return result
