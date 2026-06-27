"""Split 06 — the MCP server, driven in-process via the SDK's in-memory client (spec §6, §12-A).

These tests prove the **transport-and-protocol shell** around the Split-04/05 library, not the
boundary itself (that is ``test_boundary.py``). They drive the real :class:`FastMCP` app through the
``mcp`` SDK's in-memory client session — no live LLM, no API key — and assert:

* **S-1 / S-3** the server lists exactly the four registered tools with the **verbatim §6
  descriptions**, and advertises the **verbatim §10 ``instructions=``** (byte-exact).
* **S-2** each tool's published ``inputSchema`` is minimal and closed (``additionalProperties:false``);
  the server does **not** set Anthropic's consumer-side ``strict`` (Appendix A).
* **S-4 / S-6** the protocol round-trip preserves the Split-04/05 result shapes (RunResult /
  TableSchema / the list_tables shape), validated against their Pydantic models.
* **S-5** a write surfaces as a clean MCP **tool error** and the server **does not crash**.
* **S-7** N protocol calls → exactly N audit lines of the right status (no double/skip logging).

S-1/S-2/S-3 are pure (no DB) and always run. The DB-backed S-4/S-5/S-6/S-7 use the read-only role
via the shared conftest and skip cleanly when ``DATABASE_URL`` / ``psql`` is absent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from querygate.config import Config
from querygate.models import RunResult, TableInfo, TableSchema
from querygate.prompts import PROMPT_VERSION, SERVER_INSTRUCTIONS
from querygate.server import TOOL_DESCRIPTIONS, build_server
from mcp.shared.memory import create_connected_server_and_client_session as connect

REGISTERED = {"list_tables", "describe_table", "run_select", "search_text", "explain_select"}
APP_TABLES = {"patients", "providers", "encounters", "claims", "follow_ups"}


# ==================================================================================================
# In-process MCP client helpers (run a coroutine against a server's low-level app).
# ==================================================================================================


def _run(coro):
    return asyncio.run(coro)


async def _list_tools(server):
    async with connect(server._mcp_server) as client:
        return (await client.list_tools()).tools


async def _instructions_over_protocol(server) -> str | None:
    async with connect(server._mcp_server) as client:
        return (await client.initialize()).instructions


def _structured(result):
    """The structured payload of a CallToolResult. A list-returning tool wraps it under 'result'."""
    sc = result.structuredContent
    if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
        return sc["result"]
    return sc


def _audit_lines(cfg: Config) -> list[dict]:
    p = Path(cfg.audit_path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ==================================================================================================
# Fixtures.
# ==================================================================================================


@pytest.fixture()
def meta_server():
    """A server with no DB configured — enough for the pure protocol/schema/prompt tests."""
    return build_server(Config())


@pytest.fixture()
def ro_config(role_url: str, seeded_db, tmp_path) -> Config:
    """The read-only role + a fresh temp audit log (one per test)."""
    return Config(database_url=role_url, audit_path=str(tmp_path / "audit.jsonl"))


@pytest.fixture()
def server(ro_config: Config):
    return build_server(ro_config)


# ==================================================================================================
# S-1 — tools listed, verbatim §6 descriptions.
# ==================================================================================================


def test_s1_lists_exactly_the_registered_tools(meta_server):
    tools = _run(_list_tools(meta_server))
    assert {t.name for t in tools} == REGISTERED


def test_s1_tool_descriptions_are_verbatim_per_section6(meta_server):
    tools = {t.name: t for t in _run(_list_tools(meta_server))}
    for name in REGISTERED:
        # character-for-character match against the §6 string (so a paraphrase can't slip in).
        assert tools[name].description == TOOL_DESCRIPTIONS[name]
    # And the source-of-truth constants themselves are the literal §6 strings.
    assert TOOL_DESCRIPTIONS["run_select"] == (
        "Run a single read-only SELECT and return the rows. Only SELECT is allowed; "
        "writes are rejected. Inspect the schema first."
    )
    assert TOOL_DESCRIPTIONS["list_tables"] == (
        "List the tables available to query. Call this first to discover the schema."
    )
    assert TOOL_DESCRIPTIONS["describe_table"] == (
        "Show a table's columns and types. Call before writing a query against it."
    )
    assert TOOL_DESCRIPTIONS["search_text"] == (
        "Fuzzy-search text columns for a term when you don't know the exact value "
        "(e.g. a name spelled differently)."
    )
    assert TOOL_DESCRIPTIONS["explain_select"] == (
        "Show the query plan and estimated cost for a SELECT without running it. "
        "Use to check a heavy query before running it."
    )


# ==================================================================================================
# S-2 — inputSchema minimal + closed; no `strict`.
# ==================================================================================================


def test_s2_run_select_input_schema_minimal_and_closed(meta_server):
    schema = {t.name: t.inputSchema for t in _run(_list_tools(meta_server))}["run_select"]
    assert schema["type"] == "object"
    assert set(schema["properties"]) == {"sql"}
    assert schema["properties"]["sql"]["type"] == "string"
    assert schema["required"] == ["sql"]
    assert schema["additionalProperties"] is False


def test_s2_describe_and_search_schemas_match_signatures(meta_server):
    schema = {t.name: t.inputSchema for t in _run(_list_tools(meta_server))}
    # describe_table(table: str) — table required.
    d = schema["describe_table"]
    assert set(d["properties"]) == {"table"} and d["required"] == ["table"]
    assert d["additionalProperties"] is False
    # search_text(term: str, table: str | None = None) — term required, table optional.
    s = schema["search_text"]
    assert set(s["properties"]) == {"term", "table"}
    assert s["required"] == ["term"]  # `table` is optional → not required
    assert s["additionalProperties"] is False


def test_s2_server_does_not_claim_anthropic_strict(meta_server):
    # Per Appendix A the MCP server only publishes an inputSchema; `strict` is a consumer-side flag.
    # The real contract: no published tool (schema or model dump) carries a `strict` key.
    for t in _run(_list_tools(meta_server)):
        assert "strict" not in t.inputSchema
        dumped = t.model_dump()
        assert "strict" not in dumped
        assert "strict" not in (dumped.get("annotations") or {})
    # …and the module never *sets* strict anywhere (the docstring may mention it to say it doesn't).
    src = Path(build_server.__code__.co_filename).read_text(encoding="utf-8").lower()
    assert "strict=true" not in src and '"strict"' not in src and "'strict'" not in src


# ==================================================================================================
# S-3 — instructions verbatim + PROMPT_VERSION.
# ==================================================================================================


def test_s3_instructions_verbatim_per_section10(meta_server):
    # The server's advertised instructions equal the §10 string exactly.
    assert meta_server.instructions == SERVER_INSTRUCTIONS
    # And the same string is what the client reads over the protocol on connect.
    assert _run(_instructions_over_protocol(meta_server)) == SERVER_INSTRUCTIONS
    # PROMPT_VERSION is exposed and stable.
    assert isinstance(PROMPT_VERSION, str) and PROMPT_VERSION


def test_s3_instructions_are_the_literal_section10_text(meta_server):
    # A second, independent transcription guards against an accidental edit to the constant.
    assert SERVER_INSTRUCTIONS.startswith(
        "This server answers questions over a read-only SQL database."
    )
    assert "Only SELECT is allowed" in SERVER_INSTRUCTIONS
    assert "cite the exact SQL you ran and the row_count it returned" in SERVER_INSTRUCTIONS
    assert SERVER_INSTRUCTIONS.endswith(
        "Never state a number you did not retrieve from a tool result."
    )


# ==================================================================================================
# S-4 — happy path preserves RunResult over the protocol.
# ==================================================================================================

OVERDUE_SQL = (
    "SELECT count(*) FROM app.follow_ups "
    "WHERE completed_at IS NULL AND due_date < now()"
)


def test_s4_run_select_happy_path_round_trip(server):
    async def go():
        async with connect(server._mcp_server) as client:
            return await client.call_tool("run_select", {"sql": OVERDUE_SQL})

    result = _run(go())
    assert result.isError is False
    rr = RunResult.model_validate(_structured(result))
    assert rr.row_count == 1
    assert rr.rows == [[300]]  # the durably-overdue band (spec §11 / Split 01)
    assert rr.sql and "count(*)".lower() in rr.sql.lower()
    assert rr.truncated is False


# ==================================================================================================
# S-5 — a write surfaces as a tool error; the server stays alive.
# ==================================================================================================


def test_s5_write_is_a_clean_tool_error_no_crash(server):
    async def go():
        async with connect(server._mcp_server) as client:
            bad = await client.call_tool(
                "run_select", {"sql": "DELETE FROM app.patients WHERE name LIKE 'Smith%'"}
            )
            # …and the same connection still serves a follow-up call (process intact).
            alive = await client.call_tool("list_tables", {})
            return bad, alive

    bad, alive = _run(go())
    # The protocol surfaces a tool error (isError), not a transport crash.
    assert bad.isError is True
    msg = bad.content[0].text.lower()
    assert "delete" in msg or "select" in msg  # the guard reason is carried to the agent
    # Server still alive: the follow-up discovery call succeeds.
    assert alive.isError is False
    assert {t["table"] for t in _structured(alive)} == APP_TABLES


# ==================================================================================================
# S-6 — the discovery tools over the protocol.
# ==================================================================================================


def test_s6_discovery_tools_over_protocol(server):
    async def go():
        async with connect(server._mcp_server) as client:
            lt = await client.call_tool("list_tables", {})
            dt = await client.call_tool("describe_table", {"table": "follow_ups"})
            st = await client.call_tool("search_text", {"term": "Sara", "table": "patients"})
            return lt, dt, st

    lt, dt, st = _run(go())

    tables = [TableInfo.model_validate(x) for x in _structured(lt)]
    assert {t.table for t in tables} == APP_TABLES
    assert all(t.est_rows > 0 for t in tables)

    schema = TableSchema.model_validate(_structured(dt))
    assert schema.table == "follow_ups"
    pk = {c.name for c in schema.columns if c.is_pk}
    assert pk == {"follow_up_id"}
    refs = {c.name: c.references for c in schema.columns if c.references}
    assert refs.get("patient_id") == "patients.patient_id"

    search = RunResult.model_validate(_structured(st))
    assert ["patients", "name", "Sarah Lee"] in search.rows  # the near-duplicate case (Split 01)


# ==================================================================================================
# S-7 — exactly one audit line per protocol call.
# ==================================================================================================


def test_s7_one_audit_line_per_protocol_call(server, ro_config):
    async def go():
        async with connect(server._mcp_server) as client:
            await client.call_tool("list_tables", {})  # ok
            await client.call_tool("describe_table", {"table": "follow_ups"})  # ok
            await client.call_tool("run_select", {"sql": OVERDUE_SQL})  # ok
            await client.call_tool("run_select", {"sql": "DROP TABLE app.patients"})  # rejected
            await client.call_tool("describe_table", {"table": "pg_authid"})  # rejected (allowlist)

    _run(go())
    lines = _audit_lines(ro_config)
    assert len(lines) == 5  # exactly one per call — no double/skip logging via the wrapper
    statuses = [(ln["tool"], ln["status"]) for ln in lines]
    assert statuses == [
        ("list_tables", "ok"),
        ("describe_table", "ok"),
        ("run_select", "ok"),
        ("run_select", "rejected"),
        ("describe_table", "rejected"),
    ]


# ==================================================================================================
# Server-is-thin check (rubric: no boundary logic re-implemented — imported only).
# ==================================================================================================


def test_server_module_is_thin_imports_the_library():
    src = Path(build_server.__code__.co_filename).read_text(encoding="utf-8")
    # The server wraps the library; it must not import psycopg/sqlglot or re-implement the guard/txn.
    assert "import psycopg" not in src and "import sqlglot" not in src
    assert "guard_select" not in src and "run_readonly" not in src
    assert "from . import tools" in src  # forwards to the library functions
