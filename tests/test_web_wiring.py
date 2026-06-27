"""Split 12 — the UI ⇄ live-adapter WIRING, tested headless (U-1..U-8, pass-gates 1-5).

The `app/` render layer is the shipped mockup (already correct); this split rewired its **data source**
from the canned `scenarios()` engine to the live `/api/ask` NDJSON stream. These tests prove the wiring
without a browser: a headless JS engine (``quickjs``) loads the **real** `app/api.js` + `app/reduce.js`
and the **real** ``Component`` class extracted from ``app/QueryGate Demo.dc.html``, then drives it with
the **real captured event streams** (``tests/fixtures/web/*.json`` — recorded from a live Azure GPT-5.5
run through the real three-layer boundary) exactly as the browser would, and asserts the resulting
``state.trace`` / ``audit`` / ``boundary`` / ``messages`` + ``renderVals()`` bindings.

Why quickjs and not Node/Playwright: this box has no Node and no browser (documented env constraint);
quickjs is a tiny embeddable JS engine that runs the pure wiring + reducer headless. The render layer
itself is unchanged (proven byte-for-byte by ``test_w12_render_layer_unchanged``), so a DOM render test
would only re-test the shipped mockup. The live end-to-end stream is separately proven in
``tests/test_api_adapter.py`` (real HTTP, real DB, real model).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

quickjs = pytest.importorskip(
    "quickjs", reason="quickjs (headless JS engine) not installed; `pip install quickjs`"
)

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app"
FIX = ROOT / "tests" / "fixtures" / "web"
DC_HTML = APP / "QueryGate Demo.dc.html"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def _component_src() -> str:
    html = _read(DC_HTML)
    m = re.search(r"data-dc-script[^>]*>([\s\S]*?)</script>", html)
    assert m, "could not find the <script data-dc-script> block in the dc.html"
    return m.group(1)


def _fixture(name: str) -> list[dict]:
    return json.loads(_read(FIX / f"{name}.json"))


# --------------------------------------------------------------------------------------------------
# The headless host: real api.js + reduce.js + the real Component, with a fake DCLogic/React/document.
# --------------------------------------------------------------------------------------------------

_HOST_BOOTSTRAP = """
globalThis.window = {};
globalThis.DCLogic = class { constructor(p){ this.props=p||{}; this.state={}; } setState(){} };
globalThis.document = { getElementById:function(){return null;}, activeElement:null };
globalThis.React = { createElement:function(){return {};} };
"""


def _build_ctx() -> "quickjs.Context":
    ctx = quickjs.Context()
    ctx.eval(_HOST_BOOTSTRAP)
    ctx.eval(_read(APP / "api.js"))
    ctx.eval(_read(APP / "reduce.js"))
    # UMD attaches to globalThis when there's no CommonJS `module`; mirror onto window (the browser global).
    ctx.eval("window.QueryGateApi = globalThis.QueryGateApi; window.QueryGateReduce = globalThis.QueryGateReduce;")
    ctx.eval("globalThis.__mk = function(DCLogic, React){ " + _component_src() + "\n; return Component; };")
    ctx.eval("globalThis.Component = __mk(globalThis.DCLogic, globalThis.React);")
    return ctx


# The JS driver: instantiate the real Component, give it a setState that applies patches (functional or
# object), inject a fake `api` whose streamAsk feeds the captured events synchronously (or rejects, to
# exercise the error path), call the real ask()/onStreamEvent()/onStreamError(), then snapshot
# state + renderVals() as JSON. This runs the SAME methods the browser runs.
_DRIVER = """
(function(eventsStr, paramsStr, evalStr, mode){
  var EVENTS = JSON.parse(eventsStr), PARAMS = JSON.parse(paramsStr), SUMMARY = JSON.parse(evalStr);
  var c = new Component();
  c.setState = function(update, cb){
    var patch = (typeof update==='function') ? update(c.state) : update;
    var ns = {}; for(var k in c.state) ns[k]=c.state[k];
    for(var k in patch) ns[k]=patch[k];
    c.state = ns; if(cb) cb();
  };
  c.api = {
    streamAsk: function(q, p, onEvent){
      c.__sentParams = p;
      if(mode === 'error'){ return { catch:function(cb){ cb({ bannerText:"Can't reach the QueryGate backend — is `querygate web` running?", type:'network' }); } }; }
      EVENTS.forEach(function(ev){ onEvent(ev); });
      return { catch:function(){} };
    },
    getEval: function(){ return { then:function(f){ f(SUMMARY); return { catch:function(){} }; } }; }
  };
  if(SUMMARY){ c.setState({ eval: window.QueryGateReduce.evalVm(SUMMARY) }); }
  if(PARAMS.model){ c.setState({ model: PARAMS.model }); }
  if(PARAMS.redaction){ c.setState({ redactionOn: true }); }
  c.ask(PARAMS.question || 'q');
  var r = c.renderVals();
  return JSON.stringify({ state: c.state, sentParams: c.__sentParams, render: r });
})
"""


def _drive(ctx, events, params, eval_summary=None, mode="ok") -> dict:
    fn = ctx.eval(_DRIVER)
    out = fn(json.dumps(events), json.dumps(params), json.dumps(eval_summary), mode)
    return json.loads(out)


@pytest.fixture(scope="module")
def ctx():
    return _build_ctx()


# --------------------------------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------------------------------


def test_w12_modules_and_component_load(ctx):
    """api.js + reduce.js + the Component class all parse and load headless (no syntax error)."""
    assert ctx.eval("typeof window.QueryGateApi.makeClient") == "function"
    assert ctx.eval("typeof window.QueryGateReduce.applyEvent") == "function"
    assert ctx.eval("typeof Component") == "function"


def test_w12_u1_overdue_flow_real(ctx):
    """U-1: the overdue stream fills the proof rail with real steps, all boundary pass, cited answer."""
    events = _fixture("overdue")
    r = _drive(ctx, events, {"question": "Which providers are most overdue?", "model": "claude-opus-4-8"})
    st, rv = r["state"], r["render"]
    n_steps = sum(1 for e in events if e["type"] == "step-end")
    n_audit = sum(1 for e in events if e["type"] == "audit-line")
    msg = json.loads(json.dumps([e for e in events if e["type"] == "message"][0]))
    assert st["running"] is False
    assert len(st["trace"]) == n_steps
    assert len(st["audit"]) == n_audit
    assert rv["auditRej"] == 0 and rv["auditOk"] == n_audit
    assert st["boundary"]["mode"] == "pass"
    last = rv["messages"][-1]
    assert last["isAnswer"] is True
    assert last["rowCount"] == msg["message"]["citation"]["rowCount"]
    assert len(last["rowsView"]) == len(msg["message"]["citation"]["rows"])  # real cited rows present


def test_w12_u2_refusal_flow_real_boundary(ctx):
    """U-2 (hard gate): a REAL rejected step, Boundary tab auto-opens, real guard reason, 0 writes."""
    events = _fixture("refusal")
    r = _drive(ctx, events, {"question": "Delete all patients named Smith."})
    st, rv = r["state"], r["render"]
    # exactly one tool step, rejected, with the reject/ghost/ghost verdict
    assert len(st["trace"]) >= 1
    rejected = [s for s in st["trace"] if s["status"] == "rejected"]
    assert rejected, "expected a real rejected tool step"
    assert st["boundary"]["mode"] == "reject"
    assert st["boundary"]["l1"] == "reject" and st["boundary"]["l2"] == "ghost" and st["boundary"]["l3"] == "ghost"
    assert st["tab"] == "boundary", "Boundary tab must auto-open on a reject"
    # 0 writes executed: no `ok` write line, exactly the rejected one in the audit
    assert rv["auditOk"] == 0 and rv["auditRej"] == 1
    last = rv["messages"][-1]
    assert last["isRefusal"] is True
    # the REAL guard reason (not the dummy "Only a single read-only SELECT is permitted.")
    assert "data-modifying" in last["reason"].lower() or "delete" in last["reason"].lower()


def test_w12_u3_search_flow_real(ctx):
    """U-3: a real search_text step runs and the answer cites the real matched patient (Sarah Lee)."""
    events = _fixture("search")
    r = _drive(ctx, events, {"question": "How many follow-ups does Sara Lee have outstanding?"})
    st, rv = r["state"], r["render"]
    tools = [s["tool"] for s in st["trace"]]
    assert "search_text" in tools, "the fuzzy lookup must run a real search_text step"
    last = rv["messages"][-1]
    assert last["isAnswer"] is True
    flat = json.dumps(last["rowsView"])
    assert "Sarah Lee" in flat, "the cited rows must contain the real matched patient"


def test_w12_u4_canned_engine_removed():
    """U-4 (gate 2): the scenarios()/pick() methods + canned SQL are gone; ask() drives the live stream."""
    src = _component_src()
    assert "scenarios(){" not in src and "scenarios() {" not in src
    assert "pick(text){" not in src and "pick(text) {" not in src
    assert "OVERDUE_SQL" not in src and "SEARCH_SQL" not in src and "DELETE_SQL" not in src
    # the live path: ask() opens the real stream
    assert "this.api.streamAsk(" in src
    assert "onStreamEvent" in src


def test_w12_u5_model_reaches_backend(ctx):
    """U-5: the chosen model is sent to /api/ask, and the header honestly shows what ACTUALLY ran."""
    events = _fixture("overdue")
    r = _drive(ctx, events, {"question": "q", "model": "claude-sonnet-4-6"})
    assert r["sentParams"]["model"] == "claude-sonnet-4-6", "the chosen model must reach the backend"
    # the message reports the real model that ran; the header shows it (never faking that claude ran)
    assert r["render"]["model"] == "gpt-5.5"


def test_w12_u5_message_threads_requested_model(ctx):
    """U-5: a message event's `requested_model` (the echoed selection) threads into state honestly."""
    msg = [
        {"type": "message", "message": {"kind": "refusal", "prose": "read-only", "reason": "guard"},
         "cost": 0.001, "model": "gpt-5.5", "requested_model": "claude-sonnet-4-6", "transport": "in-process"},
    ]
    r = _drive(ctx, msg, {"question": "q"})
    assert r["state"]["requestedModel"] == "claude-sonnet-4-6"
    assert r["state"]["ranModel"] == "gpt-5.5"
    assert r["render"]["transport"] == "in-process"  # the real transport that ran


def test_w12_u5_redaction_flag_reaches_backend(ctx):
    """U-6 (part a): the redaction toggle is sent to the backend (server-side filter, not client)."""
    events = _fixture("search_redact")
    r = _drive(ctx, events, {"question": "q", "redaction": True})
    assert r["sentParams"]["redaction"] is True


def test_w12_u6_redaction_is_server_sourced(ctx):
    """U-6 (gate 3): masked cells (`***`) + the `redacted` chip come from the BACKEND, not a client fake.

    The dc.html no longer fakes `'***'` from the toggle; it renders the cells the real filter produced
    (a masked cell is the literal `***`) and the masked-column count from the real `phiCols`. Driven by
    a message whose citation carries the real server-side mask (as Split-05's filter emits).
    """
    # a real-shaped redacted citation (what the server filter produces: cell == '***', phiCols set)
    redacted_events = [
        {"type": "step-start", "step": {"tool": "run_select", "args": {"sql": "SELECT name ..."}, "status": "running"}},
        {"type": "step-end", "step": {"tool": "run_select", "args": {"sql": "SELECT name ..."}, "status": "ok",
                                       "latency": 7, "rowCount": 1, "phi": True,
                                       "boundary": {"mode": "pass", "l1": "pass", "l2": "pass", "l3": "pass"}}},
        {"type": "audit-line", "line": {"ts": "2026-06-27T00:00:00+00:00", "tool": "run_select", "args": {},
                                         "row_count": 1, "latency_ms": 7, "status": "ok", "redactions": ["patients.name"]}},
        {"type": "message", "message": {"kind": "answer", "prose": "Found one match.",
            "citation": {"sql": "SELECT name ...", "columns": ["name", "outstanding"],
                          "rows": [["***", 3]], "rowCount": 1, "total": 1, "elapsed": 7, "limit": 1000,
                          "truncated": False, "phiCols": ["patients.name"]}},
         "cost": 0.001, "model": "gpt-5.5", "requested_model": "claude-opus-4-8", "transport": "in-process"},
    ]
    r = _drive(ctx, redacted_events, {"question": "q", "redaction": True})
    last = r["render"]["messages"][-1]
    assert last["masked"] is True
    assert "redacted: 1 col" in last["maskedLabel"]
    cells = last["rowsView"][0]
    assert cells[0]["v"] == "***" and cells[0]["color"] == "#7C3AED", "masked cell rendered violet ***"
    assert cells[1]["v"] == 3 and cells[1]["color"] == "#384150", "non-PHI cell legible"
    # the audit line carries the real server-side redactions
    audit = r["state"]["audit"]
    assert any(a.get("redactions") == ["patients.name"] for a in audit)


def test_w12_u6_redaction_off_is_legible(ctx):
    """U-6: with redaction off (no phiCols, no `***`), the name cell is legible — not faked."""
    events = _fixture("search")
    r = _drive(ctx, events, {"question": "q", "redaction": False})
    last = r["render"]["messages"][-1]
    assert last["masked"] is False
    flat = json.dumps(last["rowsView"])
    assert "***" not in flat and "Sarah Lee" in flat


def test_w12_u7_error_state_honest(ctx):
    """U-7 (gate 4): a dead backend shows a clear error message — NOT canned data as a real answer."""
    r = _drive(ctx, _fixture("overdue"), {"question": "q"}, mode="error")
    st, rv = r["state"], r["render"]
    assert st["running"] is False
    last = rv["messages"][-1]
    assert last["isError"] is True
    assert last["isAnswer"] is False and last["isRefusal"] is False
    assert "reach" in last["prose"].lower() or "unreachable" in last["prose"].lower()
    assert st["streamError"], "an error banner must be recorded"
    # no canned answer leaked in
    assert not any(m.get("isAnswer") for m in rv["messages"])


def test_w12_u8_eval_tab_real_vs_sample(ctx):
    """U-8 (gate 4): /api/eval summary → real mean±spread; no run → numbers LABELLED as a sample."""
    # real summary present
    summary = {
        "available": True, "model": "gpt-5.5", "n_answer_runs": 15, "destructive_calls": 0,
        "metrics": [
            {"label": "Grounded-rate", "value": "1.00", "spread": "± 0.00", "bar": "100%"},
            {"label": "Table-precision", "value": "1.00", "spread": "± 0.00", "bar": "100%"},
            {"label": "Answer-correctness", "value": "1.00", "spread": "± 0.00", "bar": "100%"},
            {"label": "0 destructive calls", "value": "100%", "spread": "deterministic", "bar": "100%"},
        ],
        "checks": [{"label": "Layer 1 — SQL guard rejects", "ok": True}],
    }
    r = _drive(ctx, _fixture("overdue"), {"question": "q"}, eval_summary=summary)
    rv = r["render"]
    assert "GPT-5.5" in rv["evalDistHeader"] and "15 RUNS" in rv["evalDistHeader"]
    assert rv["evalMetrics"][0]["value"] == "1.00"
    assert rv["evalChecks"][0]["mark"] == "✓"

    # no run → sample, clearly labelled
    r2 = _drive(ctx, _fixture("overdue"), {"question": "q"}, eval_summary=None)
    assert "SAMPLE" in r2["render"]["evalDistHeader"]
    assert "SAMPLE" in r2["render"]["evalDetHeader"]


def test_w12_render_layer_unchanged():
    """Gate 5: traceView()/auditView()/boundaryView() are byte-for-byte the shipped mockup's."""
    src = _component_src()
    baseline = _read(FIX / "render_baseline.js")
    for fn in ("traceView", "auditView", "boundaryView"):
        block = re.search(rf"(\n  {fn}\(\)\{{.*?\n  \}}\n)", src, re.S)
        assert block, f"{fn} not found in the current dc.html"
        assert block.group(1) in baseline, f"{fn} was modified — the render layer must stay unchanged"


def test_w12_api_takelines_ndjson_framing(ctx):
    """api.js `takeLines` frames NDJSON correctly: complete lines out, partial line held as remainder."""
    out = ctx.eval(
        "JSON.stringify(window.QueryGateApi.takeLines("
        "'{\"a\":1}\\n{\"b\":2}\\n{\"c\":'))"
    )
    res = json.loads(out)
    assert res["events"] == ['{"a":1}', '{"b":2}']
    assert res["rest"] == '{"c":'  # the unterminated tail is held for the next chunk


def test_w12_statuslabel_per_tool(ctx):
    """The live status line is driven per real tool (loading states, R4)."""
    label = lambda tool, args="null": ctx.eval(
        f"window.QueryGateReduce.statusLabel('{tool}', {args})"
    )
    assert label("list_tables") == "discovering schema…"
    assert label("run_select") == "running query…"
    assert label("describe_table", "{table:'follow_ups'}") == "inspecting follow_ups…"
