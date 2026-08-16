# VaultDrop — model-1

Multi-tenant, content-addressed artifact storage: resumable chunked uploads,
cross-tenant-invisible dedup, streaming downloads, reference-counted garbage
collection, crash recovery. Python 3 stdlib only (no dependencies).

## Layout

```
vaultdrop        executable (serve | migrate)
src/vaultdrop/   implementation
  core.py        limits, path layout, streaming/durability primitives
  db.py          schema, per-thread SQLite connections, crash-recovery sweep
  store.py       uploads, chunk writes, finalize/claim, GC, validation
  httpd.py       HTTP layer, bearer auth, routing, streaming
  main.py        CLI entry
tests/           unittest suite (API, concurrency, crash, recovery, scale probe)
DECISIONS.md     architecture + claims register (read this first)
```

## Build / run

No build step. Requires Python ≥ 3.9 on macOS/Linux.

```sh
export VAULTDROP_STATE_DIR=/path/to/state   # created/initialized for you
printf '{"tenants":[{"id":"t1","token":"tok-t1"}],"admin_token":"tok-admin"}' \
       > "$VAULTDROP_STATE_DIR/tenants.json"

./vaultdrop migrate          # initialize/upgrade schema (idempotent)
PORT=8080 ./vaultdrop serve  # foreground HTTP API on $PORT
```

The schema is embedded in `src/vaultdrop/db.py` (applied idempotently at
startup); `serve` self-initializes it and runs crash recovery before
accepting traffic. `migrate` is a standalone no-op-safe initializer.

## Tests

```sh
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

- `test_api.py` — lifecycle, replay/conflict, wire limits, tenant isolation
- `test_concurrency.py` — chunk races, concurrent finalize races, finalize-vs-GC
- `test_crash.py` — SIGKILL mid chunk-write / finalize / GC, restart, retry
- `test_recovery.py` — deterministic crash-remnant sweeps
- `tests/scale_probe.py` — manual scale check (10k blobs, overlap, RSS)

Each test spawns a real `serve` subprocess over a private state dir and talks
to it over HTTP; crash tests send real `kill -9`.
