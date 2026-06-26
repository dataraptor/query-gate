"""Result filter (spec §4, §6, §8) — *the model proposes the query; code disposes of the result.*

Three deterministic steps run on **every** row set before it leaves the process, so the agent's
context can never be flooded or poisoned by a result:

1. **Serialize** every Postgres cell to a JSON-safe scalar (``str | int | float | bool | None``)
   or a JSON-safe container of them. An unhandled type is the kind of bug that 500s a demo, so the
   rule is strict: every type the demo schema can produce is mapped explicitly, and **anything
   unmapped raises a typed error** (caught upstream → ``status: error``, spec §18) — we never
   silently ``str()`` an unknown object.
2. **Redact** configured ``table.column`` cells to ``"***"`` (the PHI mechanism, spec §8). Default
   **off**. Redaction hides a column from the *result*, not from ``WHERE``/aggregates (a
   ``count(*)`` over a redacted column still works) — a feature boundary, not a bug (spec §18).
3. **Byte-cap** the serialized payload (default 256 KB, spec §6): cut any single oversized cell
   and drop trailing rows until the payload fits — never stream a megabyte cell or 100k rows.

The row ``LIMIT``/``truncated`` (the guard, Split 3) and the byte cap/``truncated_bytes`` are
**independent** signals; both can fire. ``LIMIT`` caps rows *returned*, not work *done* — the
``statement_timeout`` (Layer 2) and this byte cap bound runtime/context (spec §5 ⚠).
"""

from __future__ import annotations

import datetime as _dt
import json
from decimal import Decimal
from typing import Any, Sequence

#: A JSON-safe scalar leaf. (Containers of these are also JSON-safe.)
JSONScalar = str | int | float | bool | None

#: The placeholder a redacted cell is replaced with (spec §8).
REDACTION_MASK = "***"

#: Appended to a cell cut by the byte cap so the truncation is honest, not silent.
_CUT_MARKER = "…[truncated]"


class SerializationError(TypeError):
    """A Postgres value of a type the serializer does not handle reached the filter.

    Raised instead of silently coercing — the caller maps it to ``status: error`` rather
    than letting a raw psycopg object escape into the agent's context (spec §18).
    """


def to_json_scalar(value: Any, *, decimal_as_str: bool = False) -> Any:
    """Map a single Postgres/psycopg value to a JSON-safe value.

    Containers (``jsonb`` objects, arrays) are serialized recursively. ``Decimal`` becomes
    ``float`` by default, or ``str`` (lossless) when ``decimal_as_str`` is set (spec §20).
    An unmapped type raises :class:`SerializationError`.
    """
    # None first.
    if value is None:
        return None

    # bool BEFORE int — bool is a subclass of int in Python, and we want a real JSON bool.
    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value

    if isinstance(value, float):
        return value

    if isinstance(value, str):
        return value

    # numeric / Decimal -> float (default) or str (precision-preserving, spec §20).
    if isinstance(value, Decimal):
        return str(value) if decimal_as_str else float(value)

    # date / time / timestamp / timestamptz -> ISO-8601 string (tz offset preserved).
    # datetime is a subclass of date, so it is covered by this branch too.
    if isinstance(value, (_dt.date, _dt.time)):
        return value.isoformat()

    # bytea -> "0x…" hex string.
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "0x" + bytes(value).hex()

    # jsonb / json comes back already parsed as dict; arrays as list/tuple. Serialize the
    # leaves so a Decimal/date nested inside a jsonb document can't slip through unmapped.
    if isinstance(value, dict):
        return {
            str(k): to_json_scalar(v, decimal_as_str=decimal_as_str) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [to_json_scalar(v, decimal_as_str=decimal_as_str) for v in value]

    raise SerializationError(
        f"cannot serialize value of type {type(value).__name__!r} to a JSON-safe value"
    )


def serialize_rows(
    rows: Sequence[Sequence[Any]],
    columns: Sequence[str] | None = None,
    *,
    decimal_as_str: bool = False,
    decimal_str_columns: Sequence[str] = (),
) -> list[list[Any]]:
    """Serialize a row-major result set to JSON-safe rows (spec §7).

    ``decimal_as_str`` flips the global numeric default; ``decimal_str_columns`` names
    individual columns (by name — requires ``columns``) whose ``Decimal`` values should be
    emitted as ``str`` to preserve precision (the per-column money switch, spec §20).
    The output is always ``json.dumps``-able.
    """
    str_cols = set(decimal_str_columns)
    out: list[list[Any]] = []
    for row in rows:
        serialized: list[Any] = []
        for i, cell in enumerate(row):
            col = columns[i] if columns is not None and i < len(columns) else None
            as_str = decimal_as_str or (col is not None and col in str_cols)
            serialized.append(to_json_scalar(cell, decimal_as_str=as_str))
        out.append(serialized)
    return out


# ==================================================================================================
# Redaction (spec §8) — mask configured columns in the RESULT only.
# ==================================================================================================


def columns_to_redact(
    columns: Sequence[str],
    redact_set: set[str] | frozenset[str],
    *,
    table: str | None = None,
) -> list[str]:
    """Decide which **output column names** to mask, given the ``{table.column}`` redact set.

    Two matching modes (spec §8 R2):

    * **Precise** (``table`` known — ``describe_table`` / ``search_text``): mask column ``c`` only
      when ``f"{table}.{c}"`` is configured.
    * **By column name** (``table is None`` — ``run_select``, whose projected columns can't be
      reliably mapped back to a base table in v1): mask column ``c`` when **any** configured entry
      names that column (``*.c``). This is the documented v1 simplification — it can over-mask a
      same-named column from a non-configured table, which is the safe direction for a PHI control.
    """
    if not redact_set:
        return []
    masked: list[str] = []
    for c in columns:
        if table is not None:
            if f"{table}.{c}" in redact_set:
                masked.append(c)
        elif any(entry.rsplit(".", 1)[-1] == c for entry in redact_set):
            masked.append(c)
    return masked


def apply_redaction(
    rows: list[list[Any]], columns: Sequence[str], masked_columns: Sequence[str]
) -> list[list[Any]]:
    """Replace every cell in ``masked_columns`` with :data:`REDACTION_MASK` (a new row list)."""
    mask_idx = {i for i, c in enumerate(columns) if c in set(masked_columns)}
    if not mask_idx:
        return rows
    return [
        [REDACTION_MASK if i in mask_idx else cell for i, cell in enumerate(row)] for row in rows
    ]


# ==================================================================================================
# Byte cap (spec §6, §18) — bound the serialized payload size.
# ==================================================================================================


def _compact_bytes(value: Any) -> int:
    """Serialized size of a JSON-safe value, in UTF-8 bytes (compact separators)."""
    return len(json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _cut_cell(cell: Any, budget_bytes: int) -> Any:
    """Cut one oversized cell down to ``budget_bytes`` (+ a visible marker), honestly.

    A non-string cell (``jsonb``/array → dict/list) is rendered to its JSON text first so the cut
    value is still a JSON-safe scalar the agent can read; the original structure is intentionally
    not preserved — the point is to not ship a megabyte cell whole.
    """
    text = cell if isinstance(cell, str) else json.dumps(cell, separators=(",", ":"))
    cut = text.encode("utf-8")[: max(budget_bytes, 0)].decode("utf-8", "ignore")
    return cut + _CUT_MARKER


def apply_byte_cap(rows: list[list[Any]], byte_cap: int) -> tuple[list[list[Any]], bool]:
    """Bound the serialized row payload to ``byte_cap`` bytes; return ``(rows, truncated_bytes)``.

    Two passes (spec §6, §18):

    1. **Oversized cell** — any single cell whose serialized form alone exceeds ``byte_cap`` is cut
       to **half** the cap (so the row containing it can still fit under the cap and be returned,
       rather than dropped whole). Sets ``truncated_bytes``.
    2. **Row-wise** — if the whole payload still exceeds ``byte_cap``, drop trailing rows until it
       fits. Sets ``truncated_bytes``.

    The returned rows always serialize to ``<= byte_cap`` bytes (the post-condition the tests assert).
    """
    if byte_cap <= 0 or not rows:
        return rows, False

    truncated_bytes = False
    cell_budget = byte_cap // 2

    # Pass 1 — cut oversized individual cells so no single cell can blow the budget.
    capped: list[list[Any]] = []
    for row in rows:
        new_row = list(row)
        for i, cell in enumerate(new_row):
            if _compact_bytes(cell) > byte_cap:
                new_row[i] = _cut_cell(cell, cell_budget)
                truncated_bytes = True
        capped.append(new_row)

    # Pass 2 — row-wise truncation if the payload still exceeds the cap.
    if _compact_bytes(capped) <= byte_cap:
        return capped, truncated_bytes

    kept: list[list[Any]] = []
    running = 2  # the enclosing "[]"
    for row in capped:
        row_bytes = _compact_bytes(row) + 1  # +1 for the separating comma
        if running + row_bytes > byte_cap:
            break
        kept.append(row)
        running += row_bytes
    return kept, True
