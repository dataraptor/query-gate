"""Layer 2 — the read-only transaction wrapper (spec §5, §4).

Every query the server runs goes through :func:`run_readonly`, which executes the
equivalent of::

    BEGIN TRANSACTION READ ONLY;
    SET LOCAL statement_timeout = '5s';   -- from config; SET LOCAL so it cannot leak
    <the query>;
    COMMIT;

Postgres itself rejects any write attempt inside a ``READ ONLY`` transaction — including
writes that don't look like writes (``SELECT nextval('seq')``) — and ``statement_timeout``
is the **real runtime guard** against a pathological query (a ``LIMIT`` caps rows returned,
not work done). This is the middle layer of defense in depth.

Design notes:
- **Stateless per call** (spec §4): each call opens its own connection, runs in one
  transaction, and closes. No session state is shared between callers, so the HTTP transport
  and the parallel eval are safe, and ``SET LOCAL`` provably cannot leak across calls.
- **One statement per execute.** The query is sent through psycopg's extended protocol, which
  carries a single command — a ``;``-chained payload fails at the driver, a free fourth
  tripwire (spec §4). We never use multi-statement execution for the caller's SQL.
- **Parameters are bound** via psycopg's ``%s`` placeholders, never string-formatted.

This layer is "raw rows in, raw rows out": it knows nothing about LIMIT, redaction, or the
byte cap (those are the result filter, Splits 3/5). It only turns DB errors into typed errors.
"""

from __future__ import annotations

from typing import Any, Sequence

import psycopg

from .config import Config

#: Postgres SQLSTATE raised when ``statement_timeout`` cancels a query.
_QUERY_CANCELED = "57014"


class DBError(Exception):
    """A database error from a read-only query, wrapped for the caller to map to
    ``status: error`` (spec §18). Carries the originating SQLSTATE when available."""

    def __init__(self, message: str, *, sqlstate: str | None = None) -> None:
        super().__init__(message)
        self.sqlstate = sqlstate


class QueryTimeout(DBError):
    """Raised when ``statement_timeout`` (Layer 2's runtime guard) cancels the query."""


def run_readonly(
    sql: str,
    params: Sequence[Any] | None = None,
    *,
    config: Config | None = None,
    statement_timeout: str | None = None,
) -> tuple[list[str], list[tuple[Any, ...]]]:
    """Run one statement inside a ``READ ONLY`` transaction with a ``statement_timeout``.

    Returns ``(columns, rows)`` — column names in order and the raw psycopg rows
    (cells are turned JSON-safe by :func:`querygate.result.serialize_rows`, not here).
    Raises :class:`QueryTimeout` if the timeout fires and :class:`DBError` for any other
    database error; never swallows an error and never crashes the process.

    ``statement_timeout`` overrides the config value (used by tests to inject a short bound).
    """
    cfg = config if config is not None else Config.from_env()
    timeout = statement_timeout if statement_timeout is not None else cfg.statement_timeout
    url = cfg.require_database_url()

    try:
        # autocommit=True so WE control the transaction boundaries explicitly and the executed
        # SQL matches the spec's BEGIN READ ONLY / SET LOCAL / COMMIT exactly.
        with psycopg.connect(url, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("BEGIN TRANSACTION READ ONLY")
                # SET LOCAL (transaction-scoped) via set_config(..., is_local=True) so the
                # timeout is bound as a parameter (no string interpolation) and cannot leak
                # past COMMIT. Equivalent to: SET LOCAL statement_timeout = '<timeout>'.
                cur.execute("SELECT set_config('statement_timeout', %s, true)", (timeout,))
                # The caller's SQL: one statement, parameters bound — never formatted in.
                # prepare=True forces psycopg's *extended* protocol, under which a single
                # Execute carries exactly one command — so a ';'-chained payload fails at the
                # driver ("cannot insert multiple commands into a prepared statement") even
                # before the guard. (Parameterless execute would otherwise fall back to the
                # simple protocol, which DOES allow multiple statements.) This is the spec's
                # free fourth tripwire (§4) made real.
                cur.execute(sql, params, prepare=True)
                if cur.description is not None:
                    columns = [col.name for col in cur.description]
                    rows = cur.fetchall()
                else:
                    columns, rows = [], []
                cur.execute("COMMIT")
        return columns, rows
    except psycopg.Error as exc:
        sqlstate = getattr(exc, "sqlstate", None)
        message = str(exc).strip() or exc.__class__.__name__
        if sqlstate == _QUERY_CANCELED:
            raise QueryTimeout(message, sqlstate=sqlstate) from exc
        raise DBError(message, sqlstate=sqlstate) from exc
