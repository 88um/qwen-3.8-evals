#!/usr/bin/env python3
import json
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(__file__))

from db import Database
from store import Store
from server import VaultDropServer


def main():
    if len(sys.argv) < 2:
        print("Usage: vaultdrop <serve|migrate>", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    state_dir = os.environ.get('VAULTDROP_STATE_DIR', os.path.join(os.getcwd(), 'state'))
    os.makedirs(state_dir, exist_ok=True)

    if command == 'migrate':
        db = Database(state_dir)
        db.migrate()
        sys.exit(0)

    if command == 'serve':
        port = int(os.environ.get('PORT', '8080'))

        tenants_path = os.path.join(state_dir, 'tenants.json')
        with open(tenants_path) as f:
            tenants_config = json.load(f)

        db = Database(state_dir)
        db.migrate()
        db.recover()

        store = Store(state_dir)
        store.cleanup_tmp()

        server = VaultDropServer(port, state_dir, tenants_config, db, store)

        def handle_signal(signum, frame):
            server.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        print(f"VaultDrop serving on :{port}", file=sys.stderr, flush=True)
        server.serve_forever()
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
