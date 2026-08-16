"""SQLite connection management and migration application.

Per-operation connections: each unit of work opens its own connection, uses it,
and closes it in a finally. This gives a clean file-descriptor lifecycle under
concurrent load (no long-lived pooled connections to exhaust fds) while WAL +
busy_timeout let concurrent readers/writers make progress. Explicit transaction
control (isolation_level=None) so callers own their BEGIN/COMMIT boundaries.
"""

import sqlite3

from .config import Config

BUSY_TIMEOUT_MS = 10_000


def _pragmas(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")


def open(cfg: Config) -> sqlite3.Connection:
    """Open a fresh connection with VaultDrop pragmas. Caller must close it."""
    conn = sqlite3.connect(str(cfg.db_path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    _pragmas(conn)
    return conn


def _split_statements(sql: str) -> list[str]:
    """Split a SQL script into statements using sqlite3's own parser.

    Robust to semicolons inside comments and string literals.
    """
    out: list[str] = []
    buf: list[str] = []
    for line in sql.splitlines():
        buf.append(line)
        if sqlite3.complete_statement("\n".join(buf)):
            stmt = "\n".join(buf)
            if stmt.strip():
                out.append(stmt)
            buf = []
    if buf and "\n".join(buf).strip():
        out.append("\n".join(buf))
    return out


def _migration_files(cfg: Config) -> list:
    d = cfg.migrations_dir
    if not d.is_dir():
        return []
    return sorted(d.glob("*.sql"))


def migrate(cfg: Config) -> int:
    """Apply all migrations in filename order, each in its own transaction.

    Idempotent (IF NOT EXISTS / INSERT OR IGNORE), so re-running is a no-op.
    Returns the number of migration files applied.
    """
    conn = open(cfg)
    try:
        applied = 0
        for mf in _migration_files(cfg):
            sql = mf.read_text()
            conn.execute("BEGIN")
            try:
                for stmt in _split_statements(sql):
                    conn.execute(stmt)
                conn.execute("COMMIT")
                applied += 1
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        return applied
    finally:
        conn.close()


def current_revision(cfg: Config) -> str | None:
    conn = open(cfg)
    try:
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key='revision'"
        ).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()
