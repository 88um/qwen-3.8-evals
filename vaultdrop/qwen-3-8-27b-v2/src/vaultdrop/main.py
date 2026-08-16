"""VaultDrop CLI entry point."""

import json
import os
import signal
import sys

from . import core, db, httpd
from .store import Store


def _state_dir():
    sd = os.environ.get("VAULTDROP_STATE_DIR")
    if not sd:
        sys.stderr.write("vaultdrop: VAULTDROP_STATE_DIR is required\n")
        sys.exit(2)
    return sd


def _load_auth(sd):
    path = os.path.join(sd, "tenants.json")
    try:
        with open(path) as f:
            doc = json.load(f)
        tenants = doc["tenants"]
        admin_token = doc["admin_token"]
        out = []
        for t in tenants:
            if not isinstance(t.get("id"), str) or not isinstance(t.get("token"), str):
                raise ValueError("bad tenant entry")
            out.append(t)
        return httpd.Auth(out, admin_token)
    except (OSError, ValueError, KeyError, TypeError) as e:
        sys.stderr.write("vaultdrop: cannot load %s: %r\n" % (path, e))
        sys.exit(2)


def cmd_migrate():
    sd = _state_dir()
    db.migrate(sd)
    return 0


def cmd_serve():
    sd = _state_dir()
    port = int(os.environ.get("PORT", "8080"))
    auth = _load_auth(sd)
    db.migrate(sd)
    store = Store(sd)
    db.recover(sd, store.db)

    def _term(signum, frame):
        sys.stderr.write("[vd] signal %d; shutting down\n" % signum)
        sys.stderr.flush()
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _term)
    try:
        httpd.serve(store, auth, port)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write("usage: vaultdrop {serve|migrate}\n")
        return 0 if argv else 2
    cmd = argv[0]
    if cmd == "serve":
        return cmd_serve()
    if cmd == "migrate":
        return cmd_migrate()
    sys.stderr.write("vaultdrop: unknown command %r\n" % cmd)
    return 2
