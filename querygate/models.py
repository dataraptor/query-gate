"""Canonical data contracts for QueryGate (spec §7).

These Pydantic models are the **stable shapes** every layer above the boundary binds to:
``run_select`` returns a :class:`RunResult`; every tool call appends one :class:`AuditLine`
to the audit log. The field names are a cross-stack contract — the MCP tool schema (Split 6)
and the web UI (Splits 11–12) bind to them verbatim, so they match spec §7 exactly. Do not
rename a field without changing the spec.

Pydantic (not a bare dataclass) is deliberate: the ``mcp`` SDK derives the tool JSON schema
from these models, and ``model_validate_json`` gives the audit log a free round-trip check.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .result import JSONScalar

__all__ = [
    "JSONScalar",
    "RunResult",
    "AuditLine",
    "TableInfo",
    "ColumnInfo",
    "TableSchema",
]


class RunResult(BaseModel):
    """The envelope returned by ``run_select`` (and, in Split 5, ``search_text``).

    Carries the executed SQL and the returned row count so the agent can *cite* every answer
    (spec §9): "142 patients are overdue — from ``<sql>`` (142 rows)."
    """

    #: Column names, in order.
    columns: list[str]
    #: Row-major result; every cell is JSON-safe (the Split-2 serializer guarantees this).
    rows: list[list[JSONScalar]]
    #: Rows RETURNED (post-LIMIT) — the number the agent cites.
    row_count: int
    #: The EXACT SQL executed (post auto-LIMIT injection) — the honest citation source.
    sql: str
    #: ``True`` if the row LIMIT was hit (see ``run_select`` for the conservative signal).
    truncated: bool
    #: ``True`` if the byte cap forced an early cut (oversized cell and/or trailing rows dropped).
    truncated_bytes: bool = False
    #: Columns masked by redaction in this result, if any (spec §8); ``[]`` when redaction is off.
    redactions: list[str] = Field(default_factory=list)
    #: Wall-clock of the DB execution + serialization, in milliseconds.
    elapsed_ms: int


class TableInfo(BaseModel):
    """One entry returned by ``list_tables`` (spec §6): a table name + a row-count estimate.

    ``est_rows`` is the planner's estimate from ``pg_class.reltuples`` (populated by the Split-01
    ``ANALYZE``) — approximate by design, not an exact ``count(*)``.
    """

    table: str
    est_rows: int


class ColumnInfo(BaseModel):
    """One column in a :class:`TableSchema` (spec §7)."""

    name: str
    type: str
    nullable: bool
    is_pk: bool = False
    #: FK target as ``"<table>.<column>"`` (e.g. ``"providers.provider_id"``); ``None`` if not a FK.
    references: str | None = None


class TableSchema(BaseModel):
    """The shape ``describe_table`` returns (spec §7): columns + a few post-redaction sample rows."""

    table: str
    columns: list[ColumnInfo]
    #: A few real rows, serialized and redaction-applied — grounding context for the agent.
    sample_rows: list[list[JSONScalar]]


class AuditLine(BaseModel):
    """One JSONL line per tool call, appended to the audit log (spec §7, §9).

    A **rejected write is a first-class audit event** (``status="rejected"``) — the log is
    where the boundary is proven to have held, after the fact.
    """

    #: RFC3339 timestamp from the runtime — NEVER hard-coded (spec §9, §11).
    ts: str
    #: The tool name (``"run_select"``, later ``"search_text"`` etc.).
    tool: str
    #: The call args (e.g. ``{"sql": "..."}``). ⚠️ may contain literal values from the
    #: question — harmless on synthetic data, a PHI consideration on real data (spec §9, §21).
    args: dict
    #: Rows returned on success; ``None`` for a rejected/errored call.
    row_count: int | None
    #: Wall-clock of the whole tool call, in milliseconds.
    latency_ms: int
    #: Outcome class.
    status: Literal["ok", "rejected", "error"]
    #: The guard reason (rejected) or the DB/serializer error message (error); ``None`` on ok.
    error: str | None = None
    #: Columns masked in this result, if any. Populated in Split 5; ``[]`` here.
    redactions: list[str] = Field(default_factory=list)
