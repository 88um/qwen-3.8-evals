# VaultDrop Model 1 — hostile execution review (GPT)

Date: 2026-08-16

## Result

**Numerical score: 66.5 / 100. Rank: 1 of 1 submission evaluated in this pass.**

Model 1 clears the mechanical floor and is strong on streaming, crash recovery,
tenant scoping, and finalize/GC coordination. One adversarial storage-corruption
probe nevertheless produced a complete HTTP `200` response whose bytes did not
match the artifact's declared SHA-256. This directly breaks the registered
"No corrupt or partial download response" invariant. Under the protocol's literal
severity table, silent corruption is CRITICAL, and the claims-register doubling
rule makes the correctness deduction 50 points.

This is an independent review of the current `model-1/` tree. It is not reconciled
with another current-tree reviewer, so the ordinal rank is necessarily only 1/1,
not a field ranking.

## Mechanical floor

| Check | Result |
|---|---|
| README-only migration and serve | PASS |
| Standalone `migrations/001_initial.sql` execution | PASS |
| Candidate-authored tests | PASS: 5/5 test methods |
| Executable/interface contract | PASS |
| Runtime dependencies | PASS: Python standard library only |
| Claims register | PASS: 15 rows, including crash, GC, isolation, and scale claims |

The documented command `python3 -m unittest discover -s tests -v` completed in
1.96 seconds. The suite exercises real loopback HTTP and a real SIGKILL during a
partial PUT.

## Score

The verifiable/judgment split totals 84/16 possible points, keeping judgment well
below the protocol's 30% cap.

| Category | Verifiable | Judgment | Total |
|---|---:|---:|---:|
| Correctness under concurrency and crash | 27.5 / 55 | 0 / 0 | **27.5 / 55** |
| Data model and mechanism design | 12 / 14 | 5.5 / 6 | **17.5 / 20** |
| Tenant isolation depth | 7 / 7 | 2.5 / 3 | **9.5 / 10** |
| Test quality | 4 / 6 | 1.5 / 2 | **5.5 / 8** |
| Architecture, clarity, operability | 2 / 2 | 4.5 / 5 | **6.5 / 7** |
| **Total** | **52.5 / 84** | **14 / 16** | **66.5 / 100** |

Correctness calculation: `max(0, 100 - 50) = 50%`; scaled to the 55-point
category, `0.50 * 55 = 27.5`.

## Confirmed finding ledger

### M1-1 — in-place mutation after preflight verification is served as valid bytes

- **Grade:** CONFIRMED by runnable repro
- **Severity:** CRITICAL x2 (claimed invariant)
- **Weight:** -50
- **Broken claim:** DECISIONS.md line 69, "No corrupt or partial download
  response"
- **Observed:** HTTP `200`, `Content-Length: 268435456`, 268435456 response bytes,
  and response SHA-256 different from the artifact's declared digest

The defect is a check/use gap across two passes over the same descriptor:

1. `open_verified_artifact` opens the blob under the per-hash lock at
   `src/vaultdrop.py:575-580`, then releases that lock.
2. It hashes and length-checks the descriptor at lines 581-596.
3. It rewinds and returns the descriptor at lines 597-598.
4. The handler sends `200` and the original `Content-Length`, then performs a
   second, unchecked read at lines 939-946.
5. POSIX descriptors survive rename/unlink, which protects this path from the
   service's own atomic replacement and GC. They are not immutable snapshots of
   an inode modified in place.
6. If stored bytes change after step 2, the second pass can stream different bytes
   without detection. The reproduced response was complete, not merely a client-
   detectable short body.

The candidate's own corruption test changes the file before the GET and therefore
does not reach this boundary. The README's statement at lines 52-55 that preflight
hashing prevents corrupt bytes from leaking is too broad.

The trigger is an out-of-band in-place storage mutation rather than a VaultDrop API
write: VaultDrop's own finalize path uses atomic replacement and never modifies a
published inode. It is still within the submitted claim's stated corruption model
(`Corrupt/truncate a blob; GET must ... never [return] artifact bytes`), and the
product includes validation specifically because stored-byte corruption is in
scope. The severity follows the protocol table's explicit "silent corruption"
classification. A reviewer who treats concurrent external mutation as an edge
condition and assigns HIGH x2 instead would produce an alternative total of
**80.8/100**; that is a severity choice, not a disagreement about the reproduction.

### Runnable reproduction

Run from a scratch copy of `model-1/`. The probe uploads 256 MiB, starts a GET,
waits for the `200` headers (which arrive only after the first hash pass), rewrites
the latter half of the blob through another descriptor, and then reads the body.

```python
import hashlib, http.client, json, sys, tempfile
from pathlib import Path

sys.path.insert(0, "tests")
from test_vaultdrop import RunningService

size = 256 * 1024 * 1024
chunk_size = 32 * 1024 * 1024
chunk = b"A" * chunk_size
chunk_hash = hashlib.sha256(chunk).hexdigest()

def request(service, method, path, body=None, token=None, headers=None):
    status, raw, _ = service.request(
        method, path, body, token, headers, timeout=120
    )
    return status, json.loads(raw)

with tempfile.TemporaryDirectory() as td:
    state = Path(td)
    (state / "tenants.json").write_text(json.dumps({
        "tenants": [{"id": "t1", "token": "tok-t1"}],
        "admin_token": "tok-admin",
    }))
    service = RunningService(state)
    service.start()

    status, body = request(service, "POST", "/uploads", {
        "name": "race.bin", "total_size": size, "chunk_size": chunk_size,
    }, "tok-t1")
    upload_id = body["upload_id"]
    full_hash = hashlib.sha256()
    for index in range(size // chunk_size):
        status, _, _ = service.request(
            "PUT", f"/uploads/{upload_id}/chunks/{index}", chunk, "tok-t1",
            {"X-Chunk-SHA256": chunk_hash}, timeout=120,
        )
        assert status == 200
        full_hash.update(chunk)

    digest = full_hash.hexdigest()
    status, body = request(
        service, "POST", f"/uploads/{upload_id}/finalize",
        {"sha256": digest, "size": size}, "tok-t1",
    )
    artifact_id = body["artifact_id"]
    blob = state / "blobs" / digest[:2] / f"{digest}.blob"

    connection = http.client.HTTPConnection("127.0.0.1", service.port, timeout=120)
    connection.request(
        "GET", f"/artifacts/{artifact_id}",
        headers={"Authorization": "Bearer tok-t1"},
    )
    response = connection.getresponse()
    assert response.status == 200

    with blob.open("r+b", buffering=0) as file:
        file.seek(size // 2)
        replacement = b"B" * (1024 * 1024)
        for _ in range((size // 2) // len(replacement)):
            file.write(replacement)

    received = hashlib.sha256()
    count = 0
    while data := response.read(1024 * 1024):
        count += len(data)
        received.update(data)

    print(response.status, response.getheader("Content-Length"), count,
          received.hexdigest() == digest)
    assert count == size and received.hexdigest() != digest
    service.stop()
```

Expected final field: `False`. Observed output:

```text
200 268435456 268435456 False
```

## Verified strengths and rejected concerns

These mechanisms survived both line-by-line interleaving analysis and the listed
live probes. They move no correctness score but are recorded to prevent re-chasing.

- **REJECTED — finalize versus GC can delete referenced bytes.** Finalize and GC
  use the same per-hash lock. GC rechecks `refcount=0` in its write transaction;
  finalize installs independently assembled, fsynced bytes before committing the
  reference. Twelve barrier-synchronized race rounds all finalized and downloaded
  successfully.

- **REJECTED — crash during finalize leaves a half artifact.** A 256 MiB probe
  waited until SQLite visibly recorded `state='finalizing'`, sent SIGKILL, restarted,
  observed recovery to `uploading`, retried finalize, and downloaded the correct
  digest. The traced alternative landing after the metadata commit is also safe:
  bytes and their directory entry are fsynced before the FULL SQLite commit.

- **REJECTED — crash during GC harms a live blob.** A probe kept one referenced
  artifact beside 5,000 zero-ref blobs, started GC, observed a file in `gc-trash`,
  sent SIGKILL, and restarted. The live artifact remained byte-exact; the next GC
  removed the remainder in 0.531 seconds and left exactly the live blob row.

- **REJECTED — chunk replay/finalize races mutate accepted bytes.** The unique
  temporary plus `BEGIN IMMEDIATE` winner check places the canonical chunk only for
  the first manifest writer. Candidate tests confirmed concurrent exact duplicates,
  conflicting `409`, late-chunk rejection, and one artifact under two finalizers.

- **REJECTED — crash leaves a partial chunk accepted.** The authored SIGKILL test
  kills a PUT after only 512 KiB of a 2 MiB body; restart reports no received chunk,
  and a complete retry succeeds.

- **REJECTED — the byte path violates bounded memory.** A 2 GiB upload/finalize/
  download round-trip passed with **38.0 MiB peak server RSS**, a 1.90-second
  finalize, and a byte-exact response. The code's 1 MiB loops and 32 MiB wire check
  have no artifact-size-dependent allocation. This supports, but does not replace,
  a literal 10 GiB run.

- **REJECTED — scale limits are only documentary.** `POST /uploads` accepts through
  exactly 10 GiB, chunk declarations and PUT bodies accept through exactly 32 MiB,
  and the handlers reject larger values before reading the body. Blob sharding,
  indexed tenant lists, streamed hashing, and 64-item GC batches match the scale
  mechanisms described in DECISIONS.md.

- **REJECTED — tenant ID probing exposes another tenant's object.** Upload and
  artifact queries include `tenant_id`; foreign and unknown valid IDs converge on
  the same JSON 404 path. Tenant tokens cannot use admin routes. Identical content
  produces separate random tenant artifact IDs and one physical blob with the
  correct refcount.

## Plausible concern, zero score

**Residual dedup timing channel — PLAUSIBLE, unresolved.** Novel and deduplicated
finalization execute the same application-level streamed assembly, hash, file
fsync, atomic replacement, directory fsync, and response shape. SQLite must still
insert versus conflict-update, and filesystem/cache state cannot provide
cryptographic timing noninterference. DECISIONS.md honestly discloses that residual
at lines 24-29. I did not establish a reliable classifier, so this moves zero score.

## Judgment assessment

The schema is compact and coherent: uniqueness on `artifacts.upload_id`, checked
refcounts, tenant indexes, and explicit upload states align with the code's
transactions. The strongest design choice is installing durable bytes before the
single artifact/refcount/upload-state commit, while sharing only per-content locks
with GC. The recovery loop repairs `finalizing`, recomputes refcounts, and removes
uncommitted filesystem remnants without serving during reconciliation.

Isolation is also strong. Every tenant-facing lookup is tenant-qualified, IDs are
random and per tenant, admin authentication is separate, and dedup produces no
response-shape or skipped-work branch. The disclosed inability to promise
cryptographic timing noninterference costs a small judgment fraction but is not a
confirmed leak.

Test quality is the weakest non-correctness category. The five methods are useful
and genuinely integration-level, but the suite has no crash during active finalize,
no crash during GC, no live finalize/GC race, no validation-under-load test, and no
multi-GiB/10,000-object scale test. Most importantly, its corruption test only
modifies the blob before GET and misses the verified-then-rewound descriptor gap.

The implementation and documentation are unusually clear for a standard-library
service. Operational tradeoffs are stated honestly. The two-pass download design
doubles read I/O and, as M1-1 shows, does not by itself make the second pass an
integrity-checked snapshot.

## Limitations

- This is one reviewer. The required cross-vendor judgment average and finding
  reconciliation have not occurred.
- Only one generation is present, while the protocol calls for two per model.
- The largest live round-trip was 2 GiB rather than 10 GiB; the exact 10 GiB limit
  and streaming loop were checked statically.
- Crash probes modelled process SIGKILL, as the brief specifies, not power loss or
  torn filesystem writes.

## Verdict

**Model 1 is a well-designed, highly operable submission whose upload, durability,
GC, isolation, and scale architecture withstand hostile review. It ranks 66.5/100
because one reproducible check/use gap returns silently corrupted bytes under a
claimed promise-2 invariant, triggering the protocol's CRITICAL doubling rule.**
