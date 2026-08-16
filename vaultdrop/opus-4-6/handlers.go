package main

import (
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"strconv"
	"strings"
)

const (
	MaxChunkSize    = 10 * 1024 * 1024       // 10 MB
	MaxArtifactSize = 10 * 1024 * 1024 * 1024 // 10 GB
)

type Handler struct {
	store      *Store
	blobMgr    *BlobManager
	tenants    map[string]string // token → tenantID
	adminToken string
}

func NewHandler(store *Store, blobMgr *BlobManager, tenants map[string]string, adminToken string) *Handler {
	return &Handler{
		store:      store,
		blobMgr:    blobMgr,
		tenants:    tenants,
		adminToken: adminToken,
	}
}

func (h *Handler) authTenant(r *http.Request) string {
	tok := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	if tok == "" {
		return ""
	}
	return h.tenants[tok]
}

func (h *Handler) isAdmin(r *http.Request) bool {
	tok := strings.TrimPrefix(r.Header.Get("Authorization"), "Bearer ")
	return tok != "" && tok == h.adminToken
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func writeError(w http.ResponseWriter, status int, msg string) {
	writeJSON(w, status, ErrorResponse{Error: msg})
}

// --- POST /uploads ---
func (h *Handler) HandleCreateUpload(w http.ResponseWriter, r *http.Request) {
	tenantID := h.authTenant(r)
	if tenantID == "" {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	var req CreateUploadRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}

	if req.Name == "" {
		writeError(w, http.StatusBadRequest, "name is required")
		return
	}
	if req.TotalSize < 0 || req.TotalSize > MaxArtifactSize {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("total_size must be 0..%d", MaxArtifactSize))
		return
	}
	if req.ChunkSize <= 0 || req.ChunkSize > MaxChunkSize {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("chunk_size must be 1..%d", MaxChunkSize))
		return
	}

	numChunks := int(math.Ceil(float64(req.TotalSize) / float64(req.ChunkSize)))
	if req.TotalSize == 0 {
		numChunks = 0
	}

	upload, err := h.store.CreateUpload(r.Context(), tenantID, req.Name, req.TotalSize, req.ChunkSize, numChunks)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to create upload")
		return
	}

	if err := h.blobMgr.EnsureChunkDir(upload.ID); err != nil {
		writeError(w, http.StatusInternalServerError, "failed to create chunk directory")
		return
	}

	writeJSON(w, http.StatusOK, CreateUploadResponse{
		UploadID: upload.ID,
		Received: []int{},
	})
}

// --- PUT /uploads/{id}/chunks/{index} ---
func (h *Handler) HandleChunkUpload(w http.ResponseWriter, r *http.Request) {
	tenantID := h.authTenant(r)
	if tenantID == "" {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	uploadID, indexStr := parseChunkPath(r.URL.Path)
	if uploadID == "" || indexStr == "" {
		writeError(w, http.StatusBadRequest, "invalid path")
		return
	}
	index, err := strconv.Atoi(indexStr)
	if err != nil || index < 0 {
		writeError(w, http.StatusBadRequest, "invalid chunk index")
		return
	}

	claimedHash := r.Header.Get("X-Chunk-SHA256")
	if claimedHash == "" {
		writeError(w, http.StatusBadRequest, "X-Chunk-SHA256 header required")
		return
	}
	claimedHash = strings.ToLower(claimedHash)

	upload, err := h.store.GetUpload(r.Context(), uploadID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	if upload == nil || upload.TenantID != tenantID {
		writeError(w, http.StatusNotFound, "upload not found")
		return
	}
	if upload.State != "uploading" {
		writeError(w, http.StatusConflict, "upload is not in uploading state")
		return
	}
	if index >= upload.NumChunks {
		writeError(w, http.StatusBadRequest, fmt.Sprintf("chunk index must be 0..%d", upload.NumChunks-1))
		return
	}

	// Check if chunk already recorded in DB
	existing, err := h.store.GetChunk(r.Context(), uploadID, index)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}

	if existing != nil {
		// Chunk already recorded. Drain the body to get its hash for conflict detection.
		actualHash, _, err := h.blobMgr.WriteChunk(uploadID, index, r.Body)
		if err != nil {
			// If write fails, try comparing just the claimed hash
			if existing.SHA256 == claimedHash {
				w.WriteHeader(http.StatusOK)
				return
			}
			writeError(w, http.StatusConflict, "conflicting chunk content")
			return
		}
		if existing.SHA256 == actualHash {
			w.WriteHeader(http.StatusOK)
			return
		}
		writeError(w, http.StatusConflict, "conflicting chunk content")
		return
	}

	// New chunk: write to disk first, then record in DB.
	actualHash, size, err := h.blobMgr.WriteChunk(uploadID, index, r.Body)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "failed to write chunk")
		return
	}

	if actualHash != claimedHash {
		// Hash mismatch — remove the written file
		os.Remove(h.blobMgr.chunkPath(uploadID, index))
		writeError(w, http.StatusBadRequest, "SHA-256 mismatch")
		return
	}

	inserted, err := h.store.InsertChunk(r.Context(), uploadID, index, actualHash, size)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}

	if !inserted {
		// Concurrent insert won — check compatibility
		existing, err = h.store.GetChunk(r.Context(), uploadID, index)
		if err != nil {
			writeError(w, http.StatusInternalServerError, "database error")
			return
		}
		if existing != nil && existing.SHA256 != actualHash {
			writeError(w, http.StatusConflict, "conflicting chunk content")
			return
		}
	}

	w.WriteHeader(http.StatusOK)
}

// --- GET /uploads/{id} ---
func (h *Handler) HandleGetUpload(w http.ResponseWriter, r *http.Request) {
	tenantID := h.authTenant(r)
	if tenantID == "" {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	uploadID := lastPathSegment(r.URL.Path)
	upload, err := h.store.GetUpload(r.Context(), uploadID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	if upload == nil || upload.TenantID != tenantID {
		writeError(w, http.StatusNotFound, "not found")
		return
	}

	indices, err := h.store.GetChunkIndices(r.Context(), uploadID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	if indices == nil {
		indices = []int{}
	}

	writeJSON(w, http.StatusOK, UploadStatusResponse{
		UploadID: upload.ID,
		Received: indices,
		State:    upload.State,
	})
}

// --- POST /uploads/{id}/finalize ---
func (h *Handler) HandleFinalize(w http.ResponseWriter, r *http.Request) {
	tenantID := h.authTenant(r)
	if tenantID == "" {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) < 3 {
		writeError(w, http.StatusBadRequest, "invalid path")
		return
	}
	uploadID := parts[1]

	var req FinalizeRequest
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeError(w, http.StatusBadRequest, "invalid request body")
		return
	}
	req.SHA256 = strings.ToLower(req.SHA256)

	if req.SHA256 == "" {
		writeError(w, http.StatusBadRequest, "sha256 is required")
		return
	}

	// Quick pre-check (non-transactional)
	upload, err := h.store.GetUpload(r.Context(), uploadID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	if upload == nil || upload.TenantID != tenantID {
		writeError(w, http.StatusNotFound, "not found")
		return
	}

	// If already finalized, return existing artifact
	if upload.State == "finalized" && upload.ArtifactID != nil {
		writeJSON(w, http.StatusOK, FinalizeResponse{ArtifactID: *upload.ArtifactID})
		return
	}
	if upload.State != "uploading" {
		writeError(w, http.StatusConflict, "upload not in uploading state")
		return
	}

	// Verify all chunks present
	indices, err := h.store.GetChunkIndices(r.Context(), uploadID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	if len(indices) != upload.NumChunks {
		writeError(w, http.StatusUnprocessableEntity, fmt.Sprintf("expected %d chunks, got %d", upload.NumChunks, len(indices)))
		return
	}
	// Verify chunk files exist
	for _, idx := range indices {
		if !h.blobMgr.ChunkExists(uploadID, idx) {
			writeError(w, http.StatusUnprocessableEntity, fmt.Sprintf("chunk %d file missing", idx))
			return
		}
	}

	// Assemble blob from chunks
	tmpPath, contentHash, totalSize, err := h.blobMgr.AssembleBlob(uploadID, upload.NumChunks)
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("assembly failed: %v", err))
		return
	}

	// Verify SHA-256 and size against client's claim
	if contentHash != req.SHA256 || totalSize != req.Size {
		os.Remove(tmpPath)
		writeError(w, http.StatusUnprocessableEntity, "sha256 or size mismatch")
		return
	}

	// Acquire per-hash lock for finalize-vs-GC coordination
	h.store.LockBlob(contentHash)
	defer h.store.UnlockBlob(contentHash)

	// Install blob file (atomic rename, idempotent if already exists)
	if err := h.blobMgr.InstallBlob(tmpPath, contentHash); err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("install blob failed: %v", err))
		return
	}

	// Atomically finalize in DB: set state, create blob record, create artifact
	result, err := h.store.Finalize(r.Context(), uploadID, tenantID, contentHash, upload.Name, totalSize, contentHash)
	if err != nil {
		if strings.Contains(err.Error(), "not found") {
			writeError(w, http.StatusNotFound, "not found")
			return
		}
		writeError(w, http.StatusConflict, err.Error())
		return
	}

	// Clean up chunk files (best-effort)
	go h.blobMgr.RemoveChunks(uploadID)

	writeJSON(w, http.StatusOK, FinalizeResponse{ArtifactID: result.ArtifactID})
}

// --- GET /artifacts/{id} ---
func (h *Handler) HandleGetArtifact(w http.ResponseWriter, r *http.Request) {
	tenantID := h.authTenant(r)
	if tenantID == "" {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	artifactID := lastPathSegment(r.URL.Path)
	artifact, err := h.store.GetArtifact(r.Context(), artifactID, tenantID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	if artifact == nil {
		writeError(w, http.StatusNotFound, "not found")
		return
	}

	f, err := h.blobMgr.OpenBlob(artifact.ContentHash)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "blob not found on disk")
		return
	}
	defer f.Close()

	w.Header().Set("Content-Type", "application/octet-stream")
	w.Header().Set("Content-Length", strconv.FormatInt(artifact.Size, 10))
	w.WriteHeader(http.StatusOK)
	io.Copy(w, f)
}

// --- GET /artifacts ---
func (h *Handler) HandleListArtifacts(w http.ResponseWriter, r *http.Request) {
	tenantID := h.authTenant(r)
	if tenantID == "" {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	artifacts, err := h.store.ListArtifacts(r.Context(), tenantID)
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}

	type listItem struct {
		ArtifactID string `json:"artifact_id"`
		Name       string `json:"name"`
		Size       int64  `json:"size"`
		SHA256     string `json:"sha256"`
	}
	items := make([]listItem, len(artifacts))
	for i, a := range artifacts {
		items[i] = listItem{
			ArtifactID: a.ID,
			Name:       a.Name,
			Size:       a.Size,
			SHA256:     a.SHA256,
		}
	}
	writeJSON(w, http.StatusOK, items)
}

// --- DELETE /artifacts/{id} ---
func (h *Handler) HandleDeleteArtifact(w http.ResponseWriter, r *http.Request) {
	tenantID := h.authTenant(r)
	if tenantID == "" {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	artifactID := lastPathSegment(r.URL.Path)
	_, err := h.store.DeleteArtifact(r.Context(), artifactID, tenantID)
	if err != nil {
		if strings.Contains(err.Error(), "not found") {
			writeError(w, http.StatusNotFound, "not found")
			return
		}
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}

	writeJSON(w, http.StatusOK, map[string]string{"status": "deleted"})
}

// --- POST /admin/gc ---
func (h *Handler) HandleAdminGC(w http.ResponseWriter, r *http.Request) {
	if !h.isAdmin(r) {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	resp, err := RunGC(r.Context(), h.store, h.blobMgr)
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("gc failed: %v", err))
		return
	}

	writeJSON(w, http.StatusOK, resp)
}

// --- GET /admin/blobs ---
func (h *Handler) HandleAdminBlobs(w http.ResponseWriter, r *http.Request) {
	if !h.isAdmin(r) {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	blobs, err := h.store.ListBlobs(r.Context())
	if err != nil {
		writeError(w, http.StatusInternalServerError, "database error")
		return
	}
	if blobs == nil {
		blobs = []Blob{}
	}
	writeJSON(w, http.StatusOK, blobs)
}

// --- GET /admin/validate ---
func (h *Handler) HandleAdminValidate(w http.ResponseWriter, r *http.Request) {
	if !h.isAdmin(r) {
		writeError(w, http.StatusUnauthorized, "unauthorized")
		return
	}

	resp, err := RunValidation(r.Context(), h.store, h.blobMgr)
	if err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Sprintf("validation failed: %v", err))
		return
	}

	writeJSON(w, http.StatusOK, resp)
}

// --- GET /health ---
func (h *Handler) HandleHealth(w http.ResponseWriter, r *http.Request) {
	w.WriteHeader(http.StatusOK)
}

// --- path helpers ---

func lastPathSegment(path string) string {
	parts := strings.Split(strings.Trim(path, "/"), "/")
	if len(parts) == 0 {
		return ""
	}
	return parts[len(parts)-1]
}

// parseChunkPath extracts uploadID and chunk index from /uploads/{id}/chunks/{index}
func parseChunkPath(path string) (string, string) {
	parts := strings.Split(strings.Trim(path, "/"), "/")
	// uploads/{id}/chunks/{index}
	if len(parts) == 4 && parts[0] == "uploads" && parts[2] == "chunks" {
		return parts[1], parts[3]
	}
	return "", ""
}

