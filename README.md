# query-gate

> Full README (overview, demo, architecture) is authored in the final polish split. This stub
> records only what later splits must not lose.

## Transports

QueryGate is one server, reachable two ways (the boundary and audit guarantees are identical on both):

- **stdio** (default) — `querygate`. Drives Claude Desktop / Claude Code and the in-process eval.
- **Streamable HTTP** — `querygate --http [--port 8000]`. Serves the same four tools at
  `http://localhost:8000/mcp`, the URL the Messages API **MCP connector** points at:

  ```python
  client.beta.messages.create(
      model="claude-opus-4-8", max_tokens=4096,
      betas=["mcp-client-2025-11-20"],
      mcp_servers=[{"type": "url", "name": "querygate", "url": "http://localhost:8000/mcp"}],
      tools=[{"type": "mcp_toolset", "mcp_server_name": "querygate"}],
      messages=[{"role": "user", "content": "How many patients are overdue for follow-up?"}],
  )
  ```

  Both halves are required — `mcp_servers` **and** a matching `mcp_toolset` with the same name — or
  the request 400s (Appendix A). Verify with `python evals/check_connector.py` (needs an
  `ANTHROPIC_API_KEY`; on-demand, not the per-commit gate).

> **Scope fence (spec §2/§19/§21).** The HTTP transport binds **localhost (127.0.0.1) only**. There is
> **no authentication** and the server never leaves the loopback interface — for the demo. **Bearer-token
> auth and a non-localhost (network-exposed) bind are the §21 roadmap, not v1.** Do not expose this
> server to a network without adding auth first.
