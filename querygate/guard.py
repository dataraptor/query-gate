"""querygate/guard.py — Layer 1 of the read-only boundary: the SQL guard (spec §5, Appendix B).

A **pure function over a string**. No database, no network, no LLM — `guard_select(sql)`
takes a SQL string and returns a :class:`GuardResult` that is either *accepted* (with the
auto-``LIMIT`` injected) or a *structured rejection* naming the rule that fired. This is the
fast, legible first line of defense; Layers 2 (read-only transaction) and 3 (least-privilege
role) are the load-bearing backstops that hold even if this layer has a bug.

The load-bearing subtlety (the v1 hole this closes): the guard walks the **entire parsed
AST**, not just the statement root. A *data-modifying CTE* —
``WITH x AS (DELETE FROM patients RETURNING *) SELECT * FROM x`` — parses as a top-level
``SELECT`` with a ``DELETE`` nested inside the CTE. A root-only check passes it; the whole-AST
walk rejects it.

Design rules (spec §5):
- **Parse with sqlglot, dialect ``postgres``.** Exactly one statement, or reject.
- **Fail closed.** Anything sqlglot cannot parse — or only "understands" as a raw fallback
  ``Command`` (e.g. ``REVOKE``, ``VACUUM``, ``CALL``, ``DO``) — is **rejected**, never passed
  through. "Parsed and proven, or rejected."
- **Reject any DML/DDL node anywhere** in the tree, ``SELECT … INTO``, ``… FOR UPDATE/SHARE``,
  and any **denylisted function** call.
- **Auto-``LIMIT``** the *outermost* query when it has none; never override an existing LIMIT
  and never touch a subquery/CTE's LIMIT.

⚠️ ``LIMIT`` caps the rows *returned*, not the work *done*. ``SELECT count(*) FROM huge CROSS
JOIN huge`` returns one row but can run for minutes — Layer 2's ``statement_timeout`` bounds
runtime and the Split-5 byte cap bounds context. The guard makes no runtime/memory promise.

The guard never imports or touches the DB. ``row_limit`` is taken as a **parameter** (default
mirrors Split-02 ``config.row_limit`` = 1000) so the guard stays DB-free.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import sqlglot
from sqlglot import exp

# The denylisted-function detection below relies on the dangerous Postgres functions parsing
# to ``exp.Anonymous`` (they are non-standard, so sqlglot has no dedicated class for them).
# sqlglot logs a WARNING when it falls back to a raw ``Command`` for syntax it cannot model
# (e.g. ``REVOKE``); we *reject* those, so the warning is just noise — quiet it.
logging.getLogger("sqlglot").setLevel(logging.ERROR)

#: Auto-``LIMIT`` default. Mirrors Split-02 ``config.DEFAULT_ROW_LIMIT``; the caller normally
#: passes ``config.row_limit`` explicitly. Kept here as a literal so the guard imports no DB code.
DEFAULT_ROW_LIMIT = 1000

#: DML/DDL expression classes — reject if ANY node anywhere in the tree is one of these.
#: ``REVOKE`` has no dedicated sqlglot class (it parses to ``exp.Command``); it is caught by the
#: fail-closed ``Command`` check below, so it does not need an entry here.
_FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Alter,
    exp.Create,
    exp.TruncateTable,
    exp.Grant,
    exp.Copy,
)

#: Dangerous-function denylist (spec §5, Appendix B). File/network/DoS reach a bare SELECT could
#: still attempt, plus the sequence writes (``nextval``/``setval``). Match is on the function name,
#: case-insensitively. Most are superuser-only and already unreachable by the Layer-3 role — the
#: denylist makes the rejection **legible and testable**. Exported so the test can import it.
DENYLISTED_FUNCTIONS: frozenset[str] = frozenset(
    {
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_stat_file",
        "lo_import",
        "lo_export",
        "lo_get",
        "lo_put",
        "copy_to",
        "copy_from",
        "dblink",
        "dblink_exec",
        "dblink_connect",
        "dblink_connect_u",
        "dblink_open",
        "dblink_send_query",
        "dblink_get_result",
        "pg_sleep",
        "pg_sleep_for",
        "set_config",
        "pg_terminate_backend",
        "pg_cancel_backend",
        "pg_logical_emit_message",
        "pg_ls_waldir",
        "pg_ls_logdir",
        "pg_ls_tmpdir",
        "pg_ls_archive_statusdir",
        "query_to_xml",
        "database_to_xml",
        "schema_to_xml",
        "table_to_xml",
        "xpath",
        "nextval",
        "setval",
    }
)

#: Root expression classes the guard accepts as a top-level read query. ``WITH … SELECT`` parses
#: with the ``SELECT`` as the root (carrying the CTEs in its ``with`` arg), so it lands here too.
#: Built from whatever the pinned sqlglot exposes, so a version that renames a set-op class degrades
#: to a rejection rather than an import error.
_QUERY_ROOTS: tuple[type[exp.Expression], ...] = tuple(
    c
    for c in (
        getattr(exp, "Select", None),
        getattr(exp, "Union", None),
        getattr(exp, "Intersect", None),
        getattr(exp, "Except", None),
    )
    if c is not None
)


@dataclass(frozen=True)
class GuardResult:
    """The guard's decision.

    - ``ok=True``  → ``sql`` is the accepted, (possibly LIMIT-injected) SQL to execute.
    - ``ok=False`` → ``reason`` is a legible message and ``rule`` a machine tag for the UI's
      Boundary panel / the agent's rephrase logic (e.g. ``"dml_in_ast"``, ``"multi_statement"``,
      ``"denylisted_function"``, ``"select_into"``, ``"for_update"``, ``"parse_error"``,
      ``"unsupported_statement"``, ``"not_a_select"``).
    """

    ok: bool
    sql: str | None = None
    reason: str | None = None
    rule: str | None = None

    @classmethod
    def reject(cls, rule: str, reason: str) -> "GuardResult":
        return cls(ok=False, sql=None, reason=reason, rule=rule)

    @classmethod
    def accept(cls, sql: str) -> "GuardResult":
        return cls(ok=True, sql=sql, reason=None, rule=None)


def _func_name(node: exp.Expression) -> str | None:
    """Return the lowercased function name for a call node, else ``None``.

    Denylisted functions parse to :class:`exp.Anonymous` (no dedicated class), where ``.name`` is
    the function name. For any other :class:`exp.Func` we still extract its canonical SQL name so a
    denylisted name that *did* get a dedicated class in some sqlglot version is not missed.
    """
    if isinstance(node, exp.Anonymous):
        return (node.name or "").lower() or None
    if isinstance(node, exp.Func):
        try:
            name = node.sql_name()
        except Exception:  # pragma: no cover - defensive; sql_name is stable in pinned sqlglot
            name = node.name
        return (name or "").lower() or None
    return None


def guard_select(sql: str, row_limit: int = DEFAULT_ROW_LIMIT) -> GuardResult:
    """Validate ``sql`` as a single read-only SELECT and return a :class:`GuardResult`.

    On accept, the returned ``sql`` has an auto-``LIMIT row_limit`` appended to the outermost
    query when it had none (an existing outer LIMIT is preserved; inner LIMITs are untouched).
    On reject, ``rule``/``reason`` name the offending construct. Fails closed: any parse failure
    or unmodelled statement is a rejection, never a pass-through.
    """
    # --- Parse (fail closed on any parser error) ---------------------------------------------
    try:
        statements = sqlglot.parse(sql, dialect="postgres")
    except Exception as exc:  # ParseError, TokenError, etc. — never let an unparsed string by.
        return GuardResult.reject(
            "parse_error", f"could not parse SQL ({type(exc).__name__}): {exc}"
        )

    # Drop trailing empties (a benign trailing ``;`` yields no extra statement, but be explicit).
    statements = [s for s in statements if s is not None]

    if len(statements) == 0:
        return GuardResult.reject(
            "parse_error", "empty query — expected exactly one read-only SELECT statement"
        )
    if len(statements) > 1:
        return GuardResult.reject(
            "multi_statement",
            f"expected exactly one statement, found {len(statements)} "
            "(no ';'-chained or multi-statement payloads)",
        )

    root = statements[0]

    # --- Whole-AST walk: the load-bearing check (catches data-modifying CTEs) -----------------
    for node in root.walk():
        # 1) Any DML/DDL node anywhere in the tree.
        if isinstance(node, _FORBIDDEN_NODES):
            kind = type(node).__name__
            return GuardResult.reject(
                "dml_in_ast",
                f"query contains a data-modifying/DDL node ({kind}) somewhere in the AST; "
                "only a single read-only SELECT is allowed",
            )
        # 2) Fail-closed: a raw ``Command`` means sqlglot could not model the statement
        #    (e.g. REVOKE, VACUUM, CALL, DO). We cannot vouch for it → reject.
        if isinstance(node, exp.Command):
            return GuardResult.reject(
                "unsupported_statement",
                "statement is not a SELECT sqlglot can fully parse "
                f"({(node.name or 'unknown').upper()}); rejected fail-closed",
            )
        # 3) SELECT … INTO creates a table.
        if isinstance(node, exp.Into):
            return GuardResult.reject(
                "select_into", "SELECT … INTO creates a table and is not read-only"
            )
        # 4) SELECT … FOR UPDATE / FOR SHARE takes write locks.
        if isinstance(node, exp.Lock):
            return GuardResult.reject(
                "for_update", "SELECT … FOR UPDATE/SHARE takes row locks and is not read-only"
            )
        # 5) Denylisted dangerous function call.
        name = _func_name(node)
        if name is not None and name in DENYLISTED_FUNCTIONS:
            return GuardResult.reject(
                "denylisted_function",
                f"query calls the denylisted function {name}() "
                "(file/network/DoS reach or a sequence write)",
            )

    # --- Only a genuine read query may pass --------------------------------------------------
    if not isinstance(root, _QUERY_ROOTS):
        return GuardResult.reject(
            "not_a_select",
            f"top-level statement is {type(root).__name__}, not a SELECT / WITH … SELECT",
        )

    # A genuine read query must actually project something. ``SELECT`` / ``SELECT FROM t`` (no
    # projection) parse to a Select with empty ``expressions``; without this they slip the guard and
    # only fail at the DB as a malformed ``SELECT LIMIT n``. Fail closed on any empty SELECT in the
    # tree (a legitimate SELECT always projects ≥1 expression).
    for select in root.find_all(exp.Select):
        if not select.expressions:
            return GuardResult.reject(
                "not_a_select",
                "query has an empty SELECT (no projected columns); expected a read query that "
                "returns data",
            )

    # --- Auto-LIMIT the outermost query (preserve any existing outer LIMIT) -------------------
    # ``args['limit']`` is the OUTER query's LIMIT for both a Select and a set-operation root;
    # a LIMIT inside a subquery/CTE lives on that inner node, so it is never touched here.
    if root.args.get("limit") is None:
        root = root.limit(row_limit)

    accepted = root.sql(dialect="postgres")
    return GuardResult.accept(accepted)
