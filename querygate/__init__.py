"""QueryGate — a read-only MCP server over Postgres.

The package is built incrementally across the documented splits. The public library surface
(spec §15) is ``run_select`` — importing it *is* the three-layer read-only boundary (guard →
read-only transaction → least-privilege role). The remaining tools (``list_tables`` /
``describe_table`` / ``search_text``) and the MCP server arrive in later splits.
"""

from .config import Config
from .models import AuditLine, RunResult
from .tools import RunRejected, run_select

__version__ = "0.1.0"

__all__ = ["run_select", "RunRejected", "RunResult", "AuditLine", "Config", "__version__"]
