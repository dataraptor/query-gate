"""querygate.api — the web-demo backend adapter (Split 11).

A thin localhost adapter that runs the **real** agent loop over the live QueryGate boundary and
streams events in the shapes the existing ``app/`` UI already renders. Split 12 wires the UI to it.

* :mod:`~querygate.api.mapping`      — the one place backend shapes (``RunResult`` / ``AuditLine`` /
  boundary outcome) become UI shapes (citation / tool-step / audit-line). No DB, no web, no key.
* :mod:`~querygate.api.agent`        — drive one question through the loop, streaming UI events.
* :mod:`~querygate.api.eval_summary` — the latest Split-09 eval run in the UI's Eval-tab shape.
* :mod:`~querygate.api.server`       — the Starlette app: ``POST /api/ask`` (NDJSON), ``GET /api/eval``,
  and the unmodified ``app/`` served static. ``querygate web`` brings it all up on localhost.
"""

from . import agent, eval_summary, mapping

__all__ = ["agent", "eval_summary", "mapping", "create_app", "serve"]


def create_app(config=None):
    """Lazy re-export of :func:`querygate.api.server.create_app` (keeps Starlette import optional)."""
    from .server import create_app as _create_app

    return _create_app(config)


def serve(host: str = "127.0.0.1", port: int = 8000, config=None) -> None:
    """Lazy re-export of :func:`querygate.api.server.serve`."""
    from .server import serve as _serve

    return _serve(host=host, port=port, config=config)
