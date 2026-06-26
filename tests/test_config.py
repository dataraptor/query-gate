"""Split 02 — config + redaction-loader tests (pure, no DB, no API key)."""

from __future__ import annotations

from querygate.config import (
    DEFAULT_BYTE_CAP,
    DEFAULT_ROW_LIMIT,
    DEFAULT_STATEMENT_TIMEOUT,
    Config,
    load_redactions,
)


# Config defaults match the spec (1000 rows, 256 KB, 5s).
def test_config_defaults():
    cfg = Config()
    assert cfg.row_limit == DEFAULT_ROW_LIMIT == 1000
    assert cfg.byte_cap == DEFAULT_BYTE_CAP == 256 * 1024
    assert cfg.statement_timeout == DEFAULT_STATEMENT_TIMEOUT == "5s"
    assert cfg.decimal_as_str is False
    assert cfg.database_url is None


# Loading from a fully-unset environment must not crash; it yields the defaults.
def test_from_env_empty_is_safe():
    cfg = Config.from_env({})
    assert cfg.database_url is None
    assert cfg.row_limit == 1000
    assert cfg.redact_path is None
    assert cfg.work_mem is None


def test_from_env_overrides():
    env = {
        "QUERYGATE_DATABASE_URL": "postgresql://querygate:pw@localhost:5432/querygate",
        "QUERYGATE_ROW_LIMIT": "50",
        "QUERYGATE_BYTE_CAP": "1024",
        "QUERYGATE_STATEMENT_TIMEOUT": "2s",
        "QUERYGATE_DECIMAL_AS_STR": "true",
        "QUERYGATE_WORK_MEM": "32MB",
    }
    cfg = Config.from_env(env)
    assert cfg.database_url.endswith("/querygate")
    assert cfg.row_limit == 50
    assert cfg.byte_cap == 1024
    assert cfg.statement_timeout == "2s"
    assert cfg.decimal_as_str is True
    assert cfg.work_mem == "32MB"


def test_blank_env_values_fall_back_to_defaults():
    cfg = Config.from_env({"QUERYGATE_ROW_LIMIT": "", "QUERYGATE_STATEMENT_TIMEOUT": "  "})
    assert cfg.row_limit == 1000
    assert cfg.statement_timeout == "5s"


def test_require_database_url_raises_when_unset():
    cfg = Config()
    try:
        cfg.require_database_url()
    except RuntimeError as exc:
        assert "QUERYGATE_DATABASE_URL" in str(exc)
    else:
        raise AssertionError("expected RuntimeError when database_url is unset")


# Redaction is OFF by default: no path => empty set.
def test_load_redactions_off_by_default():
    assert load_redactions(None) == set()
    assert load_redactions("") == set()


def test_load_redactions_missing_file_is_empty(tmp_path):
    assert load_redactions(tmp_path / "nope.yaml") == set()


def test_load_redactions_mapping_form(tmp_path):
    f = tmp_path / "redact.yaml"
    f.write_text("patients:\n  - name\n  - dob\nclaims:\n  - amount\n", encoding="utf-8")
    assert load_redactions(f) == {"patients.name", "patients.dob", "claims.amount"}


def test_load_redactions_list_form(tmp_path):
    f = tmp_path / "redact.yaml"
    f.write_text("- patients.name\n- claims.amount\n", encoding="utf-8")
    assert load_redactions(f) == {"patients.name", "claims.amount"}


def test_load_redactions_empty_file(tmp_path):
    f = tmp_path / "redact.yaml"
    f.write_text("# only a comment\n", encoding="utf-8")
    assert load_redactions(f) == set()
