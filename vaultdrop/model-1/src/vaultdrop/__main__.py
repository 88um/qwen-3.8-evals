"""Command-line entrypoint: ``python -m vaultdrop <serve|migrate>``."""

import sys

from . import db, store
from .config import Config
from .httpd import make_server
from .tenants import load as load_tenants


def main(argv) -> int:
    if not argv:
        print("usage: vaultdrop <serve|migrate>", file=sys.stderr)
        return 2
    cmd = argv[0]
    cfg = Config()

    if cmd == "migrate":
        cfg.ensure_layout()
        n = db.migrate(cfg)
        print(f"migrated: {n} file(s), revision={db.current_revision(cfg)}")
        return 0

    if cmd == "serve":
        cfg.ensure_layout()
        db.migrate(cfg)                       # self-init (idempotent)
        if not cfg.tenants_path.exists():
            print(f"error: missing {cfg.tenants_path}", file=sys.stderr)
            return 1
        registry = load_tenants(cfg.tenants_path)
        store.cleanup_orphans(cfg)            # reclaim crashed staged writes
        server = make_server(cfg, registry)
        print(f"vaultdrop serving on :{cfg.port} state={cfg.state_dir}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
