"""In-process coordination locks.

Two locks, with a strict acquisition order (per-upload first, then GC) so no
deadlock is possible:

* GC_LOCK — serializes finalize's blob-claim/create against GC collection and
  validation. This is the finalize-vs-GC coordination (see DECISIONS.md). It is
  held across the DB transaction AND the filesystem effects, so the two actors
  can never interleave between a decision and its effect.

* per-upload locks — serialize chunk writes and finalizes for one upload, making
  conflicting-replay resolution deterministic (first-write-wins) and keeping a
  finalize from racing its own late chunks. Different uploads stay concurrent.
"""

import threading

_gc_lock = threading.Lock()

_registry_lock = threading.Lock()
_upload_locks: dict[str, threading.Lock] = {}


def gc_lock() -> threading.Lock:
    return _gc_lock


def upload_lock(upload_id: str) -> threading.Lock:
    with _registry_lock:
        lk = _upload_locks.get(upload_id)
        if lk is None:
            lk = threading.Lock()
            _upload_locks[upload_id] = lk
        return lk


def release_upload_lock(upload_id: str) -> None:
    """Drop a per-upload lock once its upload can no longer be mutated.

    Called after finalize/delete. Bounded memory: locks are tiny and pruned as
    uploads retire.
    """
    with _registry_lock:
        _upload_locks.pop(upload_id, None)
