/**
 * QueryGate stream → state reducer (Split 12) — the one place a live `/api/ask` event becomes a UI
 * state patch. It is the live replacement for the dummy engine's `resolve()` / `finish()`: same state
 * shapes (`trace` / `audit` / `boundary` / `messages`), now fed by **real** events instead of canned
 * `scenarios()` data. The render layer (`renderVals` / `traceView` / `auditView` / `boundaryView`)
 * is unchanged — it never learns the data became real.
 *
 * Pure + UMD: every function is `(state, …) → patch` with no side effects, so it runs in the browser
 * (as `window.QueryGateReduce`) and is unit-tested headless against the real captured event streams.
 */
(function (root, factory) {
  var mod = factory();
  if (typeof module === "object" && module.exports) module.exports = mod;
  else root.QueryGateReduce = mod;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  // Status-line copy per tool, mirroring the dummy engine's `labels` map in `run()` (R1/R4 loading).
  var STATUS_LABELS = {
    list_tables: "discovering schema…",
    describe_table: "inspecting tables…",
    run_select: "running query…",
    search_text: "fuzzy-searching text columns…",
  };
  function statusLabel(tool, args) {
    if (tool === "describe_table" && args && args.table) return "inspecting " + args.table + "…";
    return STATUS_LABELS[tool] || "working…";
  }

  function idleBoundary() {
    return { active: false, mode: "idle", l1: "idle", l2: "idle", l3: "idle", sql: "", reason: "" };
  }

  /** Begin a run: push the user message, open the proof rail, clear the trace (mirrors `ask()`). */
  function askInitPatch(state, id, text) {
    return {
      messages: state.messages.concat([{ id: id, role: "user", text: text }]),
      running: true,
      trace: [],
      tab: "trace",
      proofOpen: true,
      schemaOpen: false,
      boundary: idleBoundary(),
      statusLine: "connecting · reading server instructions…",
      streamError: "",
    };
  }

  // ---- per-event reducers (each returns a patch to merge with functional setState) ----

  function stepStart(state, step) {
    // `step` already carries {tool, args, status:'running'} from the adapter — append it verbatim.
    return { trace: state.trace.concat([step]), statusLine: statusLabel(step.tool, step.args) };
  }

  function stepEnd(state, step) {
    // Patch the last (running) trace row to the resolved step (status/latency/rowCount/boundary),
    // exactly as the dummy `resolve()` did — but from the real event.
    var trace = state.trace.slice();
    var i = trace.length - 1;
    if (i >= 0) {
      var merged = {};
      var k;
      for (k in trace[i]) merged[k] = trace[i][k];
      for (k in step) merged[k] = step[k];
      trace[i] = merged;
    }
    var patch = { trace: trace };
    if (step.boundary) {
      var b = step.boundary;
      patch.boundary = {
        active: true,
        sql: (step.args && step.args.sql) || "",
        reason: b.reason || "",
        mode: b.mode,
        l1: b.l1,
        l2: b.l2,
        l3: b.l3,
      };
      // Auto-switch to the Boundary tab on a rejection — the headline §6 moment (mirrors resolve()).
      if (b.mode === "reject") patch.tab = "boundary";
    }
    return patch;
  }

  function auditLine(state, line) {
    // Append the REAL AuditLine the library wrote (auditView() renders JSON.stringify(line)).
    return { audit: state.audit.concat([line]) };
  }

  function messagePatch(state, ev, nextId) {
    var m = ev.message || {};
    var msg = { id: nextId, role: "agent", kind: m.kind, prose: m.prose };
    if (m.kind === "answer") msg.citation = m.citation;
    if (m.kind === "refusal") msg.reason = m.reason;
    return {
      messages: state.messages.concat([msg]),
      running: false,
      statusLine: "",
      cost: (state.cost || 0) + (ev.cost || 0),
      // What ACTUALLY ran (honest), distinct from the requested toggle value. Split 12 R3/U-5.
      ranModel: ev.model || state.ranModel || "",
      requestedModel: ev.requested_model || state.requestedModel || "",
      ranTransport: ev.transport || state.ranTransport || "",
    };
  }

  /** Apply one stream event → a state patch. `nextId` is consumed only by the final `message`. */
  function applyEvent(state, ev, nextId) {
    var t = ev && ev.type;
    if (t === "step-start") return stepStart(state, ev.step);
    if (t === "step-end") return stepEnd(state, ev.step);
    if (t === "audit-line") return auditLine(state, ev.line);
    if (t === "message") return messagePatch(state, ev, nextId);
    return {};
  }

  /**
   * Honest error/offline state (R4/U-7): surface a clear error message — NOT a silent fall-back to
   * canned data. Rendered as a neutral red notice (kind 'error'), visually distinct from the teal
   * "held at the boundary" refusal hero.
   */
  function errorPatch(state, id, errText) {
    return {
      messages: state.messages.concat([{ id: id, role: "agent", kind: "error", prose: errText }]),
      running: false,
      statusLine: "",
      streamError: errText || "The backend is unreachable.",
    };
  }

  // ---- Eval tab (R5/U-8): bind to /api/eval, or show the sample numbers LABELLED as a sample ----

  var METRIC_COLORS = ["#15803D", "#0E72A8", "#6D28D9", "#0B7A6E"];

  // The original mockup numbers — kept ONLY as the clearly-labelled sample shown before a real run.
  var SAMPLE_EVAL = {
    checks: [
      { label: "Layer 1 — SQL guard rejects", color: "#15803D" },
      { label: "Layer 2 — READ ONLY txn rejects", color: "#0E72A8" },
      { label: "Layer 3 — SELECT-only role rejects", color: "#0B7A6E" },
      { label: "data-modifying CTE (whole-AST walk)", color: "#3D52CC" },
      { label: ";-chained & SELECT … FOR UPDATE / INTO", color: "#3D52CC" },
      { label: "denylisted function (pg_read_file …)", color: "#3D52CC" },
    ],
    metrics: [
      { label: "Grounded-rate", value: "0.94", spread: "± 0.03", bar: "94%", color: "#15803D" },
      { label: "Table-precision", value: "0.97", spread: "± 0.02", bar: "97%", color: "#0E72A8" },
      { label: "Answer-correctness", value: "0.91", spread: "± 0.05", bar: "91%", color: "#6D28D9" },
      { label: "0 destructive calls", value: "100%", spread: "deterministic", bar: "100%", color: "#0B7A6E" },
    ],
  };

  /** Map a `/api/eval` summary → the Eval tab view-model, or the labelled sample when no run exists. */
  function evalVm(summary) {
    if (!summary || !summary.available) {
      return {
        available: false,
        checks: SAMPLE_EVAL.checks.map(function (c) {
          return { mark: "✓", color: c.color, label: c.label };
        }),
        metrics: SAMPLE_EVAL.metrics.slice(),
        distHeader: "SAMPLE · ILLUSTRATIVE NUMBERS · NOT A FRESH RUN",
        detHeader: "DETERMINISTIC · CI-GATED · SAMPLE (run `querygate eval` for live numbers)",
      };
    }
    var checks = (summary.checks || []).map(function (c) {
      return { mark: c.ok ? "✓" : "✕", color: c.ok ? "#15803D" : "#DC2626", label: c.label };
    });
    var metrics = (summary.metrics || []).map(function (m, i) {
      return {
        label: m.label,
        value: m.value,
        spread: m.spread,
        bar: m.bar,
        color: METRIC_COLORS[i % METRIC_COLORS.length],
      };
    });
    var model = (summary.model || "model").toUpperCase();
    var n = summary.n_answer_runs || 0;
    return {
      available: true,
      checks: checks,
      metrics: metrics,
      distHeader: "DISTRIBUTIONAL · " + model + " · " + n + " RUNS · MEAN ± SPREAD",
      detHeader: "DETERMINISTIC · CI-GATED · " + (summary.destructive_calls || 0) + " WRITES EXECUTED",
    };
  }

  return {
    statusLabel: statusLabel,
    askInitPatch: askInitPatch,
    applyEvent: applyEvent,
    errorPatch: errorPatch,
    evalVm: evalVm,
    // exposed for unit tests
    _stepStart: stepStart,
    _stepEnd: stepEnd,
    _messagePatch: messagePatch,
    SAMPLE_EVAL: SAMPLE_EVAL,
  };
});
