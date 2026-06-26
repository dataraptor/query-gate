"""Split 05 — the result filter: byte cap + redaction (spec §4, §6, §8, §18).

Two layers of proof:

* **Pure unit tests** (no DB) for the filter primitives — ``apply_byte_cap``, ``apply_redaction``,
  ``columns_to_redact`` — where the byte-cap post-condition and the masking logic are exercised
  directly and fast.
* **DB-backed integration** through ``run_select`` (skips cleanly via the shared conftest when
  ``DATABASE_URL`` / ``psql`` is absent), confirming the same behaviour end to end: the row LIMIT
  and the byte cap are independent signals (R-1, R-2), an oversized cell is cut (R-3), redaction
  masks result cells and records them (R-4) **without** breaking ``WHERE``/aggregates (R-5), and
  redaction is **off** by default (R-6).
"""

from __future__ import annotations

import json

import pytest

from querygate.config import Config
from querygate.result import (
    REDACTION_MASK,
    apply_byte_cap,
    apply_redaction,
    columns_to_redact,
)
from querygate.tools import run_select


def _payload_bytes(rows) -> int:
    return len(json.dumps(rows, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


# ==============================================================================================
# Pure unit tests — the filter primitives (no DB).
# ==============================================================================================


def test_columns_to_redact_default_off():
    """R-6 (unit): an empty redact set masks nothing."""
    assert columns_to_redact(["name", "dob"], set()) == []


def test_columns_to_redact_precise_with_table():
    """With a known table, only the exact table.column entries match."""
    redact = {"patients.name", "patients.dob"}
    assert columns_to_redact(["name", "dob", "city"], redact, table="patients") == ["name", "dob"]
    # A different table with the same column name is NOT masked in precise mode.
    assert columns_to_redact(["name"], redact, table="providers") == []


def test_columns_to_redact_by_column_name_when_table_unknown():
    """run_select can't map projected columns to a base table → match by column name (any *.col)."""
    redact = {"patients.name"}
    assert columns_to_redact(["name", "city"], redact, table=None) == ["name"]


def test_apply_redaction_masks_only_named_columns():
    rows = [["Alice", "2000-01-01", "NYC"], ["Bob", "1990-05-05", "LA"]]
    out = apply_redaction(rows, ["name", "dob", "city"], ["name", "dob"])
    assert out == [[REDACTION_MASK, REDACTION_MASK, "NYC"], [REDACTION_MASK, REDACTION_MASK, "LA"]]
    # Original is not mutated (a fresh list is returned).
    assert rows[0][0] == "Alice"


def test_byte_cap_noop_under_cap():
    rows = [[1, "a"], [2, "b"]]
    out, truncated = apply_byte_cap(rows, byte_cap=10_000)
    assert out == rows and truncated is False


def test_byte_cap_rowwise_truncation():
    """R-2 (unit): a payload over the cap is truncated row-wise and ends up under the cap."""
    rows = [[i, "x" * 100] for i in range(500)]  # ~ tens of KB
    assert _payload_bytes(rows) > 2_000
    out, truncated = apply_byte_cap(rows, byte_cap=2_000)
    assert truncated is True
    assert 0 < len(out) < len(rows)
    assert _payload_bytes(out) <= 2_000  # the post-condition: actually under the cap


def test_byte_cap_oversized_cell_is_cut():
    """R-3 (unit): a single huge cell is cut (not shipped whole); the row still comes back."""
    rows = [["small", "X" * 50_000]]
    out, truncated = apply_byte_cap(rows, byte_cap=4_000)
    assert truncated is True
    assert len(out) == 1  # the row survives — the cell was cut, not the row dropped
    cut_cell = out[0][1]
    assert cut_cell.endswith("…[truncated]")
    assert len(cut_cell) < 50_000
    assert _payload_bytes(out) <= 4_000


def test_byte_cap_independent_of_row_limit():
    """The two flags are independent: a small row count can still trip the byte cap (big cells)."""
    rows = [["Y" * 30_000] for _ in range(3)]
    out, truncated = apply_byte_cap(rows, byte_cap=5_000)
    assert truncated is True
    assert _payload_bytes(out) <= 5_000


# ==============================================================================================
# DB-backed integration through run_select (compose Postgres; no API key).
# ==============================================================================================


@pytest.fixture()
def ro_config(role_url: str, seeded_db, tmp_path) -> Config:
    return Config(database_url=role_url, audit_path=str(tmp_path / "audit.jsonl"))


def _redact_config(role_url, tmp_path, mapping: dict[str, list[str]]) -> Config:
    """A Config whose redact.yaml masks the given {table: [cols]} mapping."""
    import yaml

    redact_file = tmp_path / "redact.yaml"
    redact_file.write_text(yaml.safe_dump(mapping), encoding="utf-8")
    return Config(
        database_url=role_url,
        redact_path=str(redact_file),
        audit_path=str(tmp_path / "audit.jsonl"),
    )


def _audit_lines(cfg: Config) -> list[dict]:
    from pathlib import Path

    p = Path(cfg.audit_path)
    if not p.exists():
        return []
    return [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_r1_row_limit_and_truncated(ro_config):
    """R-1: a large result is capped at row_limit with truncated=True (alongside the byte cap)."""
    res = run_select("SELECT encounter_id FROM app.encounters", config=ro_config)
    assert res.row_count == ro_config.row_limit  # 1000
    assert res.truncated is True
    assert res.truncated_bytes is False  # 1000 ints fit comfortably under 256 KB


def test_r2_byte_cap_truncates_rowwise(role_url, seeded_db, tmp_path):
    """R-2: with a small byte cap, a result is truncated row-wise and the payload is under cap."""
    cfg = Config(
        database_url=role_url,
        byte_cap=4_000,
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    res = run_select("SELECT * FROM app.patients", config=cfg)
    assert res.truncated_bytes is True
    assert _payload_bytes(res.rows) <= 4_000
    assert res.row_count < 500  # fewer than all patients came back


def test_r3_oversized_cell_cut(role_url, seeded_db, tmp_path):
    """R-3: a single huge cell (built with repeat()) is cut, not shipped whole; truncated_bytes."""
    cfg = Config(
        database_url=role_url,
        byte_cap=4_000,
        audit_path=str(tmp_path / "audit.jsonl"),
    )
    res = run_select("SELECT repeat('A', 50000) AS big", config=cfg)
    assert res.truncated_bytes is True
    assert res.row_count == 1
    assert res.rows[0][0].endswith("…[truncated]")
    assert _payload_bytes(res.rows) <= 4_000


def test_r4_redaction_masks_cells_and_records(role_url, seeded_db, tmp_path):
    """R-4: with patients.name/dob configured, those cells are '***' and recorded everywhere."""
    cfg = _redact_config(role_url, tmp_path, {"patients": ["name", "dob"]})
    res = run_select(
        "SELECT patient_id, name, dob, city FROM app.patients ORDER BY patient_id", config=cfg
    )
    assert res.redactions == ["dob", "name"]
    first = res.rows[0]
    assert first[1] == REDACTION_MASK and first[2] == REDACTION_MASK  # name, dob masked
    assert first[0] != REDACTION_MASK and first[3] != REDACTION_MASK  # patient_id, city intact
    # The audit line records the masked columns too.
    (entry,) = _audit_lines(cfg)
    assert entry["status"] == "ok"
    assert sorted(entry["redactions"]) == ["dob", "name"]


def test_r5_redaction_does_not_break_aggregates(role_url, seeded_db, tmp_path):
    """R-5: a count(*)/WHERE over a redacted column still works — only returned cells are masked."""
    cfg = _redact_config(role_url, tmp_path, {"patients": ["name", "dob"]})
    # Baseline: the true count with no redaction configured.
    plain = Config(database_url=role_url, audit_path=str(tmp_path / "plain.jsonl"))
    expected = run_select(
        "SELECT count(*) FROM app.patients WHERE name ILIKE 'A%'", config=plain
    ).rows[0][0]
    # With name redacted, the WHERE/count is unaffected (the count column isn't named 'name').
    res = run_select("SELECT count(*) FROM app.patients WHERE name ILIKE 'A%'", config=cfg)
    assert res.rows[0][0] == expected
    assert res.redactions == []  # nothing named 'name'/'dob' is in the output


def test_r6_redaction_default_off(ro_config):
    """R-6: with no redact.yaml configured, no cells are masked and redactions=[]."""
    res = run_select("SELECT name, dob FROM app.patients LIMIT 5", config=ro_config)
    assert res.redactions == []
    assert all(cell != REDACTION_MASK for row in res.rows for cell in row)
