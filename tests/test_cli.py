"""Split 07 — the CLI surface (spec §15). Drives ``querygate.cli.main(argv=...)`` in-process.

The headline gate (C-2): ``querygate query "DELETE …"`` visibly **refuses** and exits non-zero,
with the SQL never reaching the DB — the by-hand boundary demo. Plus the cited happy path (C-1),
read-only role (C-3), one audit line per call (C-4), deterministic ``seed --reset`` through the CLI
(C-5), bare = stdio server (C-6), the clean ``--http`` stub (C-7), the eval wired to the real Split-09
harness with an honest no-DB failure (C-8), and side-effect-free library imports (C-9).

The pure tests (C-6/C-7/C-8/C-9) need neither DB nor key and always run. The DB-backed query/seed
tests use the read-only role / admin URL via the shared conftest and skip cleanly when absent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from querygate import cli


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    """Keep the CLI tests hermetic: ``main()`` now loads the repo ``.env`` on start (so the quickstart
    works with no manual export), but these tests assert env-resolution behaviour and must not pick up
    the developer's local ``.env``. Neutralize the load so each test controls its own environment."""
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)


def _audit_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


@pytest.fixture()
def ro_env(monkeypatch, role_url: str, seeded_db, tmp_path):
    """Point the CLI's ``Config.from_env()`` at the read-only role + a fresh temp audit log."""
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setenv("QUERYGATE_DATABASE_URL", role_url)
    monkeypatch.setenv("QUERYGATE_AUDIT_PATH", str(audit))
    return audit


OVERDUE_SQL = (
    "SELECT count(*) FROM app.follow_ups WHERE completed_at IS NULL AND due_date < now()"
)
EXPECTED_OVERDUE = 300  # Split-01 deterministic contract


# ==================================================================================================
# C-1 — query happy path + --json.
# ==================================================================================================


def test_c1_query_happy_path(ro_env, capsys):
    rc = cli.main(["query", OVERDUE_SQL])
    assert rc == 0
    out = capsys.readouterr().out
    assert str(EXPECTED_OVERDUE) in out
    assert "row_count" in out and "sql" in out


def test_c1_query_json_is_runresult_parseable(ro_env, capsys):
    rc = cli.main(["query", OVERDUE_SQL, "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["row_count"] == 1
    assert payload["rows"] == [[EXPECTED_OVERDUE]]
    assert "count(*)".lower() in payload["sql"].lower()
    assert payload["truncated"] is False


# ==================================================================================================
# C-2 — a write refuses and exits non-zero; the SQL never reached the DB (the headline).
# ==================================================================================================


def test_c2_query_delete_refuses_nonzero(ro_env, capsys):
    audit = ro_env
    rc = cli.main(["query", "DELETE FROM app.patients"])
    assert rc != 0  # visibly fails
    err = capsys.readouterr().err
    assert "REJECTED" in err  # the guard reason is shown to the operator
    # The SQL never reached the DB: the one audit line is a Layer-1 rejection.
    lines = _audit_lines(audit)
    assert len(lines) == 1
    assert lines[0]["tool"] == "run_select" and lines[0]["status"] == "rejected"


# ==================================================================================================
# C-3 — query uses the read-only role.
# ==================================================================================================


def test_c3_query_uses_readonly_role(ro_env, role_url, monkeypatch):
    # The CLI resolves its connection from QUERYGATE_DATABASE_URL — the read-only role's URL,
    # never the admin DATABASE_URL. Assert the configured URL is exactly the read-only one.
    from querygate.config import Config

    assert Config.from_env().database_url == role_url
    # And a representative write attempted *through* query is refused (Layer 1; see C-2).
    assert cli.main(["query", "UPDATE app.patients SET name = 'x'"]) != 0


# ==================================================================================================
# C-4 — one audit line per query invocation.
# ==================================================================================================


def test_c4_query_audits_exactly_one_line(ro_env, capsys):
    audit = ro_env
    cli.main(["query", OVERDUE_SQL])
    lines = _audit_lines(audit)
    assert len(lines) == 1
    assert lines[0]["tool"] == "run_select" and lines[0]["status"] == "ok"


# ==================================================================================================
# C-5 — seed --reset is deterministic through the CLI path.
# ==================================================================================================

EXPECTED_COUNTS = {
    "patients": 500,
    "providers": 50,
    "encounters": 4000,
    "claims": 4000,
    "follow_ups": 480,
}


def test_c5_seed_reset_deterministic_via_cli(admin_url, seeded_db, monkeypatch, capsys):
    monkeypatch.setenv("DATABASE_URL", admin_url)

    rc1 = cli.main(["seed", "--reset"])
    out1 = capsys.readouterr().out
    rc2 = cli.main(["seed", "--reset"])
    out2 = capsys.readouterr().out

    assert rc1 == 0 and rc2 == 0
    for table, n in EXPECTED_COUNTS.items():
        assert f"{table}={n}" in out1
    # A second --reset reproduces the exact same summary (byte-identical determinism).
    assert out1 == out2


def test_c5_seed_without_admin_url_fails_clean(monkeypatch, capsys):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    rc = cli.main(["seed", "--database-url", ""])  # explicit empty → still no usable URL
    assert rc != 0
    assert "error" in capsys.readouterr().err.lower()


# ==================================================================================================
# C-6 — bare querygate = the stdio server (no Split-06 regression).
# ==================================================================================================


def test_c6_bare_querygate_runs_stdio_server(monkeypatch):
    called = {}

    def fake_stdio():
        called["stdio"] = True

    monkeypatch.setattr(cli, "_serve_stdio", fake_stdio)
    rc = cli.main([])
    assert rc == 0
    assert called.get("stdio") is True


def test_c6_stdio_server_still_builds_the_tools():
    # The Split-06 smoke: the server the CLI default launches still registers the read tools
    # (Split 13 added explain_select as the optional fifth — see SPLIT-13 R3).
    from querygate.config import Config
    from querygate.server import build_server

    server = build_server(Config())
    names = set(server._tool_manager._tools.keys())
    assert names == {
        "list_tables", "describe_table", "run_select", "search_text", "explain_select"
    }


# ==================================================================================================
# C-7 — --http now starts the Streamable-HTTP runner (Split 10 wired the seam).
# ==================================================================================================


def test_c7_http_routes_to_the_http_runner(monkeypatch):
    # Split 10 filled the seam: bare `--http` dispatches to the HTTP runner (which blocks serving),
    # so we monkeypatch it to capture the call instead of binding a port — same pattern as C-6.
    called = {}

    def fake_http(port):
        called["port"] = port

    monkeypatch.setattr(cli, "_serve_http", fake_http)
    rc = cli.main(["--http"])
    assert rc == 0
    assert called.get("port") == 8000  # the default port


def test_c7_http_port_is_forwarded_to_the_runner(monkeypatch):
    called = {}
    monkeypatch.setattr(cli, "_serve_http", lambda port: called.setdefault("port", port))
    rc = cli.main(["--http", "--port", "9001"])
    assert rc == 0
    assert called.get("port") == 9001


# ==================================================================================================
# C-8 — the eval is wired to the real harness (Split 09) and fails HONESTLY without a DB/key.
# ==================================================================================================


def test_c8_eval_wired_and_fails_honestly_without_db(monkeypatch, capsys):
    # Split 09 landed the real harness, so the CLI delegates to it (no more "arrives in Split 09" stub).
    assert cli.EVAL_SCRIPT.exists(), "querygate eval should delegate to evals/run_eval.py"

    # Honesty rule: with the read-only DB URL absent, the harness must fail clean (non-zero) with a
    # clear message and print NO fabricated metrics — never invent numbers. Call run_eval() directly
    # (not via subprocess) so the test stays fast and never touches the model API.
    from evals import run_eval

    monkeypatch.delenv("QUERYGATE_DATABASE_URL", raising=False)
    rc = run_eval.run_eval(
        repeats=1, quick=True, model="gpt-5.5", out=None,
        price_in=None, price_out=None, max_steps=1,
    )
    assert rc != 0
    err = capsys.readouterr().err
    assert "QUERYGATE_DATABASE_URL" in err
    for fake in ("grounded-rate", "table-precision", "mean +/-"):
        assert fake not in err


# ==================================================================================================
# C-9 — library exports import cleanly with no side effects.
# ==================================================================================================


def test_c9_library_imports_no_side_effects():
    import importlib

    mod = importlib.import_module("querygate")
    for name in ("run_select", "list_tables", "describe_table", "search_text"):
        assert callable(getattr(mod, name))


def test_c9_import_does_not_open_db_or_start_server(monkeypatch):
    # Importing the package must not connect to a DB or start a server. Make psycopg.connect raise
    # if anyone touches it during import; a fresh re-import must still succeed.
    import importlib

    import psycopg

    def _boom(*a, **k):
        raise AssertionError("querygate import opened a DB connection (side effect)")

    monkeypatch.setattr(psycopg, "connect", _boom)
    importlib.reload(importlib.import_module("querygate"))


# ==================================================================================================
# Usage errors fail cleanly (no traceback) — R1.
# ==================================================================================================


def test_unknown_subcommand_is_clean_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["frobnicate"])
    assert exc.value.code != 0  # argparse exits non-zero with a usage message, not a traceback
