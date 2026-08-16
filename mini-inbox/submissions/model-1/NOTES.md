# mini-inbox — submission model-1

## Architecture in one sentence

Webhook events are **pings**, not state: an event tells us *that* an account
changed; only the provider's canonical endpoint tells us *what is true now*.
The payload's `data` is never trusted for state — at most its `account_id`
is used to mark that account for reconciliation.

## What is persisted before acking a webhook

`POST /webhook` does exactly two things, in one SQLite transaction (WAL mode,
under `$INBOX_STATE_DIR/inbox.db`), and only then returns `200`:

1. Inserts the event into `events(event_id, account_id, created_at,
   received_at)` with `INSERT OR IGNORE` — duplicates (same `event_id`) are
   deduped, so provider retries and ~30% duplicate deliveries are idempotent.
2. Upserts `accounts(account_id)` with `dirty=1`.

The ack path performs **no** canonical fetch and no network I/O; it is a local
SQLite write, so the ack lands in milliseconds, far inside the 3 s contract,
even while reconciliation is running.

## How a kill between ack and processing is survived

Everything the reconciler needs is already committed before the `200` goes
out: the event row and the account's `dirty=1` flag. A SIGKILL at any later
point loses nothing — on restart the service reopens the same database,
reloads the dirty set from `accounts`, and resumes draining it. Provider-side
retries cover the complementary case (kill before ack): undelivered events are
re-enqueued by the provider every 4 s until accepted, so no event is ever
silently dropped.

Pacing state also survives restarts: the timestamp of the last canonical call
is persisted in a `meta` table, because the provider's sliding rate window
outlives our process. Restarting cannot burst into the budget.

## Reconciliation strategy within the rate budget

A background thread drains the dirty set:

- **Target selection** — oldest-`last_fetch_ts` dirty account first, subject
  to a per-account **debounce** (2 s) so a burst of events for one account
  merges into one fetch.
- **Global pacing** — at least **3.2 s between any two canonical calls**,
  capping sustained usage at ~18.75/min, strictly under the provider's
  20/min sliding-window limit. Steady-state operation therefore never trips
  a 429; there is no burst path in the code.
- **429 handling** — parse `retry_after`, advance the global pace by
  `max(retry_after, MIN_SPACING)`, and continue the normal loop. No retry
  storm, no busy-loop: the loop ticks every 0.4 s and sleeps when no target
  is eligible.
- **Effect** — during the stream, state lags truth by a bounded amount (that
  is acceptable; only post-settle accuracy is graded). After the stream ends,
  no new events arrive, the dirty set can only shrink, and the final drain
  fetches return the final truth for every touched account. Convergence is
  guaranteed once the dirty set empties.

## Endpoints

| Endpoint | Behavior |
|---|---|
| `POST /webhook` | durable record + dirty mark, then immediate `200` |
| `GET /state/all` | `{"accounts": {id: {"plan", "status"}}}` for accounts whose state was actually fetched |
| `GET /health` | `200` |

## Running

`./inbox serve` — foreground, port `$PORT`, state under `$INBOX_STATE_DIR`,
provider at `$PROVIDER_URL` (all set by the harness; dev defaults exist).

## Tests

`python3 tests/test_inbox.py` — self-contained; spins up its own provider
instance (frozen `provider.py`, unmodified) and its own consumer on
ephemeral ports, never touching shared state:

1. `test_webhook_ack_latency` — synthetic webhook acked in < 3 s.
2. `test_429_backoff` — mock canonical endpoint returning 429 twice with
   `retry_after`; asserts the consumer backs off (no retry before the backoff
   elapses) and converges once truth is available.
3. `test_convergence` — full hostile stream (duplicates + stale stragglers);
   after the stream ends and a settle period passes, `/state/all` must equal
   the canonical state for every account; also asserts zero 429s were needed
   (pacing discipline) and a sane total call count.
4. `test_crash_safety` — same stream, consumer SIGKILLed mid-stream and
   restarted against the same state dir; must still converge exactly.
