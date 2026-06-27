"""querygate/api/server.py — the localhost web-demo adapter (Split 11 R1/R3/R4).

Mirrors Relay's ``api/`` layer: an **adapter, not a brain**. Three jobs, zero boundary logic:

* ``POST /api/ask``  — run one question through the **real** agent loop (:mod:`querygate.api.agent`)
  and **stream** the UI-shaped events as NDJSON (``application/x-ndjson``, one JSON object per line).
* ``GET  /api/eval`` — the latest Split-09 eval summary (:mod:`querygate.api.eval_summary`), honest.
* the unmodified ``app/`` directory, served static so the whole demo is **one command**
  (``querygate web``) — the §8 "one command" ethos. This split must not edit the UI; it only serves it.

**Stream protocol = NDJSON** (chosen over SSE because ``/api/ask`` is a POST with a JSON body, which a
browser ``EventSource`` cannot do; a streaming ``fetch`` + line reader consumes NDJSON cleanly). Each
line is one event object with a ``type``: ``step-start`` / ``step-end`` / ``audit-line`` / ``message``.
Split 12 consumes these verbatim — the shapes are documented in PROGRESS.md.

Localhost-only, no auth (§19) — the demo never leaves the loopback interface.
"""

from __future__ import annotations

import dataclasses
import json
import tempfile
from pathlib import Path
from typing import Iterator

import anyio
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from querygate.config import Config

from . import agent, eval_summary

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
APP_DIR = _REPO_ROOT / "app"
INDEX_FILE = "QueryGate Demo.dc.html"
DEMO_REDACT = _HERE / "redact_demo.yaml"

#: Localhost-only bind (§19). Bearer auth + a non-loopback bind are the §21 roadmap, not v1.
HOST = "127.0.0.1"
PORT = 8000


def _request_config(base: Config, *, redaction: bool) -> Config:
    """A per-request :class:`Config`: the **read-only** role URL + a fresh per-run audit file.

    A dedicated audit file per request means the adapter reads back exactly the lines this run wrote
    (one per tool call — W-8) without racing other requests. ``redaction`` swaps in the demo
    redact config so the UI's PHI toggle is backed by the real result filter, not a UI trick.
    """
    fd, audit_path = tempfile.mkstemp(prefix="qg_web_audit_", suffix=".jsonl")
    import os

    os.close(fd)
    redact_path = str(DEMO_REDACT) if redaction else None
    return dataclasses.replace(base, audit_path=audit_path, redact_path=redact_path)


async def _ndjson_stream(events: Iterator[dict]):
    """Adapt the sync event generator to an async NDJSON byte stream.

    Each blocking ``next(events)`` (an LLM round-trip or a DB query) runs in a worker thread so it
    never blocks the event loop; each event is emitted as one ``json`` line.
    """
    it = iter(events)
    sentinel = object()
    while True:
        item = await anyio.to_thread.run_sync(lambda: next(it, sentinel))
        if item is sentinel:
            break
        yield (json.dumps(item, default=str) + "\n").encode("utf-8")


def create_app(config: Config | None = None) -> Starlette:
    """Build the demo Starlette app. ``config`` defaults to :meth:`Config.from_env` (read-only role)."""
    base_config = config if config is not None else Config.from_env()

    async def ask(request: Request) -> StreamingResponse | JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "request body must be JSON"}, status_code=400)
        question = (body or {}).get("question")
        if not question or not isinstance(question, str):
            return JSONResponse({"error": "missing 'question' (a non-empty string)"}, status_code=400)
        model = (body or {}).get("model") or None
        redaction = bool((body or {}).get("redaction", False))

        try:
            cfg = _request_config(base_config, redaction=redaction)
            cfg.require_database_url()
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)

        events = agent.stream_run(question, config=cfg, model=model)
        return StreamingResponse(_ndjson_stream(events), media_type="application/x-ndjson")

    async def eval_route(_: Request) -> JSONResponse:
        return JSONResponse(eval_summary.latest_eval_summary())

    async def index(_: Request) -> FileResponse | JSONResponse:
        index_path = APP_DIR / INDEX_FILE
        if index_path.is_file():
            return RedirectResponse(url=f"/app/{INDEX_FILE}")
        return JSONResponse({"service": "querygate-web", "api": ["/api/ask", "/api/eval"]})

    routes = [
        Route("/", index, methods=["GET"]),
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/eval", eval_route, methods=["GET"]),
    ]
    if APP_DIR.is_dir():
        # Mounted at /app so the UI's relative ./support.js resolves and the /api/* routes are never
        # shadowed. StaticFiles serves the files byte-for-byte (this split does not rewrite the UI).
        routes.append(Mount("/app", app=StaticFiles(directory=str(APP_DIR)), name="app"))

    return Starlette(routes=routes)


def serve(host: str = HOST, port: int = PORT, config: Config | None = None) -> None:
    """Run the demo over Streamable HTTP on ``host:port`` (blocks). One command for the whole demo."""
    import uvicorn

    uvicorn.run(create_app(config), host=host, port=port, log_level="info")
