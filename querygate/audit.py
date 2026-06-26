"""The audit log — one locked JSONL line per tool call (spec §9).

Every tool outcome (``ok`` / ``rejected`` / ``error``) appends exactly one :class:`AuditLine`
to ``audit.jsonl`` (path from config). This is the after-the-fact proof that the boundary held:
a rejected write is a first-class audit event, recorded with the offending SQL and the guard
reason. Timestamps come from the runtime, never hard-coded (spec §9, §11).

Two honesty notes for a real deployment (carried into the README, spec §9):
  * **Concurrency.** Under the HTTP transport, concurrent requests must not interleave a
    half-line. v1 wraps the single-line append in a *process-level* lock — each line is small,
    so the critical section is tiny. A multi-worker deployment would move the audit sink to a
    proper log pipeline (a file lock or a logging service); we mention it, we don't over-build it.
  * **PII in literals.** ``args.sql`` can contain literal values from the question (a name, an
    ID). On synthetic data this is harmless; for real PHI the audit log itself becomes sensitive
    — a consideration for the real-data path (spec §21), alongside redaction-of-the-audit-log.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from .models import AuditLine

#: Process-level lock around the single-line append (see the concurrency note above).
_AUDIT_LOCK = threading.Lock()


def now_rfc3339() -> str:
    """The current instant as an RFC3339 / ISO-8601 UTC string (e.g. ``2026-06-27T12:00:00+00:00``).

    Always from the runtime clock — the determinism rule applies to the *seed*, not to audit
    timestamps, which must reflect when the call actually happened.
    """
    return datetime.now(timezone.utc).isoformat()


def append_audit(line: AuditLine, path: str | Path) -> None:
    """Append one :class:`AuditLine` as a single JSON line to ``path``.

    The line is serialized to JSON *before* taking the lock (keep the critical section to the
    bare file write) and the directory is created if needed. Every written line is valid JSON
    and round-trips back to an :class:`AuditLine` (asserted in the tests).
    """
    payload = line.model_dump_json()
    p = Path(path)
    with _AUDIT_LOCK:
        if p.parent and not p.parent.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(payload + "\n")
