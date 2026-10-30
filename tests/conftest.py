"""Shared fixtures for the per-version database fixture tests.

Spins up a throwaway PostgreSQL database per manifest entry, loads the shared
`_base.sql` plus that version's overlay, and yields its name. Skips the whole
suite when no PostgreSQL server is reachable (the normal unit-test job has no
`postgres` service — only the dedicated `version-fixtures` job does), so
`make test` stays green on a machine with no database.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from odoo_db import db

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "versions"
MANIFEST = json.loads((FIXTURES_DIR / "manifest.json").read_text())


def _pg_available() -> bool:
    try:
        with db.admin_connect() as conn:
            conn.execute("SELECT 1")
    except psycopg.OperationalError:
        return False
    else:
        return True


def _load(dbname: str, entry: dict) -> None:
    """(Re)create `dbname` and load the base schema + the version overlay."""
    with db.admin_connect() as conn, conn.cursor() as cur:
        db.drop_database_if_exists(cur, dbname)
        db.create_database(cur, dbname)
    # read as bytes: conn.execute's typed query union accepts bytes but not a
    # runtime (non-literal) str, and psycopg runs a multi-statement bytes query
    # the same way.
    base_sql = (FIXTURES_DIR / "_base.sql").read_bytes()
    version_sql = (FIXTURES_DIR / entry["fixture"]).read_bytes()
    with psycopg.connect(f"dbname={dbname}", autocommit=True) as conn:
        conn.execute(base_sql)
        conn.execute(version_sql)


def _drop(dbname: str) -> None:
    with db.admin_connect() as conn, conn.cursor() as cur:
        db.drop_database_if_exists(cur, dbname)


@pytest.fixture(scope="module", params=MANIFEST, ids=[f"v{e['version']}" for e in MANIFEST])
def fixture_db(request) -> Iterator[tuple[dict, str]]:
    """Yield ``(manifest_entry, dbname)`` for a loaded version fixture.

    Module-scoped: the database is built once per version and shared across the
    (read-only) tests for that version, then dropped — instead of rebuilt per
    test. Skips when no PostgreSQL is reachable, or when the entry is not a SQL
    fixture (the loader only speaks SQL; real ``pgdump`` fixtures would need the
    ``pg_restore`` branch described in tests/fixtures/versions/README.md).
    """
    if not _pg_available():
        pytest.skip("no PostgreSQL server reachable (dbname=postgres)")
    entry = request.param
    if entry.get("kind", "sql") != "sql":
        pytest.skip(f"fixture kind {entry.get('kind')!r} not supported by the SQL loader yet")
    dbname = "odoo_db_fixture_" + entry["version"].replace(".", "_")
    _load(dbname, entry)
    try:
        yield entry, dbname
    finally:
        _drop(dbname)
