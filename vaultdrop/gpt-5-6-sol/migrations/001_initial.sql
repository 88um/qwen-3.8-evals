CREATE TABLE uploads (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    name TEXT NOT NULL,
    total_size INTEGER NOT NULL CHECK(total_size >= 0),
    chunk_size INTEGER NOT NULL CHECK(chunk_size > 0),
    state TEXT NOT NULL CHECK(state IN ('uploading','finalizing','finalized')),
    expected_sha256 TEXT,
    expected_size INTEGER,
    artifact_id TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX uploads_tenant_idx ON uploads(tenant_id, created_at);

CREATE TABLE chunks (
    upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL CHECK(chunk_index >= 0),
    size INTEGER NOT NULL CHECK(size >= 0),
    sha256 TEXT NOT NULL,
    PRIMARY KEY(upload_id, chunk_index)
);

CREATE TABLE blobs (
    content_hash TEXT PRIMARY KEY,
    size INTEGER NOT NULL CHECK(size >= 0),
    refcount INTEGER NOT NULL CHECK(refcount >= 0),
    validated INTEGER NOT NULL CHECK(validated IN (0,1)),
    created_at INTEGER NOT NULL
);

CREATE TABLE artifacts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    upload_id TEXT NOT NULL UNIQUE REFERENCES uploads(id),
    name TEXT NOT NULL,
    size INTEGER NOT NULL CHECK(size >= 0),
    sha256 TEXT NOT NULL REFERENCES blobs(content_hash),
    created_at INTEGER NOT NULL
);
CREATE INDEX artifacts_tenant_idx ON artifacts(tenant_id, created_at, id);
CREATE INDEX artifacts_hash_idx ON artifacts(sha256);
