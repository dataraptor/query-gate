"""Split 02 — pure serializer tests (no DB, no API key).

The guarantee under test (spec §7, §18): every Postgres type the demo schema can produce
maps to a JSON-safe value, an unmapped type fails *typed* (never a bare ``str()``, never a
crash), and the output of ``serialize_rows`` is always ``json.dumps``-able.
"""

from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest

from querygate.result import SerializationError, serialize_rows, to_json_scalar


# S1 — Decimal -> float by default; -> str with the precision switch.
def test_s1_decimal_default_float():
    out = to_json_scalar(Decimal("12.34"))
    assert out == 12.34
    assert isinstance(out, float)


def test_s1_decimal_str_switch():
    out = to_json_scalar(Decimal("12.34"), decimal_as_str=True)
    assert out == "12.34"
    assert isinstance(out, str)


def test_s1_decimal_str_preserves_precision():
    # The whole point of the str switch: a value float() would round trips losslessly.
    big = Decimal("12345678901.99")
    assert to_json_scalar(big, decimal_as_str=True) == "12345678901.99"


# S2 — date -> ISO date string; tz-aware datetime -> ISO-8601 with offset.
def test_s2_date_iso():
    assert to_json_scalar(dt.date(2021, 1, 2)) == "2021-01-02"


def test_s2_datetime_tz_iso_with_offset():
    value = dt.datetime(2021, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc)
    out = to_json_scalar(value)
    assert out == "2021-01-02T03:04:05+00:00"


def test_s2_naive_datetime_iso():
    value = dt.datetime(2021, 1, 2, 3, 4, 5)
    assert to_json_scalar(value) == "2021-01-02T03:04:05"


# S3 — jsonb (dict/list) passes through and stays JSON-serializable.
def test_s3_jsonb_dict_passthrough():
    doc = {"a": 1, "b": ["x", "y"], "c": None}
    out = to_json_scalar(doc)
    assert out == doc
    json.dumps(out)  # must not raise


def test_s3_jsonb_nested_decimal_is_serialized():
    # A Decimal nested inside a jsonb document must not slip through unmapped.
    out = to_json_scalar({"amount": Decimal("9.99")})
    assert out == {"amount": 9.99}
    json.dumps(out)


# S4 — Postgres array -> list, recursively serialized.
def test_s4_array_to_list():
    out = to_json_scalar([1, 2, 3])
    assert out == [1, 2, 3]
    assert isinstance(out, list)


def test_s4_array_recursive():
    out = to_json_scalar([Decimal("1.5"), dt.date(2020, 5, 1)])
    assert out == [1.5, "2020-05-01"]


# S5 — bytea -> "0x…" hex.
def test_s5_bytea_hex():
    assert to_json_scalar(bytes(b"\x00\xff")) == "0x00ff"


def test_s5_bytearray_and_memoryview_hex():
    assert to_json_scalar(bytearray(b"\x00\xff")) == "0x00ff"
    assert to_json_scalar(memoryview(b"\x00\xff")) == "0x00ff"


# S6 — bool / None / int / str round-trip to themselves (and bool stays bool, not int).
@pytest.mark.parametrize("value", [True, False, None, 0, 42, -7, "", "hello"])
def test_s6_scalar_roundtrip(value):
    out = to_json_scalar(value)
    assert out == value
    assert type(out) is type(value)


def test_s6_bool_is_not_int():
    # bool is a subclass of int; ensure True doesn't become 1.
    assert to_json_scalar(True) is True
    assert type(to_json_scalar(True)) is bool


# S7 — an unmapped type raises the typed error, NOT a bare str(), NOT a crash.
def test_s7_unmapped_type_raises_typed():
    class Custom:
        pass

    with pytest.raises(SerializationError):
        to_json_scalar(Custom())


def test_s7_serialize_rows_propagates_typed_error():
    class Custom:
        pass

    with pytest.raises(SerializationError):
        serialize_rows([[Custom()]])


# S8 — json.dumps(serialize_rows(rows)) succeeds for a mixed row with every type above.
def test_s8_mixed_row_is_json_encodable():
    columns = ["i", "b", "n", "d", "ts", "amount", "arr", "doc", "blob"]
    rows = [
        [
            42,
            True,
            None,
            dt.date(2021, 1, 2),
            dt.datetime(2021, 1, 2, 3, 4, 5, tzinfo=dt.timezone.utc),
            Decimal("12.34"),
            [1, 2, 3],
            {"k": "v"},
            bytes(b"\x00\xff"),
        ]
    ]
    out = serialize_rows(rows, columns)
    encoded = json.dumps(out)  # the real guarantee — must not raise
    assert json.loads(encoded) == [
        [
            42,
            True,
            None,
            "2021-01-02",
            "2021-01-02T03:04:05+00:00",
            12.34,
            [1, 2, 3],
            {"k": "v"},
            "0x00ff",
        ]
    ]


def test_s8_per_column_decimal_str_switch():
    # money column emitted as str, a different numeric column left as float.
    columns = ["amount", "score"]
    rows = [[Decimal("100.00"), Decimal("3.5")]]
    out = serialize_rows(rows, columns, decimal_str_columns=["amount"])
    assert out == [["100.00", 3.5]]
    json.dumps(out)


def test_s8_global_decimal_str_switch():
    rows = [[Decimal("1.10"), Decimal("2.20")]]
    out = serialize_rows(rows, ["a", "b"], decimal_as_str=True)
    assert out == [["1.10", "2.20"]]
