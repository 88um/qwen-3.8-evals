# mini-publisher — crash-window design

## State machine

    pending ──(daemon dispatch)──> delivering ──(HTTP 200)────────────> delivered
                                      │
                                      └──(500 / timeout / dropped conn)──> uncertain (+1 attempt)
                                                        │
                              /audit >= 1 ──────────────┤──> delivered
                                                        │
                              /audit == 0, attempts<10 ─┴──> pending (backoff >= 1s)
                              /audit == 0, attempts>=10 ────> failed

Startup recovery: any row found in `delivering` (killed mid-flight) is moved to
`uncertain` before any other work.

## Commit points relative to the HTTP call

1. `delivering` + attempt timestamp is committed to SQLite (WAL) BEFORE the POST is sent.
2. The outcome (`delivered`, or `uncertain` + attempts+1) is committed AFTER the client
   fully gives up on the request (response received, timeout fired, or socket dropped).
   There is no state write between these two points.

## Why no kill point can cause a duplicate

The receiver records an acceptance at request time, before any hang, and `/audit` is
authoritative. Therefore, once our client has fully given up on an in-flight request,
`/audit` reveals the complete truth about that request:

- killed while the POST is in flight, or before recording its outcome: on restart the
  row is `delivering` -> recovered to `uncertain` -> audited. count>=1 means `delivered`,
  never retried; count==0 means the request was provably never accepted, so retrying is
  safe.
- killed after a 200/500 arrived but before it was recorded: identical path via the
  persisted `delivering` state.
- killed between giving up and the audit, or between the audit and its state write:
  restart re-enters `uncertain` and re-audits; audit==0 can only ever follow a
  provably-dead request, so re-eligibility remains safe.

A POST is therefore only ever issued for a message whose last established truth is
"never accepted" (fresh enqueue, or audit==0 after give-up). Combined with the flock
daemon lock (at most one daemon at a time), no schedule of kills can produce two
accepted deliveries for one message_id.

`failed` requires >=10 counted attempts; every counted attempt passed through an
audit==0 confirmation before the message became retry-eligible again, and the final
failed decision itself is gated on audit==0.
