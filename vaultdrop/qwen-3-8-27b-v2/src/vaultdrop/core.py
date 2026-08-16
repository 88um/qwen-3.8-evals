"""VaultDrop core: limits, path layout, errors, streaming primitives."""

import hashlib
import json
import os

# --- Limits (stated in DECISIONS.md; enforced on the wire) -----------------
MAX_CHUNK_SIZE = 32 * 1024 * 1024        # 32 MiB: max chunk PUT body / declared chunk_size
MAX_ARTIFACT_SIZE = 10 * 1024 ** 3       # 10 GiB: max artifact total_size
STREAM_BUF = 1024 * 1024                 # 1 MiB: streaming granularity (memory bound)
JSON_BODY_MAX = 1024 * 1024              # cap for JSON request bodies

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
SHA256_HEX_LEN = 64


class ApiError(Exception):
    """Request-level failure with an HTTP status."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


# --- Relative path layout under $VAULTDROP_STATE_DIR ------------------------
def db_path(state_dir):
    return os.path.join(state_dir, "vaultdrop.db")


def chunks_dir(state_dir, upload_id):
    return os.path.join(state_dir, "chunks", upload_id)


def chunk_path(state_dir, upload_id, index):
    return os.path.join(chunks_dir(state_dir, upload_id), str(index))


def chunk_tmp_dir(state_dir, upload_id):
    return os.path.join(chunks_dir(state_dir, upload_id), ".tmp")


def assemble_tmp_dir(state_dir):
    return os.path.join(state_dir, "tmp")


def blob_path(state_dir, content_hash):
    return os.path.join(state_dir, "blobs", content_hash[:2], content_hash)


def ensure_layout(state_dir):
    for sub in ("chunks", "blobs", "tmp"):
        os.makedirs(os.path.join(state_dir, sub), exist_ok=True)


# --- Durability primitives ---------------------------------------------------
def fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_place(temp_path, final_path):
    """Atomically move temp_path to final_path; fsync contents, then directory."""
    fsync_file(temp_path)
    os.replace(temp_path, final_path)
    fsync_dir(os.path.dirname(final_path))


def unlink_quiet(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def streaming_hash_files(file_paths, sink=None, expected_size=None):
    """Stream bytes from file_paths in order into sink (a writable file object
    or None), updating and returning (sha256_hex, bytes_read).

    Raises ApiError(422) if expected_size is set and the total differs, or if a
    file is missing/short (incomplete stored chunk).
    """
    h = hashlib.sha256()
    total = 0
    for fp in file_paths:
        try:
            f = open(fp, "rb")
        except FileNotFoundError:
            raise ApiError(422, "stored chunk missing; re-upload required")
        with f:
            while True:
                b = f.read(STREAM_BUF)
                if not b:
                    break
                if sink is not None:
                    sink.write(b)
                h.update(b)
                total += len(b)
    if expected_size is not None and total != expected_size:
        raise ApiError(422, "assembled size %d != declared %d" % (total, expected_size))
    return h.hexdigest(), total


def read_json_body(rfile, content_length):
    """Read and decode a JSON request body with a hard size cap."""
    if content_length < 0:
        raise ApiError(400, "bad content-length")
    if content_length > JSON_BODY_MAX:
        raise ApiError(413, "body too large")
    raw = rfile.read(content_length) if content_length else b""
    if len(raw) != content_length:
        raise ApiError(400, "truncated body")
    try:
        return json.loads(raw)
    except ValueError:
        raise ApiError(400, "invalid json body")


def _hasher():
    return hashlib.sha256()


def valid_sha256_hex(s):
    if not isinstance(s, str) or len(s) != SHA256_HEX_LEN:
        return False
    try:
        int(s, 16)
    except ValueError:
        return False
    return True
