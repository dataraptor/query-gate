"""QueryGate — a read-only MCP server over Postgres.

The package is built incrementally across the documented splits. Split 01 lays the
foundation (synthetic DB schema, deterministic seed, least-privilege role); the
guard / transaction / server layers arrive in later splits.
"""

__version__ = "0.1.0"
