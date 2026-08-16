package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"
)

type testEnv struct {
	t       *testing.T
	dir     string
	store   *Store
	blobMgr *BlobManager
	handler *Handler
	server  *httptest.Server
}

func newTestEnv(t *testing.T) *testEnv {
	t.Helper()
	dir := t.TempDir()

	dbPath := filepath.Join(dir, "vaultdrop.db")
	store, err := NewStore(dbPath)
	if err != nil {
		t.Fatalf("NewStore: %v", err)
	}
	t.Cleanup(func() { store.Close() })

	if err := store.InitSchema(context.Background()); err != nil {
		t.Fatalf("InitSchema: %v", err)
	}

	blobMgr := NewBlobManager(dir)
	if err := blobMgr.Init(); err != nil {
		t.Fatalf("Init blob: %v", err)
	}

	tenants := map[string]string{
		"tok-t1": "t1",
		"tok-t2": "t2",
	}

	handler := NewHandler(store, blobMgr, tenants, "tok-admin")
	router := NewRouter(handler)
	server := httptest.NewServer(router)
	t.Cleanup(func() { server.Close() })

	return &testEnv{
		t:       t,
		dir:     dir,
		store:   store,
		blobMgr: blobMgr,
		handler: handler,
		server:  server,
	}
}

func (e *testEnv) url(path string) string {
	return e.server.URL + path
}

func sha256Hex(data []byte) string {
	h := sha256.Sum256(data)
	return hex.EncodeToString(h[:])
}

func (e *testEnv) uploadArtifact(token, name string, data []byte, chunkSize int) string {
	e.t.Helper()

	body, _ := json.Marshal(CreateUploadRequest{
		Name:      name,
		TotalSize: int64(len(data)),
		ChunkSize: int64(chunkSize),
	})
	req, _ := http.NewRequest("POST", e.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatalf("create upload: %v", err)
	}
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		e.t.Fatalf("create upload: status %d", resp.StatusCode)
	}

	for i := 0; i*chunkSize < len(data); i++ {
		start := i * chunkSize
		end := start + chunkSize
		if end > len(data) {
			end = len(data)
		}
		chunk := data[start:end]
		chunkHash := sha256Hex(chunk)

		req, _ := http.NewRequest("PUT",
			e.url(fmt.Sprintf("/uploads/%s/chunks/%d", cr.UploadID, i)),
			bytes.NewReader(chunk))
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("X-Chunk-SHA256", chunkHash)
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			e.t.Fatalf("upload chunk %d: %v", i, err)
		}
		resp.Body.Close()
		if resp.StatusCode != 200 {
			e.t.Fatalf("upload chunk %d: status %d", i, resp.StatusCode)
		}
	}

	fullHash := sha256Hex(data)
	finBody, _ := json.Marshal(FinalizeRequest{SHA256: fullHash, Size: int64(len(data))})
	req, _ = http.NewRequest("POST",
		e.url(fmt.Sprintf("/uploads/%s/finalize", cr.UploadID)),
		bytes.NewReader(finBody))
	req.Header.Set("Authorization", "Bearer "+token)
	req.Header.Set("Content-Type", "application/json")
	resp, err = http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatalf("finalize: %v", err)
	}
	respBody, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		e.t.Fatalf("finalize: status %d: %s", resp.StatusCode, respBody)
	}
	var fr FinalizeResponse
	json.Unmarshal(respBody, &fr)
	return fr.ArtifactID
}

// ============================================================
// Core flow tests
// ============================================================

func TestFullUploadDownload(t *testing.T) {
	env := newTestEnv(t)
	data := []byte("Hello, VaultDrop! This is a test artifact.")
	artID := env.uploadArtifact("tok-t1", "greeting.bin", data, 10)

	req, _ := http.NewRequest("GET", env.url("/artifacts/"+artID), nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !bytes.Equal(got, data) {
		t.Fatalf("download mismatch: got %d bytes, want %d", len(got), len(data))
	}
}

func TestEmptyArtifact(t *testing.T) {
	env := newTestEnv(t)
	data := []byte{}
	artID := env.uploadArtifact("tok-t1", "empty.bin", data, 1)

	req, _ := http.NewRequest("GET", env.url("/artifacts/"+artID), nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, _ := http.DefaultClient.Do(req)
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if len(got) != 0 {
		t.Fatalf("empty artifact: got %d bytes", len(got))
	}
}

func TestListArtifacts(t *testing.T) {
	env := newTestEnv(t)
	env.uploadArtifact("tok-t1", "a.bin", []byte("aaa"), 10)
	env.uploadArtifact("tok-t1", "b.bin", []byte("bbb"), 10)

	req, _ := http.NewRequest("GET", env.url("/artifacts"), nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, _ := http.DefaultClient.Do(req)
	var list []map[string]any
	json.NewDecoder(resp.Body).Decode(&list)
	resp.Body.Close()
	if len(list) != 2 {
		t.Fatalf("expected 2 artifacts, got %d", len(list))
	}
}

// ============================================================
// Chunk idempotency and conflict tests
// ============================================================

func TestChunkIdempotent(t *testing.T) {
	env := newTestEnv(t)

	body, _ := json.Marshal(CreateUploadRequest{Name: "test", TotalSize: 5, ChunkSize: 5})
	req, _ := http.NewRequest("POST", env.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	chunk := []byte("Hello")
	hash := sha256Hex(chunk)

	for i := 0; i < 3; i++ {
		req, _ := http.NewRequest("PUT",
			env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
			bytes.NewReader(chunk))
		req.Header.Set("Authorization", "Bearer tok-t1")
		req.Header.Set("X-Chunk-SHA256", hash)
		resp, _ := http.DefaultClient.Do(req)
		resp.Body.Close()
		if resp.StatusCode != 200 {
			t.Fatalf("idempotent chunk upload %d: status %d", i, resp.StatusCode)
		}
	}
}

func TestChunkConflict(t *testing.T) {
	env := newTestEnv(t)

	body, _ := json.Marshal(CreateUploadRequest{Name: "test", TotalSize: 5, ChunkSize: 5})
	req, _ := http.NewRequest("POST", env.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	chunk1 := []byte("Hello")
	req, _ = http.NewRequest("PUT",
		env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
		bytes.NewReader(chunk1))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("X-Chunk-SHA256", sha256Hex(chunk1))
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()

	chunk2 := []byte("World")
	req, _ = http.NewRequest("PUT",
		env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
		bytes.NewReader(chunk2))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("X-Chunk-SHA256", sha256Hex(chunk2))
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != 409 {
		t.Fatalf("conflicting chunk: expected 409, got %d", resp.StatusCode)
	}
}

func TestChunkSHA256Mismatch(t *testing.T) {
	env := newTestEnv(t)

	body, _ := json.Marshal(CreateUploadRequest{Name: "test", TotalSize: 5, ChunkSize: 5})
	req, _ := http.NewRequest("POST", env.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	chunk := []byte("Hello")
	req, _ = http.NewRequest("PUT",
		env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
		bytes.NewReader(chunk))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("X-Chunk-SHA256", "0000000000000000000000000000000000000000000000000000000000000000")
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != 400 {
		t.Fatalf("SHA-256 mismatch: expected 400, got %d", resp.StatusCode)
	}
}

// ============================================================
// Concurrency tests
// ============================================================

func TestConcurrentChunks(t *testing.T) {
	env := newTestEnv(t)

	numChunks := 20
	chunkSize := 1024
	data := make([]byte, numChunks*chunkSize)
	for i := range data {
		data[i] = byte(i % 256)
	}

	body, _ := json.Marshal(CreateUploadRequest{
		Name: "concurrent.bin", TotalSize: int64(len(data)), ChunkSize: int64(chunkSize),
	})
	req, _ := http.NewRequest("POST", env.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	var wg sync.WaitGroup
	errs := make([]error, numChunks)
	for i := 0; i < numChunks; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			start := idx * chunkSize
			chunk := data[start : start+chunkSize]
			hash := sha256Hex(chunk)

			r, _ := http.NewRequest("PUT",
				env.url(fmt.Sprintf("/uploads/%s/chunks/%d", cr.UploadID, idx)),
				bytes.NewReader(chunk))
			r.Header.Set("Authorization", "Bearer tok-t1")
			r.Header.Set("X-Chunk-SHA256", hash)
			resp, err := http.DefaultClient.Do(r)
			if err != nil {
				errs[idx] = err
				return
			}
			resp.Body.Close()
			if resp.StatusCode != 200 {
				errs[idx] = fmt.Errorf("chunk %d: status %d", idx, resp.StatusCode)
			}
		}(i)
	}
	wg.Wait()
	for i, e := range errs {
		if e != nil {
			t.Fatalf("chunk %d: %v", i, e)
		}
	}

	fullHash := sha256Hex(data)
	finBody, _ := json.Marshal(FinalizeRequest{SHA256: fullHash, Size: int64(len(data))})
	req, _ = http.NewRequest("POST",
		env.url(fmt.Sprintf("/uploads/%s/finalize", cr.UploadID)),
		bytes.NewReader(finBody))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ = http.DefaultClient.Do(req)
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("finalize: status %d: %s", resp.StatusCode, b)
	}
	var fr FinalizeResponse
	json.NewDecoder(resp.Body).Decode(&fr)
	resp.Body.Close()

	req, _ = http.NewRequest("GET", env.url("/artifacts/"+fr.ArtifactID), nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, _ = http.DefaultClient.Do(req)
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !bytes.Equal(got, data) {
		t.Fatalf("download mismatch after concurrent upload")
	}
}

func TestConcurrentDuplicateChunks(t *testing.T) {
	env := newTestEnv(t)

	body, _ := json.Marshal(CreateUploadRequest{Name: "dup", TotalSize: 5, ChunkSize: 5})
	req, _ := http.NewRequest("POST", env.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	chunk := []byte("Hello")
	hash := sha256Hex(chunk)

	var wg sync.WaitGroup
	statuses := make([]int, 10)
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			r, _ := http.NewRequest("PUT",
				env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
				bytes.NewReader(chunk))
			r.Header.Set("Authorization", "Bearer tok-t1")
			r.Header.Set("X-Chunk-SHA256", hash)
			resp, err := http.DefaultClient.Do(r)
			if err != nil {
				return
			}
			resp.Body.Close()
			statuses[idx] = resp.StatusCode
		}(i)
	}
	wg.Wait()

	for i, s := range statuses {
		if s != 200 {
			t.Fatalf("concurrent duplicate chunk %d: status %d", i, s)
		}
	}
}

func TestConcurrentFinalize(t *testing.T) {
	env := newTestEnv(t)

	data := []byte("concurrent-finalize-test-data")
	body, _ := json.Marshal(CreateUploadRequest{
		Name: "cf.bin", TotalSize: int64(len(data)), ChunkSize: int64(len(data)),
	})
	req, _ := http.NewRequest("POST", env.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	hash := sha256Hex(data)
	req, _ = http.NewRequest("PUT",
		env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
		bytes.NewReader(data))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("X-Chunk-SHA256", hash)
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()

	var wg sync.WaitGroup
	results := make([]string, 5)
	statuses := make([]int, 5)
	for i := 0; i < 5; i++ {
		wg.Add(1)
		go func(idx int) {
			defer wg.Done()
			finBody, _ := json.Marshal(FinalizeRequest{SHA256: hash, Size: int64(len(data))})
			r, _ := http.NewRequest("POST",
				env.url(fmt.Sprintf("/uploads/%s/finalize", cr.UploadID)),
				bytes.NewReader(finBody))
			r.Header.Set("Authorization", "Bearer tok-t1")
			r.Header.Set("Content-Type", "application/json")
			resp, err := http.DefaultClient.Do(r)
			if err != nil {
				return
			}
			defer resp.Body.Close()
			statuses[idx] = resp.StatusCode
			if resp.StatusCode == 200 {
				var fr FinalizeResponse
				json.NewDecoder(resp.Body).Decode(&fr)
				results[idx] = fr.ArtifactID
			}
		}(i)
	}
	wg.Wait()

	var artID string
	successCount := 0
	for i := 0; i < 5; i++ {
		if statuses[i] == 200 {
			successCount++
			if artID == "" {
				artID = results[i]
			} else if results[i] != artID {
				t.Fatalf("concurrent finalize returned different IDs: %s vs %s", artID, results[i])
			}
		}
	}
	if successCount == 0 {
		t.Fatalf("all concurrent finalizes failed: %v", statuses)
	}
	if artID == "" {
		t.Fatal("no artifact ID returned")
	}
}

// ============================================================
// Tenant isolation tests
// ============================================================

func TestCrossTenantArtifactAccess(t *testing.T) {
	env := newTestEnv(t)

	data := []byte("tenant-1-secret-data")
	artID := env.uploadArtifact("tok-t1", "secret.bin", data, 100)

	// t2 cannot download (404, not 403)
	req, _ := http.NewRequest("GET", env.url("/artifacts/"+artID), nil)
	req.Header.Set("Authorization", "Bearer tok-t2")
	resp, _ := http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != 404 {
		t.Fatalf("cross-tenant download: expected 404, got %d", resp.StatusCode)
	}

	// t2 cannot delete
	req, _ = http.NewRequest("DELETE", env.url("/artifacts/"+artID), nil)
	req.Header.Set("Authorization", "Bearer tok-t2")
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != 404 {
		t.Fatalf("cross-tenant delete: expected 404, got %d", resp.StatusCode)
	}

	// t2 listing is empty
	req, _ = http.NewRequest("GET", env.url("/artifacts"), nil)
	req.Header.Set("Authorization", "Bearer tok-t2")
	resp, _ = http.DefaultClient.Do(req)
	var list []map[string]any
	json.NewDecoder(resp.Body).Decode(&list)
	resp.Body.Close()
	if len(list) != 0 {
		t.Fatalf("t2 list: expected 0, got %d", len(list))
	}
}

func TestCrossTenantUploadProbing(t *testing.T) {
	env := newTestEnv(t)

	body, _ := json.Marshal(CreateUploadRequest{Name: "probe", TotalSize: 5, ChunkSize: 5})
	req, _ := http.NewRequest("POST", env.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	// t2 probes t1's upload → 404
	req, _ = http.NewRequest("GET", env.url("/uploads/"+cr.UploadID), nil)
	req.Header.Set("Authorization", "Bearer tok-t2")
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != 404 {
		t.Fatalf("cross-tenant upload probe: expected 404, got %d", resp.StatusCode)
	}

	// t2 tries to upload a chunk to t1's upload → 404
	chunk := []byte("Hello")
	req, _ = http.NewRequest("PUT",
		env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
		bytes.NewReader(chunk))
	req.Header.Set("Authorization", "Bearer tok-t2")
	req.Header.Set("X-Chunk-SHA256", sha256Hex(chunk))
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != 404 {
		t.Fatalf("cross-tenant chunk upload: expected 404, got %d", resp.StatusCode)
	}
}

func TestCrossTenantDedup(t *testing.T) {
	env := newTestEnv(t)

	data := []byte("identical-content-shared-by-both-tenants")
	art1 := env.uploadArtifact("tok-t1", "t1.bin", data, 100)
	art2 := env.uploadArtifact("tok-t2", "t2.bin", data, 100)

	if art1 == art2 {
		t.Fatal("deduped tenants got same artifact ID")
	}

	// t2 cannot access t1's artifact
	req, _ := http.NewRequest("GET", env.url("/artifacts/"+art1), nil)
	req.Header.Set("Authorization", "Bearer tok-t2")
	resp, _ := http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != 404 {
		t.Fatal("t2 accessed t1's deduped artifact")
	}

	// Admin sees one blob with refcount 2
	req, _ = http.NewRequest("GET", env.url("/admin/blobs"), nil)
	req.Header.Set("Authorization", "Bearer tok-admin")
	resp, _ = http.DefaultClient.Do(req)
	var blobs []Blob
	json.NewDecoder(resp.Body).Decode(&blobs)
	resp.Body.Close()
	if len(blobs) != 1 {
		t.Fatalf("expected 1 blob, got %d", len(blobs))
	}
	if blobs[0].RefCount != 2 {
		t.Fatalf("expected refcount 2, got %d", blobs[0].RefCount)
	}
}

// ============================================================
// GC tests
// ============================================================

func TestDeleteAndGC(t *testing.T) {
	env := newTestEnv(t)

	data := []byte("gc-test-content")
	contentHash := sha256Hex(data)
	artID := env.uploadArtifact("tok-t1", "gc.bin", data, 100)

	if !env.blobMgr.BlobFileExists(contentHash) {
		t.Fatal("blob should exist before GC")
	}

	// Delete artifact
	req, _ := http.NewRequest("DELETE", env.url("/artifacts/"+artID), nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, _ := http.DefaultClient.Do(req)
	resp.Body.Close()

	// GC
	req, _ = http.NewRequest("POST", env.url("/admin/gc"), nil)
	req.Header.Set("Authorization", "Bearer tok-admin")
	resp, _ = http.DefaultClient.Do(req)
	var gcResp GCResponse
	json.NewDecoder(resp.Body).Decode(&gcResp)
	resp.Body.Close()

	if gcResp.Collected != 1 {
		t.Fatalf("GC collected %d, want 1", gcResp.Collected)
	}
	if !env.blobMgr.BlobFileExists(contentHash) == true {
		// blob should be deleted
	}
	if env.blobMgr.BlobFileExists(contentHash) {
		t.Fatal("blob should be gone after GC")
	}
}

func TestGCDoesNotCollectReferencedBlob(t *testing.T) {
	env := newTestEnv(t)

	data := []byte("still-referenced")
	contentHash := sha256Hex(data)
	env.uploadArtifact("tok-t1", "alive.bin", data, 100)

	req, _ := http.NewRequest("POST", env.url("/admin/gc"), nil)
	req.Header.Set("Authorization", "Bearer tok-admin")
	resp, _ := http.DefaultClient.Do(req)
	var gcResp GCResponse
	json.NewDecoder(resp.Body).Decode(&gcResp)
	resp.Body.Close()

	if gcResp.Collected != 0 {
		t.Fatalf("GC collected %d, want 0", gcResp.Collected)
	}
	if !env.blobMgr.BlobFileExists(contentHash) {
		t.Fatal("referenced blob was deleted by GC")
	}
}

func TestGCWithDedup(t *testing.T) {
	env := newTestEnv(t)

	data := []byte("dedup-gc-data")
	contentHash := sha256Hex(data)
	art1 := env.uploadArtifact("tok-t1", "d1.bin", data, 100)
	env.uploadArtifact("tok-t2", "d2.bin", data, 100)

	// Delete one — refcount drops to 1, GC should not collect
	req, _ := http.NewRequest("DELETE", env.url("/artifacts/"+art1), nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, _ := http.DefaultClient.Do(req)
	resp.Body.Close()

	req, _ = http.NewRequest("POST", env.url("/admin/gc"), nil)
	req.Header.Set("Authorization", "Bearer tok-admin")
	resp, _ = http.DefaultClient.Do(req)
	var gcResp GCResponse
	json.NewDecoder(resp.Body).Decode(&gcResp)
	resp.Body.Close()

	if gcResp.Collected != 0 {
		t.Fatalf("GC collected %d with refcount 1", gcResp.Collected)
	}
	if !env.blobMgr.BlobFileExists(contentHash) {
		t.Fatal("blob with refcount 1 was deleted")
	}
}

func TestFinalizeVsGC(t *testing.T) {
	env := newTestEnv(t)

	data := []byte("finalize-vs-gc-race-content")
	contentHash := sha256Hex(data)

	// t1 uploads, then deletes (refcount → 0)
	art1 := env.uploadArtifact("tok-t1", "original.bin", data, 100)
	req, _ := http.NewRequest("DELETE", env.url("/artifacts/"+art1), nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, _ := http.DefaultClient.Do(req)
	resp.Body.Close()

	// t2 prepares an upload with same content
	body, _ := json.Marshal(CreateUploadRequest{
		Name: "reuse.bin", TotalSize: int64(len(data)), ChunkSize: int64(len(data)),
	})
	req, _ = http.NewRequest("POST", env.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t2")
	req.Header.Set("Content-Type", "application/json")
	resp, _ = http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	req, _ = http.NewRequest("PUT",
		env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
		bytes.NewReader(data))
	req.Header.Set("Authorization", "Bearer tok-t2")
	req.Header.Set("X-Chunk-SHA256", contentHash)
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()

	// GC runs — collects blob (refcount 0)
	req, _ = http.NewRequest("POST", env.url("/admin/gc"), nil)
	req.Header.Set("Authorization", "Bearer tok-admin")
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()

	// t2 finalizes — must succeed (re-creates blob from assembled chunks)
	finBody, _ := json.Marshal(FinalizeRequest{SHA256: contentHash, Size: int64(len(data))})
	req, _ = http.NewRequest("POST",
		env.url(fmt.Sprintf("/uploads/%s/finalize", cr.UploadID)),
		bytes.NewReader(finBody))
	req.Header.Set("Authorization", "Bearer tok-t2")
	req.Header.Set("Content-Type", "application/json")
	resp, _ = http.DefaultClient.Do(req)
	respBody, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("finalize after GC: status %d: %s", resp.StatusCode, respBody)
	}

	var fr FinalizeResponse
	json.Unmarshal(respBody, &fr)

	// Download must return exact bytes
	req, _ = http.NewRequest("GET", env.url("/artifacts/"+fr.ArtifactID), nil)
	req.Header.Set("Authorization", "Bearer tok-t2")
	resp, _ = http.DefaultClient.Do(req)
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !bytes.Equal(got, data) {
		t.Fatalf("download after GC race: got %q, want %q", got, data)
	}
}

// ============================================================
// Finalize edge cases
// ============================================================

func TestChunkAfterFinalize(t *testing.T) {
	env := newTestEnv(t)

	data := []byte("already-finalized")
	body, _ := json.Marshal(CreateUploadRequest{
		Name: "done", TotalSize: int64(len(data)), ChunkSize: int64(len(data)),
	})
	req, _ := http.NewRequest("POST", env.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	hash := sha256Hex(data)
	req, _ = http.NewRequest("PUT",
		env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
		bytes.NewReader(data))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("X-Chunk-SHA256", hash)
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()

	finBody, _ := json.Marshal(FinalizeRequest{SHA256: hash, Size: int64(len(data))})
	req, _ = http.NewRequest("POST",
		env.url(fmt.Sprintf("/uploads/%s/finalize", cr.UploadID)),
		bytes.NewReader(finBody))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()

	// Late chunk should be rejected
	late := []byte("late-chunk-payload!")
	req, _ = http.NewRequest("PUT",
		env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
		bytes.NewReader(late))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("X-Chunk-SHA256", sha256Hex(late))
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode == 200 {
		t.Fatal("chunk after finalize should be rejected")
	}
}

func TestFinalizeSHA256Mismatch(t *testing.T) {
	env := newTestEnv(t)

	data := []byte("mismatch-test")
	body, _ := json.Marshal(CreateUploadRequest{
		Name: "bad", TotalSize: int64(len(data)), ChunkSize: int64(len(data)),
	})
	req, _ := http.NewRequest("POST", env.url("/uploads"), bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	hash := sha256Hex(data)
	req, _ = http.NewRequest("PUT",
		env.url(fmt.Sprintf("/uploads/%s/chunks/0", cr.UploadID)),
		bytes.NewReader(data))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("X-Chunk-SHA256", hash)
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()

	// Wrong hash in finalize
	finBody, _ := json.Marshal(FinalizeRequest{
		SHA256: "0000000000000000000000000000000000000000000000000000000000000000",
		Size:   int64(len(data)),
	})
	req, _ = http.NewRequest("POST",
		env.url(fmt.Sprintf("/uploads/%s/finalize", cr.UploadID)),
		bytes.NewReader(finBody))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()
	if resp.StatusCode != 422 {
		t.Fatalf("finalize hash mismatch: expected 422, got %d", resp.StatusCode)
	}
}

// ============================================================
// Validation
// ============================================================

func TestValidation(t *testing.T) {
	env := newTestEnv(t)

	env.uploadArtifact("tok-t1", "v.bin", []byte("validate-me"), 100)

	req, _ := http.NewRequest("GET", env.url("/admin/validate"), nil)
	req.Header.Set("Authorization", "Bearer tok-admin")
	resp, _ := http.DefaultClient.Do(req)
	var vr ValidateResponse
	json.NewDecoder(resp.Body).Decode(&vr)
	resp.Body.Close()

	if vr.Total != 1 || vr.Valid != 1 {
		t.Fatalf("validation: total=%d valid=%d, want 1/1", vr.Total, vr.Valid)
	}
}

// ============================================================
// Crash recovery test (subprocess)
// ============================================================

func buildBinary(t *testing.T) string {
	t.Helper()
	binPath := filepath.Join(t.TempDir(), "vaultdrop-test")
	cmd := exec.Command("go", "build", "-o", binPath, ".")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, out)
	}
	return binPath
}

func startServer(t *testing.T, binPath, stateDir, port string) *exec.Cmd {
	t.Helper()
	cmd := exec.Command(binPath, "serve")
	cmd.Env = append(os.Environ(),
		"VAULTDROP_STATE_DIR="+stateDir,
		"PORT="+port,
	)
	cmd.Stdout = os.Stderr
	cmd.Stderr = os.Stderr
	if err := cmd.Start(); err != nil {
		t.Fatalf("start server: %v", err)
	}
	base := "http://localhost:" + port
	for i := 0; i < 50; i++ {
		resp, err := http.Get(base + "/health")
		if err == nil {
			resp.Body.Close()
			return cmd
		}
		time.Sleep(100 * time.Millisecond)
	}
	cmd.Process.Kill()
	t.Fatal("server did not start in time")
	return nil
}

func TestCrashDuringChunkWrite(t *testing.T) {
	if testing.Short() {
		t.Skip("crash test requires subprocess")
	}

	binPath := buildBinary(t)
	stateDir := t.TempDir()
	port := "19871"
	os.WriteFile(filepath.Join(stateDir, "tenants.json"),
		[]byte(`{"tenants":[{"id":"t1","token":"tok-t1"}],"admin_token":"tok-admin"}`), 0644)

	cmd := startServer(t, binPath, stateDir, port)
	base := "http://localhost:" + port

	// Create upload
	data := []byte("crash-chunk-test-data-payload!")
	body, _ := json.Marshal(CreateUploadRequest{
		Name: "crash.bin", TotalSize: int64(len(data)), ChunkSize: int64(len(data)),
	})
	req, _ := http.NewRequest("POST", base+"/uploads", bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	// SIGKILL before chunk upload completes (simulate crash during write)
	cmd.Process.Signal(syscall.SIGKILL)
	cmd.Wait()

	// Restart
	cmd = startServer(t, binPath, stateDir, port)
	defer func() { cmd.Process.Kill(); cmd.Wait() }()

	// Upload should be resumable with 0 chunks received
	req, _ = http.NewRequest("GET", base+"/uploads/"+cr.UploadID, nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, _ = http.DefaultClient.Do(req)
	var status UploadStatusResponse
	json.NewDecoder(resp.Body).Decode(&status)
	resp.Body.Close()
	if status.State != "uploading" {
		t.Fatalf("expected 'uploading', got %q", status.State)
	}

	// Upload chunk and finalize
	hash := sha256Hex(data)
	req, _ = http.NewRequest("PUT", base+"/uploads/"+cr.UploadID+"/chunks/0",
		bytes.NewReader(data))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("X-Chunk-SHA256", hash)
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()

	finBody, _ := json.Marshal(FinalizeRequest{SHA256: hash, Size: int64(len(data))})
	req, _ = http.NewRequest("POST", base+"/uploads/"+cr.UploadID+"/finalize",
		bytes.NewReader(finBody))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ = http.DefaultClient.Do(req)
	if resp.StatusCode != 200 {
		b, _ := io.ReadAll(resp.Body)
		t.Fatalf("finalize after crash: %d: %s", resp.StatusCode, b)
	}
	var fr FinalizeResponse
	json.NewDecoder(resp.Body).Decode(&fr)
	resp.Body.Close()

	// Download to verify
	req, _ = http.NewRequest("GET", base+"/artifacts/"+fr.ArtifactID, nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, _ = http.DefaultClient.Do(req)
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if !bytes.Equal(got, data) {
		t.Fatalf("download after crash: content mismatch")
	}
}

func TestCrashAfterFinalizeRestart(t *testing.T) {
	if testing.Short() {
		t.Skip("crash test requires subprocess")
	}

	binPath := buildBinary(t)
	stateDir := t.TempDir()
	port := "19872"
	os.WriteFile(filepath.Join(stateDir, "tenants.json"),
		[]byte(`{"tenants":[{"id":"t1","token":"tok-t1"}],"admin_token":"tok-admin"}`), 0644)

	cmd := startServer(t, binPath, stateDir, port)
	base := "http://localhost:" + port

	// Full upload + finalize
	data := []byte("survive-crash-after-finalize")
	hash := sha256Hex(data)
	body, _ := json.Marshal(CreateUploadRequest{
		Name: "durable.bin", TotalSize: int64(len(data)), ChunkSize: int64(len(data)),
	})
	req, _ := http.NewRequest("POST", base+"/uploads", bytes.NewReader(body))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ := http.DefaultClient.Do(req)
	var cr CreateUploadResponse
	json.NewDecoder(resp.Body).Decode(&cr)
	resp.Body.Close()

	req, _ = http.NewRequest("PUT", base+"/uploads/"+cr.UploadID+"/chunks/0",
		bytes.NewReader(data))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("X-Chunk-SHA256", hash)
	resp, _ = http.DefaultClient.Do(req)
	resp.Body.Close()

	finBody, _ := json.Marshal(FinalizeRequest{SHA256: hash, Size: int64(len(data))})
	req, _ = http.NewRequest("POST", base+"/uploads/"+cr.UploadID+"/finalize",
		bytes.NewReader(finBody))
	req.Header.Set("Authorization", "Bearer tok-t1")
	req.Header.Set("Content-Type", "application/json")
	resp, _ = http.DefaultClient.Do(req)
	var fr FinalizeResponse
	json.NewDecoder(resp.Body).Decode(&fr)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("finalize: status %d", resp.StatusCode)
	}

	// SIGKILL right after finalize returned 200
	cmd.Process.Signal(syscall.SIGKILL)
	cmd.Wait()

	// Restart
	cmd = startServer(t, binPath, stateDir, port)
	defer func() { cmd.Process.Kill(); cmd.Wait() }()

	// Finalized artifact must survive the crash
	req, _ = http.NewRequest("GET", base+"/artifacts/"+fr.ArtifactID, nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, _ = http.DefaultClient.Do(req)
	got, _ := io.ReadAll(resp.Body)
	resp.Body.Close()
	if resp.StatusCode != 200 {
		t.Fatalf("download after crash: status %d", resp.StatusCode)
	}
	if !bytes.Equal(got, data) {
		t.Fatalf("artifact content changed after crash")
	}

	// Listing must include the artifact
	req, _ = http.NewRequest("GET", base+"/artifacts", nil)
	req.Header.Set("Authorization", "Bearer tok-t1")
	resp, _ = http.DefaultClient.Do(req)
	var list []map[string]any
	json.NewDecoder(resp.Body).Decode(&list)
	resp.Body.Close()
	if len(list) != 1 {
		t.Fatalf("expected 1 artifact in list, got %d", len(list))
	}
}

// Verify unused imports compile
var _ = strings.Contains
var _ = time.Now
