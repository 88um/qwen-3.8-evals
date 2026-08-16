package main

import (
	"net/http"
	"strings"
)

func NewRouter(h *Handler) http.Handler {
	mux := http.NewServeMux()

	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		h.HandleHealth(w, r)
	})

	mux.HandleFunc("/uploads", func(w http.ResponseWriter, r *http.Request) {
		path := strings.Trim(r.URL.Path, "/")

		if path == "uploads" {
			if r.Method != http.MethodPost {
				writeError(w, http.StatusMethodNotAllowed, "method not allowed")
				return
			}
			h.HandleCreateUpload(w, r)
			return
		}

		writeError(w, http.StatusNotFound, "not found")
	})

	mux.HandleFunc("/uploads/", func(w http.ResponseWriter, r *http.Request) {
		path := strings.Trim(r.URL.Path, "/")
		parts := strings.Split(path, "/")

		// GET /uploads/{id}
		if len(parts) == 2 && r.Method == http.MethodGet {
			h.HandleGetUpload(w, r)
			return
		}

		// PUT /uploads/{id}/chunks/{index}
		if len(parts) == 4 && parts[2] == "chunks" && r.Method == http.MethodPut {
			h.HandleChunkUpload(w, r)
			return
		}

		// POST /uploads/{id}/finalize
		if len(parts) == 3 && parts[2] == "finalize" && r.Method == http.MethodPost {
			h.HandleFinalize(w, r)
			return
		}

		writeError(w, http.StatusNotFound, "not found")
	})

	mux.HandleFunc("/artifacts", func(w http.ResponseWriter, r *http.Request) {
		path := strings.Trim(r.URL.Path, "/")

		if path == "artifacts" {
			if r.Method != http.MethodGet {
				writeError(w, http.StatusMethodNotAllowed, "method not allowed")
				return
			}
			h.HandleListArtifacts(w, r)
			return
		}

		writeError(w, http.StatusNotFound, "not found")
	})

	mux.HandleFunc("/artifacts/", func(w http.ResponseWriter, r *http.Request) {
		switch r.Method {
		case http.MethodGet:
			h.HandleGetArtifact(w, r)
		case http.MethodDelete:
			h.HandleDeleteArtifact(w, r)
		default:
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
		}
	})

	mux.HandleFunc("/admin/gc", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		h.HandleAdminGC(w, r)
	})

	mux.HandleFunc("/admin/blobs", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		h.HandleAdminBlobs(w, r)
	})

	mux.HandleFunc("/admin/validate", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			writeError(w, http.StatusMethodNotAllowed, "method not allowed")
			return
		}
		h.HandleAdminValidate(w, r)
	})

	return mux
}
