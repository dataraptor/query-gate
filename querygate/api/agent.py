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
import re
import sys
from pathlib import Path
from typing import Iterator

from querygate.config import Config

from . import mapping

#: The Split-09 in-process tool-runner executes tools directly; identical boundary to stdio/HTTP.
TRANSPORT = "in-process"

#: A request that asks to **modify** data (Split 12 R4 / U-2 — the "Real forced boundary demo").
#: The agent under test never attempts a write (its prompt forbids it — :data:`EVAL_SYSTEM_PROMPT`),
#: so a well-behaved live turn refuses *proactively* and nothing reaches the boundary. To show the UI
#: the guard actually rejecting a write (the headline §6 moment), a write request is routed to a
#: deterministic **boundary demonstration**: the implied write is submitted to the **real** ``run_select``
#: guard, which rejects it for real (real rule, real reason, real audit line). Keyword detection, kept
#: deliberately simple — a false positive only means a harmless read-only SELECT is attempted (and runs).
_WRITE_INTENT = re.compile(
    r"\b(delete|drop|truncate|update|insert|remove|wipe|erase|alter|grant|revoke|"
    r"modify|overwrite|destroy|purge|set\s+all|mark\s+all)\b",
    re.IGNORECASE,
)

#: Web-only system prompt that turns a modification request into the single SQL it implies, so the
#: boundary can be shown rejecting it. **Deliberately NOT in querygate/prompts.py** — that file is the
#: grounding-eval behavior contract (changing it would silently alter the eval); this prompt never
#: reaches the agent under test, only the demonstration translator.
_TRANSLATE_SYSTEM = (
    "You convert a user's data-modification request into the single SQL statement it implies, so a "
    "read-only boundary can be shown rejecting it. Output ONLY the SQL statement — no prose, no code "
    "fences, no explanation. Tables live in schema 'app' (app.patients, app.providers, app.encounters, "
    "app.claims, app.follow_ups)."
)

#: If translation fails (or yields something the guard would *not* reject as a write), fall back to
#: this real DML so the demonstration still shows a genuine write rejection. It still executes **zero**
#: writes — the guard (Layer 1) rejects it before the DB, and the read-only role (Layer 3) would too.
_FALLBACK_WRITE_SQL = "DELETE FROM app.patients WHERE name LIKE 'Smith%'"


def is_write_request(text: str) -> bool:
    """True if ``text`` reads as a request to modify data (routes to the boundary demonstration)."""
    return bool(_WRITE_INTENT.search(text or ""))

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


def _resolve_model(requested: str | None) -> str:
    """The model the loop **actually runs** (Split 12 R3 / U-5 honesty).

    The UI's model toggle sends a *requested* model (e.g. ``claude-opus-4-8``). This repo ships only an
    Azure **GPT-5.5** deployment (no Anthropic key — the documented Split-09 deviation), so a requested
    model the local client cannot serve falls back to the default deployment, and the response reports
    the model that **actually ran** (:func:`_final_message_event` carries both ``model`` and
    ``requested_model``) — never faking that another model ran. With an Anthropic key + client wired in,
    a matching requested model would be honored unchanged.
    """
    default = _default_model()
    return requested if requested == default else default


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


def _final_message_event(
    answer: str,
    state: dict,
    config: Config,
    model: str,
    usage: dict,
    requested_model: str | None = None,
) -> dict:
    """Build the final ``message`` event (answer or refusal) + run-level cost/model/transport.

    Kind is derived from the real run, not the model's say-so:

    * a successful ``run_select`` / ``search_text`` result to cite → ``answer`` (cited honestly);
    * else a real boundary rejection happened → ``refusal`` with the **real** guard reason;
    * else (the agent never grounded anything) → ``refusal``, stated honestly.

    ``model`` is the model that **actually ran**; ``requested_model`` echoes what the UI asked for
    (Split 12 U-5) so the toggle is provably wired without faking that a model ran that didn't.
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
        "requested_model": requested_model or model,
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


def _add_usage(usage: dict, resp) -> None:
    """Fold one chat-completion response's token usage into the running ``usage`` tally (honest cost)."""
    u = getattr(resp, "usage", None)
    if u is None:
        return
    usage["input"] += getattr(u, "prompt_tokens", 0) or 0
    usage["output"] += getattr(u, "completion_tokens", 0) or 0
    details = getattr(u, "prompt_tokens_details", None)
    if details is not None:
        usage["cached"] += getattr(details, "cached_tokens", 0) or 0


# ==================================================================================================
# Write-request boundary demonstration (Split 12 R4 / U-2) — the REAL guard rejecting a REAL write.
# ==================================================================================================


def _strip_sql_fences(text: str) -> str:
    """Strip ``` fences / a leading ``sql`` tag / trailing ``;`` whitespace from a translated SQL string."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s[:3].lower() == "sql":
            s = s[3:]
    return s.strip().strip("`").strip()


def _translate_write_sql(question: str, client, model: str, usage: dict) -> str:
    """Turn a modification request into the single SQL it implies (one constrained model call).

    Real translation so the boundary demonstration is honest for **any** write request, not a canned
    statement. Falls back to :data:`_FALLBACK_WRITE_SQL` if the call fails or returns nothing.
    """
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _TRANSLATE_SYSTEM},
                {"role": "user", "content": question},
            ],
            max_completion_tokens=200,
        )
        _add_usage(usage, resp)
        sql = _strip_sql_fences(resp.choices[0].message.content or "")
        return sql or _FALLBACK_WRITE_SQL
    except Exception:  # pragma: no cover - network/parse defensive; the demo must still show a reject
        return _FALLBACK_WRITE_SQL


def _agent_refusal_prose(question: str, client, model: str, usage: dict) -> str:
    """The agent's **own** proactive refusal prose (the real model under the real eval prompt).

    The user chose to show *both* the agent's refusal message and the real boundary rejection (U-2),
    so this is the genuine model refusal — it never attempts a tool (its prompt forbids the write).
    """
    from evals import run_eval as _re

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _re.EVAL_SYSTEM_PROMPT},
                {"role": "user", "content": _re.EVAL_USER_TURN_TEMPLATE.format(question=question)},
            ],
            max_completion_tokens=300,
        )
        _add_usage(usage, resp)
        return (resp.choices[0].message.content or "").strip()
    except Exception:  # pragma: no cover - defensive; fall back to an honest fixed line
        return ""


def stream_demo_rejection(
    question: str,
    *,
    config: Config,
    client=None,
    model: str | None = None,
    requested_model: str | None = None,
) -> Iterator[dict]:
    """Stream a write request as a **real** boundary rejection + the agent's proactive refusal.

    The implied write is submitted to the real ``run_select`` guard, which rejects it for real (real
    rule / reason / audit line / ``reject[reject/ghost/ghost]`` verdict). **Zero** writes execute — the
    guard stops it at Layer 1, and the read-only role at Layer 3. The final ``message`` is a refusal
    carrying the **real guard reason**, with the agent's own refusal prose.
    """
    actual_model = _resolve_model(model)
    if client is None:
        client = _re_build_client()
    state = _new_state()
    state["audit_seen"] = _count_lines(config.audit_path)
    usage = {"input": 0, "output": 0, "cached": 0}

    sql = _translate_write_sql(question, client, actual_model, usage)
    yield from _emit_tool("run_select", {"sql": sql}, config, state)
    if not state["had_rejection"]:
        # Translation produced something the guard did not reject as a write — fall back to real DML
        # so the demonstration still shows a genuine rejection (still 0 writes).
        yield from _emit_tool("run_select", {"sql": _FALLBACK_WRITE_SQL}, config, state)

    # A write request is ALWAYS a refusal: a stray read that the translator emitted must not be cited
    # as the answer. The refusal carries the real guard reason captured from the rejected attempt.
    state["last_result"] = None
    prose = _agent_refusal_prose(question, client, actual_model, usage)
    yield _final_message_event(prose, state, config, actual_model, usage, requested_model)


def _re_build_client():
    from evals import run_eval as _re

    return _re._build_client()


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
    actual_model = _resolve_model(model)
    state = _new_state()
    state["audit_seen"] = _count_lines(config.audit_path)
    usage = {"input": 0, "output": 0, "cached": 0}
    for tool, args in scripted_calls:
        yield from _emit_tool(tool, args, config, state)
    yield _final_message_event(final_answer, state, config, actual_model, usage, model)


# ==================================================================================================
# Live drive (keyed) — an LLM chooses tools. Mirrors evals/run_eval.run_agent, but streaming.
# ==================================================================================================


def stream_live(
    question: str,
    *,
    config: Config,
    client=None,
    model: str | None = None,
    requested_model: str | None = None,
    max_steps: int = 8,
) -> Iterator[dict]:
    """Drive the agent over the four tools with a live model, streaming UI events to the caller.

    The loop bookkeeping (messages, usage, ``stop_reason`` checked **before** reading content) is the
    same as ``evals/run_eval.run_agent``; the difference is that each tool call is executed and
    emitted via :func:`_emit_tool` so the proof rail animates as it happens.
    """
    from evals import run_eval as _re

    actual_model = _resolve_model(model)
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
            model=actual_model, messages=messages, tools=tools, max_completion_tokens=4096,
        )
        _add_usage(usage, resp)

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

    yield _final_message_event(answer, state, config, actual_model, usage, requested_model)


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

    Routing:

    * ``scripted_calls`` given → replay that fixed sequence keylessly (test fixture).
    * a **write request** (:func:`is_write_request`) → the boundary demonstration: the implied write
      is submitted to the real guard and rejected for real (Split 12 R4 / U-2).
    * otherwise → the live agent loop (an LLM picks read-only tools).
    """
    if scripted_calls is not None:
        yield from stream_scripted(
            question, scripted_calls, config=config, final_answer=final_answer, model=model
        )
    elif is_write_request(question):
        yield from stream_demo_rejection(
            question, config=config, client=client, model=model, requested_model=model
        )
    else:
        yield from stream_live(
            question, config=config, client=client, model=model, requested_model=model,
            max_steps=max_steps,
        )
