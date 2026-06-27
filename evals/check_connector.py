"""Split 10 — the Messages-API **MCP connector** check (spec §12-B, Appendix A).

This is the *second* way Claude consumes QueryGate: over **Streamable HTTP**, via the Messages API's
MCP connector — the path a buyer's own backend uses. The deliverable here is the **exact** connector
call with **both halves** wired (the one thing most builds get wrong, App A):

    client.beta.messages.create(
        model="claude-opus-4-8", max_tokens=4096,
        betas=["mcp-client-2025-11-20"],
        mcp_servers=[{"type": "url", "name": "querygate", "url": "http://localhost:8000/mcp"}],
        tools=[{"type": "mcp_toolset", "mcp_server_name": "querygate"}],
        messages=[{"role": "user", "content": "How many patients are overdue for follow-up?"}],
    )

`mcp_servers` **and** a matching `mcp_toolset` with the **same name** are both required — omit the
`mcp_toolset` and the request 400s (H-7 asserts exactly that, so the contract is *proven*, not
assumed).

**Honesty (read before running).** This repo ships **no `ANTHROPIC_API_KEY`** (only an Azure GPT-5.5
deployment — see PROGRESS.md / Split 09). The connector is an Anthropic-only feature, so this check is
**on-demand**: without the key (or the `anthropic` package) it prints a clear message and exits
non-zero — it never fabricates a result. Note also that the connector fetches the MCP URL from
**Anthropic's servers**, so a bare `http://localhost:8000/mcp` is only reachable when this process and
the API share a host (or via a tunnel). Set `QUERYGATE_HTTP_URL` to a reachable URL to run it for real.

Functions here are importable so ``tests/test_connector.py`` reuses the exact request shape.
"""

from __future__ import annotations

import os
import sys
from typing import Any

MODEL = "claude-opus-4-8"  # spec Appendix A — exact id, no date suffix.
BETA = "mcp-client-2025-11-20"  # the connector beta header (App A).
SERVER_NAME = "querygate"  # the name that MUST match across both halves.
DEFAULT_URL = "http://localhost:8000/mcp"

HAPPY_MESSAGE = "How many patients are overdue for follow-up?"
WRITE_MESSAGE = "Delete the patients named Smith from the database."


def connector_available() -> tuple[bool, str]:
    """``(True, "")`` if the Anthropic SDK + key are present, else ``(False, reason)``."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False, "the `anthropic` SDK is not installed (pip install 'querygate[connector]')"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "ANTHROPIC_API_KEY is not set (this repo ships an Azure GPT-5.5 key, not Anthropic)"
    return True, ""


def connector_url() -> str:
    """The MCP server URL Anthropic should fetch (override via ``QUERYGATE_HTTP_URL`` for a tunnel)."""
    return os.environ.get("QUERYGATE_HTTP_URL", DEFAULT_URL)


def build_request(url: str, message: str, *, include_toolset: bool = True) -> dict[str, Any]:
    """The exact ``client.beta.messages.create`` kwargs (spec §12-B).

    With ``include_toolset=False`` the ``mcp_toolset`` half is omitted — the malformed request that
    the API rejects with a **400** (H-7). Everything else is identical so the 400 is unambiguous.
    """
    req: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": 4096,
        "betas": [BETA],
        "mcp_servers": [{"type": "url", "name": SERVER_NAME, "url": url}],
        "messages": [{"role": "user", "content": message}],
    }
    if include_toolset:
        req["tools"] = [{"type": "mcp_toolset", "mcp_server_name": SERVER_NAME}]
    return req


def answer_text(response: Any) -> str:
    """Concatenate the assistant's text blocks — *after* checking ``stop_reason`` (App A).

    Never index ``content[0]`` blindly: a ``refusal`` stop_reason yields no text block.
    """
    if getattr(response, "stop_reason", None) == "refusal":
        return ""
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


def run_connector(client: Any, url: str, message: str, *, include_toolset: bool = True) -> Any:
    """Make one connector call and return the raw response (or raise the API's error)."""
    return client.beta.messages.create(**build_request(url, message, include_toolset=include_toolset))


def main() -> int:
    ok, reason = connector_available()
    if not ok:
        print(f"connector check skipped: {reason}", file=sys.stderr)
        print(
            "  The HTTP transport itself is fully tested keyless in tests/test_http_transport.py "
            "(H-1..H-4). This script proves the Messages-API connector path and needs a real key.",
            file=sys.stderr,
        )
        return 2

    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    import anthropic

    url = connector_url()
    client = anthropic.Anthropic()
    print(f"connector → {url}  (model={MODEL}, beta={BETA})")

    # H-5 — happy path, both halves.
    resp = run_connector(client, url, HAPPY_MESSAGE)
    print(f"  H-5 stop_reason={resp.stop_reason}")
    print(f"  H-5 answer: {answer_text(resp)[:400]}")

    # H-6 — a write request is refused with no write executed (boundary parity with stdio).
    wresp = run_connector(client, url, WRITE_MESSAGE)
    print(f"  H-6 stop_reason={wresp.stop_reason}")
    print(f"  H-6 answer: {answer_text(wresp)[:400]}")

    # H-7 — omitting the mcp_toolset half is a documented 400 (App A).
    try:
        run_connector(client, url, HAPPY_MESSAGE, include_toolset=False)
        print("  H-7 UNEXPECTED: missing mcp_toolset did NOT 400", file=sys.stderr)
        return 1
    except anthropic.APIStatusError as exc:
        print(f"  H-7 missing-mcp_toolset → {exc.status_code} (as documented, App A)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
