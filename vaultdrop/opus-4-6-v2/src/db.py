import sqlite3
import threading
import os

SCHEMA = """
CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    total_size INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL,
    num_chunks INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'uploading',
    artifact_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    upload_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    PRIMARY KEY (upload_id, chunk_index)
);

CREATE TABLE IF NOT EXISTS blobs (
    content_hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    refcount INTEGER NOT NULL DEFAULT 0,
    validated INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    upload_id TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%f','now'))
);

CREATE INDEX IF NOT EXISTS idx_artifacts_tenant ON artifacts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_content ON artifacts(content_hash);
CREATE INDEX IF NOT EXISTS idx_chunks_upload ON chunks(upload_id);
"""


class Database:
    def __init__(self, state_dir):
        self.db_path = os.path.join(state_dir, 'vaultdrop.db')
        self._local = threading.local()

    def _conn(self):
        if not hasattr(self._local, 'conn') or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return self._local.conn

    def migrate(self):
        conn = self._conn()
        conn.executescript(SCHEMA)
        conn.commit()

    def recover(self):
        conn = self._conn()
        conn.execute("UPDATE uploads SET state='uploading' WHERE state='finalizing'")
        conn.commit()

    def create_upload(self, upload_id, tenant_id, name, total_size, chunk_size, num_chunks):
        conn = self._conn()
        conn.execute(
            "INSERT INTO uploads (id, tenant_id, name, total_size, chunk_size, num_chunks) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (upload_id, tenant_id, name, total_size, chunk_size, num_chunks),
        )
        conn.commit()

    def get_upload(self, upload_id):
        return self._conn().execute(
            "SELECT * FROM uploads WHERE id=?", (upload_id,)
        ).fetchone()

    def get_received_chunks(self, upload_id):
        rows = self._conn().execute(
            "SELECT chunk_index FROM chunks WHERE upload_id=? ORDER BY chunk_index",
            (upload_id,),
        ).fetchall()
        return [r['chunk_index'] for r in rows]

    def get_chunk(self, upload_id, chunk_index):
        return self._conn().execute(
            "SELECT * FROM chunks WHERE upload_id=? AND chunk_index=?",
            (upload_id, chunk_index),
        ).fetchone()

    def insert_chunk(self, upload_id, chunk_index, sha256, size):
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO chunks (upload_id, chunk_index, sha256, size) VALUES (?, ?, ?, ?)",
                (upload_id, chunk_index, sha256, size),
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False

    def try_set_finalizing(self, upload_id, tenant_id):
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE uploads SET state='finalizing' "
                "WHERE id=? AND tenant_id=? AND state='uploading'",
                (upload_id, tenant_id),
            )
            if cur.rowcount == 1:
                conn.commit()
                return 'ok'
            row = conn.execute(
                "SELECT state, artifact_id FROM uploads WHERE id=? AND tenant_id=?",
                (upload_id, tenant_id),
            ).fetchone()
            conn.rollback()
            if row is None:
                return 'not_found'
            if row['state'] == 'finalized':
                return 'already_finalized'
            return 'conflict'
        except Exception:
            conn.rollback()
            raise

    def commit_finalize(self, upload_id, artifact_id, tenant_id, name, size, content_hash):
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO blobs (content_hash, size, refcount) VALUES (?, ?, 1) "
                "ON CONFLICT(content_hash) DO UPDATE SET refcount=refcount+1",
                (content_hash, size),
            )
            conn.execute(
                "INSERT INTO artifacts (id, tenant_id, upload_id, name, size, content_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (artifact_id, tenant_id, upload_id, name, size, content_hash),
            )
            conn.execute(
                "UPDATE uploads SET state='finalized', artifact_id=? WHERE id=?",
                (artifact_id, upload_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def rollback_finalizing(self, upload_id):
        conn = self._conn()
        conn.execute(
            "UPDATE uploads SET state='uploading' WHERE id=? AND state='finalizing'",
            (upload_id,),
        )
        conn.commit()

    def get_artifact(self, artifact_id):
        return self._conn().execute(
            "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
        ).fetchone()

    def list_artifacts(self, tenant_id):
        return self._conn().execute(
            "SELECT id, name, size, content_hash FROM artifacts WHERE tenant_id=? ORDER BY created_at",
            (tenant_id,),
        ).fetchall()

    def delete_artifact(self, artifact_id, tenant_id):
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT content_hash FROM artifacts WHERE id=? AND tenant_id=?",
                (artifact_id, tenant_id),
            ).fetchone()
            if row is None:
                conn.rollback()
                return False
            content_hash = row['content_hash']
            conn.execute("DELETE FROM artifacts WHERE id=?", (artifact_id,))
            conn.execute(
                "UPDATE blobs SET refcount=refcount-1 WHERE content_hash=?",
                (content_hash,),
            )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise

    def get_gc_candidates(self):
        return self._conn().execute(
            "SELECT content_hash, size FROM blobs WHERE refcount<=0"
        ).fetchall()

    def gc_delete_blob(self, content_hash):
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "DELETE FROM blobs WHERE content_hash=? AND refcount<=0",
                (content_hash,),
            )
            deleted = cur.rowcount > 0
            conn.commit()
            return deleted
        except Exception:
            conn.rollback()
            raise

    def blob_exists(self, content_hash):
        row = self._conn().execute(
            "SELECT 1 FROM blobs WHERE content_hash=?", (content_hash,)
        ).fetchone()
        return row is not None

    def list_blobs(self):
        return self._conn().execute(
            "SELECT content_hash, refcount, size, validated FROM blobs ORDER BY content_hash"
        ).fetchall()

    def set_blob_validated(self, content_hash, valid):
        conn = self._conn()
        conn.execute(
            "UPDATE blobs SET validated=? WHERE content_hash=?",
            (1 if valid else -1, content_hash),
        )
        conn.commit()

    def get_blob(self, content_hash):
        return self._conn().execute(
            "SELECT * FROM blobs WHERE content_hash=?", (content_hash,)
        ).fetchone()

    def get_upload_artifact(self, upload_id):
        row = self._conn().execute(
            "SELECT artifact_id FROM uploads WHERE id=? AND state='finalized'",
            (upload_id,),
        ).fetchone()
        return row['artifact_id'] if row else None
