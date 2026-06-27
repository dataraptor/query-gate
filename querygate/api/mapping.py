"""querygate/api/mapping.py — the ONE place backend shapes become UI shapes (Split 11 R2).

Pure: no DB, no web, no model key. Every name/shape translation between the QueryGate library
(:class:`~querygate.models.RunResult` / :class:`~querygate.models.AuditLine` / the real boundary
outcome) and the shapes the **existing** ``app/`` UI already renders lives here, so Split 12 (the
UI wiring) and any future reader have a single source of truth.

The UI shapes are *not invented here* — they are read off the dummy engine in
``app/QueryGate Demo.dc.html`` (``scenarios()`` / ``resolve()`` / ``finish()`` / ``traceView()`` /
``auditView()`` / ``boundaryView()``). See PROGRESS.md "Split 11 backend->UI mapping table" for the
full field map. Do not add a field the UI does not consume.
"""

from __future__ import annotations

from typing import Any

#: Tools whose step carries a boundary verdict in the UI. Mirrors ``traceView()``'s
#: ``isRun = tool==='run_select' || tool==='search_text'`` — the boundary-bearing read tools.
BOUNDARY_TOOLS = ("run_select", "search_text")

#: Pricing per MTok ``(input_usd, output_usd)`` — mirrors ``evals/run_eval.PRICING`` (the single
#: pinned source). Used only for the per-answer ``cost`` increment the UI top bar shows. A model
#: with no entry prices at 0 (never a silently fabricated cost).
PRICING: dict[str, tuple[float, float]] = {
    "gpt-5.5": (5.0, 30.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-4-6": (3.0, 15.0),
}


def cost_usd(usage: dict[str, int], model: str) -> float:
    """Per-answer cost in USD from token usage (UI top-bar ``cost`` increment)."""
    pin, pout = PRICING.get(model, (0.0, 0.0))
    return round(usage.get("input", 0) / 1e6 * pin + usage.get("output", 0) / 1e6 * pout, 6)


def boundary_verdict(tool: str, status: str, reason: str | None) -> dict[str, str] | None:
    """Derive the UI boundary verdict ``{mode,l1,l2,l3,reason?}`` from the **real** tool outcome.

    Only ``run_select`` / ``search_text`` carry a verdict (the boundary-bearing tools — mirrors
    ``traceView()``/``boundaryView()``). The verdict is **derived, never hard-coded** (Split 11 R2/W-5):

    * ``ok``       → ``pass / pass / pass``            — ran read-only through all three layers.
    * ``rejected`` → ``reject(L1) / ghost / ghost``    — Layer 1 (guard / allowlist) stopped it; the
      read-only txn (L2) and the least-privilege role (L3) were never reached → ghost.
    * ``error``    → ``pass(L1) / error(L2) / '·'``    — passed the guard, failed at the DB (a SQL
      error or ``statement_timeout`` — both surface as ``error`` at Layer 2); L3 not evaluated.

    ``l1/l2/l3 ∈ {'pass','reject','ghost','error','·'}`` — exactly the marks ``boundaryView()`` maps.
    """
    if tool not in BOUNDARY_TOOLS:
        return None
    if status == "ok":
        return {"mode": "pass", "l1": "pass", "l2": "pass", "l3": "pass"}
    if status == "rejected":
        return {"mode": "reject", "l1": "reject", "l2": "ghost", "l3": "ghost", "reason": reason or ""}
    # status == "error": DBError / QueryTimeout / SerializationError.
    return {"mode": "error", "l1": "pass", "l2": "error", "l3": "·", "reason": reason or ""}


def running_step(tool: str, args: dict) -> dict:
    """The ``status:'running'`` tool-step emitted *before* a tool resolves (so the UI animates)."""
    return {"tool": tool, "args": args, "status": "running"}


def tool_step(
    tool: str,
    args: dict,
    status: str,
    *,
    latency_ms: int | None,
    row_count: int | None,
    error: str | None,
    redactions: list[str],
) -> dict:
    """A resolved tool-step in the UI's ``steps[]``/``traceView()`` shape.

    Fields the UI binds to: ``tool, args, status, latency, rowCount, error?, phi?, boundary?``.
    ``boundary`` is attached only for the boundary-bearing tools, derived from the real outcome.
    """
    step: dict[str, Any] = {
        "tool": tool,
        "args": args,
        "status": status,  # 'ok' | 'rejected' | 'error'
        "latency": latency_ms,
        "rowCount": row_count,
        "phi": bool(redactions),
    }
    if error:
        step["error"] = error  # shown in the trace + carried as the reject/err reason
    bv = boundary_verdict(tool, status, error)
    if bv is not None:
        step["boundary"] = bv
    return step


def audit_line_to_ui(line: dict) -> dict:
    """Project a real :class:`~querygate.models.AuditLine` (dict) to the Audit-tab shape.

    The library already wrote the line in the right shape (spec §7); the UI's ``auditView()`` shows
    ``JSON.stringify(line)``. We pass the **real** line through, selecting exactly the fields the UI
    binds to (``ts, tool, args, row_count, latency_ms, status, error?, redactions``) — never synthesize.
    """
    ui: dict[str, Any] = {
        "ts": line["ts"],
        "tool": line["tool"],
        "args": line["args"],
        "row_count": line.get("row_count"),
        "latency_ms": line.get("latency_ms"),
        "status": line["status"],
        "redactions": line.get("redactions", []),
    }
    if line.get("error"):
        ui["error"] = line["error"]
    return ui


def result_to_citation(result: dict, *, row_limit: int) -> dict:
    """Map a :class:`~querygate.models.RunResult` (dict) → the UI ``citation`` (the rename map, R2).

    The backend uses ``row_count`` / ``elapsed_ms`` / ``redactions``; the UI's citation uses
    ``rowCount`` / ``elapsed`` / ``phiCols``. **The adapter does the mapping here, in this one place.**

    * ``row_count``  → ``rowCount``
    * ``elapsed_ms`` → ``elapsed``
    * ``redactions`` → ``phiCols``    (the redacted output columns from the result filter, Split 05)
    * ``limit``      = the auto-LIMIT (``config.row_limit``) — exposed for the UI's "LIMIT n" chip.
    * ``total``      = ``rowCount`` (honest default). A separate ``count(*)`` for a group-by "grand
      total" is **not** issued — that would be an extra query and the split permits ``total=rowCount``
      when a real total isn't available. Documented in PROGRESS.md.
    """
    return {
        "sql": result["sql"],
        "columns": result["columns"],
        "rows": result["rows"],
        "rowCount": result["row_count"],
        "total": result["row_count"],  # honest default (no separate count(*)); see PROGRESS.md
        "elapsed": result["elapsed_ms"],
        "limit": row_limit,
        "truncated": result["truncated"],
        "phiCols": result.get("redactions", []),
    }


def answer_message(prose: str, citation: dict) -> dict:
    """The UI's final ``{kind:'answer', prose, citation}`` message."""
    return {"kind": "answer", "prose": prose, "citation": citation}


def refusal_message(prose: str, reason: str) -> dict:
    """The UI's final ``{kind:'refusal', prose, reason}`` message — the headline boundary refusal."""
    return {"kind": "refusal", "prose": prose, "reason": reason}
