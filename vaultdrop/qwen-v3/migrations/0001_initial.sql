-- VaultDrop schema, revision 1.
-- Applied idempotently by both `migrate` and `serve` startup.

PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;

CREATE TABLE IF NOT EXISTS uploads (
    id              TEXT PRIMARY KEY,
    tenant_id       TEXT NOT NULL,
    name            TEXT NOT NULL,
    total_size      INTEGER NOT NULL CHECK (total_size >= 0),
    chunk_size      INTEGER NOT NULL CHECK (chunk_size >= 1),
    state           TEXT NOT NULL CHECK (state IN ('open', 'finalizing', 'finalized')),
    artifact_id     TEXT,
    created_at      INTEGER NOT NULL,
    finalized_at    INTEGER,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id)
);
CREATE INDEX IF NOT EXISTS idx_uploads_tenant ON uploads(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS upload_chunks (
    upload_id    TEXT NOT NULL,
    chunk_index  INTEGER NOT NULL CHECK (chunk_index >= 0),
    size         INTEGER NOT NULL CHECK (size >= 0),
    sha256       TEXT NOT NULL,
    PRIMARY KEY (upload_id, chunk_index),
    FOREIGN KEY (upload_id) REFERENCES uploads(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artifacts (
    id          TEXT PRIMARY KEY,
    tenant_id   TEXT NOT NULL,
    name        TEXT NOT NULL,
    size        INTEGER NOT NULL CHECK (size >= 0),
    sha256      TEXT NOT NULL,
    blob_hash   TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    FOREIGN KEY (blob_hash) REFERENCES blobs(hash)
);
CREATE INDEX IF NOT EXISTS idx_artifacts_tenant ON artifacts(tenant_id, created_at);

CREATE TABLE IF NOT EXISTS blobs (
    hash         TEXT PRIMARY KEY,
    size         INTEGER NOT NULL CHECK (size >= 0),
    refcount     INTEGER NOT NULL CHECK (refcount >= 0),
    state        TEXT NOT NULL CHECK (state IN ('active', 'collecting')),
    validated    INTEGER NOT NULL DEFAULT 1 CHECK (validated IN (0, 1)),
    gc_pass_id   INTEGER,
    created_at   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_blobs_gc ON blobs(state, refcount);

CREATE TABLE IF NOT EXISTS gc_passes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  INTEGER NOT NULL,
    finished_at INTEGER,
    scanned     INTEGER NOT NULL DEFAULT 0,
    collected   INTEGER NOT NULL DEFAULT 0,
    bytes_freed INTEGER NOT NULL DEFAULT 0
);
