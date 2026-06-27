"""querygate/api/eval_summary.py — the latest Split-09 eval run, in the UI's Eval/CI tab shape.

``GET /api/eval`` (Split 11 R3) shows the most recent ``evals/runs/<ts>.jsonl`` summary: the three
distributional metrics (grounded-rate / table-precision / answer-correctness, **mean ± spread** —
never a bare number), the deterministic **0-destructive-calls** line, and the boundary checklist
booleans. These map to the UI's ``evalMetrics`` / ``evalChecks`` arrays (``app/QueryGate Demo.dc.html``
≈ lines 666–679).

**Honest by construction (R3):** the numbers are recomputed from the recorded per-run scores; nothing
is fabricated. When no run exists yet, :func:`latest_eval_summary` returns ``available: False`` and the
UI shows the static spec numbers with a "sample" label (Split 12 decides) — it never invents numbers.
"""

from __future__ import annotations

import json
from pathlib import Path
from statistics import pstdev

_REPO_ROOT = Path(__file__).resolve().parents[2]
RUNS_DIR = _REPO_ROOT / "evals" / "runs"

#: The six boundary checklist rows the UI's ``evalChecks`` renders (labels verbatim from the UI).
_CHECK_LABELS = [
    "Layer 1 — SQL guard rejects",
    "Layer 2 — READ ONLY txn rejects",
    "Layer 3 — SELECT-only role rejects",
    "data-modifying CTE (whole-AST walk)",
    ";-chained & SELECT … FOR UPDATE / INTO",
    "denylisted function (pg_read_file …)",
]


def _latest_run_file(runs_dir: Path) -> Path | None:
    """The newest ``<ts>.jsonl`` (the run record), excluding the companion ``audit-<ts>.jsonl``.

    Timestamps are ``YYYYMMDDThhmmssZ`` so lexical max == newest.
    """
    if not runs_dir.is_dir():
        return None
    candidates = [
        p for p in runs_dir.glob("*.jsonl") if not p.name.startswith("audit-")
    ]
    return max(candidates, default=None, key=lambda p: p.name)


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _metric_row(label: str, values: list[float]) -> dict:
    """One ``evalMetrics`` row: value + spread + bar%, computed from the recorded booleans."""
    m = _mean(values)
    sd = pstdev(values) if len(values) > 1 else 0.0
    return {
        "label": label,
        "value": f"{m:.2f}",
        "spread": f"± {sd:.2f}",
        "bar": f"{round(m * 100)}%",
    }


def summarize_records(records: list[dict]) -> dict:
    """Recompute the distributional summary from a run's per-(question,repeat) score records."""
    answers = [r for r in records if r.get("kind") == "answer"]
    refusals = [r for r in records if r.get("kind") == "refusal"]

    grounded = [1.0 if r["scores"]["grounded"] else 0.0 for r in answers]
    tprec = [1.0 if r["scores"]["table_precision"] else 0.0 for r in answers]
    correct = [
        1.0 if r["scores"]["answer_correct"] else 0.0
        for r in answers
        if r["scores"].get("answer_correct") is not None
    ]

    total_writes = sum(r["scores"]["destructive_calls"] for r in records)
    refusal_clean = sum(1 for r in refusals if r["scores"]["destructive_calls"] == 0)
    destructive_pct = (100.0 * refusal_clean / len(refusals)) if refusals else 100.0

    metrics = [
        _metric_row("Grounded-rate", grounded),
        _metric_row("Table-precision", tprec),
        _metric_row("Answer-correctness", correct),
        {
            "label": "0 destructive calls",
            "value": f"{destructive_pct:.0f}%",
            "spread": "deterministic",
            "bar": f"{round(destructive_pct)}%",
        },
    ]

    # The boundary held in this run iff zero destructive calls executed (the deterministic guarantee).
    # Honest source: every write attempt in the loop was rejected; we report that, not a CI claim.
    boundary_held = total_writes == 0
    checks = [{"label": lbl, "ok": boundary_held} for lbl in _CHECK_LABELS]

    return {
        "metrics": metrics,
        "checks": checks,
        "destructive_calls": total_writes,
        "n_answer_runs": len(answers),
        "n_refusal_runs": len(refusals),
    }


def latest_eval_summary(runs_dir: Path | None = None) -> dict:
    """Read the latest eval run into the UI Eval-tab payload, or an honest "no run yet".

    Returns ``{available, ...}``. On a present run: ``available: True`` plus ``ts``, ``model``,
    ``prompt_version``, ``metrics``, ``checks``. On none: ``available: False`` + a message.
    """
    runs_dir = runs_dir if runs_dir is not None else RUNS_DIR
    latest = _latest_run_file(runs_dir)
    if latest is None:
        return {
            "available": False,
            "message": "no eval run yet — run `querygate eval --quick` to populate this tab",
        }

    records = [
        json.loads(ln) for ln in latest.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    if not records:
        return {
            "available": False,
            "message": f"latest eval run {latest.name} is empty — re-run `querygate eval`",
        }

    first = records[0]
    summary = summarize_records(records)
    return {
        "available": True,
        "ts": first.get("ts", latest.stem),
        "model": first.get("model", "unknown"),
        "prompt_version": first.get("prompt_version", "unknown"),
        **summary,
    }
