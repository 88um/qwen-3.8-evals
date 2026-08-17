# VaultDrop (qwen-v3)

Multi-tenant, content-addressed artifact storage: resumable chunked upload,
cross-tenant byte dedup, crash-safe finalization, on-demand garbage
collection, background validation.

## Runtime dependencies

Python 3.10+ standard library only (`http.server`, `sqlite3`, `hashlib`,
`os`, `threading`). SQLite ships with Python. Nothing to build or vendor.

## Commands

```sh
# Initialize/upgrade schema under the state dir (idempotent, exits 0).
VAULTDROP_STATE_DIR=/path/to/state ./vaultdrop migrate

# Run the HTTP API in the foreground on $PORT.
PORT=8080 VAULTDROP_STATE_DIR=/path/to/state ./vaultdrop serve
```

Before `serve` starts, the harness seeds `$VAULTDROP_STATE_DIR/tenants.json`
(the token→tenant mapping and admin token). The service reads it at startup
as the source of truth for authentication. All persistent state (SQLite
metadata + stored bytes) lives under `$VAULTDROP_STATE_DIR`.

## Tests

```sh
python3 -m unittest tests.test_vaultdrop -v
```

18 integration tests: round-trip, concurrency (overlapping chunks, racing
finalizes, finalize-vs-GC), crash recovery (SIGKILL during chunk write,
finalize, GC), cross-tenant isolation/dedup invisibility, GC correctness,
and the scale envelope (multi-GiB streaming under the memory ceiling,
10k-blob metadata responsiveness).

## Layout

```
vaultdrop        launcher (python3 src/main.py)
src/             implementation (config, db, blobs, uploads, httpd, main)
migrations/      schema (0001_initial.sql)
tests/           integration/crash/concurrency/scale tests
DECISIONS.md     architecture + claims register
```
