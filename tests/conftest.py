"""Shared pytest fixtures for the QueryGate DB/seed/role tests.

The suite runs against a real Postgres reachable as an admin role via $DATABASE_URL.
A session fixture applies scripts/schema.sql + scripts/init_role.sql (via `psql`, the
exact artifacts docker-compose uses) and loads the deterministic seed, so the tests are
self-contained given only a blank database. No LLM, no API key.

Skips cleanly (not fails) if $DATABASE_URL is unset or `psql` is unavailable.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import psycopg
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = REPO_ROOT / "scripts"


def _load_seed_module():
    spec = importlib.util.spec_from_file_location("qg_seed", SCRIPTS / "seed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


seed_module = _load_seed_module()


@pytest.fixture(scope="session")
def admin_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        pytest.skip("DATABASE_URL not set — needs an admin Postgres connection")
    return url


@pytest.fixture(scope="session")
def role_password() -> str:
    return os.environ.get("QUERYGATE_DB_PASSWORD", "querygate_pw")


@pytest.fixture(scope="session")
def role_url(admin_url: str, role_password: str) -> str:
    """The read-only `querygate` role's connection string.

    Use $QUERYGATE_DATABASE_URL if provided, else derive it from the admin URL's
    host/port/db with the querygate user + password.
    """
    explicit = os.environ.get("QUERYGATE_DATABASE_URL")
    if explicit:
        return explicit
    parts = urllib.parse.urlsplit(admin_url)
    netloc = f"querygate:{role_password}@{parts.hostname}"
    if parts.port:
        netloc += f":{parts.port}"
    return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def _psql(url: str, *args: str) -> None:
    psql = shutil.which("psql") or os.environ.get("QG_PSQL")
    if not psql:
        pytest.skip("psql not found on PATH (needed to apply schema.sql / init_role.sql)")
    # Options MUST precede the positional dbname; psql's getopt stops at the first
    # non-option argument, so a trailing URL would cause the flags to be ignored.
    subprocess.run([psql, "-v", "ON_ERROR_STOP=1", *args, url], check=True)


@pytest.fixture(scope="session")
def seeded_db(admin_url: str, role_password: str):
    """Apply schema + Layer-3 role, then load the deterministic seed. Runs once per session."""
    _psql(admin_url, "-f", str(SCRIPTS / "schema.sql"))
    _psql(admin_url, "-v", f"pw={role_password}", "-f", str(SCRIPTS / "init_role.sql"))
    with psycopg.connect(admin_url) as conn:
        counts = seed_module.seed(conn, reset=True)
    return counts


@pytest.fixture()
def admin_conn(admin_url: str, seeded_db):
    with psycopg.connect(admin_url, autocommit=True) as conn:
        yield conn
