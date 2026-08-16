# mini-ledger (model-1) — concurrency mechanism

Implementation: Python 3 stdlib only (`http.server` + `sqlite3`). One SQLite
database under `$LEDGER_STATE_DIR`, WAL mode, `synchronous=FULL`.

## What serializes two racing holds

Every state-changing request runs inside a single SQLite write transaction
opened with `BEGIN IMMEDIATE`. SQLite admits exactly one writer at a time: the
exclusive write lock is acquired at transaction start and held until COMMIT.
The balance check and the hold insert are statements of that one transaction,
so they execute atomically under the lock. A racing hold either waits its
turn and then sees the first hold's committed state (and gets
`409 insufficient`) or proceeds against the pre-race state — never against a
phantom balance. There is no application-level queueing; the storage engine's
lock is the serializer. Primary keys on `hold_state.ref` and `topup_keys.key`
backstop the checks: even if a check ever raced, the second insert would
violate the constraint and roll back.

## Why a double-charge is structurally impossible

`/charge` and `/release` read the hold's status, flip it, and append the
terminal entry inside one serialized transaction. Only one writer executes at
a time, so of two racing terminals exactly one observes `status = 'open'` and
commits the flip; the other observes the committed terminal state and returns
409 (or 200, if it is a replay of the same terminal kind). The ledger can
therefore contain at most one terminal per ref, and it always follows its
hold, because holds and terminals are appended inside transactions ordered by
the same lock.

## Why a kill between "decide" and "append" cannot corrupt the ledger

There is no gap: the decision and the append are statements of one
transaction. SQLite either commits both or rolls back both; a SIGKILL before
COMMIT persists nothing. Commits are durable before the response is sent —
`synchronous=FULL` fsyncs the WAL — so every entry the server ever
acknowledged survives the kill, and WAL recovery on restart replays any
committed-but-uncheckpointed transactions. Entries are append-only: nothing
ever updates or deletes a ledger row, `seq` comes from AUTOINCREMENT, and the
server restarts cleanly from the recovered database.

## Idempotency

- `/account`: primary key on `accounts.account_id`; a replay finds the row and
  appends nothing.
- `/topup`: primary key on `topup_keys.key`; a replay finds the key and
  appends nothing.
- `/hold`: exact parameter match on an existing ref appends nothing; a
  parameter mismatch is `409 duplicate_ref`.
- `/charge` / `/release`: status check inside the serialized transaction;
  replay-after-terminal returns 200 and appends nothing.

## Read path

`GET /account` and `GET /ledger` are plain read transactions. Under WAL they
never block the writer and always see a consistent snapshot, so snapshots
taken mid-storm are exact prefixes of later ones.
