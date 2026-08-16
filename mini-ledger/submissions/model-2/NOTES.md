# Concurrency and Durability Notes

## What serializes two racing holds

All state-mutating operations run inside `BEGIN IMMEDIATE` transactions on
SQLite in WAL mode. `BEGIN IMMEDIATE` acquires a reserved lock at transaction
start, so two concurrent holds serialize at the SQLite locking layer: the
second thread blocks on `BEGIN IMMEDIATE` until the first commits. The balance
check (`available >= amount`) and the hold insert happen atomically within one
transaction, so two racing holds on the same account can never both pass.

## What makes a double-charge structurally impossible

Each hold has a `status` column (`open` / `charged` / `released`). A charge
reads the hold's status inside a `BEGIN IMMEDIATE` transaction:

- `open` → update to `charged`, insert ledger entry, return 200
- `charged` → return 200 (idempotent replay), no ledger entry
- `released` → return 409

Because the read-and-update is atomic within one serialized transaction, two
concurrent charges on the same ref result in exactly one winner (the first to
commit sets `charged`); the second sees `charged` and returns the replay 200
without appending. Same logic prevents charge-vs-release races: one wins, the
loser gets 409.

## Why a kill between "decide" and "append" can't corrupt the ledger

SQLite WAL with `synchronous=FULL` guarantees that `COMMIT` is durable once
the call returns (WAL frames are fsynced before the commit function returns).
All ledger appends and balance updates happen within a single transaction, so
they are atomic: either the entire transaction (balance update + ledger entry)
is committed and fsynced, or none of it is. A SIGKILL during commit means
the WAL frame was either fully written and fsynced (committed) or not
(rolled back on recovery). There is no state where the balance changed but
the ledger entry is missing, or vice versa.

On restart, SQLite's WAL recovery replays any committed-but-not-checkpointed
frames, restoring the database to its last committed state.
