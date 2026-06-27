"""Split 13 — the optional ``explain_select`` tool (spec §6, §20).

``explain_select`` is the clean upsell: it reuses the **same Layer-1 guard** ``run_select`` uses
(so every write form, the data-modifying CTE, the denylist, and ``;``-chains are rejected) and adds
**one rule** — plain ``EXPLAIN`` is planning-only and read-only, but ``EXPLAIN (ANALYZE)`` /
``EXPLAIN ANALYZE`` *executes* the query and is **rejected** (rule ``explain_analyze``).

This file is the P-1 proof:
- **P-1a** plain ``EXPLAIN <select>`` returns a real plan and **executes nothing** (a row count that
  would prove execution, e.g. an INSERT, is impossible; we assert the plan shape + that a side-effect
  table count is unchanged).
- **P-1b** ``EXPLAIN (ANALYZE)`` and ``EXPLAIN ANALYZE`` are rejected without touching the DB.
- **P-1c** a write inside the EXPLAIN (``EXPLAIN DELETE …``, a data-modifying CTE) is rejected by the
  guard.
- **P-1d** every outcome (ok / rejected / error) writes exactly one audit line.

Pure (no-DB) rejection tests always run; the happy-path plan needs the read-only role + seeded DB and
skips cleanly via the shared conftest when ``$DATABASE_URL`` / ``psql`` is absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from querygate.config import Config
from querygate.tools import RunRejected, explain_select


def _audit_lines(cfg: Config) -> list[dict]:
    p = Path(cfg.audit_path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture()
def ro_config(role_url: str, seeded_db, tmp_path) -> Config:
    """Config pointed at the read-only querygate role, audit log in a per-test temp file."""
    return Config(database_url=role_url, audit_path=str(tmp_path / "audit.jsonl"))


@pytest.fixture()
def nodb_config(tmp_path) -> Config:
    """No DB URL — enough for the pure rejection tests (the guard runs before any DB call)."""
    return Config(database_url=None, audit_path=str(tmp_path / "audit.jsonl"))


# ==================================================================================================
# P-1b — ANALYZE is rejected (it would EXECUTE the query). No DB needed: rejected before any DB call.
# ==================================================================================================

ANALYZE_FORMS = [
    "EXPLAIN ANALYZE SELECT count(*) FROM app.patients",
    "EXPLAIN (ANALYZE) SELECT count(*) FROM app.patients",
    "EXPLAIN (ANALYZE true) SELECT count(*) FROM app.patients",
    "EXPLAIN (FORMAT JSON, ANALYZE) SELECT 1",
    "explain   analyze   select 1",  # case / whitespace insensitive
    "EXPLAIN (ANALYZE, VERBOSE) SELECT 1",
]


@pytest.mark.parametrize("sql", ANALYZE_FORMS)
def test_p1b_explain_analyze_rejected(nodb_config: Config, sql: str):
    with pytest.raises(RunRejected) as exc:
        explain_select(sql, config=nodb_config)
    assert exc.value.rule == "explain_analyze"
    # rejected before the DB — and audited exactly once as `rejected`.
    lines = _audit_lines(nodb_config)
    assert len(lines) == 1
    assert lines[0]["tool"] == "explain_select" and lines[0]["status"] == "rejected"


# ==================================================================================================
# P-1c — a write inside the EXPLAIN is rejected by the SAME guard run_select uses (no DB needed).
# ==================================================================================================

WRITE_FORMS = [
    "DELETE FROM app.patients",
    "UPDATE app.patients SET name = 'x'",
    "INSERT INTO app.patients (name) VALUES ('x')",
    "DROP TABLE app.patients",
    # the load-bearing data-modifying CTE — parses as a SELECT with a nested DELETE.
    "WITH x AS (DELETE FROM app.patients RETURNING *) SELECT * FROM x",
    "SELECT pg_read_file('/etc/passwd')",  # denylisted function
    "SELECT 1; SELECT 2",  # multi-statement
    # the same write, wrapped in a plain EXPLAIN the agent may have written.
    "EXPLAIN DELETE FROM app.patients",
]


@pytest.mark.parametrize("sql", WRITE_FORMS)
def test_p1c_write_inside_explain_rejected(nodb_config: Config, sql: str):
    with pytest.raises(RunRejected) as exc:
        explain_select(sql, config=nodb_config)
    # rejected by the guard — NOT the analyze rule (these are genuine guard rejections).
    assert exc.value.rule != "explain_analyze"
    lines = _audit_lines(nodb_config)
    assert len(lines) == 1 and lines[0]["status"] == "rejected"


# ==================================================================================================
# P-1a — plain EXPLAIN returns a real plan and executes nothing. Needs the read-only role + DB.
# ==================================================================================================


def test_p1a_plain_explain_returns_a_plan(ro_config: Config):
    result = explain_select(
        "SELECT count(*) FROM app.follow_ups WHERE completed_at IS NULL", config=ro_config
    )
    # The plan is Postgres's single QUERY PLAN text column.
    assert result.columns == ["QUERY PLAN"]
    assert result.row_count >= 1
    plan_text = "\n".join(row[0] for row in result.rows)
    # A planning-only EXPLAIN reports estimated cost/rows; it never ran the query (no "actual time").
    assert "cost=" in plan_text
    assert "actual time" not in plan_text  # would appear only under ANALYZE
    # the cited SQL is the EXPLAIN we actually executed.
    assert result.sql.upper().startswith("EXPLAIN")
    # one ok audit line.
    lines = _audit_lines(ro_config)
    assert len(lines) == 1
    assert lines[0]["tool"] == "explain_select" and lines[0]["status"] == "ok"


def test_p1a_explain_executes_nothing(ro_config: Config):
    """A plain EXPLAIN of a SELECT must not change the database — prove the row count is stable."""
    count_sql = "SELECT count(*) FROM app.patients"
    from querygate.tools import run_select

    before = run_select(count_sql, config=ro_config).rows[0][0]
    explain_select("SELECT * FROM app.patients", config=ro_config)
    after = run_select(count_sql, config=ro_config).rows[0][0]
    assert before == after  # EXPLAIN ran the planner, not the query.


def test_p1a_leading_plain_explain_is_accepted(ro_config: Config):
    """The agent may write `EXPLAIN SELECT …`; we strip the prefix and plan the inner SELECT."""
    result = explain_select("EXPLAIN SELECT 1 AS one", config=ro_config)
    assert result.columns == ["QUERY PLAN"]
    assert any("cost=" in row[0] for row in result.rows)


def test_p1d_auto_limit_visible_in_plan(ro_config: Config):
    """The guard's auto-LIMIT is applied to the inner SELECT, so the plan shows a Limit node."""
    result = explain_select("SELECT * FROM app.patients", config=ro_config)
    plan_text = "\n".join(row[0] for row in result.rows)
    assert "Limit" in plan_text  # auto-LIMIT 1000 injected by the guard
