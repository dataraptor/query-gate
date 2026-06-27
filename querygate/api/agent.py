"""querygate/api/agent.py — drive ONE question through the real agent loop, streaming UI events.

This is the live half the dummy engine (``app/QueryGate Demo.dc.html`` ``run()`` on a timer) only
*simulates*. It **reuses Split 09's in-process tool-runner** (``evals/run_eval``): the same four
``querygate`` tools, the same verbatim prompts, the **same three-layer boundary**. Here the loop is a
*generator* that ``yield``s the UI-shaped events (:mod:`querygate.api.mapping`) as they happen, so
Split 12 can animate the proof rail live (schema discovery → query → boundary verdict → cited answer).

Two drive modes share one execute-and-emit core:

* **live**     — an LLM (Azure GPT-5.5; this repo has no Anthropic key, the documented Split 09
  deviation) chooses which tools to call. Needs a model key.
* **scripted** — a fixed ``[(tool, args), …]`` sequence runs through the **real** library tools
  (which need only the DB, no key). Used by the shape tests so W-1/W-3/W-4/W-5/W-8 run **keyless** in
  CI. A scripted run still exercises the real boundary, writes real audit lines, and produces real
  ``RunResult``s — only "which tool next" is fixed instead of model-chosen. It is a **test fixture**,
  never shipped as canned demo data.

**No canned data on the live path** (R1): every number, SQL string, row and boundary verdict comes
from an actual tool result this turn. Every tool call's audit line is the **real** one the library
wrote (read back from this run's audit file) — never synthesized (R2/W-8).

Transport note. The Split-09 runner executes the tools **in-process** (not over the stdio subprocess
nor the HTTP connector), so the honest ``transport`` label is ``"in-process"``. The boundary the tools
enforce is byte-for-byte the same one the stdio/HTTP server wraps.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Iterator

from querygate.config import Config

from . import mapping

#: The Split-09 in-process tool-runner executes tools directly; identical boundary to stdio/HTTP.
TRANSPORT = "in-process"

# Make ``import evals.run_eval`` work when the adapter is imported from anywhere (the ``evals/`` tree
# is run in place, not installed as a package) — mirror run_eval's own sys.path bootstrap.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _count_lines(path: str | Path) -> int:
    p = Path(path)
    if not p.exists():
        return 0
    return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())


def _read_audit_lines(path: str | Path) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _default_model() -> str:
    from evals import run_eval as _re

    return _re.DEFAULT_MODEL


def _emit_tool(tool: str, args: dict, config: Config, state: dict) -> Iterator[dict]:
    """Execute one tool through the real library (which audits) and yield its UI events.

    Yields, in order: a ``step-start`` (status running), then after execution a ``step-end`` (the
    resolved step + boundary verdict) and an ``audit-line`` (the real :class:`AuditLine` the tool
    just wrote). Stashes ``(content, status, data)`` and updates the citation/refusal bookkeeping in
    ``state`` so the caller (live loop or scripted loop) can feed the result back to the model.
    """
    from evals import run_eval as _re

    yield {"type": "step-start", "step": mapping.running_step(tool, args)}

    content, status, error, data = _re._execute_tool(tool, args, config)

    # Read the REAL audit line this call just appended (one line per call — W-8). The per-run audit
    # file means the new tail line is unambiguously this call's.
    lines = _read_audit_lines(config.audit_path)
    new = lines[state["audit_seen"]:]
    state["audit_seen"] = len(lines)
    line = new[-1] if new else None

    # row_count for the step: the real audit line's value (a number for run/list, None for a
    # rejected/errored call). Falls back to the data payload if the line is somehow missing.
    if line is not None:
        row_count = line.get("row_count")
        latency = line.get("latency_ms")
        redactions = line.get("redactions", [])
    else:  # pragma: no cover - the library always audits; defensive only
        row_count = data.get("row_count") if isinstance(data, dict) else (
            len(data) if isinstance(data, list) else None
        )
        latency = None
        redactions = []

    step = mapping.tool_step(
        tool, args, status, latency_ms=latency, row_count=row_count,
        error=error, redactions=redactions,
    )
    yield {"type": "step-end", "step": step}
    if line is not None:
        yield {"type": "audit-line", "line": mapping.audit_line_to_ui(line)}

    # Citation / refusal bookkeeping (boundary tools only).
    if tool in mapping.BOUNDARY_TOOLS:
        if status == "rejected":
            state["had_rejection"] = True
            state["last_reject_reason"] = error or "rejected by the boundary"
        elif status == "ok" and isinstance(data, dict):
            state["last_result"] = data

    state["last"] = (content, status, data)


def _final_message_event(answer: str, state: dict, config: Config, model: str, usage: dict) -> dict:
    """Build the final ``message`` event (answer or refusal) + run-level cost/model/transport.

    Kind is derived from the real run, not the model's say-so:

    * a successful ``run_select`` / ``search_text`` result to cite → ``answer`` (cited honestly);
    * else a real boundary rejection happened → ``refusal`` with the **real** guard reason;
    * else (the agent never grounded anything) → ``refusal``, stated honestly.
    """
    if state.get("last_result") is not None:
        citation = mapping.result_to_citation(state["last_result"], row_limit=config.row_limit)
        message = mapping.answer_message(answer, citation)
    elif state.get("had_rejection"):
        message = mapping.refusal_message(answer, state["last_reject_reason"])
    else:
        message = mapping.refusal_message(
            answer or "I could not answer that from the data.",
            "the agent did not produce a grounded result to cite",
        )
    return {
        "type": "message",
        "message": message,
        "cost": mapping.cost_usd(usage, model),
        "model": model,
        "transport": TRANSPORT,
    }


def _new_state() -> dict:
    return {
        "audit_seen": 0,
        "last_result": None,
        "had_rejection": False,
        "last_reject_reason": "",
        "last": (None, None, None),
    }


# ==================================================================================================
# Scripted drive (keyless) — a fixed tool sequence through the real boundary. A test fixture.
# ==================================================================================================


def stream_scripted(
    question: str,
    scripted_calls: list[tuple[str, dict]],
    *,
    config: Config,
    final_answer: str = "",
    model: str | None = None,
) -> Iterator[dict]:
    """Replay a fixed ``[(tool, args), …]`` sequence through the real tools, streaming UI events.

    Keyless: needs only the DB. Used by the shape tests. ``model`` defaults to the live model id so
    the event shape is identical to the live path (cost is 0 — no model actually ran).
    """
    model = model or _default_model()
    state = _new_state()
    state["audit_seen"] = _count_lines(config.audit_path)
    usage = {"input": 0, "output": 0, "cached": 0}
    for tool, args in scripted_calls:
        yield from _emit_tool(tool, args, config, state)
    yield _final_message_event(final_answer, state, config, model, usage)


# ==================================================================================================
# Live drive (keyed) — an LLM chooses tools. Mirrors evals/run_eval.run_agent, but streaming.
# ==================================================================================================


def stream_live(
    question: str,
    *,
    config: Config,
    client=None,
    model: str | None = None,
    max_steps: int = 8,
) -> Iterator[dict]:
    """Drive the agent over the four tools with a live model, streaming UI events to the caller.

    The loop bookkeeping (messages, usage, ``stop_reason`` checked **before** reading content) is the
    same as ``evals/run_eval.run_agent``; the difference is that each tool call is executed and
    emitted via :func:`_emit_tool` so the proof rail animates as it happens.
    """
    from evals import run_eval as _re

    model = model or _default_model()
    if client is None:
        client = _re._build_client()

    tools = _re._openai_tools()
    messages: list[dict] = [
        {"role": "system", "content": _re.EVAL_SYSTEM_PROMPT},
        {"role": "user", "content": _re.EVAL_USER_TURN_TEMPLATE.format(question=question)},
    ]
    usage = {"input": 0, "output": 0, "cached": 0}
    state = _new_state()
    state["audit_seen"] = _count_lines(config.audit_path)
    answer = ""

    for _ in range(max_steps):
        resp = client.chat.completions.create(
            model=model, messages=messages, tools=tools, max_completion_tokens=4096,
        )
        u = getattr(resp, "usage", None)
        if u is not None:
            usage["input"] += getattr(u, "prompt_tokens", 0) or 0
            usage["output"] += getattr(u, "completion_tokens", 0) or 0
            details = getattr(u, "prompt_tokens_details", None)
            if details is not None:
                usage["cached"] += getattr(details, "cached_tokens", 0) or 0

        choice = resp.choices[0]
        message = choice.message

        # Check refusal / stop_reason BEFORE reading content (spec R2, App A).
        refusal = getattr(message, "refusal", None)
        if refusal:
            answer = refusal
            break
        if choice.finish_reason == "content_filter":
            answer = message.content or ""
            break

        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            messages.append(
                {
                    "role": "assistant",
                    "content": message.content,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                        }
                        for tc in tool_calls
                    ],
                }
            )
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                yield from _emit_tool(name, args, config, state)
                content, _status, _data = state["last"]
                messages.append(
                    {"role": "tool", "tool_call_id": tc.id, "content": json.dumps(content, default=str)}
                )
            continue

        # No tool calls → this is the final answer.
        answer = message.content or ""
        break

    yield _final_message_event(answer, state, config, model, usage)


def stream_run(
    question: str,
    *,
    config: Config,
    client=None,
    model: str | None = None,
    scripted_calls: list[tuple[str, dict]] | None = None,
    final_answer: str = "",
    max_steps: int = 8,
) -> Iterator[dict]:
    """Single entry point: stream a run for ``question`` as UI events.

    Live by default (an LLM picks tools); pass ``scripted_calls`` to replay a fixed sequence keylessly.
    """
    if scripted_calls is not None:
        yield from stream_scripted(
            question, scripted_calls, config=config, final_answer=final_answer, model=model
        )
    else:
        yield from stream_live(
            question, config=config, client=client, model=model, max_steps=max_steps
        )
