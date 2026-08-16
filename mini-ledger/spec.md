# mini-ledger — Product Spec

Build a credit ledger service for a product where customers pre-pay credits
and every paid action places a hold that is later charged or released. Money
correctness is the entire product: the service is graded by a harness that
fires deliberately racing, duplicated, and replayed requests from many
concurrent clients, SIGKILLs the server mid-storm, then proves every invariant
from your own ledger dump. "The code checks first" is not a mechanism —
correctness must survive true concurrency.

## Shared workspace rules — read first

Several independent submissions are built from this same spec, possibly
concurrently on this machine. You have been assigned a **submission ID** (e.g.
`model-a`) in your kickoff instructions.

- Create **every file you write** inside `submissions/<your-id>/`. Nothing in
  the shared root.
- Never read, modify, or write another submission's directory; never modify
  the shared `spec.md`.
- The grading harness (`grader.py`, anything under `results/`) may be present
  elsewhere on this machine. It is off-limits: do not read, run, or search
  for it. Consulting it is a disqualifying protocol violation.
- When testing, pick your own port (not the default of any other tool) and
  keep all state inside your submission directory.

## Interface contract

Submission root must contain an executable `ledger` (any language on a stock
macOS dev machine; stdlib preferred, vendor anything else). `./ledger serve`
runs an HTTP server **in the foreground** on port `$PORT`, all persistent
state under `$LEDGER_STATE_DIR` (both env vars set by the harness). SQLite is
recommended. All bodies are JSON; all amounts are positive integers.

| Endpoint | Behavior |
|---|---|
| `POST /account` `{"account_id", "initial"}` | Create account. Idempotent by `account_id` (replay → 200, no second entry). |
| `POST /topup` `{"account_id", "amount", "key"}` | Add credits. Idempotent by `key`: an exact replay returns 200 and appends nothing. 404 unknown account. |
| `POST /hold` `{"account_id", "ref", "amount"}` | Place a hold. 200 `{"held": true}`; 409 `{"error": "insufficient"}` if `available < amount`; 409 `{"error": "duplicate_ref"}` if `ref` exists with different parameters. An exact replay of an existing hold → 200, appends nothing. |
| `POST /charge` `{"ref"}` | Convert the hold to a charge, exactly once. 200 first time and on replay-after-charge; 409 if the ref was released; 404 unknown ref. |
| `POST /release` `{"ref"}` | Mirror of charge: exactly once; 200 on release and replay-after-release; 409 if charged; 404 unknown. |
| `GET /account?account_id=X` | `{"balance", "available"}`. `balance = initial + topups − charges`; `available = balance − open holds`. |
| `GET /ledger` | `{"entries": [...]}` — the full append-only ledger (see below). |

### The ledger dump is the proof

`GET /ledger` returns every entry ever written, in order:

```json
{"seq": 7, "kind": "hold", "account_id": "a3", "ref": "job-19",
 "amount": 2, "key": null}
```

`seq` strictly increasing; `kind` one of `account_open`, `topup`, `hold`,
`charge`, `release`; `key` set on topups, `ref` set on hold/charge/release
(a charge/release entry carries its hold's `account_id` and `amount`).

## The invariants (this is what is graded)

The harness snapshots `/ledger` throughout, replays your final dump entry by
entry, and asserts:

1. **Never negative.** Replaying the ledger prefix by prefix, no account's
   `available` ever dips below zero — at any point in history, not just at
   the end. Two racing holds that both pass a balance check is the classic
   failure here.
2. **At most one hold and at most one terminal.** Per `ref`: ≤1 `hold` entry,
   ≤1 `charge`-or-`release` entry, and the terminal only after its hold.
   Racing `charge` vs `release` on the same ref: exactly one wins with 200,
   the loser gets 409, the ledger shows exactly one terminal.
3. **Replays append nothing.** Verbatim re-POSTs (same `key`, same hold
   params, repeated charge) return success but add zero entries.
4. **Append-only.** Every earlier snapshot is an exact prefix of every later
   one. No entry is ever mutated or removed — including across a SIGKILL.
5. **Durable.** The server's process group is SIGKILLed mid-storm and
   restarted; the ledger afterward must contain every entry from the last
   pre-kill snapshot, and the server must keep working.
6. **Truthful responses.** A 200 on a state-changing request corresponds to a
   ledger entry (or is an exact replay); a 409 corresponds to none.
7. **Balances reconcile.** Final `GET /account` values equal the values
   recomputed from your dump.

## Operational requirements

- Handle concurrent requests correctly — the harness uses many simultaneous
  clients with barrier-synchronized collisions. Serialize at the storage
  layer (transactions/constraints), not with "check then insert" application
  logic.
- Respond within 5 seconds per request under this load.
- Include your own tests and a `NOTES.md` stating your concurrency mechanism:
  what serializes two racing holds, what makes a double-charge structurally
  impossible, and why a kill between "decide" and "append" can't corrupt the
  ledger.
