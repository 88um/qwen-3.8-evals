"""Runtime configuration: state-dir resolution, limits, derived paths.

All persistent state lives under $VAULTDROP_STATE_DIR. Limits are fixed here and
stated in DECISIONS.md; they are enforced at the HTTP boundary.
"""

import os
from pathlib import Path

# --- Limits (see DECISIONS.md, "Limits") -----------------------------------
MAX_CHUNK_SIZE = 16 * 1024 * 1024        # 16 MiB per chunk PUT body
MAX_ARTIFACT_SIZE = 64 * 1024 * 1024     # 64 MiB per artifact
MAX_NAME_LEN = 512                       # upload/artifact name length
MAX_CONCURRENT_GC_BLOBS = None           # None = unbounded (single pass)

# --- State-dir layout -------------------------------------------------------
#   <state>/vaultdrop.db          SQLite metadata (WAL)
#   <state>/blobs/<hex>           content-addressed byte blobs (immutable)
#   <state>/chunks/<upload>/<i>   staged upload chunks (intermediate)
#   <state>/tmp/                  durable-write staging (temp + fsync + rename)
#   <state>/trash/                GC-collected blobs (removed after pass)


class Config:
    def __init__(self, state_dir: str | None = None):
        self.state_dir = Path(
            state_dir or os.environ.get("VAULTDROP_STATE_DIR") or "."
        ).resolve()
        self.port = int(os.environ.get("PORT", "8080"))

        self.db_path = self.state_dir / "vaultdrop.db"
        self.blobs_dir = self.state_dir / "blobs"
        self.chunks_dir = self.state_dir / "chunks"
        self.tmp_dir = self.state_dir / "tmp"
        self.trash_dir = self.state_dir / "trash"
        self.tenants_path = self.state_dir / "tenants.json"

        # Migration SQL lives at <root>/migrations, where <root> is two levels
        # above this package (src/vaultdrop -> src -> root).
        self.migrations_dir = Path(__file__).resolve().parents[2] / "migrations"

    def ensure_layout(self) -> None:
        for d in (self.state_dir, self.blobs_dir, self.chunks_dir,
                  self.tmp_dir, self.trash_dir):
            d.mkdir(parents=True, exist_ok=True)


def blob_path(cfg: Config, content_hash: str) -> Path:
    return cfg.blobs_dir / content_hash


def chunk_path(cfg: Config, upload_id: str, index: int) -> Path:
    return cfg.chunks_dir / upload_id / str(index)
