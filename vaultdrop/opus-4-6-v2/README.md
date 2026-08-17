# VaultDrop

Multi-tenant artifact storage service with resumable chunked uploads, content-addressed deduplication, and crash-safe garbage collection.

## Requirements

- Python 3.9+ (standard library only, no external dependencies)
- macOS or Linux

## Quick Start

```bash
# Initialize the database
export VAULTDROP_STATE_DIR=/path/to/state
./vaultdrop migrate

# Write tenant configuration
cat > "$VAULTDROP_STATE_DIR/tenants.json" <<'EOF'
{
  "tenants": [{"id": "t1", "token": "tok-t1"}, {"id": "t2", "token": "tok-t2"}],
  "admin_token": "tok-admin"
}
EOF

# Start the server
export PORT=8080
./vaultdrop serve
```

## Commands

- `./vaultdrop serve` — Run the HTTP API in the foreground on `$PORT` (default 8080). State stored under `$VAULTDROP_STATE_DIR`.
- `./vaultdrop migrate` — Initialize or upgrade the database schema under `$VAULTDROP_STATE_DIR`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `PORT` | `8080` | HTTP listen port |
| `VAULTDROP_STATE_DIR` | `./state` | Directory for database and blob storage |

## Running Tests

```bash
python3 -m pytest tests/ -v
```

## API

See `product.md` for the full endpoint specification. Limits:

- Max artifact size: 10 GiB
- Max chunk body: 64 MiB
- Streaming I/O with bounded memory (64 KB buffer)
