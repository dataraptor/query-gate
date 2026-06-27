# Architecture

> 🟦 deterministic (code, the boundary) · 🟨 LLM (distributional, never trusted with safety) · 🟥 rejection / refusal path.

QueryGate's whole design fits in one sentence: **the model proposes a query, and deterministic
code disposes of it and its result.** Claude writes SQL via tool use; QueryGate validates that SQL
across three independent layers, runs it read-only, filters the rows, logs the call, and hands back
a cited answer. Nothing the model says is ever trusted to keep the data safe — the safety property
is a property of the *code*, asserted in CI.

This document covers the component wiring, the read-only boundary, the lifecycle of a single tool
call, and the two-tier CI that makes it structurally impossible to ship a write path.

- [Component wiring](#component-wiring)
- [The read-only boundary](#the-read-only-boundary)
- [Lifecycle of one `run_select`](#lifecycle-of-one-run_select)
- [The tool surface](#the-tool-surface)
- [Transports — one engine, four front doors](#transports--one-engine-four-front-doors)
- [The web demo path](#the-web-demo-path)
- [Two-tier CI](#two-tier-ci)
- [Where each guarantee lives](#where-each-guarantee-lives)

---

## Component wiring

The orchestrator is a plain importable package, [`querygate/`](../querygate/). **Everything that
needs the read path imports [`querygate.tools`](../querygate/tools.py) directly and runs it
in-process. There is no service-to-service HTTP between the Python components.** The only network
hops are the *front doors* — the MCP transports an external Claude client connects over, and the
browser talking to the web adapter.

```mermaid
flowchart TB
    subgraph clients["Front doors (the only network hops)"]
        DESK["Claude Desktop / Claude Code<br/>stdio (MCP)"]
        API_C["Messages API MCP connector<br/>Streamable HTTP"]
        BROWSER["Browser<br/>app/ UI · renders only"]
    end

    subgraph inproc["Import querygate.tools directly — in-process, no HTTP"]
        SRV["server.py<br/>FastMCP shell (thin)"]
        WEB["api/<br/>web adapter (thin)"]
        EVALS["evals/<br/>grounding harness ×N"]
        CLI["cli.py<br/>querygate query / seed"]
    end

    CORE["<b>tools.py</b> — the read path<br/>list_tables · describe_table · run_select · search_text · explain_select"]

    subgraph boundary["The read-only boundary + filter"]
        L1["guard.py · Layer 1<br/>SQL guard (pure fn)"]
        L2["db.py · Layer 2<br/>READ ONLY txn + timeout"]
        FILT["result.py<br/>serialize · redact · byte-cap"]
    end

    DB[("PostgreSQL 16<br/><b>Layer 3</b>: SELECT-only role")]
    AUDIT[["audit.jsonl<br/>one line per call (ok / rejected / error)"]]

    DESK --> SRV
    API_C --> SRV
    BROWSER -- "HTTP / NDJSON" --> WEB

    SRV --> CORE
    WEB --> CORE
    EVALS --> CORE
    CLI --> CORE

    CORE --> L1 --> L2 --> FILT
    L2 <--> DB
    CORE --> AUDIT

    style L1 fill:#dbeafe,stroke:#1e40af
    style L2 fill:#dbeafe,stroke:#1e40af
    style FILT fill:#dbeafe,stroke:#1e40af
    style DB fill:#dbeafe,stroke:#1e40af
    style CORE fill:#dbeafe,stroke:#1e40af
    style WEB fill:#fef9c3,stroke:#a16207
    style EVALS fill:#fef9c3,stroke:#a16207
    style AUDIT fill:#f3f4f6,stroke:#6b7280
```

Why the in-process rule matters:

- **`evals/` and `api/` import `core` in-process.** That is what makes eval runs isolated and
  parallel-safe (each call opens its own connection and transaction; the boundary is
  [stateless per call](../querygate/db.py)), and it is why the web demo and the eval exercise the
  *byte-for-byte same boundary* — the web adapter reuses the eval harness's in-process tool-runner
  ([`evals/run_eval._execute_tool`](../evals/run_eval.py)), it does not reimplement it.
- **The browser can't import a Python module**, so it is the one component that talks over
  **HTTP / NDJSON**, through [`api/`](../querygate/api/server.py) — a *thin adapter* that only
  reshapes what the boundary already produced. The "no safety/model logic in `api/`" rule is a
  passing grep test ([`tests/test_web_wiring.py`](../tests/test_web_wiring.py)).
- **The MCP transports** (stdio, Streamable HTTP) are how an *external* Claude connects. They wrap
  the same four tools and enforce the same boundary; only the framing differs.

---

## The read-only boundary

Every query the server runs passes three independent layers, in order. The signature property is
that **no single layer is load-bearing on its own** — a gap in any one leaves the system *degraded,
not breached*, because the other two still hold. This is defense in depth, made testable.

```mermaid
flowchart TB
    SQL([Agent-proposed SQL]) --> L1

    subgraph L1box["Layer 1 — SQL guard (guard.py)"]
        L1["sqlglot parse, dialect postgres<br/>· exactly one statement<br/>· whole-AST walk: reject any DML/DDL node<br/>· reject SELECT INTO / FOR UPDATE / denylisted fns<br/>· fail closed on anything unparsable<br/>· auto-LIMIT the outermost query"]
    end

    L1 -- "reject" --> REJ["RunRejected<br/>+ one audit line<br/>SQL never reaches the DB"]
    L1 -- "accept (+ auto-LIMIT)" --> L2

    subgraph L2box["Layer 2 — read-only transaction (db.py)"]
        L2["BEGIN TRANSACTION READ ONLY<br/>SET LOCAL statement_timeout = '5s'<br/>&lt;query&gt; (one statement, params bound)<br/>COMMIT"]
    end

    L2 --> L3

    subgraph L3box["Layer 3 — least-privilege role (init_role.sql)"]
        L3["connect as querygate role<br/>· SELECT-only grants, no write privilege<br/>· default_transaction_read_only = on<br/>· USAGE on app schema only<br/>· no sequence USAGE (nextval unreachable)"]
    end

    L3 --> FILT["result.py filter<br/>serialize · redact · byte-cap"]
    FILT --> OUT([Cited RunResult + one 'ok' audit line])

    style L1 fill:#dbeafe,stroke:#1e40af
    style L2 fill:#dbeafe,stroke:#1e40af
    style L3 fill:#dbeafe,stroke:#1e40af
    style FILT fill:#dbeafe,stroke:#1e40af
    style REJ fill:#fee2e2,stroke:#b91c1c
```

**Order matters, but priority is the reverse of order.** Layers 2 and 3 are the load-bearing
guarantees — Postgres enforces them regardless of what the guard misses. Layer 1 is the fast,
legible *first* line: it produces clean, machine-tagged error messages and the auto-`LIMIT` before
anything touches the database.

| Layer | Where | What it stops | Holds even if… |
|---|---|---|---|
| **1 — SQL guard** | [`guard.py`](../querygate/guard.py) | writes, multi-statements, data-modifying CTEs, `SELECT … INTO`, `FOR UPDATE/SHARE`, a dangerous-function denylist; **fails closed** on anything `sqlglot` can't fully parse | — (it is the fallible one; that's why 2 & 3 exist) |
| **2 — read-only txn** | [`db.py`](../querygate/db.py) | any write inside a `READ ONLY` transaction, *including* writes that don't look like writes (`SELECT nextval('seq')`); `statement_timeout` bounds runtime | the guard had a bug |
| **3 — least-privilege role** | [`init_role.sql`](../scripts/init_role.sql) | every `INSERT/UPDATE/DELETE/DDL` — the grant simply does not exist on the connection | *every line of application code were wrong* |

The deep version of this — the threat model, the data-modifying-CTE case, prompt injection via data
— lives in [security-model.md](security-model.md).

---

## Lifecycle of one `run_select`

A single call, traced end to end. The two outcomes (cited result vs. rejected write) both write
**exactly one** audit line — the rejection is a first-class audit event, which is how the boundary
is proven to have held after the fact.

```mermaid
sequenceDiagram
    autonumber
    participant A as Agent (Claude / GPT)
    participant T as tools.run_select
    participant G as guard.py (L1)
    participant D as db.py (L2)
    participant P as Postgres (L3 role)
    participant F as result.py
    participant L as audit.jsonl

    A->>T: run_select(sql)
    T->>G: guard_select(sql, row_limit)
    alt write / unparsable / denylisted
        G-->>T: reject(rule, reason)
        T->>L: append AuditLine(status="rejected")
        T-->>A: RunRejected — never hit the DB
    else accepted (+ auto-LIMIT)
        G-->>T: accept(approved_sql)
        T->>D: run_readonly(approved_sql)
        D->>P: BEGIN READ ONLY · SET LOCAL timeout · query · COMMIT
        P-->>D: columns, raw rows
        D-->>T: (columns, rows)
        T->>F: serialize → redact → byte-cap
        F-->>T: cited RunResult
        T->>L: append AuditLine(status="ok", row_count)
        T-->>A: RunResult (sql + row_count = the citation)
    end
```

The `RunResult` carries the **exact SQL executed** (post auto-`LIMIT`) and the **row count** — those
two fields are the citation. The honesty rail above this (proven in the eval) is that the agent may
only state numbers that appear in a tool result it actually received this turn.

---

## The tool surface

Five read-only tools, all routed through the same boundary. Each is a thin function in
[`tools.py`](../querygate/tools.py); the MCP server and the eval present the *identical* descriptions
and a minimal, closed (`additionalProperties: false`) input schema.

| Tool | Purpose | Injection defense |
|---|---|---|
| `list_tables` | Discover the `app`-schema tables + row-count estimates. Call first. | reads `pg_catalog` only |
| `describe_table` | A table's columns, types, PK/FK + a few sample rows. | `table` validated against the live allowlist **before** any query; never formatted into SQL |
| `run_select` | *The* product tool — run one read-only `SELECT`, cited. | the full three-layer boundary |
| `search_text` | Fuzzy `ILIKE` lookup across text columns ("name spelled differently"). | `table` allowlisted; `term` is a **bound parameter**, never concatenated |
| `explain_select` | Plan + estimated cost **without running** the query. | same Layer-1 guard, **plus** `EXPLAIN (ANALYZE)` is rejected (it would execute) |

**Every** outcome of **every** tool — `ok`, `rejected`, or `error` — appends exactly one
`AuditLine`. That invariant is what makes the audit log a complete record of the boundary's
behavior.

---

## Transports — one engine, four front doors

The same four-tool engine is reachable four ways. None of them changes the boundary; they only
change the framing.

```mermaid
flowchart LR
    subgraph front["Front doors"]
        STDIO["querygate<br/>stdio (MCP)"]
        HTTP["querygate --http<br/>Streamable HTTP /mcp"]
        WEBCMD["querygate web<br/>app/ UI + /api adapter"]
        QUERY["querygate query '&lt;SELECT&gt;'<br/>by-hand CLI"]
    end
    ENGINE["build_server() / tools.py<br/>same 4 tools · same boundary · same audit"]
    STDIO --> ENGINE
    HTTP --> ENGINE
    WEBCMD --> ENGINE
    QUERY --> ENGINE
    style ENGINE fill:#dbeafe,stroke:#1e40af
```

- **stdio** ([`server.main`](../querygate/server.py)) — for Claude Desktop / Claude Code.
- **Streamable HTTP** ([`server.serve_http`](../querygate/server.py)) — at
  `http://localhost:8000/mcp`, the path the Messages API MCP connector uses. **Localhost-only, no
  auth** — a deliberate v1 scope fence (auth + a network bind are roadmap).
- **web** ([`api/server.py`](../querygate/api/server.py)) — the demo UI + the `/api/*` adapter.
- **query** ([`cli.py`](../querygate/cli.py)) — run one `SELECT` by hand and print the cited result;
  a write visibly refuses and exits non-zero.

---

## The web demo path

The browser is the only client that can't import the engine, so it goes over HTTP. The adapter is
deliberately a *reshaper*, not a brain — it runs the **real** agent loop (or, for a write request, a
real boundary demonstration) and streams UI-shaped events as NDJSON.

```mermaid
sequenceDiagram
    autonumber
    participant B as Browser (app/)
    participant W as api/server.py (adapter)
    participant AG as api/agent.py (real loop)
    participant T as tools.py (boundary)
    participant M as Model (Azure GPT-5.5)

    B->>W: POST /api/ask {question}
    W->>AG: stream_run(question, fresh per-run audit)
    alt write request (delete/drop/…)
        AG->>T: run_select(implied write SQL)
        T-->>AG: RunRejected (real rule, real reason)
        AG-->>B: NDJSON step-end (reject) + refusal message
    else read question
        loop until final answer
            AG->>M: chat.completions (tools)
            M-->>AG: tool call
            AG->>T: execute tool (real boundary + audit)
            T-->>AG: RunResult
            AG-->>B: NDJSON step-start / step-end / audit-line
        end
        AG-->>B: NDJSON message (cited answer) + cost/model
    end
```

Honesty rails on this path: **no canned data** — every number, SQL string, row, and boundary verdict
comes from an actual tool result this turn; each `audit-line` event is the *real* line the library
wrote, read back from a per-request audit file (cleaned up after the response). When no model key is
configured, `/api/ask` emits an honest "no model key" error event rather than hanging or fabricating
an answer. See [`api/agent.py`](../querygate/api/agent.py).

---

## Two-tier CI

The headline honesty mechanism: it is **structurally impossible to gate a commit on an LLM number.**
Safety is deterministic and gated per commit; quality is distributional and reported, never gated.

```mermaid
flowchart TD
    C([commit / PR]) --> G

    subgraph tier1["Tier-1 — per-commit gate · NO API key · free"]
        G["spin up real Postgres 16<br/>schema.sql → init_role.sql → fixed seed"]
        G --> T["pytest (full Tier-1 suite)"]
        T --> B["boundary proof: a write rejected at<br/>each layer INDEPENDENTLY<br/>test_b1_* / test_b2_* / test_b3_*"]
    end

    N([nightly / on demand]) --> F

    subgraph tier2["Tier-2 — grounding eval · WITH model key · distributional"]
        F["querygate eval --repeats N"]
        F --> R["frozen gold set ×N<br/>grounded-rate · table-precision · answer-correctness<br/>+ 0-destructive-calls (deterministic line)"]
        R --> A["report mean ± spread, captioned with N<br/>NEVER a bare number; never hard-fails on wobble"]
    end

    style G fill:#dbeafe,stroke:#1e40af
    style T fill:#dbeafe,stroke:#1e40af
    style B fill:#dbeafe,stroke:#1e40af
    style F fill:#fef9c3,stroke:#a16207
    style R fill:#fef9c3,stroke:#a16207
    style A fill:#fef9c3,stroke:#a16207
```

- **Tier-1** ([`.github/workflows/ci.yml`](../.github/workflows/ci.yml)) is keyless and
  deterministic. It stands up a real Postgres 16, applies `schema.sql`, `init_role.sql` (the Layer-3
  role), and the fixed seed, then runs the full suite. The centerpiece is
  [`tests/test_boundary.py`](../tests/test_boundary.py): a hand-crafted write rejected at each of the
  three layers *independently*, in a readable log.
- **Tier-2** ([`evals/`](../evals/README.md)) needs a model key and is non-deterministic, so it is
  **not** in the commit gate. It runs the frozen gold set N times and reports mean ± spread. The one
  exact line is `0 destructive calls` (target 100%) — and a write attempt there is a real failure
  (non-zero exit), but groundedness wobble never hard-fails.

The reason this split is the point: a number that can't be reproduced byte-for-byte can't be a gate
without inviting either flakiness or fudging. By construction, the only things that can fail a commit
are deterministic facts about the boundary.

---

## Where each guarantee lives

| Guarantee | Enforced by | Proven by |
|---|---|---|
| No write reaches the data | Layers 1 + 2 + 3 | [`tests/test_boundary.py`](../tests/test_boundary.py), Tier-1 CI |
| Data-modifying CTE rejected | whole-AST walk ([`guard.py`](../querygate/guard.py)) | [`tests/test_guard.py`](../tests/test_guard.py) |
| Every call is audited | [`tools.py`](../querygate/tools.py) + [`audit.py`](../querygate/audit.py) | [`tests/test_audit.py`](../tests/test_audit.py) |
| Result can't flood/poison context | [`result.py`](../querygate/result.py) (byte cap, typed serialize) | [`tests/test_result.py`](../tests/test_result.py) |
| Answers cite real numbers | the grounding eval | [`evals/`](../evals/README.md) (distributional) |
| No safety/model logic in `api/` | the adapter discipline | [`tests/test_web_wiring.py`](../tests/test_web_wiring.py) (grep test) |
| Identifiers never injected | allowlist + bound params ([`tools.py`](../querygate/tools.py)) | [`tests/test_tools.py`](../tests/test_tools.py) |

See also: [security-model.md](security-model.md) (the boundary deep dive + threat model) and
[data-model.md](data-model.md) (the synthetic EHR/claims schema).
