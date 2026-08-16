# Design Notes

## What is persisted before acking a webhook

The webhook handler runs inside a `BEGIN IMMEDIATE` transaction with
`synchronous=FULL`. Before returning 200, it durably writes:

1. The event itself (deduped by `event_id`) into the `events` table
2. An interim account state update (using the event's `created_at` to keep
   the newest event's data as our best guess)
3. A `dirty` flag for the account, triggering background reconciliation

The 200 response is sent only after `COMMIT` returns, which with
`synchronous=FULL` means the WAL frame is fsynced.

## How a kill between ack and processing is survived

If the process is SIGKILLed after the `COMMIT` (data fsynced) but before the
200 reaches the provider: the event is durable in SQLite. The provider retries,
the handler sees the duplicate `event_id`, skips it, and returns 200.

If killed before `COMMIT`: the transaction rolls back (SQLite WAL recovery).
The provider retries, and we process the event fresh.

The dirty flag also survives the kill. On restart, the background reconciler
picks up all dirty accounts and fetches their canonical state.

## Reconciliation strategy within the rate budget

A background thread polls the `dirty` table every 0.5 seconds and fetches
canonical state from `GET /subscription` for each dirty account:

- A sliding window tracks API call timestamps; we stay under 18 calls/minute
  (under the 20/min limit) to leave headroom.
- On 429, we read `retry_after` from the response and sleep accordingly.
- On success, the canonical plan+status overwrites the account row and the
  dirty flag is removed, all within one transaction.
- After canonical reconciliation, `updated_at` is set to 1e18 so that
  subsequent stale event arrivals don't overwrite canonical truth in
  `/state/all`. The account is still re-marked dirty on any new event,
  triggering another canonical fetch.

With 10 accounts and ~3 calls/second capacity, a full sweep completes in
under 10 seconds, well within any reasonable settle period.
