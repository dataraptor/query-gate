"""The ``querygate`` command line (spec §15, §12).

One binary, four jobs:

* ``querygate``                       — run the MCP server over **stdio** (the default; Split 06).
* ``querygate --http [--port 8000]``  — run over Streamable HTTP (localhost). *Impl is Split 10*;
  until then this flag fails **cleanly** ("arrives in Split 10"), never with a traceback.
* ``querygate seed [--reset]``        — (re)build the synthetic DB from the fixed seed (Split 01),
  connecting as the **admin** ``DATABASE_URL`` (seeding needs the privileged role).
* ``querygate query "<SELECT ...>"``  — run one SELECT through the **full three-layer boundary** by
  hand and print the cited :class:`~querygate.models.RunResult` (or ``--json``). A write visibly
  **refuses** and exits non-zero — the by-hand boundary demo (the Session-1/3 "verified by hand" DoD).
  Connects as the **read-only** role (``QUERYGATE_DATABASE_URL``), never the admin URL.
* ``querygate eval [...]``            — the grounding eval. *Impl is Split 09*; here it delegates to
  ``evals/run_eval.py`` if present, else prints an honest "arrives in Split 09" and exits non-zero —
  it never prints invented metrics.

Framework: the stdlib :mod:`argparse` (no extra dependency; the surface is small and the §15 shape
maps onto it directly). Errors are surfaced honestly with a clear message + a non-zero exit, never a
raw traceback (spec §18/§20).
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .db import DBError
from .result import SerializationError
from .tools import RunRejected, ToolRejected, run_select

REPO_ROOT = Path(__file__).resolve().parent.parent
SEED_SCRIPT = REPO_ROOT / "scripts" / "seed.py"
EVAL_SCRIPT = REPO_ROOT / "evals" / "run_eval.py"

# Exit codes. 0 = ok. The headline refusal (a write blocked at the guard) and the unimplemented
# surfaces (--http / eval) exit 2; a DB/config/runtime failure exits 3. Anything non-zero satisfies
# the "visibly fails" contract; the split distinctions just make scripting precise.
EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_UNIMPLEMENTED = 2
EXIT_ERROR = 3


# ==================================================================================================
# Transport runners (stdio now; HTTP is the Split-10 seam).
# ==================================================================================================


def _serve_stdio() -> None:
    """Run the MCP server over stdio — delegates to the Split-06 entry point (no logic here)."""
    from . import server

    server.main()


def _serve_http(port: int) -> None:
    """The Streamable-HTTP transport seam (spec §12). Filled in by Split 10.

    Until then this raises a typed, legible :class:`NotImplementedError` that :func:`main` turns into
    a clean message + non-zero exit (R4) — never a traceback. When Split 10 lands it replaces this
    body with the FastMCP HTTP runner; the CLI dispatch above needs no change.
    """
    raise NotImplementedError(
        "the Streamable-HTTP transport arrives in Split 10 - for now run the default stdio server "
        "(`querygate`) or use Claude Desktop's stdio config. (requested port: %d)" % port
    )


# ==================================================================================================
# seed — delegate to scripts/seed.py (never duplicate the seed logic).
# ==================================================================================================


def _load_seed_module():
    """Import ``scripts/seed.py`` by path (it is a script dir, not an installed package)."""
    spec = importlib.util.spec_from_file_location("querygate_seed", SEED_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _cmd_seed(args: argparse.Namespace) -> int:
    """(Re)build the synthetic DB as the **admin** role and print the row-count summary (R2)."""
    import psycopg

    # Seeding needs the privileged role — the admin DATABASE_URL, NOT the read-only QUERYGATE one.
    admin_url = args.database_url or os.environ.get("DATABASE_URL")
    if not admin_url:
        print(
            "error: no admin connection string — set $DATABASE_URL (the privileged role used to "
            "build the DB; the read-only QUERYGATE_DATABASE_URL cannot seed) or pass --database-url",
            file=sys.stderr,
        )
        return EXIT_ERROR

    seed_module = _load_seed_module()
    # `seed()` always truncates-and-reloads (idempotent, byte-identical — Split 01 R3); the --reset
    # flag is accepted for §15 parity and makes the reset explicit.
    with psycopg.connect(admin_url) as conn:
        counts = seed_module.seed(conn, reset=True)

    total = sum(counts.values())
    summary = ", ".join(f"{k}={v}" for k, v in counts.items())
    print(f"seeded {total} rows ({'reset' if args.reset else 'reloaded'}): {summary}")
    return EXIT_OK


# ==================================================================================================
# query — the by-hand boundary harness over run_select (R3).
# ==================================================================================================


def _format_run_result(result) -> str:
    """Render a :class:`RunResult` as a legible columns+rows table plus its cited metadata."""
    cols = result.columns
    cells = [[("" if v is None else str(v)) for v in row] for row in result.rows]
    widths = [len(c) for c in cols]
    for row in cells:
        for i, c in enumerate(row):
            widths[i] = max(widths[i], len(c))

    def _fmt(row: list[str]) -> str:
        return "  ".join(c.ljust(widths[i]) for i, c in enumerate(row))

    lines = [
        _fmt(cols),
        "  ".join("-" * w for w in widths),
        *(_fmt(row) for row in cells),
    ]
    if not cells:
        lines.append("(0 rows)")

    redactions = ", ".join(result.redactions) if result.redactions else "(none)"
    meta = [
        "",
        f"row_count       : {result.row_count}",
        f"truncated       : {result.truncated}    truncated_bytes : {result.truncated_bytes}",
        f"redactions      : {redactions}",
        f"elapsed_ms      : {result.elapsed_ms}",
        f"sql             : {result.sql}",
    ]
    return "\n".join(lines + meta)


def _cmd_query(args: argparse.Namespace) -> int:
    """Run one SELECT through ``run_select`` (Layer 1→2→3 + filter) and print it, or refuse (R3)."""
    cfg = Config.from_env()
    try:
        cfg.require_database_url()  # the read-only role's URL — fail clearly if it is unset.
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    try:
        result = run_select(args.sql, config=cfg)
    except (RunRejected, ToolRejected) as exc:
        # The headline by-hand refusal: the guard rejected the SQL and it never reached the DB.
        print(f"REJECTED [{exc.rule}]: {exc.reason}", file=sys.stderr)
        return EXIT_REJECTED
    except (DBError, SerializationError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    if args.json:
        print(result.model_dump_json(indent=2))
    else:
        print(_format_run_result(result))
    return EXIT_OK


# ==================================================================================================
# eval — the Split-09 seam (honest stub; never prints fabricated metrics, R5).
# ==================================================================================================


def _cmd_eval(args: argparse.Namespace) -> int:
    if EVAL_SCRIPT.exists():
        # Split 09 landed the real harness — delegate to it, forwarding the parsed flags verbatim.
        argv = [sys.executable, str(EVAL_SCRIPT)]
        if args.repeats is not None:
            argv += ["--repeats", str(args.repeats)]
        if args.quick:
            argv += ["--quick"]
        if args.model is not None:
            argv += ["--model", args.model]
        if args.out is not None:
            argv += ["--out", args.out]
        return subprocess.call(argv)

    print(
        "error: the grounding eval arrives in Split 09 (evals/run_eval.py is not present yet). "
        "No metrics are reported by this stub - it never prints invented numbers.",
        file=sys.stderr,
    )
    return EXIT_UNIMPLEMENTED


# ==================================================================================================
# Argument parser + dispatch.
# ==================================================================================================


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="querygate",
        description="QueryGate — a read-only MCP server over Postgres (run server / seed / query).",
    )
    parser.add_argument("--version", action="version", version=f"querygate {__version__}")
    # Top-level transport selection: bare `querygate` = stdio; `--http` selects Streamable HTTP.
    parser.add_argument(
        "--http", action="store_true",
        help="run the server over Streamable HTTP (localhost) instead of stdio [Split 10]",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="port for --http (default: 8000)",
    )

    sub = parser.add_subparsers(dest="command", metavar="{seed,query,eval}")

    s = sub.add_parser("seed", help="(re)build the synthetic DB from the fixed seed")
    s.add_argument("--reset", action="store_true", help="truncate and reload (seeding always resets)")
    s.add_argument(
        "--database-url", default=None,
        help="admin connection string (defaults to $DATABASE_URL)",
    )
    s.set_defaults(func=_cmd_seed)

    q = sub.add_parser("query", help="run one SELECT through the full boundary and print RunResult")
    q.add_argument("sql", help="the SELECT statement to run (writes are rejected)")
    q.add_argument("--json", action="store_true", help="print the raw RunResult JSON (for scripting)")
    q.set_defaults(func=_cmd_query)

    e = sub.add_parser("eval", help="run the grounding eval [Split 09]")
    e.add_argument("--repeats", type=int, default=None, help="repeats per question")
    e.add_argument("--quick", action="store_true", help="fast smoke subset")
    e.add_argument("--model", default=None, help="model id under test")
    e.add_argument("--out", default=None, help="output JSONL path")
    e.set_defaults(func=_cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Returns a process exit code (0 ok, non-zero on refusal/error)."""
    parser = _build_parser()
    args = parser.parse_args(argv)

    # No subcommand → run the server. `--http` selects the transport (Split-10 seam).
    if args.command is None:
        if args.http:
            try:
                _serve_http(args.port)
            except NotImplementedError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return EXIT_UNIMPLEMENTED
            return EXIT_OK
        _serve_stdio()
        return EXIT_OK

    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover - thin entrypoint
    raise SystemExit(main())
