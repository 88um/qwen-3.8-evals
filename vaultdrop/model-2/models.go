package main

type TenantsConfig struct {
	Tenants    []TenantEntry `json:"tenants"`
	AdminToken string        `json:"admin_token"`
}

type TenantEntry struct {
	ID    string `json:"id"`
	Token string `json:"token"`
}

type Upload struct {
	ID        string `json:"upload_id"`
	TenantID  string `json:"-"`
	Name      string `json:"name"`
	TotalSize int64  `json:"total_size"`
	ChunkSize int64  `json:"chunk_size"`
	NumChunks int    `json:"num_chunks"`
	State     string `json:"state"`
	ArtifactID *string `json:"artifact_id,omitempty"`
}

type Chunk struct {
	UploadID   string `json:"-"`
	ChunkIndex int    `json:"chunk_index"`
	SHA256     string `json:"sha256"`
	Size       int64  `json:"size"`
}

type Artifact struct {
	ID          string `json:"artifact_id"`
	TenantID    string `json:"-"`
	UploadID    string `json:"-"`
	Name        string `json:"name"`
	Size        int64  `json:"size"`
	SHA256      string `json:"sha256"`
	ContentHash string `json:"-"`
}

type Blob struct {
	ContentHash string `json:"content_hash"`
	RefCount    int    `json:"refcount"`
	Size        int64  `json:"size"`
	Validated   bool   `json:"validated"`
}

type CreateUploadRequest struct {
	Name      string `json:"name"`
	TotalSize int64  `json:"total_size"`
	ChunkSize int64  `json:"chunk_size"`
}

type CreateUploadResponse struct {
	UploadID string `json:"upload_id"`
	Received []int  `json:"received"`
}

type UploadStatusResponse struct {
	UploadID string `json:"upload_id"`
	Received []int  `json:"received"`
	State    string `json:"state"`
}

type FinalizeRequest struct {
	SHA256 string `json:"sha256"`
	Size   int64  `json:"size"`
}

type FinalizeResponse struct {
	ArtifactID string `json:"artifact_id"`
}

type GCResponse struct {
	Scanned    int   `json:"scanned"`
	Collected  int   `json:"collected"`
	BytesFreed int64 `json:"bytes_freed"`
}

type ValidateResponse struct {
	Total     int `json:"total"`
	Valid     int `json:"valid"`
	Invalid   int `json:"invalid"`
	Missing   int `json:"missing"`
}

type ErrorResponse struct {
	Error string `json:"error"`
}
