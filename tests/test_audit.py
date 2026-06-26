"""Split 04 — audit-log tests (NO database, NO API key needed).

These prove the audit module's contract and the **Layer-1 rejection** audit path, which fires
before any DB call — so the whole file runs without Postgres:

- the JSONL append round-trips back to an ``AuditLine`` and writes exactly one line per call;
- ``ts`` comes from the runtime (a real RFC3339 timestamp close to now), never hard-coded (A4);
- a rejected write through ``run_select`` writes exactly one ``status:"rejected"`` line carrying
  the offending ``args.sql`` and the guard reason (A2), and the SQL never reaches a DB;
- concurrent appends never interleave a half-line (the process-level lock holds).
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from querygate.audit import append_audit, now_rfc3339
from querygate.config import Config
from querygate.models import AuditLine
from querygate.tools import RunRejected, run_select


def _read_lines(path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


# --- The audit module itself ------------------------------------------------------------------


def test_append_roundtrips_to_auditline(tmp_path):
    path = tmp_path / "audit.jsonl"
    line = AuditLine(
        ts=now_rfc3339(),
        tool="run_select",
        args={"sql": "SELECT 1"},
        row_count=1,
        latency_ms=3,
        status="ok",
    )
    append_audit(line, path)

    raw = path.read_text(encoding="utf-8")
    assert raw.endswith("\n")
    # Each line is valid JSON and round-trips back to an identical AuditLine.
    parsed = AuditLine.model_validate_json(raw.strip())
    assert parsed == line
    assert parsed.redactions == []  # default populated, not dropped


def test_one_line_per_call(tmp_path):
    """A5 (unit): N appends → exactly N lines, no double-write, no missing line."""
    path = tmp_path / "audit.jsonl"
    for i in range(5):
        append_audit(
            AuditLine(
                ts=now_rfc3339(), tool="run_select", args={"i": i},
                row_count=i, latency_ms=1, status="ok",
            ),
            path,
        )
    lines = _read_lines(path)
    assert len(lines) == 5
    assert [ln["args"]["i"] for ln in lines] == [0, 1, 2, 3, 4]


def test_ts_is_rfc3339_from_runtime():
    """A4: ts parses as a real RFC3339 timestamp close to now — not a frozen string."""
    before = datetime.now(timezone.utc)
    ts = now_rfc3339()
    after = datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(ts)  # raises if not ISO-8601/RFC3339
    assert parsed.tzinfo is not None, "timestamp must be tz-aware (UTC)"
    assert before <= parsed <= after, "ts must come from the runtime clock, not be hard-coded"


def test_concurrent_appends_do_not_interleave(tmp_path):
    """The process-level lock keeps concurrent appends from interleaving a half-line."""
    path = tmp_path / "audit.jsonl"
    n_threads, per_thread = 8, 50

    def worker(tid: int) -> None:
        for j in range(per_thread):
            append_audit(
                AuditLine(
                    ts=now_rfc3339(), tool="run_select",
                    args={"tid": tid, "j": j}, row_count=0, latency_ms=1, status="ok",
                ),
                path,
            )

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    lines = _read_lines(path)  # json.loads on every line would raise on a torn write
    assert len(lines) == n_threads * per_thread
    # Every (tid, j) pair appears exactly once — nothing dropped or duplicated.
    seen = {(ln["args"]["tid"], ln["args"]["j"]) for ln in lines}
    assert len(seen) == n_threads * per_thread


# --- The Layer-1 rejection audit path (no DB: the guard rejects before any DB call) -----------

# (rule, sql) — a slice of the Appendix-B write corpus that the guard rejects at Layer 1.
_REJECT_CORPUS = [
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
    ("denylisted_function", "SELECT nextval('some_seq')"),
]


@pytest.mark.parametrize("rule,sql", _REJECT_CORPUS, ids=[c[1][:24] for c in _REJECT_CORPUS])
def test_a2_rejected_write_audits_exactly_one_line(tmp_path, rule, sql):
    """A2: a rejected write writes exactly ONE status:'rejected' line with the offending
    args.sql + the guard reason, and the SQL never reaches a DB (database_url is None)."""
    path = tmp_path / "audit.jsonl"
    # database_url is deliberately None — if the guard let any of these through to the DB,
    # run_readonly would raise a *config* error, not RunRejected. So a clean RunRejected here
    # is itself proof the SQL never left Layer 1.
    cfg = Config(database_url=None, audit_path=str(path))

    with pytest.raises(RunRejected) as excinfo:
        run_select(sql, config=cfg)
    assert excinfo.value.rule == rule

    lines = _read_lines(path)
    assert len(lines) == 1, "a rejection must write exactly one audit line"
    (entry,) = lines
    assert entry["status"] == "rejected"
    assert entry["tool"] == "run_select"
    assert entry["args"]["sql"] == sql  # the offending SQL is recorded verbatim
    assert entry["row_count"] is None
    assert entry["error"] and len(entry["error"]) > 5  # a legible reason is present
    AuditLine.model_validate(entry)  # the line round-trips


def test_a5_n_calls_n_lines_mixed(tmp_path):
    """A5: several rejected calls → exactly that many lines (no double/missing logging)."""
    path = tmp_path / "audit.jsonl"
    cfg = Config(database_url=None, audit_path=str(path))
    for sql in ("DROP TABLE app.patients", "DELETE FROM app.claims", "TRUNCATE app.encounters"):
        with pytest.raises(RunRejected):
            run_select(sql, config=cfg)
    assert len(_read_lines(path)) == 3
