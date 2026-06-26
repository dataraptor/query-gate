"""``python -m querygate`` → the QueryGate CLI (spec §15).

Bare ``python -m querygate`` runs the stdio MCP server (the default); the ``seed`` / ``query`` /
``eval`` subcommands and the ``--http`` flag are dispatched by :func:`querygate.cli.main`.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
