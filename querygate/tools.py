"""The tool surface — ``run_select`` composes the three-layer read-only boundary (spec §5, §15).

``run_select(sql)`` is *the* product function: it runs the agent's SQL through

    Layer 1 — the SQL guard (:func:`querygate.guard.guard_select`, a pure whole-AST walk),
    Layer 2 — the read-only transaction (:func:`querygate.db.run_readonly`), and
    Layer 3 — the least-privilege DB role (the connection URL the txn uses),

serializes every cell to JSON-safe (Split 2), and returns a cited :class:`RunResult`. **Every**
outcome — ok, rejected, or errored — appends exactly one :class:`AuditLine` to the audit log
(spec §9), so the boundary is provable after the fact. The signature property of the product —
*the server cannot execute a write* — is what ``tests/test_boundary.py`` asserts at each layer
independently.

Importing this function **is** the boundary: there is no code path here that reaches a write.

Truncation signal (the conservative v1 choice, spec §6 ⚠). ``truncated`` is set when
``row_count == config.row_limit`` — i.e. the result came back exactly at the auto-LIMIT cap.
This *over-flags* the rare case of a query whose true result is exactly ``row_limit`` rows
(it is reported truncated even though nothing was dropped). For v1 this conservative
over-flagging is the accepted trade: it keeps the guard's injected ``LIMIT row_limit`` (Split 3)
unchanged and the citation SQL honest (``LIMIT 1000``, not ``LIMIT 1001``). The precise
alternative — fetch ``row_limit + 1`` and drop the extra — would change the injected LIMIT and
muddy the citation; it is recorded in PROGRESS.md as the road not taken.
"""

from __future__ import annotations

import time

from .audit import append_audit, now_rfc3339
from .config import Config
from .db import DBError, run_readonly
from .guard import guard_select
from .models import AuditLine, RunResult
from .result import SerializationError, serialize_rows

__all__ = ["run_select", "RunRejected"]


class RunRejected(Exception):
    """Layer 1 rejected the SQL — it never reached the database (spec §5).

    Carries the guard's machine ``rule`` tag and a legible ``reason`` so the caller (the MCP
    server, Split 6) can return a clean ``tool_result`` error and the agent can rephrase
    (spec §3 step 5: the agent sees the error, apologizes, does not retry the same SQL).
    """

    def __init__(self, rule: str, reason: str) -> None:
        super().__init__(reason)
        self.rule = rule
        self.reason = reason


def run_select(sql: str, *, config: Config | None = None) -> RunResult:
    """Run one read-only ``SELECT`` through all three boundary layers and cite the result.

    Returns a :class:`RunResult` on success. Raises :class:`RunRejected` if the guard rejects
    the SQL (Layer 1; the SQL is never sent to the DB), or :class:`~querygate.db.DBError` /
    :class:`~querygate.db.QueryTimeout` / :class:`~querygate.result.SerializationError` on a
    DB/serialization failure. **Every** outcome writes exactly one audit line first; this
    function never crashes the process — it raises a typed error the caller maps to a tool error.
    """
    cfg = config if config is not None else Config.from_env()
    tool = "run_select"
    args = {"sql": sql}
    started = time.monotonic()

    def _audit(status: str, *, row_count: int | None, error: str | None) -> None:
        append_audit(
            AuditLine(
                ts=now_rfc3339(),
                tool=tool,
                args=args,
                row_count=row_count,
                latency_ms=round((time.monotonic() - started) * 1000),
                status=status,
                error=error,
            ),
            cfg.audit_path,
        )

    # --- Layer 1: the SQL guard. On reject, the SQL is NOT sent to the DB. ---------------------
    decision = guard_select(sql, row_limit=cfg.row_limit)
    if not decision.ok:
        _audit("rejected", row_count=None, error=decision.reason)
        raise RunRejected(decision.rule or "rejected", decision.reason or "rejected by guard")

    approved_sql = decision.sql or sql

    # --- Layers 2 + 3: read-only transaction as the least-privilege role, then serialize. -----
    exec_start = time.monotonic()
    try:
        columns, raw_rows = run_readonly(approved_sql, config=cfg)
        rows = serialize_rows(raw_rows, columns, decimal_as_str=cfg.decimal_as_str)
    except (DBError, SerializationError) as exc:
        # Clean error envelope: audit it, re-raise typed — never let the process crash (spec §18).
        _audit("error", row_count=None, error=str(exc))
        raise
    elapsed_ms = round((time.monotonic() - exec_start) * 1000)

    row_count = len(rows)
    result = RunResult(
        columns=columns,
        rows=rows,
        row_count=row_count,
        sql=approved_sql,  # the EXACT SQL executed — the honest citation source.
        truncated=row_count == cfg.row_limit,  # conservative cap signal (see module docstring).
        elapsed_ms=elapsed_ms,
    )
    _audit("ok", row_count=row_count, error=None)
    return result
