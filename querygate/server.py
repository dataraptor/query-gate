"""The MCP server — a thin FastMCP shell around the Split-04/05 library (spec §6, §12-A, App A).

This module makes QueryGate a **real MCP server**: a :class:`FastMCP` app that registers the four
read-only tools, publishes a minimal well-formed ``inputSchema`` for each, ships the verbatim server
``instructions=`` (:data:`~querygate.prompts.SERVER_INSTRUCTIONS`), and runs over **stdio** so Claude
Desktop / Claude Code can connect.

**Nothing in the boundary lives here.** Every tool is a *thin wrapper*: it parses its args, calls the
matching library function (which runs the three-layer boundary + result filter + audit), and returns
that function's Pydantic result. A rejected/errored call raises a typed exception which FastMCP turns
into an MCP **tool error** the agent can read — the server process never crashes (spec §3/§18).

Per Appendix A, the server only publishes an ``inputSchema``; it does **not** set Anthropic's
consumer-side ``strict`` flag (that is the eval harness's job, Split 09).

Claude Desktop stdio config (spec §12-A), accurate for this entry point::

    { "mcpServers": { "querygate": { "command": "uv", "args": ["run", "querygate"] } } }
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from .config import Config
from .models import RunResult, TableInfo, TableSchema
from .prompts import SERVER_INSTRUCTIONS
from . import tools as _tools

__all__ = ["build_server", "main"]

#: The verbatim §6 tool descriptions (the trigger-condition phrasing — "Call this first…", "Call
#: before writing a query…"). Spec §6 notes these give a measurable should-call lift; do not shorten.
#: Exported so the test can assert the registered descriptions match §6 character-for-character.
TOOL_DESCRIPTIONS = {
    "list_tables": "List the tables available to query. Call this first to discover the schema.",
    "describe_table": (
        "Show a table's columns and types. Call before writing a query against it."
    ),
    "run_select": (
        "Run a single read-only SELECT and return the rows. Only SELECT is allowed; "
        "writes are rejected. Inspect the schema first."
    ),
    "search_text": (
        "Fuzzy-search text columns for a term when you don't know the exact value "
        "(e.g. a name spelled differently)."
    ),
}


def build_server(config: Config | None = None) -> FastMCP:
    """Build the ``querygate`` FastMCP app with the four tools registered.

    ``config`` is resolved once here (defaulting to :meth:`Config.from_env`) and closed over by every
    tool, so the published tool schemas never expose the ``config`` argument and a test can inject a
    test-DB/temp-audit config. The boundary logic is **imported, not re-implemented** — each tool
    forwards to the matching :mod:`querygate.tools` function.
    """
    cfg = config if config is not None else Config.from_env()
    mcp = FastMCP("querygate", instructions=SERVER_INSTRUCTIONS)

    @mcp.tool(name="list_tables", description=TOOL_DESCRIPTIONS["list_tables"])
    def list_tables() -> list[TableInfo]:
        return _tools.list_tables(config=cfg)

    @mcp.tool(name="describe_table", description=TOOL_DESCRIPTIONS["describe_table"])
    def describe_table(table: str) -> TableSchema:
        return _tools.describe_table(table, config=cfg)

    @mcp.tool(name="run_select", description=TOOL_DESCRIPTIONS["run_select"])
    def run_select(sql: str) -> RunResult:
        return _tools.run_select(sql, config=cfg)

    @mcp.tool(name="search_text", description=TOOL_DESCRIPTIONS["search_text"])
    def search_text(term: str, table: str | None = None) -> RunResult:
        return _tools.search_text(term, table, config=cfg)

    # Keep the published inputSchema minimal *and* closed: a one-field-per-arg schema plus
    # `additionalProperties: false`, so e.g. run_select advertises exactly {sql: str, required,
    # additionalProperties: false} (spec §6/§7). FastMCP derives the properties/required from the
    # function signature; we only tighten it to forbid extra keys.
    for name in TOOL_DESCRIPTIONS:
        tool = mcp._tool_manager.get_tool(name)
        tool.parameters.setdefault("additionalProperties", False)

    return mcp


# The Streamable-HTTP transport (spec §12-B). Bound to localhost only for the demo — a buyer's
# backend reaches it via the Messages API MCP connector. Bearer-token auth + a non-localhost bind
# are the §21 roadmap, NOT v1 (the §2/§19 scope fence): this server has no auth and never leaves
# the loopback interface.
HTTP_HOST = "127.0.0.1"  # loopback only — see the §19 scope fence above.
HTTP_PORT = 8000
HTTP_PATH = "/mcp"  # so the connector URL is http://localhost:8000/mcp (spec §12-B).


def build_http_server(
    host: str = HTTP_HOST, port: int = HTTP_PORT, config: Config | None = None
) -> FastMCP:
    """Build the same four-tool app, configured for the **Streamable HTTP** transport.

    Returns the configured (but **not yet running**) :class:`FastMCP` so a test can assert the bind
    address is loopback before anything listens. ``host`` defaults to ``127.0.0.1`` — passing a
    non-loopback address is possible but is the §21 roadmap, deliberately not exposed on the CLI.
    """
    mcp = build_server(config)
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.streamable_http_path = HTTP_PATH
    return mcp


def serve_http(
    host: str = HTTP_HOST, port: int = HTTP_PORT, config: Config | None = None
) -> None:
    """Run the four-tool app over Streamable HTTP on ``host:port`` at ``/mcp`` (blocks).

    The boundary is unchanged from stdio: every tool call routes through the same library functions,
    writes the same audit lines (under the Split-04 process lock — so concurrent in-flight HTTP
    requests can't interleave a half line, §9), and rejects writes identically.
    """
    build_http_server(host=host, port=port, config=config).run(transport="streamable-http")


def main() -> None:
    """Console-script / ``python -m querygate`` entry point: run the MCP server over **stdio**.

    The full CLI (``--http``, ``seed``, ``query``, ``eval``) is Split 07; here the default, no-arg
    behavior is the stdio server (spec §12-A / §15).
    """
    build_server().run()  # FastMCP.run() defaults to the stdio transport.


if __name__ == "__main__":
    main()
