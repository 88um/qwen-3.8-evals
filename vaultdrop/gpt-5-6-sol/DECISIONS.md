# VaultDrop design decisions

## Architecture

One foreground `ThreadingHTTPServer` process owns a SQLite database in WAL mode
(`synchronous=FULL`) and POSIX files below the state directory. SQLite contains
tenant-scoped upload/artifact metadata, immutable chunk manifests, blob metadata,
and checked refcounts. Raw chunks and blobs are files. Blob paths use the full
SHA-256 under one of 256 prefix directories, avoiding a 10,000-entry flat directory
without making a full GC fsync thousands of distinct directories.

Each request opens its own SQLite connection. Read transactions do not block file
hashing or streaming. SQLite necessarily serializes its short metadata writes, but
large chunk transfer, artifact assembly, validation hashing, and download hashing
all occur outside write transactions. Per-content in-process locks coordinate only
operations on one SHA-256; distinct finalizations overlap. This is a single-process,
single-node design as required.

Chunks and artifacts stream in 1 MiB blocks. The maximum artifact is 10 GiB and the
maximum chunk is 32 MiB. Both limits are checked before reading a body. JSON is
limited to 64 KiB. Listings/results at the required 10,000-object scale are held in
memory (roughly a few MiB); artifact bytes never are.

Every finalize writes and fsyncs a complete staging blob even on a dedup hit, then
atomically replaces the addressed blob with the verified identical content. Thus a
tenant receives no ID, response field, skipped hashing/write path, or explicit
timing branch that reveals a prior tenant's identical blob. Artifact and upload IDs
are independent 128-bit random values. As with any shared disk service, aggregate
resource contention is not a cryptographic timing noninterference guarantee.

## Durability ordering

A chunk PUT writes a uniquely named temporary, verifies SHA-256, fsyncs the file,
then under a short SQLite write transaction atomically renames it to its immutable
index, fsyncs the chunk directory, inserts the manifest row, and commits. A kill
before commit leaves either only a removable temporary/orphan or a complete file
without metadata; neither is accepted as received.

Finalize first atomically changes `uploading` to `finalizing` after checking manifest
count and byte total. It assembles and hashes outside SQLite. On a match it fsyncs
the staging file, persists any new prefix directory, takes the content lock,
atomically replaces the blob, fsyncs its directory, then in one FULL SQLite commit
inserts/upserts the blob, inserts the unique artifact, increments refcount, and sets
the upload to `finalized`. Therefore a returned 2xx has both durable bytes and
durable metadata, in that order. Startup resets an uncommitted `finalizing` upload,
removes temporaries/uncommitted blobs, recomputes refcounts from artifacts, and
retains every committed artifact.

GC takes content locks in sorted batches of 64. In each short write transaction it
rechecks `refcount=0`, renames bytes to durable GC trash, fsyncs source/trash
directories, deletes metadata, and commits; only then does it unlink trash. A kill
before commit leaves recoverable/unreferenced trash; a kill after commit leaves
trash with no metadata, deleted at startup. Finalize uses the same content lock. GC
may win before finalize has a reference, but finalize still has its independently
assembled bytes and recreates the blob before committing the reference. Once a
reference commits, GC's recheck cannot collect it.

## Claims register

| Invariant | Exact mechanism | Reviewer check |
|---|---|---|
| Tenant data is private | Every upload/artifact lookup includes `tenant_id`; random IDs; owner mismatch and unknown both return 404. Admin token is separate and tenant tokens get 403 on admin routes. Listings filter by tenant. | Probe both tenants' IDs and lists; try tenant tokens on admin routes. |
| Dedup does not merge tenant artifacts or expose a hit | `artifacts.id` is random and tenant-scoped; only `sha256` references the shared blob. Every finalize performs the same streamed assembly, hash, file fsync, atomic replacement, directory fsync, and response shape. | Upload identical bytes as two tenants; compare responses, IDs, access, and admin refcount. |
| A chunk is complete or absent | Unique temp + streamed hash + file fsync + atomic rename + directory fsync precede manifest commit. `(upload_id,chunk_index)` is a primary key. | Kill mid-PUT; restart and inspect `received`; race exact/conflicting PUTs. |
| Exact chunk replay is idempotent; conflict cannot overwrite | The first manifest row wins inside `BEGIN IMMEDIATE`; existing size/hash returns 200 only on equality, otherwise 409. Rename occurs only for the winner. | Concurrent duplicates, then a different body at the same index. |
| Finalized bytes are immutable | Finalize freezes state before assembly. PUT can no longer create a manifest row; late PUTs return 409 and cannot touch chunk/blob files. | Race final chunk/finalize and PUT after finalize. |
| At most one artifact per upload | `uploads.state` transition occurs in a write transaction and `artifacts.upload_id` is UNIQUE. A racing caller gets 409 while active or the same ID after commit. | Barrier two finalize calls and count artifacts. |
| Mismatch creates no artifact and remains retryable | Size/count checked before freeze; streamed byte count/full SHA checked before blob metadata. On 422, state returns to `uploading`. | Finalize with bad size/hash, list, then retry correctly. |
| No corrupt or partial download response | Tenant/artifact lookup is repeated under the content lock; file is opened before releasing it. The entire open descriptor is streamed through SHA-256/size verification before headers, then rewound and streamed with exact Content-Length. POSIX open descriptors survive GC unlink. | Corrupt/truncate a blob; GET must return JSON 500, never artifact bytes. Race GET/delete/GC. |
| Finalize durability survives SIGKILL | Fsync staging file → persist prefix → atomic replace → fsync blob dir → one `synchronous=FULL` metadata/refcount/artifact commit → 2xx. | Kill at each boundary, restart, retry/list/download. |
| Refcount cannot diverge from artifacts | Artifact insert/refcount increment/upload completion share one transaction; delete/refcount decrement share one transaction and enforce `refcount>0`; startup recomputes counts from artifacts. | Crash around finalize/delete and compare admin counts to artifact references. |
| Finalize and GC cannot lose bytes | Same per-hash lock; GC rechecks zero inside its delete transaction. Finalize installs durable bytes before its reference transaction. | Delete last reference, race GC with identical finalize, then download. |
| GC crash cannot harm live bytes | Only zero-ref rows enter; lock plus transactional recheck; durable rename-to-trash before DB delete; trash unlink after commit; startup reconciliation. | SIGKILL during batches, restart, download every live artifact and rerun GC. |
| Validation does not stop the world | It holds a content lock only to open/check inode, hashes outside locks/transactions, and performs one brief batch metadata commit. No global validation lock is shared with requests. | Run validation over a full store while timing unrelated finalize/download. |
| Maintenance and distinct finalizes remain concurrent | 1 MiB streaming outside DB writes; per-hash locks; GC metadata batches are capped at 64; SQLite indexes cover tenant lists and hash/refcount lookups. | Concurrently finalize distinct multi-GiB contents; exercise 10,000 blobs and maintenance. |
| Per-operation memory stays below 512 MiB | Request/file paths use fixed 1 MiB buffers; chunks cap at 32 MiB on the wire without buffering; metadata-only 10k lists are the largest materialized collections. | Measure RSS during 10 GiB PUT/finalize/GET and 10k list/GC/validate. |
