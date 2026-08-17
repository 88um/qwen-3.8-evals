# VaultDrop

VaultDrop is a standard-library Python service for resumable, multi-tenant,
content-addressed artifact storage.

## Requirements and build

- Python 3.10 or newer
- macOS or Linux with a local filesystem that provides POSIX atomic rename and
  open-file-descriptor unlink semantics
- No third-party packages and no build step

The executable is already marked executable. To initialize a state directory:

```sh
export VAULTDROP_STATE_DIR=/path/to/state
./vaultdrop migrate
```

Before serving, create `$VAULTDROP_STATE_DIR/tenants.json` in the contract format,
then run:

```sh
export PORT=8080
./vaultdrop serve
```

`serve` runs in the foreground and also applies schema initialization and crash
recovery. All metadata, chunks, blobs, temporary files, and GC trash remain under
`$VAULTDROP_STATE_DIR`.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The suite uses only loopback HTTP and temporary directories. It covers concurrent
out-of-order and duplicate chunk PUTs, conflicting replay, concurrent finalize,
late chunks, tenant isolation, cross-tenant dedup, refcounted GC, corruption
rejection before response streaming, checksum retry, and real SIGKILL/restart
recovery during a partial chunk write and after a durable finalize.

## Limits

- Maximum artifact size: exactly 10 GiB
- Maximum declared and accepted chunk size: exactly 32 MiB
- JSON request limit: 64 KiB
- Artifact names: 1–1024 characters

Chunk PUTs require `Content-Length`; chunked transfer encoding is rejected by the
absence of that header. I/O uses 1 MiB buffers. A download performs a complete
streaming integrity pass before writing the `200` response, then streams from the
same open descriptor. This doubles sequential read I/O intentionally so corruption
can fail as JSON without first leaking a partial `200` body.
