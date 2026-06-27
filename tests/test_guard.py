"""Exhaustive unit tests for the Layer-1 SQL guard (Split 03, spec §5 + Appendix B).

Pure: no DB, no network, no API key. Fast. The reject corpus is Appendix B verbatim plus the
fail-closed and edge cases; the accept corpus proves valid SELECT/CTE/window/join all pass with
auto-``LIMIT`` injected or preserved.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from querygate.guard import (
    DENYLISTED_FUNCTIONS,
    DEFAULT_ROW_LIMIT,
    guard_select,
)

import pytest


# --------------------------------------------------------------------------------------------
# Reject corpus (Appendix B) — each MUST be ok=False; assert the rule where the spec names one.
# --------------------------------------------------------------------------------------------

REJECT_CASES = [
    ("DELETE FROM patients WHERE name LIKE 'Smith%'", "dml_in_ast"),
    ("UPDATE claims SET amount = 0", "dml_in_ast"),
    ("INSERT INTO patients(name) VALUES ('x')", "dml_in_ast"),
    ("DROP TABLE patients", "dml_in_ast"),
    ("TRUNCATE encounters", "dml_in_ast"),
    ("SELECT 1; DROP TABLE patients", "multi_statement"),
    ("WITH x AS (DELETE FROM patients RETURNING *) SELECT * FROM x", "dml_in_ast"),
    ("SELECT * INTO new_tbl FROM patients", "select_into"),
    ("SELECT * FROM patients FOR UPDATE", "for_update"),
    ("SELECT * FROM patients FOR SHARE", "for_update"),
    ("SELECT pg_read_file('/etc/passwd')", "denylisted_function"),
    ("SELECT nextval('some_seq')", "denylisted_function"),
    ("SELECT pg_sleep(10)", "denylisted_function"),
    # MERGE / GRANT / ALTER / CREATE / COPY round out the forbidden-node set.
    ("MERGE INTO t USING s ON t.id = s.id WHEN MATCHED THEN DELETE", "dml_in_ast"),
    ("GRANT SELECT ON patients TO bob", "dml_in_ast"),
    ("ALTER TABLE patients ADD COLUMN x int", "dml_in_ast"),
    ("CREATE TABLE t (a int)", "dml_in_ast"),
    ("COPY patients TO '/tmp/x'", "dml_in_ast"),
    # REVOKE / VACUUM / CALL / DO have no sqlglot model → fail-closed Command rejection.
    ("REVOKE SELECT ON patients FROM bob", "unsupported_statement"),
    ("VACUUM patients", "unsupported_statement"),
    # Malformed → fail closed.
    ("SELECT FROM WHERE )(", "parse_error"),
    # Empty / whitespace → not a single valid SELECT.
    ("", "parse_error"),
    ("   ", "parse_error"),
    # A projection-less SELECT parses to an empty Select but is malformed — reject, don't let it
    # reach the DB as a bare ``SELECT LIMIT n``.
    ("SELECT", "not_a_select"),
    ("SELECT FROM patients", "not_a_select"),
    # Dangerous-function siblings of already-denied calls (network / large-object / WAL-write).
    ("SELECT dblink_connect('host=evil')", "denylisted_function"),
    ("SELECT lo_put(1, 0, 'x')", "denylisted_function"),
    ("SELECT pg_logical_emit_message(true, 'a', 'b')", "denylisted_function"),
    # Two SELECTs → multi-statement.
    ("SELECT * FROM a; SELECT * FROM b", "multi_statement"),
    # Comment/whitespace obfuscation does not hide a second statement.
    ("SELECT 1 /* hi */ ; DROP TABLE x", "multi_statement"),
]


@pytest.mark.parametrize("sql,expected_rule", REJECT_CASES, ids=[c[0][:40] for c in REJECT_CASES])
def test_reject_corpus(sql: str, expected_rule: str) -> None:
    res = guard_select(sql)
    assert res.ok is False, f"expected rejection for: {sql!r}"
    assert res.sql is None
    assert res.rule == expected_rule, f"{sql!r}: got rule {res.rule!r}, want {expected_rule!r}"
    # Reasons must be specific (non-empty, legible) for the UI Boundary panel / agent rephrase.
    assert res.reason and len(res.reason) > 5


def test_denylist_every_entry_rejected() -> None:
    """Every name in the exported denylist constant is rejected when called."""
    for fn in sorted(DENYLISTED_FUNCTIONS):
        res = guard_select(f"SELECT {fn}('a', 'b')")
        assert res.ok is False, f"{fn} should be rejected"
        assert res.rule == "denylisted_function", f"{fn}: rule was {res.rule}"
        assert fn in res.reason


def test_denylist_case_insensitive() -> None:
    assert guard_select("SELECT PG_SLEEP(10)").rule == "denylisted_function"
    assert guard_select("SELECT Pg_Read_File('/x')").rule == "denylisted_function"


# --------------------------------------------------------------------------------------------
# The headline whole-AST case: data-modifying CTE, and the root-only-would-pass proof.
# --------------------------------------------------------------------------------------------

CTE_DELETE = "WITH x AS (DELETE FROM patients RETURNING *) SELECT * FROM x"


def test_data_modifying_cte_rejected() -> None:
    res = guard_select(CTE_DELETE)
    assert res.ok is False
    assert res.rule == "dml_in_ast"


def test_root_only_check_would_have_passed_the_cte() -> None:
    """Evidence the whole-AST walk is load-bearing.

    The data-modifying CTE parses as a top-level ``Select`` — a *root-only* check (only look at
    ``type(root)``) sees a SELECT and would PASS it. Our guard walks the whole tree, finds the
    nested ``Delete``, and rejects. This asserts both halves so the proof can't silently rot.
    """
    root = sqlglot.parse_one(CTE_DELETE, dialect="postgres")
    # Root-only view: it really is a Select (so a naive check passes).
    assert isinstance(root, exp.Select)
    # Whole-AST view: a Delete is nested inside, which is what our guard catches.
    assert any(isinstance(n, exp.Delete) for n in root.walk())
    # And the guard rejects it.
    assert guard_select(CTE_DELETE).ok is False


def test_lowercase_and_mixedcase_dml_rejected() -> None:
    assert guard_select("delete from patients").ok is False
    assert guard_select("DeLeTe FROM patients").ok is False
    assert guard_select("delete from patients").rule == "dml_in_ast"


# --------------------------------------------------------------------------------------------
# Accept corpus — each MUST be ok=True; assert auto-LIMIT behaviour.
# --------------------------------------------------------------------------------------------

def _has_outer_limit(sql: str, value: int) -> bool:
    root = sqlglot.parse_one(sql, dialect="postgres")
    limit = root.args.get("limit")
    return limit is not None and limit.expression.name == str(value)


def test_accept_count_injects_limit() -> None:
    res = guard_select("SELECT count(*) FROM follow_ups WHERE completed_at IS NULL")
    assert res.ok is True
    assert res.reason is None and res.rule is None
    assert "LIMIT 1000" in res.sql.upper()
    assert _has_outer_limit(res.sql, DEFAULT_ROW_LIMIT)


def test_accept_cte_select() -> None:
    res = guard_select("WITH od AS (SELECT id FROM patients) SELECT * FROM od")
    assert res.ok is True
    assert _has_outer_limit(res.sql, DEFAULT_ROW_LIMIT)


def test_accept_window_function() -> None:
    res = guard_select("SELECT name, row_number() over (order by id) FROM patients")
    assert res.ok is True
    assert _has_outer_limit(res.sql, DEFAULT_ROW_LIMIT)


def test_accept_money_demo_join_groupby() -> None:
    sql = (
        "SELECT p.id, p.name, count(*) AS overdue "
        "FROM providers p "
        "JOIN patients pt ON pt.primary_provider_id = p.id "
        "JOIN follow_ups f ON f.patient_id = pt.id "
        "WHERE f.completed_at IS NULL AND f.due_date < now() "
        "GROUP BY p.id, p.name "
        "ORDER BY overdue DESC"
    )
    res = guard_select(sql)
    assert res.ok is True
    assert _has_outer_limit(res.sql, DEFAULT_ROW_LIMIT)


def test_existing_limit_preserved_not_overridden() -> None:
    res = guard_select("SELECT * FROM patients LIMIT 5")
    assert res.ok is True
    # The 5 is unchanged and NOT lowered/raised to row_limit.
    assert _has_outer_limit(res.sql, 5)
    assert "1000" not in res.sql


def test_inner_limit_untouched_outer_injected() -> None:
    res = guard_select("SELECT * FROM (SELECT * FROM patients LIMIT 3) t")
    assert res.ok is True
    root = sqlglot.parse_one(res.sql, dialect="postgres")
    # Outer LIMIT is the row_limit ...
    assert _has_outer_limit(res.sql, DEFAULT_ROW_LIMIT)
    # ... and the inner subquery still has exactly its original LIMIT 3 (not rewritten).
    inner = root.find(exp.Subquery)
    inner_limit = inner.this.args.get("limit")
    assert inner_limit is not None and inner_limit.expression.name == "3"


def test_auto_limit_only_one_extra_limit_added() -> None:
    """Auto-LIMIT touches only the outer query — no extra LIMITs sprinkled into CTEs/subqueries."""
    sql = "WITH od AS (SELECT id FROM patients) SELECT * FROM od"
    before = sum(1 for _ in sqlglot.parse_one(sql, dialect="postgres").find_all(exp.Limit))
    res = guard_select(sql)
    after = sum(1 for _ in sqlglot.parse_one(res.sql, dialect="postgres").find_all(exp.Limit))
    assert after == before + 1


def test_custom_row_limit_parameter() -> None:
    res = guard_select("SELECT 1", row_limit=42)
    assert res.ok is True
    assert _has_outer_limit(res.sql, 42)


def test_accepted_sql_is_reparseable() -> None:
    """The guard must never emit SQL it (or Postgres) can't read back."""
    for sql in [
        "SELECT count(*) FROM follow_ups WHERE completed_at IS NULL",
        "WITH od AS (SELECT id FROM patients) SELECT * FROM od",
        "SELECT name, row_number() over (order by id) FROM patients",
        "SELECT * FROM patients LIMIT 5",
        "SELECT a FROM t1 UNION SELECT a FROM t2",
    ]:
        res = guard_select(sql)
        assert res.ok is True
        # Re-parse the accepted SQL: must not raise.
        sqlglot.parse_one(res.sql, dialect="postgres")


def test_union_gets_outer_limit() -> None:
    res = guard_select("SELECT a FROM t1 UNION SELECT a FROM t2")
    assert res.ok is True
    assert "LIMIT 1000" in res.sql.upper()


# --------------------------------------------------------------------------------------------
# Purity: the guard imports no DB driver.
# --------------------------------------------------------------------------------------------

def test_guard_module_does_not_import_psycopg() -> None:
    import querygate.guard as g

    src = __import__("inspect").getsource(g)
    assert "psycopg" not in src, "the guard must stay DB-free (no psycopg import)"
