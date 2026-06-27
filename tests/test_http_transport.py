"""Split 10 — the Streamable-HTTP transport (spec §12-B), keyless + CI-safe.

These tests prove the **second transport**: ``querygate --http`` serves the *same* four-tool app
over Streamable HTTP, bound to **localhost only**, and the boundary/audit guarantees are unchanged
from stdio. They spend **no** API tokens — an MCP **protocol** client (not the Anthropic API) drives
the running server. The keyed Messages-API connector tests live in ``test_connector.py``.

* **H-1** — ``querygate --http --port <p>`` starts and the ``/mcp`` endpoint is reachable on
  ``127.0.0.1:<p>``.
* **H-2** — localhost-only: the bind host is loopback, and the port is **not** reachable on the
  machine's non-loopback (LAN) address (the §2/§19 scope fence).
* **H-3** — an MCP streamable-http client lists the same four tools, gets the correct ``run_select``
  result (overdue = 300, cited SQL + ``row_count``), and a write is a clean tool error.
* **H-4** — concurrent in-flight HTTP tool calls each write **exactly one** well-formed audit line;
  no half-lines interleaved under concurrency (the §9 audit-lock proven, not just claimed).

The H-2 bind-config assertion is pure and always runs. The server-backed tests use the read-only role
via the shared conftest and skip cleanly when ``DATABASE_URL`` / ``psql`` is absent.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from querygate.server import HTTP_HOST, HTTP_PATH, build_http_server

REPO_ROOT = Path(__file__).resolve().parent.parent
REGISTERED = {"list_tables", "describe_table", "run_select", "search_text"}
OVERDUE_SQL = (
    "SELECT count(*) FROM app.follow_ups WHERE completed_at IS NULL AND due_date < now()"
)
EXPECTED_OVERDUE = 300  # Split-01 deterministic contract

# A minimal MCP `initialize` body — used only as an HTTP readiness probe (a protocol method, not a
# tool call, so it writes no audit line). When this returns 200 the session manager's lifespan is up.
_INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "querygate-test", "version": "0"},
    },
}
_INIT_HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


def _free_port() -> int:
    """Grab an unused localhost TCP port (small TOCTOU race, fine for a test harness)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _lan_ip() -> str | None:
    """This host's primary non-loopback IPv4, or ``None`` if offline. Sends no packets."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no traffic — just picks the egress interface
        ip = s.getsockname()[0]
        return ip if ip and not ip.startswith("127.") else None
    except OSError:
        return None
    finally:
        s.close()


def _http_ready(url: str, proc: subprocess.Popen, timeout: float = 30.0) -> bool:
    """Poll the MCP endpoint with a real ``initialize`` until it 200s (not just a TCP connect).

    A bare TCP connect succeeds the instant uvicorn opens its socket — *before* the session
    manager's lifespan is running, when requests 405. Gating on a 200 ``initialize`` avoids that race.
    """
    end = time.time() + timeout
    while time.time() < end:
        if proc.poll() is not None:
            return False
        try:
            if httpx.post(url, json=_INIT_BODY, headers=_INIT_HEADERS, timeout=2.0).status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


@contextmanager
def _running_http_server(role_url: str, audit_path: Path, port: int | None = None):
    """Launch ``python -m querygate --http --port <p>`` as a real subprocess and wait until ready.

    Yields ``(port, url)``. The subprocess reads its config from the env (read-only role URL +
    a per-test audit file), so the boundary it enforces is identical to stdio. Killed on exit.
    """
    port = port or _free_port()
    env = dict(os.environ)
    env["QUERYGATE_DATABASE_URL"] = role_url
    env["QUERYGATE_AUDIT_PATH"] = str(audit_path)
    proc = subprocess.Popen(
        [sys.executable, "-m", "querygate", "--http", "--port", str(port)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(REPO_ROOT),
    )
    url = f"http://127.0.0.1:{port}{HTTP_PATH}"
    try:
        if not _http_ready(url, proc):
            raise RuntimeError("querygate --http did not become ready (the /mcp endpoint never 200ed)")
        yield port, url
    finally:
        proc.kill()  # uvicorn holds SSE streams open; kill is the reliable cross-platform stop.
        proc.wait()


@pytest.fixture()
def http_server(role_url: str, seeded_db, tmp_path):
    """A running ``--http`` server against the read-only role + a fresh per-test audit log."""
    audit = tmp_path / "audit.jsonl"
    with _running_http_server(role_url, audit) as (port, url):
        yield port, url, audit


def _audit_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ==================================================================================================
# Async MCP-client helpers (streamable-http transport).
# ==================================================================================================


async def _list_tool_names(url: str) -> set[str]:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return {t.name for t in (await session.list_tools()).tools}


async def _call(url: str, name: str, args: dict):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, args)


async def _concurrent_calls(url: str, name: str, args: dict, n: int):
    """Fire ``n`` tool calls concurrently over a single session (in-flight requests overlap)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await asyncio.gather(*[session.call_tool(name, args) for _ in range(n)])


def _run(coro, timeout: float = 40.0):
    return asyncio.run(asyncio.wait_for(coro, timeout=timeout))


def _structured(result):
    """The structured payload of a CallToolResult; a list-returning tool wraps it under 'result'."""
    sc = result.structuredContent
    if isinstance(sc, dict) and set(sc.keys()) == {"result"}:
        return sc["result"]
    return sc


# ==================================================================================================
# H-1 — the server starts and /mcp is reachable on localhost.
# ==================================================================================================


def test_h1_http_server_starts_on_localhost(http_server):
    port, url, _ = http_server
    # Reachable on the loopback interface: a TCP connect succeeds…
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass
    # …and the /mcp endpoint answers an MCP initialize with 200 (the protocol really runs here).
    r = httpx.post(url, json=_INIT_BODY, headers=_INIT_HEADERS, timeout=5)
    assert r.status_code == 200
    assert "event: message" in r.text or '"result"' in r.text


# ==================================================================================================
# H-2 — localhost-only (the §2/§19 scope fence): loopback bind, no non-loopback reach.
# ==================================================================================================


def test_h2_bind_host_is_loopback():
    # Pure assertion (no DB, always runs): the HTTP app is configured to bind loopback only.
    assert HTTP_HOST == "127.0.0.1"
    assert build_http_server().settings.host == "127.0.0.1"


def test_h2_not_reachable_on_non_loopback(http_server):
    port, _, _ = http_server
    lan = _lan_ip()
    if not lan:
        pytest.skip("no non-loopback interface to prove the negative against")
    # The server bound 127.0.0.1 only, so its port must NOT accept a connection on the LAN address.
    with pytest.raises((ConnectionRefusedError, OSError)):
        with socket.create_connection((lan, port), timeout=2):
            pass
    # Sanity: the same port DOES accept on loopback (so the negative above isn't trivially true).
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass


# ==================================================================================================
# H-3 — tools + rejection over HTTP via the MCP protocol client (no API tokens).
# ==================================================================================================


def test_h3_tools_list_over_http(http_server):
    _, url, _ = http_server
    assert _run(_list_tool_names(url)) == REGISTERED


def test_h3_run_select_happy_path_over_http(http_server):
    _, url, _ = http_server
    result = _run(_call(url, "run_select", {"sql": OVERDUE_SQL}))
    assert result.isError is False
    payload = _structured(result)
    assert payload["row_count"] == 1
    assert payload["rows"] == [[EXPECTED_OVERDUE]]
    assert "count(*)" in payload["sql"].lower()  # the citation survived the connector round-trip
    assert payload["truncated"] is False


def test_h3_write_is_clean_tool_error_over_http(http_server):
    _, url, _ = http_server
    bad = _run(_call(url, "run_select", {"sql": "DELETE FROM app.patients WHERE name LIKE 'Smith%'"}))
    assert bad.isError is True  # the boundary holds over HTTP — a protocol tool error, not a crash
    msg = bad.content[0].text.lower()
    assert "delete" in msg or "select" in msg or "data-modifying" in msg


# ==================================================================================================
# H-4 — audit parity + no interleave under concurrency (the §9 lock).
# ==================================================================================================


def test_h4_concurrent_calls_each_audit_one_clean_line(http_server):
    _, url, audit = http_server
    n = 8
    results = _run(_concurrent_calls(url, "list_tables", {}, n))
    assert all(r.isError is False for r in results)  # all 8 in-flight calls succeeded

    # Exactly one well-formed JSONL line per call — every line parses (no half-line interleaved
    # under the audit lock), and each is the expected (tool, status).
    raw = audit.read_text(encoding="utf-8").splitlines()
    parsed = [json.loads(ln) for ln in raw if ln.strip()]  # raises if any line is a half-write
    assert len(parsed) == n
    assert all(ln["tool"] == "list_tables" and ln["status"] == "ok" for ln in parsed)
    # And every line carries the full audit shape (no truncated record slipped through).
    for ln in parsed:
        assert {"ts", "tool", "status"} <= set(ln)
