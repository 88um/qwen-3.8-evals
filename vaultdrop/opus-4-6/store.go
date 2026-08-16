package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"fmt"
	"sync"

	_ "modernc.org/sqlite"
)

func newID() string {
	b := make([]byte, 16)
	rand.Read(b)
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	return fmt.Sprintf("%08x-%04x-%04x-%04x-%012x", b[0:4], b[4:6], b[6:8], b[8:10], b[10:])
}

type Store struct {
	db       *sql.DB
	blobLocks sync.Map // map[string]*sync.Mutex — per content-hash lock
}

func NewStore(dbPath string) (*Store, error) {
	db, err := sql.Open("sqlite", dbPath)
	if err != nil {
		return nil, fmt.Errorf("open db: %w", err)
	}
	// SQLite supports one writer at a time; a single connection avoids lock
	// contention and ensures PRAGMAs persist across all queries.
	db.SetMaxOpenConns(1)
	if _, err := db.Exec("PRAGMA journal_mode=WAL"); err != nil {
		return nil, fmt.Errorf("set WAL: %w", err)
	}
	if _, err := db.Exec("PRAGMA busy_timeout=10000"); err != nil {
		return nil, fmt.Errorf("set busy_timeout: %w", err)
	}
	if _, err := db.Exec("PRAGMA synchronous=FULL"); err != nil {
		return nil, fmt.Errorf("set synchronous: %w", err)
	}
	if _, err := db.Exec("PRAGMA foreign_keys=ON"); err != nil {
		return nil, fmt.Errorf("set foreign_keys: %w", err)
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error {
	return s.db.Close()
}

func (s *Store) InitSchema(ctx context.Context) error {
	schema := `
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
	`
	_, err := s.db.ExecContext(ctx, schema)
	return err
}

// RecoverCrash resets uploads stuck in 'finalizing' state back to 'uploading'.
func (s *Store) RecoverCrash(ctx context.Context) error {
	_, err := s.db.ExecContext(ctx, `UPDATE uploads SET state = 'uploading' WHERE state = 'finalizing'`)
	return err
}

func (s *Store) LockBlob(hash string) {
	v, _ := s.blobLocks.LoadOrStore(hash, &sync.Mutex{})
	v.(*sync.Mutex).Lock()
}

func (s *Store) UnlockBlob(hash string) {
	v, ok := s.blobLocks.Load(hash)
	if ok {
		v.(*sync.Mutex).Unlock()
	}
}

// --- Upload operations ---

func (s *Store) CreateUpload(ctx context.Context, tenantID, name string, totalSize, chunkSize int64, numChunks int) (*Upload, error) {
	id := newID()
	_, err := s.db.ExecContext(ctx,
		`INSERT INTO uploads (id, tenant_id, name, total_size, chunk_size, num_chunks, state) VALUES (?, ?, ?, ?, ?, ?, 'uploading')`,
		id, tenantID, name, totalSize, chunkSize, numChunks)
	if err != nil {
		return nil, err
	}
	return &Upload{
		ID:        id,
		TenantID:  tenantID,
		Name:      name,
		TotalSize: totalSize,
		ChunkSize: chunkSize,
		NumChunks: numChunks,
		State:     "uploading",
	}, nil
}

func (s *Store) GetUpload(ctx context.Context, uploadID string) (*Upload, error) {
	u := &Upload{}
	err := s.db.QueryRowContext(ctx,
		`SELECT id, tenant_id, name, total_size, chunk_size, num_chunks, state, artifact_id FROM uploads WHERE id = ?`,
		uploadID).Scan(&u.ID, &u.TenantID, &u.Name, &u.TotalSize, &u.ChunkSize, &u.NumChunks, &u.State, &u.ArtifactID)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return u, err
}

func (s *Store) GetChunkIndices(ctx context.Context, uploadID string) ([]int, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT chunk_index FROM chunks WHERE upload_id = ? ORDER BY chunk_index`, uploadID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var indices []int
	for rows.Next() {
		var idx int
		if err := rows.Scan(&idx); err != nil {
			return nil, err
		}
		indices = append(indices, idx)
	}
	return indices, rows.Err()
}

func (s *Store) GetChunk(ctx context.Context, uploadID string, index int) (*Chunk, error) {
	c := &Chunk{}
	err := s.db.QueryRowContext(ctx,
		`SELECT upload_id, chunk_index, sha256, size FROM chunks WHERE upload_id = ? AND chunk_index = ?`,
		uploadID, index).Scan(&c.UploadID, &c.ChunkIndex, &c.SHA256, &c.Size)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return c, err
}

func (s *Store) InsertChunk(ctx context.Context, uploadID string, index int, sha256 string, size int64) (bool, error) {
	res, err := s.db.ExecContext(ctx,
		`INSERT INTO chunks (upload_id, chunk_index, sha256, size) VALUES (?, ?, ?, ?) ON CONFLICT(upload_id, chunk_index) DO NOTHING`,
		uploadID, index, sha256, size)
	if err != nil {
		return false, err
	}
	n, _ := res.RowsAffected()
	return n == 1, nil
}

// --- Finalize (single IMMEDIATE transaction) ---

type FinalizeResult struct {
	ArtifactID string
	IsNew      bool
}

func (s *Store) Finalize(ctx context.Context, uploadID, tenantID, contentHash, name string, size int64, sha256 string) (*FinalizeResult, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{})
	if err != nil {
		return nil, fmt.Errorf("begin tx: %w", err)
	}
	defer tx.Rollback()

	// Acquire write lock immediately
	if _, err := tx.ExecContext(ctx, "SELECT 1"); err != nil {
		return nil, err
	}

	var state string
	var existingArtifactID *string
	var uploadTenant string
	err = tx.QueryRowContext(ctx,
		`SELECT state, artifact_id, tenant_id FROM uploads WHERE id = ?`, uploadID).Scan(&state, &existingArtifactID, &uploadTenant)
	if err == sql.ErrNoRows {
		return nil, fmt.Errorf("upload not found")
	}
	if err != nil {
		return nil, err
	}
	if uploadTenant != tenantID {
		return nil, fmt.Errorf("upload not found")
	}

	if state == "finalized" && existingArtifactID != nil {
		return &FinalizeResult{ArtifactID: *existingArtifactID, IsNew: false}, nil
	}
	if state != "uploading" {
		return nil, fmt.Errorf("upload in unexpected state: %s", state)
	}

	// Mark as finalizing to prevent concurrent finalize
	res, err := tx.ExecContext(ctx,
		`UPDATE uploads SET state = 'finalizing' WHERE id = ? AND state = 'uploading'`, uploadID)
	if err != nil {
		return nil, err
	}
	n, _ := res.RowsAffected()
	if n != 1 {
		// Concurrent finalize won the race
		err = tx.QueryRowContext(ctx,
			`SELECT state, artifact_id FROM uploads WHERE id = ?`, uploadID).Scan(&state, &existingArtifactID)
		if err != nil {
			return nil, err
		}
		if state == "finalized" && existingArtifactID != nil {
			return &FinalizeResult{ArtifactID: *existingArtifactID, IsNew: false}, nil
		}
		return nil, fmt.Errorf("concurrent finalize conflict")
	}

	// Insert or increment blob refcount
	_, err = tx.ExecContext(ctx,
		`INSERT INTO blobs (content_hash, size, refcount) VALUES (?, ?, 1)
		 ON CONFLICT(content_hash) DO UPDATE SET refcount = refcount + 1`,
		contentHash, size)
	if err != nil {
		return nil, fmt.Errorf("upsert blob: %w", err)
	}

	artifactID := newID()
	_, err = tx.ExecContext(ctx,
		`INSERT INTO artifacts (id, tenant_id, upload_id, name, size, sha256, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?)`,
		artifactID, tenantID, uploadID, name, size, sha256, contentHash)
	if err != nil {
		return nil, fmt.Errorf("insert artifact: %w", err)
	}

	_, err = tx.ExecContext(ctx,
		`UPDATE uploads SET state = 'finalized', artifact_id = ? WHERE id = ?`,
		artifactID, uploadID)
	if err != nil {
		return nil, fmt.Errorf("update upload: %w", err)
	}

	if err := tx.Commit(); err != nil {
		return nil, fmt.Errorf("commit: %w", err)
	}

	return &FinalizeResult{ArtifactID: artifactID, IsNew: true}, nil
}

// --- Artifact operations ---

func (s *Store) GetArtifact(ctx context.Context, artifactID, tenantID string) (*Artifact, error) {
	a := &Artifact{}
	err := s.db.QueryRowContext(ctx,
		`SELECT id, tenant_id, upload_id, name, size, sha256, content_hash FROM artifacts WHERE id = ? AND tenant_id = ?`,
		artifactID, tenantID).Scan(&a.ID, &a.TenantID, &a.UploadID, &a.Name, &a.Size, &a.SHA256, &a.ContentHash)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return a, err
}

func (s *Store) ListArtifacts(ctx context.Context, tenantID string) ([]Artifact, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT id, tenant_id, upload_id, name, size, sha256, content_hash FROM artifacts WHERE tenant_id = ? ORDER BY created_at`,
		tenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var arts []Artifact
	for rows.Next() {
		var a Artifact
		if err := rows.Scan(&a.ID, &a.TenantID, &a.UploadID, &a.Name, &a.Size, &a.SHA256, &a.ContentHash); err != nil {
			return nil, err
		}
		arts = append(arts, a)
	}
	return arts, rows.Err()
}

func (s *Store) DeleteArtifact(ctx context.Context, artifactID, tenantID string) (string, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{})
	if err != nil {
		return "", err
	}
	defer tx.Rollback()

	var contentHash string
	err = tx.QueryRowContext(ctx,
		`SELECT content_hash FROM artifacts WHERE id = ? AND tenant_id = ?`,
		artifactID, tenantID).Scan(&contentHash)
	if err == sql.ErrNoRows {
		return "", fmt.Errorf("not found")
	}
	if err != nil {
		return "", err
	}

	_, err = tx.ExecContext(ctx, `DELETE FROM artifacts WHERE id = ?`, artifactID)
	if err != nil {
		return "", err
	}

	_, err = tx.ExecContext(ctx,
		`UPDATE blobs SET refcount = refcount - 1 WHERE content_hash = ?`, contentHash)
	if err != nil {
		return "", err
	}

	return contentHash, tx.Commit()
}

// --- Blob operations for GC ---

type GCBlobInfo struct {
	ContentHash string
	Size        int64
}

func (s *Store) CollectUnreferencedBlobs(ctx context.Context) ([]GCBlobInfo, error) {
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{})
	if err != nil {
		return nil, err
	}
	defer tx.Rollback()

	rows, err := tx.QueryContext(ctx,
		`SELECT content_hash, size FROM blobs WHERE refcount <= 0`)
	if err != nil {
		return nil, err
	}
	var blobs []GCBlobInfo
	for rows.Next() {
		var b GCBlobInfo
		if err := rows.Scan(&b.ContentHash, &b.Size); err != nil {
			rows.Close()
			return nil, err
		}
		blobs = append(blobs, b)
	}
	rows.Close()
	if err := rows.Err(); err != nil {
		return nil, err
	}

	if len(blobs) > 0 {
		for _, b := range blobs {
			_, err := tx.ExecContext(ctx, `DELETE FROM blobs WHERE content_hash = ? AND refcount <= 0`, b.ContentHash)
			if err != nil {
				return nil, err
			}
		}
	}

	return blobs, tx.Commit()
}

// BlobExists checks if a blob record exists (used by GC re-check after lock).
func (s *Store) BlobExists(ctx context.Context, contentHash string) (bool, error) {
	var n int
	err := s.db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM blobs WHERE content_hash = ?`, contentHash).Scan(&n)
	return n > 0, err
}

func (s *Store) ListBlobs(ctx context.Context) ([]Blob, error) {
	rows, err := s.db.QueryContext(ctx,
		`SELECT content_hash, refcount, size, validated FROM blobs ORDER BY content_hash`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var blobs []Blob
	for rows.Next() {
		var b Blob
		var validated int
		if err := rows.Scan(&b.ContentHash, &b.RefCount, &b.Size, &validated); err != nil {
			return nil, err
		}
		b.Validated = validated != 0
		blobs = append(blobs, b)
	}
	return blobs, rows.Err()
}

func (s *Store) MarkBlobValidated(ctx context.Context, contentHash string, valid bool) error {
	v := 0
	if valid {
		v = 1
	}
	_, err := s.db.ExecContext(ctx,
		`UPDATE blobs SET validated = ?, validated_at = datetime('now') WHERE content_hash = ?`, v, contentHash)
	return err
}

func (s *Store) CountBlobs(ctx context.Context) (int, error) {
	var n int
	err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM blobs`).Scan(&n)
	return n, err
}
