"""Result filter — **serializer portion only** (spec §7).

Every Postgres cell must become a JSON-safe scalar (``str | int | float | bool | None``)
or a JSON-safe container of them before it can reach the agent. An unhandled type is the
kind of bug that 500s a demo, so the rule is strict: every type the demo schema can produce
is mapped explicitly, and **anything unmapped raises a typed error** (caught upstream and
turned into ``status: error``, spec §18) — we never silently ``str()`` an unknown object.

Scope note: this file holds ONLY the type serializer. The auto-``LIMIT`` enforcement, the
byte cap, and redaction (the rest of the "result filter") arrive in Split 5.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Any, Sequence

#: A JSON-safe scalar leaf. (Containers of these are also JSON-safe.)
JSONScalar = str | int | float | bool | None


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
