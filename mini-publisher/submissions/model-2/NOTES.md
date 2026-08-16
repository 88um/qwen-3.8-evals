# Crash-Window Design

## State machine

```
pending → delivering → delivered      (200 from receiver)
                     → uncertain      (500, timeout, connection drop, or crash)
uncertain → delivered                 (audit shows accepted_count ≥ 1)
          → pending                   (audit shows accepted_count = 0, cf < 10)
          → failed                    (audit shows accepted_count = 0, cf ≥ 10)
```

On daemon startup: `delivering → uncertain` (crash recovery).

## Commit points relative to the HTTP call

1. **Before POST**: durably write `state = 'delivering'` with `SYNCHRONOUS=FULL`. This is the commit point. If we crash after this but before the POST, the message stays in `delivering`. On restart it becomes `uncertain` and we audit.

2. **After POST returns 200**: write `state = 'delivered'`. If we crash before this commit, the message is still `delivering` → on restart becomes `uncertain` → audit confirms `accepted_count=1` → marked `delivered`. No duplicate because we never re-POST an `uncertain` message.

3. **After POST returns 500**: write `state = 'uncertain'`. The receiver guarantees 500 = not accepted. Routing through uncertain→audit anyway so every attempt is audit-confirmed (required for the `failed` terminal state).

4. **After POST times out / connection drops**: write `state = 'uncertain'`. Audit resolves.

## Why no kill point causes a duplicate

The only way to create a duplicate is to POST the same `message_id` twice where both are accepted. We only POST from the `pending → delivering` transition. A message can only return to `pending` from `uncertain`, and only after `/audit` confirms `accepted_count = 0`. Therefore:

- If a POST was accepted, audit will show `≥ 1` and the message goes to `delivered` (terminal).
- If a POST was not accepted (or never sent due to crash), audit shows `0` and the message may retry.
- There is never a window where we re-POST a message whose previous POST was accepted.

The audit endpoint is queried only after the HTTP client has fully released the connection (exception caught, socket closed), and the receiver records acceptance before hanging, so the audit result is always current by the time we read it.

## SQLite durability

WAL mode with `PRAGMA synchronous=FULL` ensures every committed transaction is fsynced to the WAL before `commit()` returns. SIGKILL after commit cannot lose the write.
