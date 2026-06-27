/**
 * QueryGate API client (Split 12) — a thin transport over the Split 11 web adapter routes.
 *
 * No business logic, no UI shapes: just `fetch` + NDJSON line framing for `POST /api/ask` (the live
 * agent loop, streamed) and `GET /api/eval` (the latest grounding-eval summary). The adapter already
 * emits events in the UI's exact shapes (Split 11 mapping table), so this layer never reshapes data —
 * it parses the stream and hands each event to a callback. A dead/erroring backend becomes a typed
 * Error the UI surfaces honestly (never a silent fall-back to canned data — Split 12 R4).
 *
 * Dependency-free + UMD so it runs in the browser (as `window.QueryGateApi`) and under a headless JS
 * engine in the test suite (as a CommonJS module). `fetch` is injectable for tests.
 */
(function (root, factory) {
  var mod = factory();
  if (typeof module === "object" && module.exports) module.exports = mod;
  else root.QueryGateApi = mod;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  /** Human-facing text for an error (offline / HTTP / config). Never invents a result. */
  function bannerText(err) {
    if (!err) return "Something went wrong talking to the QueryGate backend.";
    if (err.type === "network") {
      return "Can't reach the QueryGate backend — is `querygate web` running?";
    }
    if (err.type === "config") {
      return err.message || "The backend is not configured (database URL missing).";
    }
    return err.message || "Something went wrong talking to the QueryGate backend.";
  }

  function shapeError(status, body) {
    body = body || {};
    var msg = body.error || body.message || "request failed (" + status + ")";
    var err = new Error(msg);
    err.name = "QueryGateApiError";
    err.status = status;
    // 503 from the adapter = the read-only DB URL isn't configured (an honest config error).
    err.type = status === 503 ? "config" : "http";
    err.bannerText = bannerText(err);
    return err;
  }

  function networkError() {
    var err = new Error("Can't reach the QueryGate backend — is `querygate web` running?");
    err.name = "QueryGateApiError";
    err.status = 0;
    err.type = "network";
    err.bannerText = bannerText(err);
    return err;
  }

  /**
   * Pure NDJSON line framing: split an accumulating buffer into complete lines (raw strings) and the
   * unterminated remainder. Exposed for unit tests — the heart of the streaming reader.
   */
  function takeLines(buffer) {
    var events = [];
    var idx;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      var line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (line) events.push(line);
    }
    return { events: events, rest: buffer };
  }

  function makeClient(opts) {
    opts = opts || {};
    var baseUrl = opts.baseUrl != null ? opts.baseUrl : "";
    var fetchImpl = opts.fetch || (typeof fetch !== "undefined" ? fetch : null);

    function dispatch(line, onEvent) {
      var ev;
      try {
        ev = JSON.parse(line);
      } catch (e) {
        return; // a non-JSON line (shouldn't happen) is skipped, never crashes the stream
      }
      try {
        onEvent(ev);
      } catch (e) {
        // a render handler threw — keep consuming the stream so one bad event can't wedge the UI
      }
    }

    /** Read a streaming response body (browser `ReadableStream`) line-by-line into `onEvent`. */
    function consumeStream(res, onEvent) {
      if (res.body && typeof res.body.getReader === "function" && typeof TextDecoder !== "undefined") {
        var reader = res.body.getReader();
        var decoder = new TextDecoder();
        var buffer = "";
        var pump = function () {
          return reader.read().then(function (r) {
            if (r.done) {
              var tail = buffer.trim();
              if (tail) dispatch(tail, onEvent);
              return;
            }
            buffer += decoder.decode(r.value, { stream: true });
            var taken = takeLines(buffer);
            buffer = taken.rest;
            for (var i = 0; i < taken.events.length; i++) dispatch(taken.events[i], onEvent);
            return pump();
          });
        };
        return pump();
      }
      // Fallback (no streaming body — e.g. a test fetch): buffer the whole text, then frame it.
      return res.text().then(function (text) {
        (text || "").split("\n").forEach(function (l) {
          l = l.trim();
          if (l) dispatch(l, onEvent);
        });
      });
    }

    /**
     * Stream one question through the live agent loop. Calls `onEvent(ev)` for every NDJSON event
     * (`step-start` / `step-end` / `audit-line` / `message`). Resolves when the stream ends; rejects
     * with a typed Error on a network failure or an HTTP error (so the UI shows an honest error state).
     */
    function streamAsk(question, params, onEvent) {
      params = params || {};
      if (!fetchImpl) return Promise.reject(networkError());
      var body = { question: question };
      if (params.model) body.model = params.model;
      if (params.redaction != null) body.redaction = !!params.redaction;
      return fetchImpl(baseUrl + "/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }).then(
        function (res) {
          if (!res.ok) {
            return res.json().then(
              function (b) {
                throw shapeError(res.status, b);
              },
              function () {
                throw shapeError(res.status, null);
              }
            );
          }
          return consumeStream(res, onEvent);
        },
        function () {
          throw networkError();
        }
      );
    }

    /** Fetch the latest grounding-eval summary (`{available, metrics, checks, ...}`). */
    function getEval() {
      if (!fetchImpl) return Promise.reject(networkError());
      return fetchImpl(baseUrl + "/api/eval", { method: "GET" }).then(
        function (res) {
          return res.json().then(function (b) {
            if (!res.ok) throw shapeError(res.status, b);
            return b;
          });
        },
        function () {
          throw networkError();
        }
      );
    }

    return { streamAsk: streamAsk, getEval: getEval };
  }

  return {
    makeClient: makeClient,
    takeLines: takeLines,
    bannerText: bannerText,
    shapeError: shapeError,
  };
});
