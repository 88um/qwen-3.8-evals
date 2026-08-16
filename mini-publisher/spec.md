# mini-publisher — Product Spec

Build a small, crash-safe message delivery tool: a daemon that delivers enqueued
messages to an unreliable external receiver **at most once each**, survives being
killed at any moment, and always tells the truth about what happened.

You are given `receiver.py` in this directory — a fake external service you deliver
to. You may run it freely during development. **Do not modify it.** Your grade comes
from an automated harness that runs your program against this exact receiver while
repeatedly SIGKILLing your daemon's entire process group at random moments.

## Shared workspace rules — read first

Several independent submissions are built from this same spec, possibly
concurrently on this machine. You have been assigned a **submission ID** (e.g.
`model-a`) in your kickoff instructions.

- Create **every file you write** inside your own submission directory
  (`submissions/<your-id>/`). Nothing goes in the shared root — no stray
  `NOTES.md`, test files, databases, or scratch files outside your directory.
- Standard filenames (`publisher`, `NOTES.md`, tests) keep their plain names
  *inside* your directory — the grading harness expects `./publisher` at your
  submission root.
- Never read, modify, or write another submission's directory, and never modify
  the shared `spec.md` or `receiver.py`.
- The grading harness (`grader.py`, anything under `results/`) may be present
  elsewhere on this machine. It is off-limits: do not read, run, or search for
  it. Build to this spec, not to the test — consulting the harness is treated
  as a disqualifying protocol violation.
- When you run `receiver.py` during development, **pick your own port** (e.g.
  `PORT=8341 python3 receiver.py`) instead of the default 8077 — another
  submission may be testing at the same time. Your program must read the
  receiver's location from `RECEIVER_URL` at runtime, never hardcode a port.
- Keep dev state inside your directory too (set `PUBLISHER_STATE_DIR` to a
  subdirectory of your submission dir when testing).

## The receiver you deliver to

Start it with `python3 receiver.py` (env: `PORT`, default 8077). Semantics:

| Interaction | Meaning |
|---|---|
| `POST /deliver` with JSON `{"message_id": "...", "payload": "..."}` → `200` | The receiver accepted **this** delivery and recorded it. |
| → `500` | Guaranteed **not** accepted. Safe to retry. |
| Request hangs (up to ~30 s) then the connection drops with no response | **Unknown.** The receiver may or may not have accepted it before hanging. |
| `GET /audit?message_id=X` → `{"message_id": X, "accepted_count": N}` | **Authoritative and always reliable.** The true number of times X has been accepted. This endpoint never lies, never hangs, and is cheap. Use it. |

Critical property: **the receiver does not deduplicate.** If you POST the same
`message_id` twice and both are accepted, it records two acceptances — that is a
duplicate delivery, the worst outcome this product can produce. A `200` response
refers only to the request that received it.

## Interface contract (the harness depends on this exactly)

Your submission's root directory must contain an executable named `publisher`
(shebang script is fine, any language available on a stock macOS dev machine —
prefer standard library only; if you need dependencies, vendor them into this
directory). It must support:

- `./publisher daemon` — runs the delivery daemon **in the foreground** (do not
  detach/daemonize; the harness backgrounds it and kills its process group).
- `./publisher enqueue <message_id> <payload>` — enqueues a message. Must work
  whether or not the daemon is running. Exit 0 on success. Re-enqueueing an
  existing `message_id` must be a no-op (exit 0), never a second logical message.
- `./publisher status --json` — prints to stdout a JSON object:
  `{"messages": [{"id": "...", "state": "...", "attempts": N}, ...]}`
  covering every message ever enqueued. Must work while the daemon is running.

`state` must be exactly one of: `pending`, `delivering`, `uncertain`,
`delivered`, `failed`.

Environment variables the harness sets:

- `PUBLISHER_STATE_DIR` — a directory (created for you, initially empty) where ALL
  of your persistent state must live. No state anywhere else.
- `RECEIVER_URL` — base URL of the receiver, e.g. `http://127.0.0.1:8077`.

## The promises (this is what is graded)

1. **At most once.** The receiver must never record `accepted_count >= 2` for any
   message — not under crashes, restarts, retries, or ambiguous outcomes. This is
   the catastrophic failure. There is no partial credit against it.
2. **Nothing lost.** Every enqueued message eventually reaches a terminal state.
   With this receiver's accept rate, effectively every message can be delivered;
   `failed` is permitted only after at least 10 attempts, each one confirmed
   not-accepted via `/audit`.
3. **Crash-safe.** The daemon's whole process group is SIGKILLed at arbitrary
   moments (emulating power loss) and restarted. All promises must hold across any
   kill point — including between issuing the HTTP request and recording its
   outcome. `enqueue` and `status` must work while the daemon is down.
4. **Ambiguity is a state, not a judgment call.** A timeout or dropped connection
   means *unknown*. The message must move to `uncertain` and must not be retried
   until `/audit` establishes the truth: `accepted_count >= 1` → `delivered`;
   `= 0` (queried only after your client has fully given up on the in-flight
   request) → eligible to retry. Never blind-retry after an ambiguous outcome.
5. **Truthful status.** `delivered` must mean the receiver really accepted it
   (exactly once); `failed` must mean it really never did. The harness diffs your
   final `status --json` against the receiver's ground truth log.

## Operational requirements

- Set client timeouts (the receiver hangs up to ~30 s; don't wait forever).
- Back off at least 1 second between attempts for the same message.
- SQLite is recommended for state (mind concurrent access from the CLI while the
  daemon runs — busy timeouts / WAL). Any durable design is acceptable.
- Concurrency across messages is allowed but not required. Correctness beats
  throughput; the harness allows generous settling time.
- Include your own tests, and a short `NOTES.md` stating your crash-window design:
  where the commit points are relative to the HTTP call, and why no kill point
  can cause a duplicate.

## What "done" looks like

`./publisher daemon` running, 50 messages enqueued while the daemon is being
SIGKILLed every few seconds, and after things settle: every message `delivered`
(or a rare, audit-verified `failed`), receiver ground truth showing every
`message_id` accepted exactly once, and `status --json` matching that truth.
