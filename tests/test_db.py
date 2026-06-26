"""Split 02 — Layer-2 read-only transaction tests (needs the compose Postgres + read-only role).

Skips cleanly (via the shared conftest fixtures) when no DATABASE_URL / psql is available.

What these prove:
- D1: a read works through the wrapper.
- D2: a write issued *through the read-only role* is rejected (combined Layers 2+3 smoke —
  NOT a Layer-2-alone proof; that is Split 04's test_boundary.py).
- D3: the READ ONLY transaction rejects a non-DML write (``nextval``) **isolated to Layer 2**
  by connecting as the write-capable admin role — the only thing that can stop it is the txn.
- D4: ``statement_timeout`` actually bounds runtime (the real runtime guard).
- D5: ``SET LOCAL`` does not leak to a fresh session.
- D6: the one-statement extended protocol rejects a ``;``-chained payload (the free tripwire).
"""

from __future__ import annotations

import time

import psycopg
import pytest

from querygate.config import Config
from querygate.db import DBError, QueryTimeout, run_readonly


@pytest.fixture()
def ro_config(role_url: str, seeded_db) -> Config:
    """Config pointed at the **read-only** querygate role, against the seeded DB."""
    return Config(database_url=role_url)


# D1 — a read works and returns the seeded count.
def test_d1_read_returns_seeded_count(ro_config, seeded_db):
    columns, rows = run_readonly("SELECT count(*) AS n FROM app.patients", config=ro_config)
    assert columns == ["n"]
    assert rows[0][0] == seeded_db["patients"] == 500


# D2 — writes through the read-only role are rejected (combined Layers 2+3 smoke).
@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO app.patients (name, dob, sex, city, registered_at) "
        "VALUES ('x', '2000-01-01', 'F', 'Town', now())",
        "UPDATE app.patients SET name = 'x' WHERE patient_id = 1",
        "DELETE FROM app.patients WHERE patient_id = 1",
        "CREATE TABLE app.should_not_exist (id int)",
    ],
)
def test_d2_writes_are_rejected(ro_config, sql):
    with pytest.raises(DBError) as excinfo:
        run_readonly(sql, config=ro_config)
    msg = str(excinfo.value).lower()
    # Either layer may be the rejecter (read-only txn OR missing privilege) — both are fine.
    assert "read-only" in msg or "permission" in msg or "must be owner" in msg


# D3 — the READ ONLY transaction rejects a sequence write, isolated to Layer 2.
# Connecting as the write-capable ADMIN role means the ONLY thing that can stop nextval
# is the read-only transaction — so a "read-only" error here is a clean Layer-2 proof.
def test_d3_readonly_txn_rejects_nextval(admin_url, admin_conn):
    seq = admin_conn.execute(
        "SELECT pg_get_serial_sequence('app.patients', 'patient_id')"
    ).fetchone()[0]
    assert seq, "expected an identity sequence on app.patients.patient_id"
    admin_cfg = Config(database_url=admin_url)
    with pytest.raises(DBError) as excinfo:
        run_readonly(f"SELECT nextval('{seq}')", config=admin_cfg)
    assert "read-only" in str(excinfo.value).lower()


# D4 — statement_timeout bounds runtime: a 10s sleep dies at ~the short injected timeout.
def test_d4_statement_timeout_fires(ro_config):
    start = time.monotonic()
    with pytest.raises(QueryTimeout):
        run_readonly("SELECT pg_sleep(10)", config=ro_config, statement_timeout="500ms")
    elapsed = time.monotonic() - start
    assert elapsed < 5, f"timeout should fire near 0.5s, not run the full 10s (took {elapsed:.1f}s)"


# D5 — SET LOCAL does not leak: a fresh session does not carry the injected timeout.
def test_d5_set_local_does_not_leak(ro_config, role_url):
    run_readonly("SELECT 1", config=ro_config, statement_timeout="1234ms")
    with psycopg.connect(role_url) as conn:
        value = conn.execute("SHOW statement_timeout").fetchone()[0]
    assert value != "1234ms", f"SET LOCAL leaked into a fresh session: {value!r}"


# D6 — the extended protocol (forced via prepare=True) rejects a ;-chained payload, so a
# multi-statement string fails at the driver before it can run (the free fourth tripwire, §4).
def test_d6_multistatement_is_rejected(ro_config):
    with pytest.raises(DBError) as excinfo:
        run_readonly("SELECT 1; SELECT 2", config=ro_config)
    assert "multiple commands" in str(excinfo.value).lower()
