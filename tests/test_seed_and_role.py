"""Split 01 acceptance tests — synthetic DB, deterministic seed, Layer-3 role.

No LLM, no API key. Run against a real Postgres (see conftest). The headline is T7:
the read-only role cannot write *at the database layer, with no app code in the path*.
"""

from __future__ import annotations

import hashlib

import psycopg
import pytest

# --- The deterministic contract (documented expected numbers; see scripts/seed.py) ---
EXPECTED_COUNTS = {
    "patients": 500,
    "providers": 50,
    "encounters": 4000,
    "claims": 4000,
    "follow_ups": 480,
}
EXPECTED_OVERDUE = 300            # follow_ups: completed_at IS NULL AND due_date < now()
EXPECTED_TOP_PROVIDER = 1         # the money-demo "most overdue patients" provider
EXPECTED_TOP_OVERDUE_PATIENTS = 60

APP_TABLES = ["patients", "providers", "encounters", "claims", "follow_ups"]

EXPECTED_COLUMNS = {
    "patients": {"patient_id", "name", "dob", "sex", "city", "registered_at", "last_contacted_at"},
    "providers": {"provider_id", "name", "specialty", "npi"},
    "encounters": {"encounter_id", "patient_id", "provider_id", "date", "type", "status"},
    "claims": {"claim_id", "encounter_id", "amount", "status", "submitted_at", "paid_at"},
    "follow_ups": {"follow_up_id", "patient_id", "due_date", "completed_at"},
}

EXPECTED_FKS = {
    ("encounters", "patient_id", "patients", "patient_id"),
    ("encounters", "provider_id", "providers", "provider_id"),
    ("claims", "encounter_id", "encounters", "encounter_id"),
    ("follow_ups", "patient_id", "patients", "patient_id"),
}

WRITE_STATEMENTS = [
    ("INSERT", "INSERT INTO app.patients (name, dob, sex, city, registered_at) "
               "VALUES ('x', DATE '1990-01-01', 'M', 'x', now())"),
    ("UPDATE", "UPDATE app.patients SET name = 'hacked'"),
    ("DELETE", "DELETE FROM app.patients"),
    ("CREATE", "CREATE TABLE app.evil (id int)"),
    ("DROP", "DROP TABLE app.patients"),
    ("TRUNCATE", "TRUNCATE app.patients"),
]


# ---------------------------------------------------------------------------
# T1 — schema: tables, columns, PKs, FKs
# ---------------------------------------------------------------------------
def test_t1_tables_and_columns_exist(admin_conn):
    rows = admin_conn.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'app'"
    ).fetchall()
    found = {r[0] for r in rows}
    assert set(APP_TABLES) <= found, f"missing tables: {set(APP_TABLES) - found}"

    for table, cols in EXPECTED_COLUMNS.items():
        rows = admin_conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'app' AND table_name = %s",
            (table,),
        ).fetchall()
        assert {r[0] for r in rows} == cols, f"{table} columns mismatch"


def test_t1_primary_keys(admin_conn):
    for table in APP_TABLES:
        rows = admin_conn.execute(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
             AND tc.table_schema = kcu.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema = 'app' AND tc.table_name = %s
            """,
            (table,),
        ).fetchall()
        assert len(rows) == 1, f"{table} should have a single-column PK"
        assert rows[0][0].endswith("_id"), f"{table} PK column should be the *_id column"


def test_t1_foreign_keys(admin_conn):
    rows = admin_conn.execute(
        """
        SELECT tc.table_name, kcu.column_name, ccu.table_name, ccu.column_name
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON tc.constraint_name = kcu.constraint_name AND tc.table_schema = kcu.table_schema
        JOIN information_schema.constraint_column_usage ccu
          ON tc.constraint_name = ccu.constraint_name AND tc.table_schema = ccu.table_schema
        WHERE tc.constraint_type = 'FOREIGN KEY' AND tc.table_schema = 'app'
        """
    ).fetchall()
    found = {(t, c, rt, rc) for (t, c, rt, rc) in rows}
    assert EXPECTED_FKS <= found, f"missing FKs: {EXPECTED_FKS - found}"


def test_t1_key_column_types(admin_conn):
    types = dict(admin_conn.execute(
        "SELECT table_name || '.' || column_name, data_type "
        "FROM information_schema.columns WHERE table_schema = 'app'"
    ).fetchall())
    assert types["claims.amount"] == "numeric"
    assert types["patients.dob"] == "date"
    assert types["follow_ups.due_date"] == "date"
    assert types["patients.registered_at"] == "timestamp with time zone"
    assert types["claims.paid_at"] == "timestamp with time zone"


# ---------------------------------------------------------------------------
# T2 — deterministic volumes (exact counts; identical after a reseed)
# ---------------------------------------------------------------------------
def test_t2_exact_row_counts(admin_conn):
    for table, expected in EXPECTED_COUNTS.items():
        (count,) = admin_conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
        assert count == expected, f"{table}: expected {expected}, got {count}"


def _db_digest(conn) -> str:
    """A stable digest of every row in every app table (ordered by PK)."""
    h = hashlib.sha256()
    order = {
        "patients": "patient_id", "providers": "provider_id", "encounters": "encounter_id",
        "claims": "claim_id", "follow_ups": "follow_up_id",
    }
    for table in APP_TABLES:
        rows = conn.execute(f"SELECT * FROM app.{table} ORDER BY {order[table]}").fetchall()
        h.update(repr(rows).encode("utf-8"))
    return h.hexdigest()


def test_t2_reseed_is_byte_identical(admin_conn, admin_url):
    from conftest import seed_module  # the loaded scripts/seed.py

    digest_before = _db_digest(admin_conn)
    with psycopg.connect(admin_url) as conn:
        seed_module.seed(conn, reset=True)
    digest_after = _db_digest(admin_conn)
    assert digest_before == digest_after, "reseed produced different data — not deterministic"
    # counts still exact after reseed
    for table, expected in EXPECTED_COUNTS.items():
        (count,) = admin_conn.execute(f"SELECT count(*) FROM app.{table}").fetchone()
        assert count == expected


# ---------------------------------------------------------------------------
# T3 — time-stable bands (the now() fix)
# ---------------------------------------------------------------------------
def test_t3_overdue_count_is_fixed(admin_conn):
    (overdue,) = admin_conn.execute(
        "SELECT count(*) FROM app.follow_ups WHERE completed_at IS NULL AND due_date < now()"
    ).fetchone()
    assert overdue == EXPECTED_OVERDUE


def test_t3_band_gap_nothing_within_a_year(admin_conn):
    (near,) = admin_conn.execute(
        "SELECT count(*) FROM app.follow_ups "
        "WHERE due_date BETWEEN (now() - interval '1 year')::date "
        "                   AND (now() + interval '1 year')::date"
    ).fetchone()
    assert near == 0, f"{near} follow_ups fall within 1 year of now() — band gap broken"


# ---------------------------------------------------------------------------
# T4 — money-demo quirk: a unique top provider for overdue patients
# ---------------------------------------------------------------------------
def test_t4_unique_top_provider(admin_conn):
    rows = admin_conn.execute(
        """
        SELECT e.provider_id, count(DISTINCT f.patient_id) AS overdue_patients
        FROM app.follow_ups f
        JOIN app.encounters e ON e.patient_id = f.patient_id
        WHERE f.completed_at IS NULL AND f.due_date < now()
        GROUP BY e.provider_id
        ORDER BY overdue_patients DESC, e.provider_id
        """
    ).fetchall()
    assert len(rows) >= 2
    top_provider, top_count = rows[0]
    _, runner_up = rows[1]
    assert top_count > runner_up, f"top {top_count} not strictly greater than runner-up {runner_up}"
    assert top_provider == EXPECTED_TOP_PROVIDER
    assert top_count == EXPECTED_TOP_OVERDUE_PATIENTS


# ---------------------------------------------------------------------------
# T5 — search_text quirk: "Sarah Lee" exists, "Sara Lee" does not
# ---------------------------------------------------------------------------
def test_t5_sarah_lee_quirk(admin_conn):
    (sarah,) = admin_conn.execute(
        "SELECT count(*) FROM app.patients WHERE name = 'Sarah Lee'"
    ).fetchone()
    (sara,) = admin_conn.execute(
        "SELECT count(*) FROM app.patients WHERE name = 'Sara Lee'"
    ).fetchone()
    assert sarah == 1, "expected exactly one 'Sarah Lee'"
    assert sara == 0, "there must be no exact 'Sara Lee' (fuzzy search must be required)"


# ---------------------------------------------------------------------------
# T6 — ANALYZE ran (reltuples populated, not -1)
# ---------------------------------------------------------------------------
def test_t6_analyze_ran(admin_conn):
    (reltuples,) = admin_conn.execute(
        "SELECT reltuples FROM pg_class WHERE oid = 'app.patients'::regclass"
    ).fetchone()
    assert reltuples > 0, f"pg_class.reltuples = {reltuples}; ANALYZE did not run"


# ---------------------------------------------------------------------------
# T7 — Layer 3: the read-only role cannot write (the load-bearing proof)
# ---------------------------------------------------------------------------
def test_t7_role_can_select(role_url, seeded_db):
    with psycopg.connect(role_url) as conn:
        (count,) = conn.execute("SELECT count(*) FROM app.patients").fetchone()
        assert count == EXPECTED_COUNTS["patients"]


@pytest.mark.parametrize("kind,sql", WRITE_STATEMENTS, ids=[k for k, _ in WRITE_STATEMENTS])
def test_t7_role_cannot_write(role_url, seeded_db, kind, sql):
    # Fresh connection per statement so an aborted transaction can't mask the next assertion.
    with pytest.raises(psycopg.Error) as exc:
        with psycopg.connect(role_url) as conn:
            conn.execute(sql)
            conn.commit()
    msg = str(exc.value).lower()
    assert ("permission denied" in msg) or ("read-only" in msg), \
        f"{kind} failed but not with a privilege/read-only error: {exc.value!r}"


@pytest.mark.parametrize("kind,sql", WRITE_STATEMENTS, ids=[k for k, _ in WRITE_STATEMENTS])
def test_t7_grants_block_writes_even_with_readonly_disabled(role_url, seeded_db, kind, sql):
    """The bedrock: even if the (overridable) read-only default is turned OFF, the role's
    *lack of write grants* still blocks every write. This is the Layer-3 guarantee that
    holds "even if every line of application code were wrong" — it does not depend on the
    read-only transaction at all.
    """
    with pytest.raises(psycopg.Error) as exc:
        with psycopg.connect(role_url, autocommit=True) as conn:
            conn.execute("SET default_transaction_read_only = off")
            conn.execute(sql)
    msg = str(exc.value).lower()
    assert ("permission denied" in msg) or ("must be owner" in msg), \
        f"{kind} was not blocked by grants alone: {exc.value!r}"


def test_t7_role_cannot_call_nextval(role_url, admin_conn, seeded_db):
    # A write Postgres recognises that isn't DML syntax. Find a real sequence first.
    seq = admin_conn.execute(
        "SELECT schemaname || '.' || sequencename FROM pg_sequences "
        "WHERE schemaname = 'app' LIMIT 1"
    ).fetchone()
    if not seq:
        pytest.skip("no sequence reachable in schema app to test nextval")
    with pytest.raises(psycopg.Error) as exc:
        with psycopg.connect(role_url) as conn:
            conn.execute(f"SELECT nextval('{seq[0]}')")
    msg = str(exc.value).lower()
    assert ("permission denied" in msg) or ("read-only" in msg), \
        f"nextval failed but not with a privilege/read-only error: {exc.value!r}"
