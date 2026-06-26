"""Split 05 — the discovery tools + injection closure (spec §6, §8, §13, §18).

``list_tables`` / ``describe_table`` / ``search_text`` as plain library functions (Split 6 wraps
them in the MCP server). The load-bearing tests here are the injection ones:

* **T-3 / T-5** — ``describe_table`` and ``search_text`` reject a non-allowlisted / injection
  ``table`` value; no arbitrary identifier is ever formatted into SQL. (Hard gate.)
* **T-6** — ``search_text``'s ``term`` is a **bound parameter**, immune to SQL metacharacters.

Plus the contracts (T-1/T-2/T-7), the near-duplicate search (T-4, the "Sara" → "Sarah Lee" case),
and one audit line per call (T-8). Needs the compose Postgres + read-only role; skips cleanly via
the shared conftest when DATABASE_URL / psql is absent.
"""

from __future__ import annotations

import json

import pytest

from querygate.config import Config
from querygate.models import RunResult, TableInfo, TableSchema
from querygate.tools import (
    ToolRejected,
    describe_table,
    list_tables,
    run_select,
    search_text,
)

APP_TABLES = {"patients", "providers", "encounters", "claims", "follow_ups"}


@pytest.fixture()
def ro_config(role_url: str, seeded_db, tmp_path) -> Config:
    return Config(database_url=role_url, audit_path=str(tmp_path / "audit.jsonl"))


def _audit_lines(cfg: Config) -> list[dict]:
    from pathlib import Path

    p = Path(cfg.audit_path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ==============================================================================================
# T-1 — list_tables.
# ==============================================================================================


def test_t1_list_tables(ro_config):
    tables = list_tables(config=ro_config)
    assert {t.table for t in tables} == APP_TABLES
    assert all(isinstance(t, TableInfo) for t in tables)
    assert all(t.est_rows > 0 for t in tables), "ANALYZE should have populated reltuples"
    # Exactly one ok audit line.
    (entry,) = _audit_lines(ro_config)
    assert entry["tool"] == "list_tables" and entry["status"] == "ok"


# ==============================================================================================
# T-2 — describe_table contract; T-3 — allowlist rejection.
# ==============================================================================================


def test_t2_describe_table_contract(ro_config):
    schema = describe_table("follow_ups", config=ro_config)
    assert isinstance(schema, TableSchema)
    assert schema.table == "follow_ups"
    cols = {c.name: c for c in schema.columns}
    assert set(cols) == {"follow_up_id", "patient_id", "due_date", "completed_at"}
    # PK on follow_up_id.
    assert cols["follow_up_id"].is_pk is True
    assert cols["patient_id"].is_pk is False
    # FK on patient_id → patients.patient_id.
    assert cols["patient_id"].references == "patients.patient_id"
    assert cols["follow_up_id"].references is None
    # nullable correct: completed_at is nullable, follow_up_id is not.
    assert cols["completed_at"].nullable is True
    assert cols["follow_up_id"].nullable is False
    # a few sample rows came back.
    assert 0 < len(schema.sample_rows) <= 3
    assert len(schema.sample_rows[0]) == len(schema.columns)
    (entry,) = _audit_lines(ro_config)
    assert entry["tool"] == "describe_table" and entry["status"] == "ok"


@pytest.mark.parametrize(
    "bad_table",
    ["pg_authid", "patients; DROP TABLE patients", "app.patients", "PATIENTS", "does_not_exist"],
)
def test_t3_describe_table_allowlist_rejects(ro_config, bad_table):
    """T-3 (hard gate): a non-allowlisted / injection table is rejected, not queried."""
    with pytest.raises(ToolRejected) as excinfo:
        describe_table(bad_table, config=ro_config)
    assert excinfo.value.rule == "table_not_allowed"
    # Audited rejected (the allowlist round-trip is the only DB touch; no introspection ran).
    lines = _audit_lines(ro_config)
    assert len(lines) == 1
    assert lines[0]["tool"] == "describe_table" and lines[0]["status"] == "rejected"
    assert lines[0]["args"]["table"] == bad_table


# ==============================================================================================
# T-4 — search_text finds the near-duplicate; T-5 — identifier injection closed.
# ==============================================================================================


def test_t4_search_text_finds_sarah_lee(ro_config):
    """T-4: searching 'Sara' (no 'Sara Lee' exists) surfaces the real 'Sarah Lee' (Split-01 quirk)."""
    res = search_text("Sara", table="patients", config=ro_config)
    assert isinstance(res, RunResult)
    assert res.columns == ["source_table", "source_column", "match_value"]
    values = {row[2] for row in res.rows}
    assert "Sarah Lee" in values
    # Carries sql + row_count (the citation contract).
    assert res.row_count == len(res.rows) >= 1
    assert "ILIKE" in res.sql.upper()


@pytest.mark.parametrize(
    "bad_table",
    ["patients; DROP TABLE patients", "pg_authid", "app.patients", "information_schema.columns"],
)
def test_t5_search_text_identifier_injection_rejected(ro_config, bad_table):
    """T-5 (hard gate): an injection table value is rejected by the allowlist — no SQL is built."""
    with pytest.raises(ToolRejected) as excinfo:
        search_text("x", table=bad_table, config=ro_config)
    assert excinfo.value.rule == "table_not_allowed"
    lines = _audit_lines(ro_config)
    assert len(lines) == 1 and lines[0]["status"] == "rejected"
    assert lines[0]["args"]["table"] == bad_table


def test_t6_search_text_term_is_bound_not_concatenated(ro_config):
    """T-6 (hard gate): a term full of SQL metacharacters is a literal search string, not code.

    If `term` were concatenated, ``' OR 1=1 --`` would match every row; bound, it matches only
    rows whose text literally contains that string (none) — and the executed SQL shows `%s`.
    """
    res = search_text("' OR 1=1 --", table="patients", config=ro_config)
    # The metachar string is searched literally — patients don't contain it, so zero matches.
    assert res.row_count == 0
    # Proof the term is a bound parameter: the cited SQL carries the %s placeholder, not the value.
    assert "%s" in res.sql
    assert "1=1" not in res.sql

    # Sanity: a normal substring still matches a lot, so the tool genuinely searches.
    busy = search_text("a", table="patients", config=ro_config)
    assert busy.row_count > res.row_count


def test_t6_search_text_no_table_searches_all_allowlisted(ro_config):
    """search_text(table=None) sweeps every allowlisted table's text columns (one RunResult)."""
    res = search_text("Cardiology", config=ro_config)
    # specialty lives in providers; the sweep should surface provider rows.
    tables_hit = {row[0] for row in res.rows}
    assert "providers" in tables_hit


# ==============================================================================================
# T-7 — tool contracts validate; T-8 — every call audits one line.
# ==============================================================================================


def test_t7_tool_contracts_validate(ro_config):
    """All four tool outputs validate against their Pydantic models, incl. sql + row_count."""
    tables = list_tables(config=ro_config)
    [TableInfo.model_validate(t.model_dump()) for t in tables]

    schema = describe_table("patients", config=ro_config)
    TableSchema.model_validate(schema.model_dump())

    run = run_select("SELECT provider_id FROM app.providers", config=ro_config)
    RunResult.model_validate(run.model_dump())
    assert run.sql and run.row_count == len(run.rows)

    search = search_text("Lee", table="patients", config=ro_config)
    RunResult.model_validate(search.model_dump())
    assert search.sql and search.row_count == len(search.rows)


def test_t8_each_tool_audits_one_line(ro_config):
    """T-8: a sequence of tool calls writes exactly one correctly-typed line each, in order."""
    list_tables(config=ro_config)
    describe_table("claims", config=ro_config)
    search_text("Lee", table="patients", config=ro_config)
    with pytest.raises(ToolRejected):
        describe_table("pg_authid", config=ro_config)

    lines = _audit_lines(ro_config)
    assert [(ln["tool"], ln["status"]) for ln in lines] == [
        ("list_tables", "ok"),
        ("describe_table", "ok"),
        ("search_text", "ok"),
        ("describe_table", "rejected"),
    ]


def test_t8_redaction_populates_audit(role_url, seeded_db, tmp_path):
    """When redaction masks a column in search_text, the audit line records the table.column."""
    import yaml

    redact_file = tmp_path / "redact.yaml"
    redact_file.write_text(yaml.safe_dump({"patients": ["name"]}), encoding="utf-8")
    cfg = Config(
        database_url=role_url,
        redact_path=str(redact_file),
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    res = search_text("Sarah", table="patients", config=cfg)
    # The name match_value is masked, and patients.name is recorded.
    assert res.redactions == ["patients.name"]
    assert all(row[2] == "***" for row in res.rows if row[1] == "name")
    (entry,) = _audit_lines(cfg)
    assert entry["redactions"] == ["patients.name"]
