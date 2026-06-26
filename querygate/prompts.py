"""Canonical prompts & server instructions (spec §10), behind ``PROMPT_VERSION``.

There is **no system prompt the *server* owns at runtime** — the server's only "prompt" is the
FastMCP ``instructions=`` string the agent reads on connect (:data:`SERVER_INSTRUCTIONS`). The eval
harness (Split 09) supplies the *agent's* system prompt (:data:`EVAL_SYSTEM_PROMPT`) and the
per-question user turn (:data:`EVAL_USER_TURN_TEMPLATE`); those live here too so **every** prompt the
product ships sits in one versioned file.

⚠️ These strings are **verbatim** from spec §10 and are part of the behavior contract the grounding
eval depends on. Do not paraphrase, reword, reflow, or "improve" them. Any change to a prompt **must**
bump :data:`PROMPT_VERSION` (the eval records it on every run for reproducibility — spec §11/§14).
"""

from __future__ import annotations

__all__ = [
    "PROMPT_VERSION",
    "SERVER_INSTRUCTIONS",
    "EVAL_SYSTEM_PROMPT",
    "EVAL_USER_TURN_TEMPLATE",
]

#: Stable id for the prompt set. Bump on **any** change to a prompt below (date-based; spec §11).
PROMPT_VERSION = "2026-06-27"

#: The FastMCP ``instructions=`` string the agent reads on connect (spec §10) — **verbatim**.
#: This is the only "prompt" the server owns at runtime (Appendix A).
SERVER_INSTRUCTIONS = """This server answers questions over a read-only SQL database.

- Call list_tables first to see what exists, then describe_table before writing
  a query against a table you have not inspected.
- Use run_select to run a single read-only SELECT. Only SELECT is allowed;
  any write (INSERT/UPDATE/DELETE/DROP/...) is rejected by design — do not
  attempt one, and do not apologize at length if a write is refused.
- When you don't know an exact text value (a name spelled differently), use
  search_text before giving up.
- In every answer, cite the exact SQL you ran and the row_count it returned.
  Never state a number you did not retrieve from a tool result."""

#: The system prompt for the *agent under test* in the grounding eval (spec §10) — **verbatim**.
#: Supplied by the eval harness (Split 09), not by the server. Kept here so all prompts are versioned.
EVAL_SYSTEM_PROMPT = """You answer questions about a database using the provided tools. Discover the
schema before querying. Ground every numeric or factual claim in an actual
run_select result and cite the SQL and row count. If a request would change
data, explain that the database is read-only and do not attempt the write."""

#: The eval user-turn format (spec §10): the question goes in the **user** turn, after the cached
#: prefix — never interpolated into the system prompt (a silent cache invalidator, spec §14).
EVAL_USER_TURN_TEMPLATE = "QUESTION:\n{question}"
