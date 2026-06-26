"""The tool surface — the read path as plain Python functions (spec §5, §6, §15).

This module is QueryGate's library API; Split 6 wraps each function in an ``@mcp.tool()``.

- :func:`run_select` — *the* product function. Runs the agent's SQL through the three-layer
  read-only boundary (Layer 1 guard → Layer 2 read-only txn → Layer 3 least-privilege role),
  serializes, redacts, and byte-caps the result, and returns a **cited** :class:`RunResult`.
- :func:`list_tables` — the discovery entry point (table names + row-count estimates).
- :func:`describe_table` — a table's columns/types/PK/FK + sample rows, **allowlist-guarded**.
- :func:`search_text` — fuzzy ``ILIKE`` lookup across text columns, **injection-closed at the
  source**: the ``table`` is validated against the live allowlist and the ``term`` is a bound
  parameter — an arbitrary identifier is never formatted into SQL.

**Every** outcome of **every** tool — ok, rejected, or errored — appends exactly one
:class:`AuditLine` to the audit log (spec §9), so the boundary is provable after the fact.

Truncation signals (spec §6). ``truncated`` is set when the row LIMIT was hit (the conservative
``row_count == config.row_limit`` v1 choice — see the note below). ``truncated_bytes`` is set when
the byte cap cut an oversized cell or dropped trailing rows. They are independent — both can fire.

Conservative row-LIMIT signal. ``truncated`` over-flags the rare case of a query whose true result
is exactly ``row_limit`` rows. For v1 this keeps the guard's injected ``LIMIT row_limit`` (Split 3)
and the citation SQL honest; the precise ``LIMIT row_limit + 1`` alternative is the road not taken
(recorded in PROGRESS.md).
"""

from __future__ import annotations

import time

from .audit import append_audit, now_rfc3339
from .config import Config
from .db import DBError, run_readonly
from .guard import guard_select
from .models import AuditLine, ColumnInfo, RunResult, TableInfo, TableSchema
from .result import (
    SerializationError,
    apply_byte_cap,
    apply_redaction,
    columns_to_redact,
    serialize_rows,
)

__all__ = [
    "run_select",
    "list_tables",
    "describe_table",
    "search_text",
    "RunRejected",
    "ToolRejected",
]

#: Postgres data types treated as text for ``search_text`` (the ``ILIKE`` targets).
_TEXT_TYPES = ("text", "character varying", "character")

#: How many rows ``describe_table`` returns as grounding context.
_SAMPLE_ROWS = 3


class RunRejected(Exception):
    """Layer 1 rejected the SQL — it never reached the database (spec §5).

    Carries the guard's machine ``rule`` tag and a legible ``reason`` so the caller (the MCP
    server, Split 6) can return a clean ``tool_result`` error and the agent can rephrase
    (spec §3 step 5: the agent sees the error, apologizes, does not retry the same SQL).
    """

    def __init__(self, rule: str, reason: str) -> None:
        super().__init__(reason)
        self.rule = rule
        self.reason = reason


class ToolRejected(Exception):
    """A discovery tool refused its input before touching the DB (spec §6).

    The headline case: ``describe_table`` / ``search_text`` got a ``table`` that is **not** in the
    live allowlist (``"pg_authid"``, ``"patients; DROP TABLE patients"``). No arbitrary identifier
    is ever formatted into SQL — the value is rejected at the source. Carries a machine ``rule`` +
    a legible ``reason``, mirroring :class:`RunRejected`.
    """

    def __init__(self, rule: str, reason: str) -> None:
        super().__init__(reason)
        self.rule = rule
        self.reason = reason


# ==================================================================================================
# Shared helpers.
# ==================================================================================================


def _resolve(config: Config | None) -> Config:
    return config if config is not None else Config.from_env()


def _audit(
    cfg: Config,
    tool: str,
    args: dict,
    *,
    status: str,
    row_count: int | None,
    error: str | None,
    redactions: list[str],
    started: float,
) -> None:
    """Append exactly one audit line for a tool call (spec §9)."""
    append_audit(
        AuditLine(
            ts=now_rfc3339(),
            tool=tool,
            args=args,
            row_count=row_count,
            latency_ms=round((time.monotonic() - started) * 1000),
            status=status,
            error=error,
            redactions=redactions,
        ),
        cfg.audit_path,
    )


def _allowed_tables(cfg: Config) -> set[str]:
    """The live table allowlist — the same ``app``-schema tables :func:`list_tables` reports.

    Sourced from ``pg_catalog`` (not a hard-coded list) so it tracks the real DB. This is the set
    ``describe_table`` / ``search_text`` validate a ``table`` argument against (spec §6).
    """
    sql = (
        "SELECT c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'app' AND c.relkind = 'r'"
    )
    _, rows = run_readonly(sql, config=cfg)
    return {r[0] for r in rows}


def _build_run_result(
    cfg: Config, columns: list[str], raw_rows: list, approved_sql: str, *, table: str | None
) -> tuple[RunResult, list[str]]:
    """Serialize → redact → byte-cap a raw row set into a cited :class:`RunResult`.

    Returns ``(result, redactions)``. ``table`` enables precise ``table.column`` redaction; pass
    ``None`` for ``run_select`` (redaction then matches by output column name — see
    :func:`querygate.result.columns_to_redact`).
    """
    rows = serialize_rows(raw_rows, columns, decimal_as_str=cfg.decimal_as_str)

    redact_set = cfg.load_redactions()
    masked = columns_to_redact(columns, redact_set, table=table)
    rows = apply_redaction(rows, columns, masked)

    db_row_count = len(rows)  # rows the DB returned (post row-LIMIT, pre byte-cap)
    rows, truncated_bytes = apply_byte_cap(rows, cfg.byte_cap)

    result = RunResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        sql=approved_sql,  # the EXACT SQL executed — the honest citation source.
        truncated=db_row_count == cfg.row_limit,  # conservative cap signal (see module docstring).
        truncated_bytes=truncated_bytes,
        redactions=sorted(masked),
        elapsed_ms=0,  # filled in by the caller (it owns the timing window)
    )
    return result, sorted(masked)


# ==================================================================================================
# run_select — the three-layer boundary + the result filter.
# ==================================================================================================


def run_select(sql: str, *, config: Config | None = None) -> RunResult:
    """Run one read-only ``SELECT`` through all three boundary layers and cite the result.

    Returns a :class:`RunResult` on success. Raises :class:`RunRejected` if the guard rejects the
    SQL (Layer 1; the SQL is never sent to the DB), or :class:`~querygate.db.DBError` /
    :class:`~querygate.db.QueryTimeout` / :class:`~querygate.result.SerializationError` on a
    DB/serialization failure. **Every** outcome writes exactly one audit line first; this function
    never crashes the process — it raises a typed error the caller maps to a tool error.
    """
    cfg = _resolve(config)
    args = {"sql": sql}
    started = time.monotonic()

    # --- Layer 1: the SQL guard. On reject, the SQL is NOT sent to the DB. ---------------------
    decision = guard_select(sql, row_limit=cfg.row_limit)
    if not decision.ok:
        _audit(
            cfg, "run_select", args, status="rejected", row_count=None,
            error=decision.reason, redactions=[], started=started,
        )
        raise RunRejected(decision.rule or "rejected", decision.reason or "rejected by guard")

    approved_sql = decision.sql or sql

    # --- Layers 2 + 3: read-only transaction as the least-privilege role, then the filter. -----
    exec_start = time.monotonic()
    try:
        columns, raw_rows = run_readonly(approved_sql, config=cfg)
        result, redactions = _build_run_result(
            cfg, columns, raw_rows, approved_sql, table=None
        )
    except (DBError, SerializationError) as exc:
        _audit(
            cfg, "run_select", args, status="error", row_count=None,
            error=str(exc), redactions=[], started=started,
        )
        raise
    result.elapsed_ms = round((time.monotonic() - exec_start) * 1000)

    _audit(
        cfg, "run_select", args, status="ok", row_count=result.row_count,
        error=None, redactions=redactions, started=started,
    )
    return result


# ==================================================================================================
# list_tables — discovery.
# ==================================================================================================


def list_tables(*, config: Config | None = None) -> list[TableInfo]:
    """List the ``app``-schema tables + row-count estimates (spec §6). Call this first.

    The estimate is ``pg_class.reltuples`` (the planner's, populated by ``ANALYZE``) — approximate
    by design. Writes one ``ok``/``error`` audit line.
    """
    cfg = _resolve(config)
    started = time.monotonic()
    sql = (
        "SELECT c.relname, c.reltuples::bigint AS est_rows FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'app' AND c.relkind = 'r' ORDER BY c.relname"
    )
    try:
        _, rows = run_readonly(sql, config=cfg)
    except DBError as exc:
        _audit(
            cfg, "list_tables", {}, status="error", row_count=None,
            error=str(exc), redactions=[], started=started,
        )
        raise
    tables = [TableInfo(table=r[0], est_rows=int(r[1])) for r in rows]
    _audit(
        cfg, "list_tables", {}, status="ok", row_count=len(tables),
        error=None, redactions=[], started=started,
    )
    return tables


# ==================================================================================================
# describe_table — allowlist-guarded schema introspection.
# ==================================================================================================


def describe_table(table: str, *, config: Config | None = None) -> TableSchema:
    """Return a table's columns/types/PK/FK + a few post-redaction sample rows (spec §6, §7).

    ``table`` is validated against the live allowlist **before** any ``information_schema`` query;
    anything else (``"pg_authid"``, ``"patients; DROP …"``) raises :class:`ToolRejected` and is
    audited ``rejected`` — the value is never formatted into SQL. Writes one audit line.
    """
    cfg = _resolve(config)
    args = {"table": table}
    started = time.monotonic()

    allowed = _allowed_tables(cfg)
    if table not in allowed:
        reason = f"table {table!r} is not in the allowlist {sorted(allowed)}"
        _audit(
            cfg, "describe_table", args, status="rejected", row_count=None,
            error=reason, redactions=[], started=started,
        )
        raise ToolRejected("table_not_allowed", reason)

    try:
        # `table` is now a proven-safe whitelisted identifier; still passed as a bound parameter
        # to every information_schema query below (no string interpolation of the identifier).
        col_rows = run_readonly(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = 'app' AND table_name = %s ORDER BY ordinal_position",
            [table],
            config=cfg,
        )[1]
        # PK / FK come from pg_catalog (not information_schema): the catalog is readable by all,
        # whereas information_schema.*_constraints is privilege-filtered and hides constraints from
        # a SELECT-only role. All app FKs are single-column, so ANY(conkey)/ANY(confkey) pairs 1:1.
        pk_rows = run_readonly(
            "SELECT a.attname FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = ANY(i.indkey) "
            "WHERE n.nspname = 'app' AND c.relname = %s AND i.indisprimary",
            [table],
            config=cfg,
        )[1]
        fk_rows = run_readonly(
            "SELECT att.attname, cl2.relname, att2.attname FROM pg_constraint con "
            "JOIN pg_class cl ON cl.oid = con.conrelid "
            "JOIN pg_namespace n ON n.oid = cl.relnamespace "
            "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = ANY(con.conkey) "
            "JOIN pg_class cl2 ON cl2.oid = con.confrelid "
            "JOIN pg_attribute att2 ON att2.attrelid = con.confrelid "
            "  AND att2.attnum = ANY(con.confkey) "
            "WHERE n.nspname = 'app' AND cl.relname = %s AND con.contype = 'f'",
            [table],
            config=cfg,
        )[1]
        # sample rows from the (whitelisted) table; serialized + redaction-applied.
        sample_cols, sample_raw = run_readonly(
            f"SELECT * FROM app.{table} LIMIT {_SAMPLE_ROWS}", config=cfg
        )
    except DBError as exc:
        _audit(
            cfg, "describe_table", args, status="error", row_count=None,
            error=str(exc), redactions=[], started=started,
        )
        raise

    pk = {r[0] for r in pk_rows}
    fk = {r[0]: f"{r[1]}.{r[2]}" for r in fk_rows}
    columns = [
        ColumnInfo(
            name=name,
            type=dtype,
            nullable=(is_nullable == "YES"),
            is_pk=name in pk,
            references=fk.get(name),
        )
        for name, dtype, is_nullable in col_rows
    ]

    sample_rows = serialize_rows(sample_raw, sample_cols, decimal_as_str=cfg.decimal_as_str)
    redact_set = cfg.load_redactions()
    masked = columns_to_redact(sample_cols, redact_set, table=table)
    sample_rows = apply_redaction(sample_rows, sample_cols, masked)

    schema = TableSchema(table=table, columns=columns, sample_rows=sample_rows)
    _audit(
        cfg, "describe_table", args, status="ok", row_count=len(sample_rows),
        error=None, redactions=sorted(masked), started=started,
    )
    return schema


# ==================================================================================================
# search_text — fuzzy lookup, injection-closed at the source.
# ==================================================================================================


def _text_columns(cfg: Config, tables: list[str]) -> dict[str, list[str]]:
    """Map each table to its text columns (the ``ILIKE`` targets), from ``information_schema``."""
    out: dict[str, list[str]] = {t: [] for t in tables}
    if not tables:
        return out
    rows = run_readonly(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema = 'app' AND table_name = ANY(%s) AND data_type = ANY(%s) "
        "ORDER BY table_name, ordinal_position",
        [tables, list(_TEXT_TYPES)],
        config=cfg,
    )[1]
    for tbl, col in rows:
        out.setdefault(tbl, []).append(col)
    return out


def search_text(term: str, table: str | None = None, *, config: Config | None = None) -> RunResult:
    """Fuzzy-search text columns for ``term`` (``ILIKE '%term%'``), spec §6.

    Returns a :class:`RunResult` whose rows are uniform ``(source_table, source_column,
    match_value)`` triples — so one shape covers both a single ``table`` and (``table=None``) a
    sweep across every allowlisted table. Answers the "name spelled differently" case
    (search ``"Sara"`` → the real ``"Sarah Lee"``).

    Two injection defenses, closed at the source (spec §6, §18):

    * **``table``** is validated against the live allowlist; anything else raises
      :class:`ToolRejected` — no arbitrary identifier is ever formatted into SQL. The per-table /
      per-column identifiers woven into the generated SQL come from the allowlist +
      ``information_schema``, never from the caller.
    * **``term``** is a **bound parameter** (``ILIKE %s``), never concatenated — immune to SQL
      metacharacters.

    The generated SQL still runs through Layers 2 + 3, and the result passes the full filter
    (redaction + byte cap). Writes one audit line.
    """
    cfg = _resolve(config)
    args = {"term": term, "table": table}
    started = time.monotonic()

    allowed = _allowed_tables(cfg)
    targets = [table] if table is not None else sorted(allowed)
    for t in targets:
        if t not in allowed:
            reason = f"table {t!r} is not in the allowlist {sorted(allowed)}"
            _audit(
                cfg, "search_text", args, status="rejected", row_count=None,
                error=reason, redactions=[], started=started,
            )
            raise ToolRejected("table_not_allowed", reason)

    try:
        text_cols = _text_columns(cfg, targets)
        # Build one UNION ALL of (literal table, literal column, value) per text column. The table
        # and column literals are whitelisted identifiers emitted as SQL string LITERALS (quoted),
        # not interpolated identifiers; `term` is the only datum and it is a bound parameter.
        parts: list[str] = []
        params: list[str] = []
        pattern = f"%{term}%"
        for t in targets:
            for col in text_cols.get(t, []):
                parts.append(
                    f"SELECT '{t}' AS source_table, '{col}' AS source_column, "
                    f"{col}::text AS match_value FROM app.{t} WHERE {col} ILIKE %s"
                )
                params.append(pattern)

        if not parts:
            # No text columns in the target(s): a valid, well-shaped empty result.
            sql = (
                "SELECT NULL::text AS source_table, NULL::text AS source_column, "
                "NULL::text AS match_value WHERE false"
            )
            params = []
        else:
            sql = " UNION ALL ".join(parts) + f" LIMIT {cfg.row_limit}"

        columns, raw_rows = run_readonly(sql, params, config=cfg)
    except DBError as exc:
        _audit(
            cfg, "search_text", args, status="error", row_count=None,
            error=str(exc), redactions=[], started=started,
        )
        raise

    rows = serialize_rows(raw_rows, columns, decimal_as_str=cfg.decimal_as_str)

    # Per-row redaction: this result's masked column is the *value* of `source_column`, so we mask
    # by each row's (source_table, source_column) pair — precise, since each row names its origin.
    redact_set = cfg.load_redactions()
    masked_pairs: set[str] = set()
    if redact_set:
        st_i, sc_i, mv_i = (
            columns.index("source_table"),
            columns.index("source_column"),
            columns.index("match_value"),
        )
        for row in rows:
            pair = f"{row[st_i]}.{row[sc_i]}"
            if pair in redact_set:
                row[mv_i] = "***"
                masked_pairs.add(pair)

    db_row_count = len(rows)
    rows, truncated_bytes = apply_byte_cap(rows, cfg.byte_cap)

    result = RunResult(
        columns=columns,
        rows=rows,
        row_count=len(rows),
        sql=sql,
        truncated=db_row_count == cfg.row_limit,
        truncated_bytes=truncated_bytes,
        redactions=sorted(masked_pairs),
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )
    _audit(
        cfg, "search_text", args, status="ok", row_count=result.row_count,
        error=None, redactions=sorted(masked_pairs), started=started,
    )
    return result
