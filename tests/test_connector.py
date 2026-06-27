"""Split 10 — the keyed Messages-API **MCP connector** tests (spec §12-B, App A).

These drive a **running** ``querygate --http`` server through the Anthropic Messages API MCP
connector and prove boundary parity over HTTP:

* **H-5** — the §12-B call (both halves) returns a grounded answer citing SQL + ``row_count``.
* **H-6** — a write request over the connector is refused with **0 writes executed** (a ``rejected``
  audit line) — identical to stdio.
* **H-7** — omitting the ``mcp_toolset`` half is the documented **400** (App A) — the negative test
  that proves the contract is understood.

**On-demand, not CI.** They need an ``ANTHROPIC_API_KEY`` (and the ``anthropic`` SDK). This repo ships
**only** an Azure GPT-5.5 key (PROGRESS.md / Split 09), so in this environment they **skip cleanly** —
never fail, never fabricate. Note too that the connector fetches the URL from Anthropic's servers, so
``http://localhost:8000/mcp`` is only reachable when the server is exposed to the API (set
``QUERYGATE_HTTP_URL`` to a tunnel URL). The keyless H-1..H-4 in ``test_http_transport.py`` prove the
transport itself without a key.
"""

from __future__ import annotations

import pytest

from evals.check_connector import (
    HAPPY_MESSAGE,
    WRITE_MESSAGE,
    answer_text,
    connector_available,
    connector_url,
    run_connector,
)

# Skip the whole module unless the Anthropic SDK + key are present (the on-demand posture).
_available, _reason = connector_available()
pytestmark = pytest.mark.skipif(not _available, reason=f"connector unavailable: {_reason}")

EXPECTED_OVERDUE = 300  # Split-01 deterministic contract


@pytest.fixture()
def connector_client():
    import anthropic

    return anthropic.Anthropic()


@pytest.fixture()
def served_url(role_url, seeded_db, tmp_path):
    """A running --http server + its audit path. Reuses the keyless launcher.

    Yields ``(url, audit_path)``. If ``QUERYGATE_HTTP_URL`` is set (a tunnel reachable from the API),
    that URL is used instead of the local subprocess.
    """
    import os

    from test_http_transport import _audit_lines, _running_http_server  # noqa: F401

    override = os.environ.get("QUERYGATE_HTTP_URL")
    audit = tmp_path / "audit.jsonl"
    if override:
        yield override, audit
        return
    with _running_http_server(role_url, audit) as (_, url):
        yield url, audit


# ==================================================================================================
# H-5 — connector happy path, both halves.
# ==================================================================================================


def test_h5_connector_happy_path(connector_client, served_url):
    url, _ = served_url
    resp = run_connector(connector_client, url, HAPPY_MESSAGE)
    assert resp.stop_reason != "refusal"  # checked before reading content (App A)
    text = answer_text(resp)
    assert str(EXPECTED_OVERDUE) in text  # the grounded number, from a tool result


# ==================================================================================================
# H-6 — connector refusal parity: a write is refused with 0 writes executed.
# ==================================================================================================


def test_h6_connector_refuses_write_zero_executed(connector_client, served_url):
    from test_http_transport import _audit_lines

    url, audit = served_url
    run_connector(connector_client, url, WRITE_MESSAGE)

    lines = _audit_lines(audit)
    # The boundary holds over the connector exactly as over stdio: a write the model attempts is
    # rejected at the guard, never executed. The SQL lives in the audit line's `args`; any line whose
    # SQL carries a data-modifying verb must be `rejected` (a Layer-1 refusal), never `ok`.
    write_verbs = ("delete", "update", "insert", "drop", "truncate", "alter")

    def _sql(ln: dict) -> str:
        return str((ln.get("args") or {}).get("sql", "")).lower()

    for ln in lines:
        if any(v in _sql(ln) for v in write_verbs):
            assert ln["status"] == "rejected", f"a write reached status={ln['status']}: {_sql(ln)!r}"
    # The invariant regardless of what the model attempted: never an `ok` write (0 executed writes).
    assert not any(
        ln["status"] == "ok" and any(v in _sql(ln) for v in write_verbs) for ln in lines
    )


# ==================================================================================================
# H-7 — the missing-mcp_toolset 400 (the contract, App A).
# ==================================================================================================


def test_h7_missing_mcp_toolset_is_400(connector_client, served_url):
    import anthropic

    url, _ = served_url
    with pytest.raises(anthropic.APIStatusError) as exc:
        run_connector(connector_client, url, HAPPY_MESSAGE, include_toolset=False)
    assert exc.value.status_code == 400  # omitting the matching mcp_toolset is a validation 400
