"""Split 13 P-2 — the redaction-on cut, exercised through the SHIPPED root ``redact.yaml``.

Split 05 proved the redaction *primitives*; this test proves the **documented demo cut** the README
points reviewers at: pointing ``QUERYGATE_REDACT_PATH`` at the repo's ``redact.yaml`` masks the PHI
columns as ``***`` in a real ``run_select`` answer, records them in ``RunResult.redactions`` AND the
audit line, and **aggregates over a masked column still return the true value** (the redaction-vs-
aggregates boundary, §18). It also guards the shipped config file from silently rotting.

Needs the read-only role + seeded DB; skips cleanly via the shared conftest when DB/psql is absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from querygate.config import Config
from querygate.tools import run_select

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPED_REDACT = REPO_ROOT / "redact.yaml"


def _audit_lines(cfg: Config) -> list[dict]:
    p = Path(cfg.audit_path)
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture()
def redacted_config(role_url: str, seeded_db, tmp_path) -> Config:
    """The read-only role with the SHIPPED redact.yaml loaded (redaction ON)."""
    return Config(
        database_url=role_url,
        audit_path=str(tmp_path / "audit.jsonl"),
        redact_path=str(SHIPPED_REDACT),
    )


def test_shipped_redact_yaml_declares_the_phi_cut():
    """The shipped file documents the PHI cut (patients.name, patients.dob) and loads cleanly."""
    from querygate.config import load_redactions

    assert SHIPPED_REDACT.exists()
    entries = load_redactions(str(SHIPPED_REDACT))
    assert {"patients.name", "patients.dob"} <= entries


def test_p2_redaction_on_masks_phi_and_records_it(redacted_config: Config):
    result = run_select(
        "SELECT name, dob, city FROM app.patients ORDER BY patient_id LIMIT 3",
        config=redacted_config,
    )
    # Every name/dob cell is masked; city (not configured) is left intact.
    name_i, dob_i, city_i = (result.columns.index(c) for c in ("name", "dob", "city"))
    for row in result.rows:
        assert row[name_i] == "***"
        assert row[dob_i] == "***"
        assert row[city_i] != "***"  # a non-PHI column is untouched
    # Recorded in the result AND the audit line.
    assert set(result.redactions) == {"name", "dob"}
    line = _audit_lines(redacted_config)[-1]
    assert line["status"] == "ok"
    assert set(line["redactions"]) == {"name", "dob"}


def test_p2_aggregate_over_masked_column_still_works(redacted_config: Config):
    """Redaction hides the column from the *result*, not from WHERE/aggregates (§18)."""
    masked = run_select(
        "SELECT count(*) FROM app.patients WHERE name ILIKE 'A%'", config=redacted_config
    )
    # The output column is `count`, not `name`, so nothing is masked and the count is real.
    assert masked.redactions == []
    count = masked.rows[0][0]
    assert isinstance(count, int) and count > 0

    # Cross-check against the same query with redaction OFF — identical count.
    plain = run_select(
        "SELECT count(*) FROM app.patients WHERE name ILIKE 'A%'",
        config=Config(database_url=redacted_config.database_url, audit_path=redacted_config.audit_path),
    )
    assert plain.rows[0][0] == count
