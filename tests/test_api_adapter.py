"""Split 11 — the web-demo backend adapter: event-shape conformance + the real boundary on the
demo path (spec/UI §4–6; SPLIT-11 W-1..W-8).

The adapter (``querygate.api``) runs the real agent loop over the live boundary and streams events in
the **exact** shapes the existing ``app/`` UI already renders. These tests drive the adapter directly
(no browser) and assert the event/field shapes the UI binds to — plus that the headline refusal and
the boundary verdicts are **real**, not scripted.

Most tests run **keyless** by driving a fixed tool sequence through the real tools (``scripted_calls``)
— they need only the compose Postgres + read-only role (shared conftest), no model key, so they run in
CI. The one **live** test (an LLM choosing tools) is gated on an Azure/OpenAI key and skips cleanly
without one. Pure mapping tests (W-2) need neither DB nor key.
"""

from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path

import pytest

from querygate.api import agent, eval_summary, mapping
from querygate.config import Config

# ==================================================================================================
# Fixtures.
# ==================================================================================================


@pytest.fixture()
def ro_config(role_url: str, seeded_db, tmp_path) -> Config:
    """A read-only-role Config with a per-test audit file (one line per tool call — W-8)."""
    return Config(database_url=role_url, audit_path=str(tmp_path / "audit.jsonl"))


# A real money-demo style overdue query (count of overdue follow-ups). Deterministic on the seed.
OVERDUE_SQL = (
    "SELECT count(*) AS overdue FROM app.follow_ups "
    "WHERE completed_at IS NULL AND due_date < now()"
)
DELETE_SQL = "DELETE FROM app.patients WHERE name ILIKE '%Smith%'"


def _drain(events) -> list[dict]:
    return list(events)


def _by_type(events: list[dict], t: str) -> list[dict]:
    return [e for e in events if e["type"] == t]


def _assert_keys(obj: dict, required: dict) -> None:
    """Assert ``obj`` has each key with a value of the allowed type(s)."""
    for key, types in required.items():
        assert key in obj, f"missing key {key!r} in {obj!r}"
        assert isinstance(obj[key], types), f"key {key!r} is {type(obj[key])}, want {types}"


# Shapes the UI binds to (read off app/QueryGate Demo.dc.html). The load-bearing contract.
_STEP_END_KEYS = {"tool": str, "args": dict, "status": str, "phi": bool}
_AUDIT_KEYS = {"ts": str, "tool": str, "args": dict, "status": str, "redactions": list}
_CITATION_KEYS = {
    "sql": str, "columns": list, "rows": list, "rowCount": int, "total": int,
    "elapsed": int, "limit": int, "truncated": bool, "phiCols": list,
}
_BOUNDARY_KEYS = {"mode": str, "l1": str, "l2": str, "l3": str}


# ==================================================================================================
# W-2 — mapping correctness (pure; no DB, no key).
# ==================================================================================================


def test_w2_result_to_citation_rename_mapping():
    """RunResult.row_count→rowCount, elapsed_ms→elapsed, redactions→phiCols — not dropped (W-2)."""
    result = {
        "sql": "SELECT 1 LIMIT 1000", "columns": ["x"], "rows": [[1]],
        "row_count": 7, "elapsed_ms": 41, "truncated": False, "redactions": ["name"],
    }
    cit = mapping.result_to_citation(result, row_limit=1000)
    assert cit["rowCount"] == 7  # row_count -> rowCount
    assert cit["elapsed"] == 41  # elapsed_ms -> elapsed
    assert cit["phiCols"] == ["name"]  # redactions -> phiCols
    assert cit["limit"] == 1000  # the auto-LIMIT exposed
    assert cit["total"] == 7  # honest default == rowCount
    assert cit["truncated"] is False
    _assert_keys(cit, _CITATION_KEYS)


def test_w2_boundary_verdict_derived_per_status():
    """The verdict is a pure function of (tool, status) — pass/reject/error differ (W-5 unit)."""
    ok = mapping.boundary_verdict("run_select", "ok", None)
    rej = mapping.boundary_verdict("run_select", "rejected", "DELETE found")
    err = mapping.boundary_verdict("run_select", "error", "canceling statement due to timeout")
    assert ok == {"mode": "pass", "l1": "pass", "l2": "pass", "l3": "pass"}
    assert rej["mode"] == "reject" and rej["l1"] == "reject" and rej["l2"] == "ghost" and rej["l3"] == "ghost"
    assert err["mode"] == "error" and err["l1"] == "pass" and err["l2"] == "error" and err["l3"] == "·"
    # Non-boundary tools carry no verdict.
    assert mapping.boundary_verdict("list_tables", "ok", None) is None


def test_w2_audit_line_passthrough_is_real_shape():
    """The audit-line projection keeps the real §7 AuditLine fields, never synthesizes (W-2/R2)."""
    line = {
        "ts": "2026-06-27T00:00:00+00:00", "tool": "run_select", "args": {"sql": "SELECT 1"},
        "row_count": 1, "latency_ms": 5, "status": "ok", "error": None, "redactions": [],
    }
    ui = mapping.audit_line_to_ui(line)
    _assert_keys(ui, _AUDIT_KEYS)
    assert ui["row_count"] == 1 and ui["latency_ms"] == 5
    assert "error" not in ui  # None error is dropped, matching the UI's resolve()


# ==================================================================================================
# W-1 — event/field shape conformance over a real (scripted) run (DB; keyless).
# ==================================================================================================


def test_w1_overdue_stream_shapes(ro_config):
    """The overdue run streams tool-steps, audit-lines, and an answer with a full citation (W-1)."""
    calls = [
        ("list_tables", {}),
        ("describe_table", {"table": "follow_ups"}),
        ("run_select", {"sql": OVERDUE_SQL}),
    ]
    events = _drain(agent.stream_run(
        "Which providers have the most patients overdue for follow-up?",
        config=ro_config, scripted_calls=calls, final_answer="300 follow-ups are overdue.",
    ))

    # Every tool call → exactly one running step, one resolved step, one audit line.
    assert len(_by_type(events, "step-start")) == 3
    assert len(_by_type(events, "step-end")) == 3
    assert len(_by_type(events, "audit-line")) == 3

    for ev in _by_type(events, "step-end"):
        _assert_keys(ev["step"], _STEP_END_KEYS)
        assert ev["step"]["status"] in ("ok", "rejected", "error")
    for ev in _by_type(events, "audit-line"):
        _assert_keys(ev["line"], _AUDIT_KEYS)

    # The run_select step carries a real boundary verdict (pass/pass/pass for an OK read).
    run_step = next(e["step"] for e in _by_type(events, "step-end") if e["step"]["tool"] == "run_select")
    _assert_keys(run_step["boundary"], _BOUNDARY_KEYS)
    assert run_step["boundary"] == {"mode": "pass", "l1": "pass", "l2": "pass", "l3": "pass"}

    # Final answer message + a citation with every field the UI binds to + run-level fields.
    (msg,) = _by_type(events, "message")
    assert msg["message"]["kind"] == "answer"
    _assert_keys(msg["message"]["citation"], _CITATION_KEYS)
    assert msg["message"]["citation"]["rows"] == [[300]]  # the real overdue count, not canned
    _assert_keys(msg, {"cost": (int, float), "model": str, "transport": str})
    assert msg["transport"] == "in-process"


def test_w1_step_start_precedes_step_end(ro_config):
    """A step's running event is emitted before its resolved event (so the UI animates) — R1."""
    events = _drain(agent.stream_run(
        "overdue?", config=ro_config,
        scripted_calls=[("run_select", {"sql": OVERDUE_SQL})], final_answer="300.",
    ))
    types = [e["type"] for e in events]
    assert types.index("step-start") < types.index("step-end") < types.index("audit-line")


# ==================================================================================================
# W-3 — the real refusal path (DB; keyless). 0 writes executed.
# ==================================================================================================


def test_w3_delete_is_real_refusal_zero_writes(ro_config):
    """"Delete the Smith patients" → a real rejected step + rejected audit + refusal, 0 writes (W-3)."""
    # Baseline patient count (proves the write never executed).
    before = agent.stream_run("count", config=ro_config,
                              scripted_calls=[("run_select", {"sql": "SELECT count(*) AS n FROM app.patients"})],
                              final_answer="")
    before_count = next(e for e in before if e["type"] == "message")["message"]["citation"]["rows"][0][0]

    fresh = dataclasses.replace(ro_config, audit_path=ro_config.audit_path + ".del")
    events = _drain(agent.stream_run(
        "Delete all patients named Smith.", config=fresh,
        scripted_calls=[("run_select", {"sql": DELETE_SQL})], final_answer="I won't attempt that.",
    ))

    (step_end,) = _by_type(events, "step-end")
    assert step_end["step"]["status"] == "rejected"
    assert step_end["step"]["boundary"] == {
        "mode": "reject", "l1": "reject", "l2": "ghost", "l3": "ghost",
        "reason": step_end["step"]["boundary"]["reason"],
    }
    assert step_end["step"]["boundary"]["reason"]  # a real, non-empty guard reason

    (audit_ev,) = _by_type(events, "audit-line")
    assert audit_ev["line"]["status"] == "rejected"
    assert audit_ev["line"]["error"]  # the guard reason recorded in the audit log

    (msg,) = _by_type(events, "message")
    assert msg["message"]["kind"] == "refusal"
    assert msg["message"]["reason"] == audit_ev["line"]["error"]  # refusal cites the real reason

    # 0 writes executed: the patient count is unchanged.
    after = agent.stream_run("count", config=ro_config,
                             scripted_calls=[("run_select", {"sql": "SELECT count(*) AS n FROM app.patients"})],
                             final_answer="")
    after_count = next(e for e in after if e["type"] == "message")["message"]["citation"]["rows"][0][0]
    assert after_count == before_count


# ==================================================================================================
# W-4 — the search path cites the real matched row (DB; keyless).
# ==================================================================================================


def test_w4_search_cites_real_matched_row(ro_config):
    """The Sara/Sarah question streams a search_text step + an answer citing the real "Sarah Lee" row."""
    events = _drain(agent.stream_run(
        "How many follow-ups does Sara Lee have outstanding?", config=ro_config,
        scripted_calls=[("list_tables", {}), ("search_text", {"term": "Sara", "table": "patients"})],
        final_answer="The closest match is Sarah Lee.",
    ))
    tools = [e["step"]["tool"] for e in _by_type(events, "step-end")]
    assert "search_text" in tools

    (msg,) = _by_type(events, "message")
    citation = msg["message"]["citation"]
    flat = [cell for row in citation["rows"] for cell in row]
    assert "Sarah Lee" in flat  # the real Split-05 T4 match, not canned
    # Redaction OFF by default → phiCols empty (reflects redaction state — W-4).
    assert citation["phiCols"] == []


def test_w4_redaction_state_surfaces_in_phicols(role_url, seeded_db, tmp_path):
    """With redaction ON, the masked column surfaces in phiCols and the cell is masked (W-4)."""
    redact_file = tmp_path / "redact.yaml"
    redact_file.write_text("patients:\n  - name\n", encoding="utf-8")
    cfg = Config(database_url=role_url, audit_path=str(tmp_path / "a.jsonl"), redact_path=str(redact_file))
    events = _drain(agent.stream_run(
        "find Sara", config=cfg,
        scripted_calls=[("search_text", {"term": "Sarah Lee", "table": "patients"})],
        final_answer="match",
    ))
    citation = next(e for e in events if e["type"] == "message")["message"]["citation"]
    assert citation["phiCols"] == ["patients.name"]  # redactions -> phiCols
    flat = [cell for row in citation["rows"] for cell in row]
    assert "***" in flat and "Sarah Lee" not in flat  # the PHI cell is masked


# ==================================================================================================
# W-5 — the boundary verdict is real (reject / ok / error differ) (DB; keyless).
# ==================================================================================================


def test_w5_boundary_verdicts_differ_by_real_outcome(ro_config):
    """Reject / ok / timeout produce different l1/l2/l3 verdicts — derived, not hard-coded (W-5)."""
    def verdict(cfg, sql):
        events = _drain(agent.stream_run(
            "q", config=cfg, scripted_calls=[("run_select", {"sql": sql})], final_answer="x",
        ))
        return next(e["step"]["boundary"] for e in events if e["type"] == "step-end")

    ok = verdict(ro_config, OVERDUE_SQL)
    rej = verdict(dataclasses.replace(ro_config, audit_path=ro_config.audit_path + ".r"), DELETE_SQL)
    # Force a Layer-2 statement_timeout: a tiny timeout + a deliberately slow (but guard-legal)
    # query — a cartesian self-join the planner must scan before the (one-row) count returns.
    slow_cfg = dataclasses.replace(ro_config, audit_path=ro_config.audit_path + ".t", statement_timeout="50ms")
    err = verdict(slow_cfg, "SELECT count(*) FROM app.claims a CROSS JOIN app.claims b CROSS JOIN app.claims c")

    assert ok["mode"] == "pass"
    assert rej["mode"] == "reject"
    assert err["mode"] == "error" and err["l2"] == "error"
    assert ok != rej != err and ok != err  # all three genuinely differ


# ==================================================================================================
# W-8 — one audit line per tool call on the demo path (DB; keyless).
# ==================================================================================================


def test_w8_one_audit_line_per_tool_call(ro_config):
    """A full run writes exactly one audit line per tool call (no double-logging via the adapter)."""
    calls = [("list_tables", {}), ("describe_table", {"table": "patients"}),
             ("run_select", {"sql": OVERDUE_SQL})]
    _drain(agent.stream_run("q", config=ro_config, scripted_calls=calls, final_answer="x"))
    lines = [ln for ln in Path(ro_config.audit_path).read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == len(calls)  # exactly N lines for N calls
    # And the count of audit-line events equals the count of tool calls (no synthetic lines).
    events = _drain(agent.stream_run("q2", config=dataclasses.replace(ro_config, audit_path=ro_config.audit_path + "2"),
                                     scripted_calls=calls, final_answer="x"))
    assert len(_by_type(events, "audit-line")) == len(calls)


# ==================================================================================================
# W-6 — /api/eval is honest (no DB, no key).
# ==================================================================================================


def test_w6_eval_summary_present_is_real():
    """With Split-09 runs present, the summary returns real metrics + 0-destructive (W-6)."""
    summary = eval_summary.latest_eval_summary()
    if not summary["available"]:
        pytest.skip("no eval run present in evals/runs/ — covered by the 'no run' case below")
    labels = [m["label"] for m in summary["metrics"]]
    assert labels == ["Grounded-rate", "Table-precision", "Answer-correctness", "0 destructive calls"]
    assert len(summary["checks"]) == 6
    # The deterministic line is honest: 0 destructive calls in the run.
    assert summary["destructive_calls"] == 0
    assert summary["metrics"][-1]["value"].endswith("%")


def test_w6_eval_summary_no_run_is_honest(tmp_path):
    """With no run present, the summary is an honest "no run yet" — never fabricated numbers (W-6)."""
    summary = eval_summary.latest_eval_summary(runs_dir=tmp_path)
    assert summary["available"] is False
    assert "metrics" not in summary and "no eval run yet" in summary["message"]


def test_w6_summarize_records_computes_mean_spread():
    """The distributional summary is recomputed from recorded scores (mean ± spread), not invented."""
    records = [
        {"kind": "answer", "scores": {"grounded": True, "table_precision": True, "answer_correct": True, "destructive_calls": 0}},
        {"kind": "answer", "scores": {"grounded": False, "table_precision": True, "answer_correct": None, "destructive_calls": 0}},
        {"kind": "refusal", "scores": {"grounded": False, "table_precision": False, "answer_correct": None, "destructive_calls": 0}},
    ]
    s = eval_summary.summarize_records(records)
    grounded = next(m for m in s["metrics"] if m["label"] == "Grounded-rate")
    assert grounded["value"] == "0.50"  # 1 of 2 answer runs grounded
    assert s["destructive_calls"] == 0
    assert all(c["ok"] for c in s["checks"])  # boundary held → all checks pass


# ==================================================================================================
# W-7 — the static server serves the UI files unmodified (no DB, no key).
# ==================================================================================================


@pytest.fixture()
def web_client():
    from starlette.testclient import TestClient

    from querygate.api.server import create_app

    # A config with a dummy URL is fine — the static + /api/eval routes never touch the DB.
    app = create_app(Config(database_url="postgresql://unused/db"))
    return TestClient(app)


def test_w7_static_serves_ui_bytes_unchanged(web_client):
    """The mounted app/ serves the dc.html + support.js byte-for-byte (the adapter doesn't rewrite)."""
    repo_root = Path(__file__).resolve().parent.parent
    for fname in ("QueryGate Demo.dc.html", "support.js"):
        resp = web_client.get(f"/app/{fname}")
        assert resp.status_code == 200, fname
        assert resp.content == (repo_root / "app" / fname).read_bytes(), f"{fname} bytes changed"


def test_w7_root_redirects_to_ui(web_client):
    """GET / redirects to the served prototype so the demo runs from one origin (R4)."""
    resp = web_client.get("/", follow_redirects=False)
    assert resp.status_code in (307, 308)
    assert resp.headers["location"].endswith("dc.html")  # %20-encoded space is fine


def test_w7_eval_endpoint_serves_json(web_client):
    """GET /api/eval returns the honest summary JSON (no DB, no key)."""
    body = web_client.get("/api/eval").json()
    assert "available" in body


def test_ask_requires_question(web_client):
    """POST /api/ask with no question is a clean 400, not a crash."""
    assert web_client.post("/api/ask", json={}).status_code == 400


# ==================================================================================================
# W-1 (live) — one real keyed run of the agent loop (gated on a model key; skips in keyless CI).
# ==================================================================================================


def _has_model_key() -> bool:
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    except Exception:
        pass
    return bool(os.environ.get("AZURE_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"))


@pytest.mark.skipif(not _has_model_key(), reason="no model key (Azure/OpenAI) — live agent loop test")
def test_w1_live_overdue_real_loop(ro_config):
    """A real LLM-driven run over the overdue question streams real steps + a final message (W-1)."""
    events = _drain(agent.stream_run(
        "Which providers have the most patients overdue for follow-up?",
        config=ro_config, max_steps=8,
    ))
    # At least one run_select with a real boundary verdict, and a final message.
    run_steps = [e["step"] for e in _by_type(events, "step-end") if e["step"]["tool"] == "run_select"]
    assert run_steps, "the agent should have run a SELECT"
    assert all("boundary" in s for s in run_steps)
    (msg,) = _by_type(events, "message")
    assert msg["message"]["kind"] in ("answer", "refusal")
    assert msg["model"] and msg["transport"] == "in-process"
