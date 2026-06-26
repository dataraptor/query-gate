"""``python -m querygate`` → launch the stdio MCP server (spec §12-A / §15).

The default, no-arg behavior is the stdio server. The full CLI surface (``--http``, ``seed``,
``query``, ``eval``) arrives in Split 07; until then this and the ``querygate`` console script both
resolve to :func:`querygate.server.main`.
"""

from .server import main

if __name__ == "__main__":
    main()
