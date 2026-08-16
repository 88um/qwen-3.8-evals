# VaultDrop

Multi-tenant artifact storage service with content-addressed dedup, resumable
chunked uploads, and crash-safe semantics.

## Requirements

- Go 1.22+ (tested with Go 1.26)
- macOS or Linux

## Build

```bash
go build -o vaultdrop .
```

Or use the wrapper script directly (compiles on each run):

```bash
./vaultdrop serve
```

## Run

### 1. Prepare state directory

```bash
export VAULTDROP_STATE_DIR=/path/to/state
mkdir -p "$VAULTDROP_STATE_DIR"
```

### 2. Write tenants config

```bash
cat > "$VAULTDROP_STATE_DIR/tenants.json" << 'EOF'
{
  "tenants": [
    {"id": "t1", "token": "tok-t1"},
    {"id": "t2", "token": "tok-t2"}
  ],
  "admin_token": "tok-admin"
}
EOF
```

### 3. Initialize schema (optional — `serve` self-initializes)

```bash
./vaultdrop migrate
```

### 4. Start server

```bash
PORT=8080 ./vaultdrop serve
```

## Test

```bash
go test -v -count=1 ./...
```

Skip crash-recovery tests (which build/SIGKILL a subprocess):

```bash
go test -short -v -count=1 ./...
```

## Limits

| Limit | Value |
|---|---|
| Max chunk size | 10 MB |
| Max artifact size | 10 GB |
