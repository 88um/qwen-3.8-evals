package main

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

type BlobManager struct {
	stateDir string
}

func NewBlobManager(stateDir string) *BlobManager {
	return &BlobManager{stateDir: stateDir}
}

func (bm *BlobManager) Init() error {
	for _, d := range []string{bm.chunksRoot(), bm.blobsRoot()} {
		if err := os.MkdirAll(d, 0755); err != nil {
			return err
		}
	}
	return nil
}

func (bm *BlobManager) chunksRoot() string {
	return filepath.Join(bm.stateDir, "chunks")
}

func (bm *BlobManager) blobsRoot() string {
	return filepath.Join(bm.stateDir, "blobs")
}

func (bm *BlobManager) chunkDir(uploadID string) string {
	return filepath.Join(bm.chunksRoot(), uploadID)
}

func (bm *BlobManager) chunkPath(uploadID string, index int) string {
	return filepath.Join(bm.chunkDir(uploadID), fmt.Sprintf("%d", index))
}

func (bm *BlobManager) blobDir(contentHash string) string {
	return filepath.Join(bm.blobsRoot(), contentHash[:2])
}

func (bm *BlobManager) BlobPath(contentHash string) string {
	return filepath.Join(bm.blobDir(contentHash), contentHash)
}

func (bm *BlobManager) EnsureChunkDir(uploadID string) error {
	return os.MkdirAll(bm.chunkDir(uploadID), 0755)
}

// WriteChunk streams the body to a temp file, computes SHA-256, and atomically
// renames to the final chunk path. Returns the hex-encoded SHA-256 and byte count.
func (bm *BlobManager) WriteChunk(uploadID string, index int, body io.Reader) (string, int64, error) {
	dir := bm.chunkDir(uploadID)
	tmp, err := os.CreateTemp(dir, ".chunk-*.tmp")
	if err != nil {
		return "", 0, fmt.Errorf("create temp chunk: %w", err)
	}
	tmpName := tmp.Name()
	defer func() {
		tmp.Close()
		os.Remove(tmpName) // no-op if already renamed
	}()

	hasher := sha256.New()
	w := io.MultiWriter(tmp, hasher)
	n, err := io.Copy(w, body)
	if err != nil {
		return "", 0, fmt.Errorf("write chunk: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		return "", 0, fmt.Errorf("fsync chunk: %w", err)
	}
	if err := tmp.Close(); err != nil {
		return "", 0, err
	}

	hash := hex.EncodeToString(hasher.Sum(nil))
	final := bm.chunkPath(uploadID, index)
	if err := os.Rename(tmpName, final); err != nil {
		return "", 0, fmt.Errorf("rename chunk: %w", err)
	}
	syncDir(dir)
	return hash, n, nil
}

// ChunkExists checks if the chunk file exists on disk.
func (bm *BlobManager) ChunkExists(uploadID string, index int) bool {
	_, err := os.Stat(bm.chunkPath(uploadID, index))
	return err == nil
}

// AssembleBlob concatenates all chunks for an upload into a temp file in the blobs
// directory, computing the full SHA-256. Returns the temp path, hash, and total size.
func (bm *BlobManager) AssembleBlob(uploadID string, numChunks int) (string, string, int64, error) {
	tmp, err := os.CreateTemp(bm.blobsRoot(), ".blob-*.tmp")
	if err != nil {
		return "", "", 0, fmt.Errorf("create temp blob: %w", err)
	}
	tmpName := tmp.Name()

	hasher := sha256.New()
	w := io.MultiWriter(tmp, hasher)
	var total int64

	for i := 0; i < numChunks; i++ {
		p := bm.chunkPath(uploadID, i)
		f, err := os.Open(p)
		if err != nil {
			tmp.Close()
			os.Remove(tmpName)
			return "", "", 0, fmt.Errorf("open chunk %d: %w", i, err)
		}
		n, err := io.Copy(w, f)
		f.Close()
		if err != nil {
			tmp.Close()
			os.Remove(tmpName)
			return "", "", 0, fmt.Errorf("copy chunk %d: %w", i, err)
		}
		total += n
	}

	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return "", "", 0, fmt.Errorf("fsync blob: %w", err)
	}
	tmp.Close()

	hash := hex.EncodeToString(hasher.Sum(nil))
	return tmpName, hash, total, nil
}

// InstallBlob atomically moves the temp file to its content-addressed path.
// If the blob file already exists, removes the temp file.
func (bm *BlobManager) InstallBlob(tmpPath, contentHash string) error {
	dir := bm.blobDir(contentHash)
	if err := os.MkdirAll(dir, 0755); err != nil {
		return err
	}
	final := bm.BlobPath(contentHash)
	if _, err := os.Stat(final); err == nil {
		os.Remove(tmpPath)
		return nil
	}
	if err := os.Rename(tmpPath, final); err != nil {
		return fmt.Errorf("rename blob: %w", err)
	}
	syncDir(dir)
	return nil
}

// OpenBlob opens the blob file for reading (streaming downloads).
func (bm *BlobManager) OpenBlob(contentHash string) (*os.File, error) {
	return os.Open(bm.BlobPath(contentHash))
}

// BlobFileExists checks if the blob file exists on disk.
func (bm *BlobManager) BlobFileExists(contentHash string) bool {
	_, err := os.Stat(bm.BlobPath(contentHash))
	return err == nil
}

// RemoveBlob deletes the blob file from disk.
func (bm *BlobManager) RemoveBlob(contentHash string) error {
	err := os.Remove(bm.BlobPath(contentHash))
	if os.IsNotExist(err) {
		return nil
	}
	return err
}

// RemoveChunks deletes the chunk directory for an upload.
func (bm *BlobManager) RemoveChunks(uploadID string) error {
	return os.RemoveAll(bm.chunkDir(uploadID))
}

// HashBlobFile reads the blob file and computes its SHA-256.
func (bm *BlobManager) HashBlobFile(contentHash string) (string, error) {
	f, err := os.Open(bm.BlobPath(contentHash))
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func syncDir(dir string) {
	d, err := os.Open(dir)
	if err != nil {
		return
	}
	d.Sync()
	d.Close()
}
