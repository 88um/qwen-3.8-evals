# VaultDrop

A multi-tenant, content-addressed artifact storage service. Clients upload large
files in resumable chunks; the service stores each distinct byte-content once
(SHA-256 content addressing, deduplicated across the whole system) and serves back
the exact bytes to their owner. An admin surface triggers synchronous
garbage-collection and validation.

Standard-library Python only — nothing to vendor. See `DECISIONS.md` for the
architecture and the claims register (the invariants, their mechanisms, and how
each is checked).

## Runtime dependencies

- Python 3.10+ (developed on 3.14). Standard library only: `sqlite3`, `hashlib`,
  `threading`, `http.server`, `urllib`.
- `pytest` to run the test suite.

## Layout

```
vaultdrop/            executable launcher (puts src/ on PYTHONPATH, runs the CLI)
src/vaultdrop/        implementation
migrations/*.sql      schema (applied idempotently)
tests/                test suite (drives a real `serve` subprocess over HTTP)
DECISIONS.md          architecture + claims register
```

## Run

All persistent state lives under `$VAULTDROP_STATE_DIR` (created on demand). The
harness/operator seeds `$VAULTDROP_STATE_DIR/tenants.json` before `serve` starts:

```json
{"tenants": [{"id": "t1", "token": "tok-t1"}, {"id": "t2", "token": "tok-t2"}],
 "admin_token": "tok-admin"}
```

```sh
# initialize/upgrade the schema (no-op if serve self-initializes)
VAULTDROP_STATE_DIR=/var/lib/vaultdrop ./vaultdrop migrate

# serve the HTTP API in the foreground on $PORT
VAULTDROP_STATE_DIR=/var/lib/vaultdrop PORT=8080 ./vaultdrop serve
```

`serve` self-initializes the schema (idempotent) and reclaims orphaned files from
any prior crashed run before accepting traffic.

## Endpoints (summary)

| Method + path | Auth | Purpose |
|---|---|---|
| `POST /uploads` | tenant | start a resumable upload |
| `PUT /uploads/{id}/chunks/{index}` | tenant | store a chunk (`X-Chunk-SHA256`); idempotent replay `200`, conflict `409` |
| `GET /uploads/{id}` | tenant (owner) | upload state + received indices |
| `POST /uploads/{id}/finalize` | tenant (owner) | assemble + verify; create the artifact |
| `GET /artifacts/{id}` | tenant (owner) | stream the exact bytes (read-boundary verified) |
| `GET /artifacts` | tenant | list the caller's artifacts |
| `DELETE /artifacts/{id}` | tenant (owner) | delete metadata; bytes collectable when unreferenced |
| `POST /admin/gc` | admin | synchronous garbage-collection pass |
| `GET /admin/blobs` | admin | inspect the physical byte-store |
| `GET /admin/validate` | admin | re-hash stored bytes, report mismatches |
| `GET /health` | none | liveness |

Cross-tenant or unknown ids return `404` — existence itself is private.

## Tests

The suite drives a real `serve` subprocess against a throwaway state dir, over
HTTP, and — for the crash tests — SIGKILLs the process mid-operation and restarts
it against the same state dir.

```sh
cd tests
python3 -m pytest            # whole suite
python3 -m pytest test_crash.py -v   # crash-recovery probes
```

- `test_functional.py` — upload/finalize/download/GC/validate lifecycle.
- `test_isolation.py` — cross-tenant probing, dedup invisibility, admin authz.
- `test_concurrency.py` — concurrent chunks, conflicting replay, concurrent
  finalize, finalize-vs-late-chunk, finalize-vs-GC.
- `test_crash.py` — SIGKILL during chunk write / finalize / GC, then restart and
  assert the recovery invariants.

Fresh (uncached) payloads keep crash-test operations slow enough that an early
signal reliably lands mid-operation; each asserted invariant holds for every crash
landing point, so the tests are valid regardless of exact scheduling.
