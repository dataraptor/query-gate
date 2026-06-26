"""QueryGate — a read-only MCP server over Postgres.

The package is built incrementally across the documented splits. The public library surface
(spec §15) is the read path as plain functions — ``run_select`` (importing it *is* the three-layer
read-only boundary: guard → read-only transaction → least-privilege role) plus the discovery tools
``list_tables`` / ``describe_table`` / ``search_text``, all behind the deterministic result filter
(serialize → redact → byte cap). The MCP server wrapping these arrives in Split 6.
"""

from .config import Config
from .models import AuditLine, ColumnInfo, RunResult, TableInfo, TableSchema
from .tools import (
    RunRejected,
    ToolRejected,
    describe_table,
    list_tables,
    run_select,
    search_text,
)

__version__ = "0.1.0"

__all__ = [
    "run_select",
    "list_tables",
    "describe_table",
    "search_text",
    "RunRejected",
    "ToolRejected",
    "RunResult",
    "AuditLine",
    "TableInfo",
    "ColumnInfo",
    "TableSchema",
    "Config",
    "__version__",
]
