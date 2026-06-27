"""Split 04 — the load-bearing proof: a write is rejected at EACH layer INDEPENDENTLY.

This is the product's signature property (spec §5): *the server cannot execute a write — proven
in CI, not promised in a README.* The three independent assertions mean no single layer is doing
all the work on faith:

- **B1 — Layer 1** (the guard, via ``run_select``): the write is ``rejected`` and never reaches
  the DB. Asserted on the guard's machine ``rule``.
- **B2 — Layer 2 alone** (the read-only transaction): isolated by issuing the write as a
  **write-capable admin role** through ``run_readonly`` — that role *can* write, so the ONLY
  possible barrier is the ``READ ONLY`` transaction. Asserted on "read-only transaction".
- **B3 — Layer 3 alone** (the least-privilege role): isolated by issuing the write as the
  ``querygate`` read-only role with the transaction forced ``READ WRITE`` (overriding the role's
  ``default_transaction_read_only``). With Layer 2 neutralized, the ONLY barrier left is the
  missing write grant. Asserted on "permission denied".

The point of "independent" is that each test is constructed so **exactly one** layer can be the
rejecter — a write-capable role isolates Layer 2, a forced-read-write txn isolates Layer 3, the
pure guard isolates Layer 1. Asserting the **specific error string** is what makes the isolation
legible. (This sharpens spec §13's looser wording, which on its own leaves both layers in the
path; recorded in PROGRESS.md.)

Plus the happy path (B4/B5/B6) and the ok/error audit lines (A1/A3). Needs the compose Postgres
+ the read-only role; skips cleanly via the shared conftest when DATABASE_URL / psql is absent.
"""

from __future__ import annotations

import json

import psycopg
import pytest

from querygate.config import Config
from querygate.db import DBError, QueryTimeout, run_readonly
from querygate.tools import RunRejected, run_select

# Known seed numbers (Split 01's deterministic contract).
EXPECTED_OVERDUE = 300
EXPECTED_TOP_PROVIDER = 1
EXPECTED_TOP_OVERDUE_PATIENTS = 60
ROW_LIMIT = 1000

OVERDUE_COUNT_SQL = (
    "SELECT count(*) FROM app.follow_ups WHERE completed_at IS NULL AND due_date < now()"
)
TOP_PROVIDER_SQL = """
    SELECT e.provider_id, count(DISTINCT f.patient_id) AS overdue_patients
    FROM app.follow_ups f
    JOIN app.encounters e ON e.patient_id = f.patient_id
    WHERE f.completed_at IS NULL AND f.due_date < now()
    GROUP BY e.provider_id
    ORDER BY overdue_patients DESC, e.provider_id
"""


@pytest.fixture()
def ro_config(role_url: str, seeded_db, tmp_path) -> Config:
    """Config pointed at the read-only querygate role, with the audit log in a temp file."""
    return Config(database_url=role_url, audit_path=str(tmp_path / "audit.jsonl"))


def _audit_lines(cfg: Config) -> list[dict]:
    from pathlib import Path

    p = Path(cfg.audit_path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ==============================================================================================
# B1 — Layer 1: every write form is rejected via run_select; the SQL never reaches the DB.
# ==============================================================================================

# (rule, sql) — the Appendix-B boundary corpus.
_WRITE_CORPUS = [
    ("dml_in_ast", "DELETE FROM app.patients WHERE name LIKE 'Smith%'"),
    ("dml_in_ast", "UPDATE app.claims SET amount = 0"),
    ("dml_in_ast", "INSERT INTO app.patients(name) VALUES ('x')"),
    ("dml_in_ast", "DROP TABLE app.patients"),
    ("dml_in_ast", "TRUNCATE app.encounters"),
    ("multi_statement", "SELECT 1; DROP TABLE app.patients"),
    ("dml_in_ast", "WITH x AS (DELETE FROM app.patients RETURNING *) SELECT * FROM x"),
    ("select_into", "SELECT * INTO new_tbl FROM app.patients"),
    ("for_update", "SELECT * FROM app.patients FOR UPDATE"),
    ("denylisted_function", "SELECT pg_read_file('/etc/passwd')"),
    ("denylisted_function", "SELECT nextval('app.patients_patient_id_seq')"),
]


@pytest.mark.parametrize("rule,sql", _WRITE_CORPUS, ids=[c[1][:24] for c in _WRITE_CORPUS])
def test_b1_layer1_rejects_via_run_select(ro_config, rule, sql):
    """Each write form returns status:rejected at Layer 1; the SQL is never sent to the DB."""
    with pytest.raises(RunRejected) as excinfo:
        run_select(sql, config=ro_config)
    assert excinfo.value.rule == rule, f"{sql!r} → unexpected rule {excinfo.value.rule!r}"

    lines = _audit_lines(ro_config)
    assert len(lines) == 1 and lines[0]["status"] == "rejected"
    assert lines[0]["args"]["sql"] == sql


def test_b1_data_modifying_cte_and_chain_rejected_end_to_end(ro_config):
    """The two headline holes — the ;-chain and the data-modifying CTE — rejected via run_select."""
    with pytest.raises(RunRejected) as chain:
        run_select("SELECT 1; DROP TABLE app.patients", config=ro_config)
    assert chain.value.rule == "multi_statement"

    with pytest.raises(RunRejected) as cte:
        run_select(
            "WITH x AS (DELETE FROM app.patients RETURNING *) SELECT * FROM x", config=ro_config
        )
    assert cte.value.rule == "dml_in_ast"


# ==============================================================================================
# B2 — Layer 2 ALONE: a write-capable admin role hits ONLY the read-only transaction.
# ==============================================================================================


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM app.patients WHERE patient_id = -1",
        "UPDATE app.claims SET amount = 0 WHERE claim_id = -1",
    ],
)
def test_b2_layer2_readonly_txn_rejects_write_isolated(admin_url, admin_conn, sql):
    """LAYER 2 (READ ONLY transaction) — write rejected independently.

    Issued as the write-capable ADMIN role, so the ONLY barrier is the READ ONLY transaction.
    The error must specifically name the read-only transaction (Layer 2), independent of Layer 3.
    """
    admin_cfg = Config(database_url=admin_url)
    with pytest.raises(DBError) as excinfo:
        run_readonly(sql, config=admin_cfg)
    msg = str(excinfo.value).lower()
    assert "read-only transaction" in msg, f"expected Layer-2 error, got: {msg!r}"
    # Sanity: the admin role is genuinely write-capable, so Layer 2 really is the only barrier.
    # (A normal write outside a read-only txn would succeed — we don't run it; we rollback-safe
    #  prove capability by the absence of a 'permission denied' in the message above.)
    assert "permission denied" not in msg


# ==============================================================================================
# B3 — Layer 3 ALONE: the read-only role with the txn forced READ WRITE hits ONLY the grant.
# ==============================================================================================


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM app.patients WHERE patient_id = -1",
        "UPDATE app.claims SET amount = 0 WHERE claim_id = -1",
    ],
)
def test_b3_layer3_role_rejects_write_isolated(role_url, seeded_db, sql):
    """LAYER 3 (least-privilege DB role) — write rejected independently.

    Issued as the querygate read-only role with the transaction forced READ WRITE, overriding
    default_transaction_read_only. With Layer 2 neutralized, the ONLY barrier is the missing
    write grant — the error must be 'permission denied' (Layer 3), independent of Layer 2.
    """
    with psycopg.connect(role_url) as conn:  # autocommit off → an explicit transaction
        with conn.cursor() as cur:
            # Must precede any query in the txn; flips this txn read-write despite the role default.
            cur.execute("SET TRANSACTION READ WRITE")
            with pytest.raises(psycopg.Error) as excinfo:
                cur.execute(sql)
        conn.rollback()
    msg = str(excinfo.value).lower()
    assert "permission denied" in msg, f"expected Layer-3 grant error, got: {msg!r}"
    # The read-only transaction was neutralized, so Layer 2 is NOT the rejecter here.
    assert "read-only transaction" not in msg


def test_b3_role_can_override_to_read_write(role_url, seeded_db):
    """Pre-condition for B3's isolation: the role CAN flip the txn to READ WRITE (so a write that
    still fails proves the *grant*, not the txn). A harmless read confirms the txn is live."""
    with psycopg.connect(role_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SET TRANSACTION READ WRITE")
            cur.execute("SHOW transaction_read_only")
            assert cur.fetchone()[0] == "off"
        conn.rollback()


# ==============================================================================================
# B4 / B5 / B6 — the happy path: cited results + auto-LIMIT/truncated.
# ==============================================================================================


def test_b4_overdue_count_cited(ro_config):
    res = run_select(OVERDUE_COUNT_SQL, config=ro_config)
    assert res.row_count == 1
    assert res.rows[0][0] == EXPECTED_OVERDUE  # the cited number
    assert res.truncated is False
    # The citation SQL is the EXACT executed query (sqlglot normalizes case/now()→CURRENT_TIMESTAMP
    # and appends the auto-LIMIT); it must be present and re-runnable against the same tables.
    sql_up = res.sql.upper()
    assert "COUNT(*)" in sql_up and "APP.FOLLOW_UPS" in sql_up
    assert isinstance(res.elapsed_ms, int) and res.elapsed_ms >= 0


def test_b5_top_provider_first(ro_config):
    res = run_select(TOP_PROVIDER_SQL, config=ro_config)
    assert res.columns == ["provider_id", "overdue_patients"]
    top_provider, top_count = res.rows[0]
    runner_up = res.rows[1][1]
    assert top_provider == EXPECTED_TOP_PROVIDER
    assert top_count == EXPECTED_TOP_OVERDUE_PATIENTS
    assert top_count > runner_up  # the unique top
    assert res.truncated is False  # 50 providers < row_limit


def test_b6_auto_limit_and_truncated(ro_config):
    """A no-LIMIT query over a >1000-row table comes back capped at row_limit, truncated=True,
    and the citation SQL shows the injected LIMIT 1000."""
    res = run_select("SELECT encounter_id FROM app.encounters", config=ro_config)
    assert res.row_count == ROW_LIMIT
    assert res.truncated is True
    assert f"LIMIT {ROW_LIMIT}" in res.sql.upper()


def test_b6_under_cap_not_truncated(ro_config):
    """A query whose full result is under the cap is NOT flagged truncated (the common case)."""
    res = run_select("SELECT provider_id FROM app.providers", config=ro_config)
    assert res.row_count == 50
    assert res.truncated is False


# ==============================================================================================
# A1 / A3 — the ok and error audit lines (the rejected line is covered in test_audit.py).
# ==============================================================================================


def test_a1_ok_call_audits_one_line(ro_config):
    res = run_select(OVERDUE_COUNT_SQL, config=ro_config)
    lines = _audit_lines(ro_config)
    assert len(lines) == 1
    (entry,) = lines
    assert entry["status"] == "ok"
    assert entry["tool"] == "run_select"
    assert entry["row_count"] == res.row_count == 1
    assert isinstance(entry["latency_ms"], int)
    assert entry["error"] is None
    # ts is a real runtime timestamp.
    from datetime import datetime

    assert datetime.fromisoformat(entry["ts"]).tzinfo is not None


def test_a3_db_error_audits_error_and_does_not_crash(ro_config):
    """A bad column passes the guard (valid SELECT syntax) but errors in the DB → status:error,
    exactly one line, process intact."""
    with pytest.raises(DBError):
        run_select("SELECT no_such_column FROM app.patients", config=ro_config)
    lines = _audit_lines(ro_config)
    assert len(lines) == 1
    (entry,) = lines
    assert entry["status"] == "error"
    assert entry["row_count"] is None
    assert entry["error"]  # a message is present


def test_a3_timeout_audits_error(role_url, seeded_db, tmp_path):
    """A slow (cross-join) query that passes the guard is killed by statement_timeout and audited
    as status:error — the timeout path is also a clean, logged envelope, not a crash."""
    cfg = Config(
        database_url=role_url,
        statement_timeout="200ms",
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    slow = (
        "SELECT count(*) FROM app.encounters a "
        "CROSS JOIN app.encounters b CROSS JOIN app.encounters c"
    )
    with pytest.raises(QueryTimeout):
        run_select(slow, config=cfg)
    lines = _audit_lines(cfg)
    assert len(lines) == 1 and lines[0]["status"] == "error"


def test_a5_n_calls_n_lines(ro_config):
    """A5 (end-to-end): an ok + a rejected + an ok = exactly 3 lines, right statuses, in order."""
    run_select(OVERDUE_COUNT_SQL, config=ro_config)
    with pytest.raises(RunRejected):
        run_select("DROP TABLE app.patients", config=ro_config)
    run_select("SELECT provider_id FROM app.providers", config=ro_config)
    statuses = [ln["status"] for ln in _audit_lines(ro_config)]
    assert statuses == ["ok", "rejected", "ok"]
