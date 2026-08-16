# Engineering Plan Authoring Standard

Attach this to any request for an engineering plan. It defines *how* the document is
written, not *what* it should decide. Every technical judgment remains yours.

---

## The one rule

**Show the mechanism. Never assert that a mechanism exists.**

A reader must be able to check your claim without asking you a question. If you claim
something about data, show the table. If you claim something about cost, show the
arithmetic. If you claim something about behavior under failure, show the sequence.

Everything below is that rule applied to specific cases.

---

## Required structure

Use these section numbers and titles, in this order. A reader will diff your document
against others, so the skeleton must match.

1. **Technology decisions** — one subsection per decision
2. **System architecture** — processes, responsibilities, communication
3. **Data model** — DDL
4. **Invariant enforcement map** — table
5. **Failure-mode walkthrough** — numbered sequences
6. **AI strategy** — models, prompts, validation, measurement, cost
7. **Testing and release confidence**
8. **Delivery phases** — with exit criteria
9. **Security and privacy**
10. **Risk register** — table
11. **Explicit tradeoffs** — where you accepted weaker behavior, and why
12. **Where this is stronger than required** — and why
13. **Assumptions** — every place the brief was silent

Do not add sections. Do not merge sections. If a section does not apply, write one line
saying so and why.

---

## Rules, with examples

### R1 — Every decision is Choice / Rejected / Why

Three labeled parts. The rejected alternative must be the **strongest** competing option,
not a strawman. The "why" must name what you are optimizing for.

> **Choice:** PostgreSQL 16.
> **Rejected:** SQLite. Genuinely simpler and zero-ops, and it would work for the read
> path. Rejected because concurrent writers from the web process and workers need
> row-level locking, and `SELECT … FOR UPDATE SKIP LOCKED` has no SQLite equivalent.
> **Why:** the invariants in §4 are enforced by conditional updates under concurrency;
> that requirement outranks the operational simplicity SQLite would buy.

Rejecting an option because it is "less popular," "heavier," or "not ideal" is not a
reason. Name the specific capability you would lose or the specific cost you would pay.

### R2 — Data model means DDL

Write `CREATE TABLE`. Include column types, `NOT NULL`, `CHECK`, `UNIQUE`, foreign keys,
and the indexes that enforce something. A schema described in prose cannot be reviewed.

> ✗ `application(id, user_id, status, credit_hold, row_version, ...)` — lifecycle
>   transitions are guarded by `row_version`.
>
> ✓
> ```sql
> CREATE TABLE applications (
>     id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
>     user_id UUID NOT NULL REFERENCES users(id),
>     status TEXT NOT NULL CHECK (status IN ('queued','preparing','ready','submitting','submitted','failed','canceled')),
>     created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
> );
> CREATE UNIQUE INDEX applications_one_active_per_job
>     ON applications (user_id, job_id)
>     WHERE status NOT IN ('submitted','failed','canceled');
> ```

If a constraint cannot be expressed in DDL, say which code path enforces it and name the
function.

### R3 — Every mechanism claim names the artifact that enforces it

A mechanism is a table, a constraint, an index, a lock, a transaction boundary, an
ordering, or a named function. It is not an intention.

> ✗ The system ensures only one worker processes an application at a time.
> ✓ `SELECT … FOR UPDATE` on the application row inside the transaction that flips
>   `status` from `ready` to `submitting`; the second worker's `WHERE status = 'ready'`
>   matches zero rows and it exits.

### R4 — Numbers, not adjectives

Every quantity gets a value and a derivation. If you write a magnitude word, replace it.

Required numbers include, at minimum: concurrency limits · every timeout · every retry
count · every threshold · cache lifetimes · retention periods · rate limits · queue depth
alarms · monthly cost per component · tokens and dollars per AI task.

> ✗ A handful of concurrent browser sessions, with a reasonable timeout.
> ✓ 4 concurrent browser contexts (4 GB RAM ÷ ~800 MB per Chromium context, leaving
>   headroom for the worker process). Per-operation timeout 30 s; whole-job timeout 5 min.

### R5 — Cost claims are arithmetic

Show inputs, outputs, unit prices, and the sum. Then show the same for the worst realistic
case, and say what happens when it exceeds the budget.

> ✓ Resume extraction: 2K in / 1K out at $0.15 / $0.60 per 1M = $0.0009.
> [… each task …] Median total: $0.005. P95 (long resume, 40-field form, one
> regeneration): $0.021. Budget is $0.10; the cap is enforced by <named mechanism>,
> which does <specific thing> when reached.

An estimate that does not multiply by the number of times the call actually happens is
wrong. State the call count per unit of work, and check it against the scale in the brief.

### R6 — One choice. No menus, no TBDs

Never write "X or Y," "we could," "to be determined," or "depending on requirements."
Where the brief is silent, decide, and record the assumption in §13.

> ✗ Sessions stored in Redis (or a PostgreSQL session table).
> ✓ Sessions stored in a PostgreSQL `sessions` table. Assumption: no second datastore is
>   introduced for session state; revisit if session reads exceed <n>/s.

### R7 — Failure modes are numbered sequences with an evidence line

Format: **Scenario** (one sentence) → **What happens** (numbered steps, each naming a
mechanism) → **Evidence** (what a person could inspect afterward to confirm it worked).

> **Scenario:** The worker crashes after filling the form and before submitting.
> **What happens:**
> 1. The Postgres connection drops; the open transaction rolls back.
> 2. The `applications` row remains at `status = 'submitting'` (committed earlier).
> 3. <next mechanism> …
> **Evidence:** `application_events` shows … ; the trace file at … shows …

Every step must name a mechanism. A step that says "the system detects this" is not a
step.

### R8 — The invariant map is a three-column table

| Invariant | Mechanism | Evidence it works |

The mechanism column is structural — schema, constraint, transaction, ordering. The
evidence column is a test you will actually run, named specifically enough to write:
which test, what it does, what it asserts. "Unit tests" is not evidence.

### R9 — Phases close on evidence, never on dates

Each phase: what gets built, then **exit criteria** as statements a person can verify as
true or false. Do not estimate durations. Do not put a calendar anywhere.

> ✗ Duration estimate: 2 weeks.
> ✓ **Exit criteria:** upload a resume and see an extracted draft · edit any field ·
>   publish and confirm a new immutable version row exists · re-upload and confirm v1 and
>   v2 both resolve to their original content.

Name explicitly which phase is the first end-to-end run, and justify every phase you
placed before it.

### R10 — If you claim a tool can do something, name the API

Do not attribute a capability to a library without naming the specific call, flag, or
file format that provides it. If you are unsure whether it exists, choose a different
mechanism.

> ✗ The browser session is parked to disk and resumed later.
> ✓ `browserContext.storageState({path})` persists cookies and localStorage. It does not
>   persist DOM state, so <the design accounts for this by …>.

### R11 — Tradeoffs are disclosed, not hidden

§11 lists every place you knowingly deliver less than the brief asks. If you weakened a
stated requirement — even slightly, even for a good reason — it belongs there with the
rationale. A reviewer finding an undisclosed weakening treats it as concealment.

§12 lists where you exceed the brief and why. Both sections must be non-empty.

### R12 — Assumptions are inline and collected

When the brief is silent, state the assumption where you make it *and* list it in §13.
Never ask the reader a question. Never leave the gap open.

---

## Banned phrases

Each of these marks a place where an assertion replaced a mechanism. Find and replace
every one before submitting.

| Banned | Replace with |
|---|---|
| "the system ensures / guarantees / handles" | the constraint, lock, or ordering that does it |
| "we'll monitor / alert on / track" | what *prevents* it; monitoring may be added *after* |
| "robust / scalable / reliable / seamless" | the number or mechanism that makes it so |
| "a handful / a few / several / small / bounded" | the integer |
| "reasonable / appropriate / sufficient" | the value and its derivation |
| "gracefully degrades" | what specifically happens, in order |
| "as needed / if necessary / where appropriate" | the condition, stated |
| "best practice / industry standard" | the reason it applies here |
| "many systems do X" | what *this* system does, and what it does when X is absent |
| "TBD / to be determined / we could use" | the decision |
| "should / would / might" (about your own design) | "does" |

---

## Before you submit

Run this list against your own draft. Fix, do not annotate.

1. Every §1 decision has all three parts, and each rejected alternative is the strongest
   one available.
2. §3 is executable DDL. Every invariant in §4 that lives in the schema is visible there.
3. Every row of §4 names a structural mechanism and a specific, writable test.
4. Every §5 scenario is numbered, every step names a mechanism, every scenario ends with
   an evidence line.
5. Every number in the document has a derivation. Search the draft for each banned phrase
   and confirm zero hits.
6. §6 contains multiplied-out arithmetic, and the call counts match the scale in the brief.
7. §8 has no dates and no durations; exit criteria are verifiable statements.
8. §11 and §12 are both non-empty, and §11 includes every requirement you softened.
9. §13 lists every assumption, and no question is asked anywhere in the document.
10. Every capability you attribute to a library names the API that provides it.
