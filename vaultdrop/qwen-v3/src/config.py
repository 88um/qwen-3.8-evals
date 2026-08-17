"""VaultDrop configuration: limits, paths, tunables.

Every limit stated here is enforced on the wire (see DECISIONS.md, claims
register). Values are at or above the scale-envelope minimums of product.md
section 2.
"""

import os

# --- State layout -----------------------------------------------------------

STATE_DIR = os.environ.get("VAULTDROP_STATE_DIR") or os.path.abspath(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "state")
)
DB_PATH = os.path.join(STATE_DIR, "vaultdrop.db")
BLOBS_DIR = os.path.join(STATE_DIR, "blobs")
CHUNKS_DIR = os.path.join(STATE_DIR, "chunks")
STAGING_DIR = os.path.join(STATE_DIR, "staging")
TENANTS_PATH = os.path.join(STATE_DIR, "tenants.json")

# --- Wire limits (scale envelope: product.md section 2) ---------------------

MAX_CHUNK_SIZE = 32 * 1024 * 1024          # 32 MiB per chunk PUT body
MAX_ARTIFACT_SIZE = 10 * 1024 * 1024 * 1024  # 10 GiB per artifact
MAX_NAME_LEN = 255

# --- I/O --------------------------------------------------------------------

IO_BLOCK = 1024 * 1024  # streaming block size; bounds per-request memory

# --- Concurrency tunables ----------------------------------------------------

SQLITE_BUSY_TIMEOUT_S = 30.0   # writers serialize here; all write txns are short
FINALIZE_LOSER_POLL_MS = 50    # loser of a finalize race polls for the winner
FINALIZE_LOSER_TIMEOUT_S = 600
GC_WAIT_POLL_MS = 50           # finalize blocked behind an in-flight GC pass
GC_WAIT_TIMEOUT_S = 300
ORPHAN_SETTLE_MS = 2000        # settle window distinguishing a concurrent
                               # same-content finalize from a crashed orphan

# --- Misc --------------------------------------------------------------------

HTTP_TIMEOUT_S = 300.0
