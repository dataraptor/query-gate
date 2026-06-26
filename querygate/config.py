"""Typed configuration for QueryGate (spec §14, §16).

A single frozen ``Config`` object holding the runtime knobs every layer reads:
the auto-``LIMIT`` row cap (Layer 1 / result filter), the serialized byte cap
(result filter), the ``statement_timeout`` (Layer 2), and the **read-only role's**
connection URL (Layer 3). Everything is overridable by environment variable and
has a documented default; loading never crashes when the optional values are unset.

A frozen dataclass (not Pydantic ``BaseSettings``) is deliberate here — the config
is a handful of scalars read from ``os.environ``, so a dataclass keeps the
dependency surface small and the loading rules explicit (spec §14 lets us pick
either). The redaction *loader* lives here; the *application* of redaction is Split 5.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Defaults, named so the tests and the README can cite them (spec §14).
DEFAULT_ROW_LIMIT = 1000
DEFAULT_BYTE_CAP = 256 * 1024  # 256 KB, measured on the serialized payload
DEFAULT_STATEMENT_TIMEOUT = "5s"
DEFAULT_AUDIT_PATH = "audit.jsonl"  # one JSONL line per tool call (spec §9)

_TRUTHY = {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, environ: dict[str, str]) -> int:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_bool(name: str, default: bool, environ: dict[str, str]) -> bool:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in _TRUTHY


def _env_str(name: str, default: str | None, environ: dict[str, str]) -> str | None:
    raw = environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw


@dataclass(frozen=True)
class Config:
    """Immutable runtime configuration. Build it with :meth:`from_env`."""

    #: The **read-only** role's connection URL (``QUERYGATE_DATABASE_URL``). NEVER the
    #: admin/migration URL — the whole Layer-3 story rests on the server only ever
    #: connecting with least privilege. ``None`` if unset (db calls will then error clearly).
    database_url: str | None = None

    #: Auto-``LIMIT`` default — appended by the guard when the query has none (Splits 3/5).
    row_limit: int = DEFAULT_ROW_LIMIT

    #: Serialized-payload cap in bytes; the result filter truncates past it (Split 5).
    byte_cap: int = DEFAULT_BYTE_CAP

    #: Layer-2 ``statement_timeout`` (a Postgres interval literal, e.g. ``"5s"``, ``"500ms"``).
    statement_timeout: str = DEFAULT_STATEMENT_TIMEOUT

    #: Path to ``redact.yaml``; ``None`` => redaction off (the default, spec §8).
    redact_path: str | None = None

    #: Path to the JSONL audit log; one line per tool call is appended here (spec §9).
    audit_path: str = DEFAULT_AUDIT_PATH

    #: Optional ``work_mem`` ceiling note for the role (spec §5). Set on the role in
    #: ``init_role.sql``; carried here for visibility/documentation, not applied per call.
    work_mem: str | None = None

    #: Global default for numeric serialization: ``Decimal -> float`` (False) or ``str``
    #: to preserve precision (spec §20). Per-column overrides are passed to ``serialize_rows``.
    decimal_as_str: bool = False

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Config":
        """Load config from the environment. Missing optional values fall back to defaults;
        this never raises for unset values (only for a genuinely malformed int)."""
        env = dict(os.environ if environ is None else environ)
        return cls(
            database_url=_env_str("QUERYGATE_DATABASE_URL", None, env),
            row_limit=_env_int("QUERYGATE_ROW_LIMIT", DEFAULT_ROW_LIMIT, env),
            byte_cap=_env_int("QUERYGATE_BYTE_CAP", DEFAULT_BYTE_CAP, env),
            statement_timeout=_env_str(
                "QUERYGATE_STATEMENT_TIMEOUT", DEFAULT_STATEMENT_TIMEOUT, env
            ),
            redact_path=_env_str("QUERYGATE_REDACT_PATH", None, env),
            audit_path=_env_str("QUERYGATE_AUDIT_PATH", DEFAULT_AUDIT_PATH, env)
            or DEFAULT_AUDIT_PATH,
            work_mem=_env_str("QUERYGATE_WORK_MEM", None, env),
            decimal_as_str=_env_bool("QUERYGATE_DECIMAL_AS_STR", False, env),
        )

    def require_database_url(self) -> str:
        """Return ``database_url`` or raise a clear error if it was never configured."""
        if not self.database_url:
            raise RuntimeError(
                "QUERYGATE_DATABASE_URL is not set — the server needs the read-only "
                "role's connection string (never the admin URL)."
            )
        return self.database_url

    def load_redactions(self) -> set[str]:
        """Convenience: load this config's redact file into a ``{table.column}`` set."""
        return load_redactions(self.redact_path)


def load_redactions(path: str | os.PathLike[str] | None) -> set[str]:
    """Load a ``redact.yaml`` into a set of ``"table.column"`` entries.

    Default **off** (spec §8): ``None`` or a missing/empty file => empty set. Accepts
    either a mapping ``{table: [col, ...]}`` or a flat list of ``"table.column"`` strings.
    The *application* of these (masking cells with ``"***"``) is Split 5; this is only the loader.
    """
    if not path:
        return set()
    p = Path(path)
    if not p.exists():
        return set()
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return set()

    import yaml  # local import: only needed when redaction is actually configured

    data = yaml.safe_load(text)
    if not data:
        return set()

    out: set[str] = set()
    if isinstance(data, dict):
        for table, cols in data.items():
            for col in cols or []:
                out.add(f"{table}.{col}")
    elif isinstance(data, list):
        for entry in data:
            out.add(str(entry))
    else:
        raise ValueError(
            f"redact file {p} must be a mapping {{table: [cols]}} or a list of 'table.column'"
        )
    return out
