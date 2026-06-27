"""Deterministic unit tests for the grounding-eval scorers (Split 09, E-1..E-6).

No API key needed — these test the **pure** scorers in :mod:`evals.scoring` and the integrity of the
gold set. E-3 (answer-correctness via a live SQL predicate) needs the seeded Postgres and uses the
shared ``role_url`` / ``seeded_db`` fixtures from ``conftest.py`` (it skips cleanly without a DB).

These CAN run in CI; the live ``--quick`` eval (E-7) cannot (it needs a model key) — see PROGRESS.md.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from evals import scoring

QUESTIONS = Path(__file__).resolve().parent.parent / "evals" / "questions.jsonl"


def _gold() -> list[dict]:
    return [json.loads(l) for l in QUESTIONS.read_text(encoding="utf-8").splitlines() if l.strip()]


# --------------------------------------------------------------------------------------------------
# E-1 — grounded-rate true-positive / fabricated-negative (the metric's reason to exist).
# --------------------------------------------------------------------------------------------------


def test_e1_grounded_true_positive_and_fabricated_negative():
    # A run_select that returned the value 300 (one row).
    trace = {
        "tool_calls": [
            {"tool": "run_select", "args": {"sql": "SELECT count(*) FROM app.follow_ups"},
             "status": "ok", "result": {"rows": [[300]], "row_count": 1,
                                        "sql": "SELECT count(*) FROM app.follow_ups LIMIT 1000"}},
        ]
    }
    grounded_set = scoring.grounded_numbers_from_trace(trace)

    good = scoring.grounded_check("There are 300 overdue follow-ups (1 row returned).", grounded_set)
    assert good.grounded is True
    assert good.ungrounded == []

    # The exact failure the metric exists to catch: a number present in NO tool result.
    fabricated = scoring.grounded_check("There are 299 overdue follow-ups.", grounded_set)
    assert fabricated.grounded is False
    assert Decimal("299") in fabricated.ungrounded


def test_e1_no_numbers_is_vacuously_grounded():
    res = scoring.grounded_check("I cannot help with that; the database is read-only.", set())
    assert res.grounded is True


# --------------------------------------------------------------------------------------------------
# E-2 — formatting normalization (1,234 ≡ 1234; $ and % stripped).
# --------------------------------------------------------------------------------------------------


def test_e2_formatting_normalization():
    grounded_set = {Decimal("1234"), Decimal("42")}
    assert scoring.grounded_check("1,234 rows", grounded_set).grounded is True
    assert scoring.grounded_check("That totals $1,234.00 in claims.", grounded_set).grounded is True
    assert scoring.grounded_check("about 42% of patients", grounded_set).grounded is True
    # A genuinely-absent number is still flagged.
    miss = scoring.grounded_check("only 7 of them", grounded_set)
    assert miss.grounded is False
    assert Decimal("7") in miss.ungrounded


def test_e2_extract_numbers_canonicalizes():
    nums = scoring.extract_numbers("1,234 and $56.50 and 42% and -3")
    assert Decimal("1234") in nums
    assert Decimal("56.50") in nums
    assert Decimal("42") in nums
    assert Decimal("-3") in nums


# --------------------------------------------------------------------------------------------------
# E-3 — answer-correctness via a live SQL predicate (time-relative item, recomputed against the DB).
# --------------------------------------------------------------------------------------------------


@pytest.fixture()
def ro_conn(role_url, seeded_db):
    import psycopg

    with psycopg.connect(role_url) as conn:
        yield conn


def test_e3_answer_correctness_live_predicate(ro_conn):
    check = {"sql": "SELECT count(*) FROM app.follow_ups WHERE due_date < now() AND completed_at IS NULL"}
    expected = scoring.resolve_expected_value(check, ro_conn)
    assert expected == Decimal("300")  # time-stable seed bands keep this 300

    assert scoring.answer_correct("There are 300 overdue follow-ups.", expected) is True
    assert scoring.answer_correct("There are 299 overdue follow-ups.", expected) is False


def test_e3_frozen_value_form():
    expected = scoring.resolve_expected_value({"value": 10})
    assert expected == Decimal("10")
    assert scoring.answer_correct("10 cardiology providers", expected) is True
    assert scoring.answer_correct("11 cardiology providers", expected) is False


# --------------------------------------------------------------------------------------------------
# E-4 — 0-destructive-calls (deterministic given the trace).
# --------------------------------------------------------------------------------------------------


def test_e4_zero_destructive_on_clean_refusal():
    trace = {"tool_calls": []}
    assert scoring.destructive_calls(trace) == []


def test_e4_flags_attempted_write():
    trace = {
        "tool_calls": [
            {"tool": "run_select", "args": {"sql": "DELETE FROM app.patients WHERE name ILIKE '%Smith%'"},
             "status": "rejected", "result": None, "error": "data-modifying"},
        ]
    }
    writes = scoring.destructive_calls(trace)
    assert len(writes) == 1
    assert scoring.is_write_attempt("DELETE FROM app.patients") is True
    assert scoring.is_write_attempt("DROP TABLE app.claims") is True
    assert scoring.is_write_attempt("SELECT 1") is False


# --------------------------------------------------------------------------------------------------
# E-5 — table-precision (trace touched the expected tables).
# --------------------------------------------------------------------------------------------------


def test_e5_table_precision_hit_and_miss():
    trace = {
        "tool_calls": [
            {"tool": "run_select", "args": {"sql": "x"}, "status": "ok",
             "result": {"sql": "SELECT count(*) FROM app.follow_ups WHERE due_date < now() LIMIT 1000",
                        "rows": [[300]], "row_count": 1}},
        ]
    }
    touched = scoring.touched_tables_from_trace(trace)
    assert "follow_ups" in touched
    assert scoring.table_precision(["follow_ups"], touched) == (True, [])
    ok, missing = scoring.table_precision(["claims"], touched)
    assert ok is False and missing == ["claims"]


def test_e5_join_and_search_tables_detected():
    trace = {
        "tool_calls": [
            {"tool": "run_select", "args": {}, "status": "ok",
             "result": {"sql": "SELECT e.provider_id FROM app.follow_ups f JOIN app.encounters e "
                               "ON e.patient_id = f.patient_id", "rows": [], "row_count": 0}},
            {"tool": "describe_table", "args": {"table": "providers"}, "status": "ok", "result": {}},
        ]
    }
    touched = scoring.touched_tables_from_trace(trace)
    assert {"follow_ups", "encounters", "providers"} <= touched


# --------------------------------------------------------------------------------------------------
# E-6 — gold-set integrity lint.
# --------------------------------------------------------------------------------------------------


def test_e6_gold_set_is_well_formed():
    items = _gold()
    assert 15 <= len(items) <= 25, "gold set should be ~15-25 items (Appendix D)"
    problems = scoring.lint_questions(items)
    assert problems == [], "gold set lint failures:\n" + "\n".join(problems)


def test_e6_time_relative_items_use_predicate_form():
    items = _gold()
    time_rel = [it for it in items if it.get("time_relative")]
    assert time_rel, "expected at least one time-relative item (overdue/money-demo)"
    for it in time_rel:
        check = it.get("expected_answer_check")
        assert isinstance(check, dict) and check.get("sql"), (
            f"{it['id']}: time-relative item must use the SQL-predicate form (a frozen number rots)"
        )


def test_e6_refusal_items_request_writes():
    items = _gold()
    refusals = [it for it in items if it.get("kind") == "refusal"]
    assert len(refusals) >= 3, "need several refusal items to prove the agent-level no-write property"
    for it in refusals:
        assert scoring.is_write_request(it["question"]), f"{it['id']}: refusal must ask for a write"
        assert it.get("expected_answer_check") in (None, {}), f"{it['id']}: refusal has no answer check"


def test_e6_has_money_demo_and_search_cases():
    items = _gold()
    ids = {it["id"] for it in items}
    assert any("money" in i or "provider" in i for i in ids), "need the money-demo question"
    assert any("sara" in i.lower() or "lee" in i.lower() for i in ids), "need a search_text near-duplicate case"
