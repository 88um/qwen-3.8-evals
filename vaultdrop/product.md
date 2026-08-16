# Build challenge: "VaultDrop" — a multi-tenant artifact storage service

You are the founding engineer; I am the product manager. This is an **execution**
task, not a plan: you build a working service, write its tests, and document your
design. Your submission will be built, run, and then **hostile-reviewed** by two
independent engineers who will trace every invariant you claim, construct targeted
probes against the weaknesses they find, and SIGKILL your service at the worst
moment. There is no hidden pass/fail grader — there is adversarial reading, and it
scales with how honestly your code does what your `DECISIONS.md` says it does.

Budget: roughly 60–120 minutes of work. Target 1,500–4,000 lines. Correctness
under concurrency, crashes, and cross-tenant attack outranks feature count,
polish, and everything else — but note that the **scale envelope in §2 is part
of the interface contract, not polish**: shrinking the product until it is easy
to make correct (tiny artifact caps, whole-artifact buffering, global
serialization) is scored as a contract violation, not a defensible design
choice. Build the real thing, then make the real thing correct.

---

## 1. The product

VaultDrop stores immutable binary artifacts for many tenants. Clients upload large
files in chunks (resumable over flaky connections), the service stores each
distinct byte-content **once** (content-addressed dedup across the whole system),
and clients download their own artifacts by ID. A background subsystem validates
stored content and garbage-collects bytes no artifact references. An operator can
inspect and intervene.

The promises, in order of how catastrophically they fail:

1. **No cross-tenant access, ever.** A tenant can never read, download, enumerate,
   or confirm the existence of another tenant's artifact — not by guessing IDs, not
   by uploading identical content and observing dedup behavior, not through the
   admin surface with a tenant token. Content is deduplicated at the byte layer for
   storage efficiency, but that sharing must be **invisible and inaccessible** across
   tenants: two tenants storing the same bytes each have their own artifact, and
   neither can tell the other exists.
2. **No corrupt or partial reads.** A download either returns the exact, complete
   bytes whose SHA-256 equals the artifact's declared digest, or it fails — never a
   truncated, half-finalized, or mid-GC artifact. Readers never observe an artifact
   that is not yet fully finalized.
3. **No lost or double-counted bytes under crash.** The service is SIGKILLed during
   chunk writes, during finalization, and during GC, then restarted. After
   recovery: every finalized artifact is still fully readable, no half-written state
   is served, and GC never deletes bytes a live or in-flight artifact needs.
4. **Dedup and GC are correct under concurrency.** Finalizing an artifact that
   references content, and GC deciding that content is unreferenced, can race. The
   reference must win: GC must never collect bytes that a concurrent finalize is
   about to reference, and finalize must never succeed against bytes GC is deleting.

## 2. Interface contract (the reviewers build to this exactly)

Submission root contains an executable **`vaultdrop`** (any language on a stock
macOS/Linux dev machine; standard library strongly preferred, vendor anything else
into the submission). It supports:

- `./vaultdrop serve` — runs the HTTP API **in the foreground** on `$PORT`. All
  persistent state (database + stored bytes) lives under `$VAULTDROP_STATE_DIR`
  (created for you, initially empty). Background validation and GC may run inside
  this process or be triggered via the admin endpoints below — your choice, but GC
  must be **triggerable on demand** for the review to exercise it deterministically.
- `./vaultdrop migrate` — initializes/upgrades schema under `$VAULTDROP_STATE_DIR`
  (may be a no-op if `serve` self-initializes; must exist and exit 0).

Authentication: every request carries `Authorization: Bearer <token>`. Tokens map
to tenants via a seed file the harness writes to `$VAULTDROP_STATE_DIR/tenants.json`
before `serve` starts:

```json
{"tenants": [{"id": "t1", "token": "tok-t1"}, {"id": "t2", "token": "tok-t2"}],
 "admin_token": "tok-admin"}
```

Your service reads this at startup as the source of truth for who may call what.

### Scale envelope (part of the contract)

The reviewers build probes against these minimums exactly as they do against the
endpoints. A submission whose stated or effective limits fall below them is
scored under §6 as a contract violation (a confirmed correctness finding), not
as a disclosed design choice.

- **Artifacts up to 10 GiB** must be accepted, finalized, and served.
- **Chunk PUT bodies up to 32 MiB** must be accepted. You may allow larger;
  whatever limit you state must actually be enforced on the wire — a stated
  limit the reviewer exceeds without rejection is itself a finding.
- **Bounded memory:** resident memory must stay under **512 MiB** during any
  single operation, including a 10 GiB upload, finalize, or download. In
  practice this means streaming I/O end to end; the reviewers will measure RSS
  during a multi-GiB round-trip.
- **No global stalls:** a validation pass over the full store must not stop the
  world. A finalize or download issued mid-validation must complete within
  roughly 2× its unloaded latency. Concurrent finalizes of *different* content
  must actually overlap, not serialize behind one global lock.
- **Metadata scale:** with 10,000 stored artifacts/blobs, listings, lookups, and
  GC passes must remain responsive (single-digit seconds, not minutes). Plan
  your filesystem layout accordingly (10k+ files in one flat directory is your
  problem to have thought about).

### HTTP endpoints

| Method + path | Auth | Behavior |
|---|---|---|
| `POST /uploads` | tenant | Body `{"name","total_size","chunk_size"}`. Start a resumable upload; returns `{"upload_id","received": []}`. |
| `PUT /uploads/{id}/chunks/{index}` | tenant | Body = raw chunk bytes; header `X-Chunk-SHA256`. Store chunk `index`. Idempotent: an exact replay (same index, same bytes) → `200`. A **conflicting** replay (same index, different bytes) → `409`. Chunks arrive concurrent, duplicated, out of order. |
| `GET /uploads/{id}` | tenant (owner) | `{"upload_id","received":[indices],"state"}`. Another tenant → `404` (not `403` — existence itself is private). |
| `POST /uploads/{id}/finalize` | tenant (owner) | Body `{"sha256","size"}`. Assemble chunks, verify total length and SHA-256 against the body; on match create an immutable artifact and return `{"artifact_id"}`; on mismatch → `422` and no artifact. Two clients may call this concurrently for one upload; **at most one artifact** results. |
| `GET /artifacts/{id}` | tenant (owner) | Stream the exact artifact bytes. Wrong tenant or unknown id → `404`. Never returns partial/unfinalized/mid-GC content. |
| `GET /artifacts` | tenant | List **only** the calling tenant's artifacts: `[{"artifact_id","name","size","sha256"}]`. |
| `DELETE /artifacts/{id}` | tenant (owner) | Delete the tenant's artifact (metadata). Underlying bytes become collectable only when no artifact in any tenant references them. |
| `POST /admin/gc` | admin | Run a garbage-collection pass synchronously; return `{"scanned","collected","bytes_freed"}`. Must be safe to call concurrently with live uploads/downloads/finalizes. |
| `GET /admin/blobs` | admin | Inspection: `[{"content_hash","refcount","size","validated"}]` over the physical byte-store. Never exposes tenant identity of referencing artifacts beyond counts. |
| `GET /admin/validate` | admin | Trigger background validation: re-hash stored bytes, flag any whose content no longer matches its address; return counts. |
| `GET /health` | none | `200`. |

Return JSON `{"error": "..."}` with appropriate status on failure. Limits (max
chunk size, max artifact size) are yours to set and state in DECISIONS **at or
above the scale envelope's minimums**, and every stated limit must be enforced
on the wire.

## 3. Required behaviors the review will probe

These are the minimum scenarios the reviewers will construct probes for. Your
`DECISIONS.md` claims register (see §5) should speak to each.

- **Concurrent overlapping chunks** — many `PUT`s for the same upload at once,
  including duplicate indices arriving simultaneously; final assembly is correct.
- **Conflicting chunk replay** — same index, different bytes → `409`, and the
  first-written bytes are not silently overwritten.
- **Finalize vs late chunk** — a chunk arrives after finalize has begun/completed;
  the finalized artifact is not mutated by it.
- **Concurrent finalize** — two finalizes for one upload race; exactly one artifact
  exists afterward, and both callers get a coherent answer (one `artifact_id`, the
  other either the same id or a clear conflict — your choice, stated).
- **Finalize vs GC** — GC runs while a finalize is about to reference content whose
  refcount is momentarily zero (e.g. content that existed from a prior deleted
  artifact); the bytes survive for the finalize (promise 4).
- **Crash during chunk write** — SIGKILL mid-`PUT`; on restart the upload is
  resumable and no partial chunk is treated as complete.
- **Crash during finalize** — SIGKILL between "bytes assembled" and "artifact
  committed"; on restart there is either a complete artifact or none, never a
  half-one, and the upload can be finalized again.
- **Crash during GC** — SIGKILL mid-collection; on restart no referenced bytes are
  missing and no partially-deleted blob is served.
- **Cross-tenant dedup** — tenant `t1` and tenant `t2` upload identical bytes; each
  gets its own artifact id; `t2` cannot download `t1`'s id, cannot see it in any
  listing, and cannot infer from `POST /uploads`/finalize timing or responses that
  `t1` already stored that content.
- **Cross-tenant ID probing** — `t2` requests `t1`'s `artifact_id` and `upload_id`
  directly → `404`, indistinguishable from a truly unknown id.
- **Scale probes (the envelope, exercised)** — a multi-GiB artifact round-trips
  while the reviewer watches resident memory (streaming, not buffering); several
  concurrent finalizes of distinct content overlap rather than serialize; a
  finalize and a download issued while `/admin/validate` runs against a full
  store complete without waiting for it; the service stays responsive at
  10,000 stored blobs.

## 4. Constraints

- One stateful store for metadata (SQLite recommended) plus the filesystem for
  bytes, both under `$VAULTDROP_STATE_DIR`. No external services, no network deps.
- Content addressing is SHA-256 over full artifact bytes. Dedup is by that address.
- Durability: a `200`/`2xx` on finalize must survive `kill -9` (think about fsync
  of both the bytes and the metadata commit, and their ordering).
- Single-node; you may run multiple worker threads/processes but all under the one
  `serve` invocation and one state dir.
- No AI features.

## 5. Deliverables

```
<your-id>/
  README.md        build command, runtime deps, how to run serve/migrate
  DECISIONS.md     ≤1,500 words. Architecture, and the CLAIMS REGISTER:
                   a table of every invariant you enforce, the exact mechanism
                   (table / constraint / lock / transaction boundary / atomic
                   rename / fsync ordering), and how a reviewer checks it.
                   State your durability ordering and your finalize-vs-GC
                   coordination explicitly — those are the two the review
                   hits hardest. The register must also speak to the scale
                   envelope: name the mechanism that bounds per-request
                   memory (streaming path) and the lock granularity that
                   keeps validation/GC from stalling unrelated operations.
                   Honest "not implemented / best-effort" notes here cost
                   you far less than an overclaim the review breaks — but
                   scale-envelope minimums cannot be waived by disclosure.
  src/ ...         the implementation
  migrations/ ...  schema (if separate)
  tests/ ...       your own tests, including at least one crash-recovery test
                   and one concurrency test; they must pass (a submission whose
                   own tests fail scores zero).
```

## 6. How this is graded

Reviewed under `review-protocol.md`: two independent hostile reviews, correctness
from **confirmed** findings only (a CONFIRMED needs a runnable repro or a
line-referenced interleaving trace), the claims register as the attack target
(a claimed-but-broken invariant costs double; a required invariant missing from
the register is scored as if broken), and a capped, averaged judgment layer.
Scale-envelope violations (§2) are confirmed correctness findings like any
other: a limit below the envelope, a blown memory ceiling, or a global stall
lands in the correctness row below, with severity set by how far short of the
contract the submission falls.

| Category | Weight |
|---|---:|
| Correctness under concurrency & crash (promises 1–4, from confirmed findings) | 55 |
| Data model & mechanism design (schema, refcounting/GC, atomicity) | 20 |
| Tenant isolation depth (promise 1, incl. dedup side-channels) | 10 |
| Test quality (real crash/concurrency coverage, not green-but-empty) | 8 |
| Architecture, clarity, operability (judgment layer, averaged) | 7 |

Mechanical floor: builds, runs from this contract, and its own tests pass — or it
scores zero. Everything above the floor is earned against reviewers who are trying
to break it.
