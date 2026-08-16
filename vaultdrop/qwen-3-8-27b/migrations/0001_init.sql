-- VaultDrop schema, revision 0001.
-- Applied idempotently by `migrate` / serve self-init. SQLite, WAL mode.
--
-- Invariants encoded here:
--   * uploads.state is a strict lifecycle column ('open' | 'finalized'); the
--     finalize claim is an atomic conditional UPDATE on this column.
--   * chunks is keyed (upload_id, index): a chunk is "received" iff a row exists.
--   * blobs.refcount is the single reference count GC decides from; GC only
--     collects rows where refcount = 0.
--   * artifacts.blob_hash links an artifact to exactly one content blob.

CREATE TABLE IF NOT EXISTS uploads (
    upload_id   TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    total_size  INTEGER NOT NULL CHECK (total_size >= 0),
    chunk_size  INTEGER NOT NULL CHECK (chunk_size > 0),
    state       TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open','finalized')),
    artifact_id TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_uploads_tenant ON uploads (tenant_id);

CREATE TABLE IF NOT EXISTS chunks (
    upload_id TEXT NOT NULL,
    "index"   INTEGER NOT NULL CHECK ("index" >= 0),
    size      INTEGER NOT NULL CHECK (size >= 0),
    sha256    TEXT NOT NULL,
    PRIMARY KEY (upload_id, "index")
);

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL CHECK (size >= 0),
    sha256      TEXT NOT NULL,
    blob_hash   TEXT NOT NULL,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_tenant ON artifacts (tenant_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_blob   ON artifacts (blob_hash);

CREATE TABLE IF NOT EXISTS blobs (
    content_hash TEXT PRIMARY KEY,
    size         INTEGER NOT NULL CHECK (size >= 0),
    refcount     INTEGER NOT NULL DEFAULT 0 CHECK (refcount >= 0),
    validated    INTEGER NOT NULL DEFAULT 0 CHECK (validated IN (0,1,2,3))
    -- validated: 0=unchecked, 1=ok, 2=mismatch, 3=missing-file
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT OR IGNORE INTO schema_meta (key, value) VALUES ('revision', '0001');
