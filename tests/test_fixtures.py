"""Per-version shape assertions, parametrized over the fixture manifest.

The manifest's ``expected_shape`` is the test oracle — there is no second
hardcoded copy to drift from. For each version this checks three things
(mirroring the spec's section 9):

1. odoo-db detects the declared version (every other branch hangs off this);
2. the ``db.*`` shape probes report the declared shape;
3. the shape-sensitive functions behind ``users`` / ``crons`` / ``list`` /
   the ``prepare-audit`` probes run to completion and return the right keys.

Assertions are on shape and key set, never on row counts: the fixtures carry a
handful of rows on purpose, and counting them would be brittle without proving
anything the shape does not already.
"""

from __future__ import annotations

import psycopg
from typer.testing import CliRunner

from odoo_db import db
from odoo_db.main import app

runner = CliRunner()


def _presence_table(cur: psycopg.Cursor) -> str | None:
    cur.execute("SELECT tablename FROM pg_tables WHERE tablename IN ('mail_presence', 'bus_presence')")
    rows = [r[0] for r in cur.fetchall()]
    return rows[0] if rows else None


def _groups_source(cur: psycopg.Cursor) -> str:
    _cols, join = db._groups_category_sql(cur)
    return "privilege_id" if "res_groups_privilege" in join else "category_id"


def _last_login_source(cur: psycopg.Cursor) -> str:
    return "res_users_log" if db._has_res_users_log(cur) else "column"


def test_version_detection(fixture_db):
    """The shape assumption the whole tool hangs off: version is read right."""
    entry, dbname = fixture_db
    summary = db.get_db_summary(dbname)
    assert summary is not None
    assert summary.version == entry["version"]


def test_neutralized(fixture_db):
    _entry, dbname = fixture_db
    with db.cursor(dbname) as cur:
        assert db.get_is_neutralized(cur) is True
        # the flag `list` reads per database comes from the same key
        assert db.get_database_flags(cur)["neutralized"] is True


def test_modules_match_manifest(fixture_db):
    """The manifest's ``modules`` is the installed set `get_modules` returns."""
    entry, dbname = fixture_db
    with db.cursor(dbname) as cur:
        installed = {m["name"] for m in db.get_modules(cur)}
    assert installed == set(entry["modules"])


def test_shape_probes(fixture_db):
    entry, dbname = fixture_db
    exp = entry["expected_shape"]
    with db.cursor(dbname) as cur:
        assert db._has_res_users_log(cur) == exp["has_res_users_log"]
        assert db._has_totp_secret(cur) == exp["has_totp_secret"]
        assert db._has_passkey(cur) == exp["has_passkey_table"]
        assert db._has_cron_failure_tracking(cur) == exp["has_cron_failure_cols"]
        assert db._has_cron_progress(cur) == exp["has_cron_progress"]
        assert _presence_table(cur) == exp["presence_table"]
        assert _groups_source(cur) == exp["groups_category_source"]
        assert _last_login_source(cur) == exp["last_login_source"]


def test_shape_sensitive_commands_complete(fixture_db):
    entry, dbname = fixture_db
    exp = entry["expected_shape"]
    with db.cursor(dbname) as cur:
        # users (+ --summarize)
        users = db.get_users(cur, include_inactive=True)
        assert users, "expected seeded users"
        assert {"login", "name", "state", "last_login", "mfa", "active"} <= set(users[0])
        summary = db.summarize_users(db.get_users(cur), by="month")
        assert set(summary) == {"last_login_by_month", "mfa_by_type"}

        # last_login must resolve from the version's source and be correct, not
        # merely non-null: res_users_log (MAX create_date = 2024-03-05) on 9.0+,
        # the real login_date column (2024-02-01) on 8.0. This is the whole point
        # of the version branch — a wrong source that still "runs" would pass a
        # key-set-only check.
        admin = next(u for u in users if u["login"] == "admin")
        expected_last_login = "2024-03-05" if exp["has_res_users_log"] else "2024-02-01"
        assert admin["last_login"].startswith(expected_last_login)

        # mfa string reflects the version's auth shape
        if exp["has_totp_secret"]:
            assert "totp" in admin["mfa"]
        if exp["has_passkey_table"]:
            assert "passkey" in admin["mfa"]

        # crons; failure columns appear only on 18+
        crons = db.get_crons(cur)
        assert crons and {"name", "interval", "nextcall"} <= set(crons[0])
        if exp["has_cron_failure_cols"]:
            assert {"failure_count", "first_failure_date"} <= set(crons[0])

        # prepare-audit probes (the NDA-free, shape-sensitive subset)
        by_year = db.get_users_by_year(cur)
        assert isinstance(by_year, dict)
        last_login = db.get_users_last_login(cur)
        assert {"by_year", "by_month", "never"} <= set(last_login)
        # the seeded portal user never logged in -> exercises both never-count
        # branches (NOT EXISTS res_users_log, and login_date IS NULL on 8.0).
        assert last_login["never"] == 1
        inventory = db.get_cron_inventory(cur)
        assert inventory and {"name", "active", "code_based", "has_xmlid"} <= set(inventory[0])


def test_cli_commands_run(fixture_db):
    """The Typer commands (not just the db.* layer) run to completion.

    Covers the command wiring main.py adds on top of db.* — the --summarize flag
    path, JSON serialization, the writer. `list` is excluded: it scans every
    database on the server, not just this fixture, so it is left to the db-layer
    `get_db_summary` check in test_version_detection. `prepare-audit` is excluded
    by design (its full command needs more schema than these fixtures carry — see
    README); its shape-sensitive probes are covered above.
    """
    _entry, dbname = fixture_db
    for args in (
        ["--output-format", "json", "users", dbname, "--summarize"],
        ["--output-format", "json", "users", dbname, "--all"],
        ["--output-format", "json", "crons", dbname],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 0, f"{args} failed:\n{result.output}"
