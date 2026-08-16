CREATE TABLE IF NOT EXISTS uploads (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    total_size INTEGER NOT NULL,
    chunk_size INTEGER NOT NULL,
    num_chunks INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'uploading',
    artifact_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chunks (
    upload_id TEXT NOT NULL REFERENCES uploads(id),
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
    validated_at TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    upload_id TEXT NOT NULL,
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content_hash TEXT NOT NULL REFERENCES blobs(content_hash),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_artifacts_tenant ON artifacts(tenant_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_content_hash ON artifacts(content_hash);
CREATE INDEX IF NOT EXISTS idx_uploads_tenant ON uploads(tenant_id);
CREATE INDEX IF NOT EXISTS idx_blobs_refcount ON blobs(refcount);
