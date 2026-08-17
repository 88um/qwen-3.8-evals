"""VaultDrop entry point: `serve` and `migrate`."""

import os
import sys

import config
import db
import httpd


def cmd_serve():
    db.bootstrap()
    port = os.environ.get("PORT", "8080")
    try:
        port = int(port)
    except ValueError:
        sys.stderr.write("PORT must be an integer\n")
        return 2
    httpd.serve(port)
    return 0


def cmd_migrate():
    db.bootstrap()
    return 0


def main(argv):
    if len(argv) != 2 or argv[1] not in ("serve", "migrate"):
        sys.stderr.write("usage: vaultdrop {serve|migrate}\n")
        return 2
    if argv[1] == "serve":
        return cmd_serve()
    return cmd_migrate()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
