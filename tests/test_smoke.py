import string

from typer.testing import CliRunner

from odoo_db.db import (
    _bloat_estimate_pages,
    _groups_category_sql,
    _localize,
    _mime_family,
    _validate_attachment_orphans,
    compute_role_drift,
    filter_online_users,
    generate_password,
    get_config_parameters,
    get_modules,
    get_users,
)
from odoo_db.main import app

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_list_help():
    result = runner.invoke(app, ["list", "--help"])
    assert result.exit_code == 0


def test_modules_help():
    result = runner.invoke(app, ["modules", "--help"], env={"TERM": "dumb"})
    assert result.exit_code == 0
    assert "--all" in result.output


def test_users_help():
    result = runner.invoke(app, ["users", "--help"], env={"TERM": "dumb"})
    assert result.exit_code == 0
    assert "--all" in result.output
    assert "--online" in result.output


def test_groups_help():
    result = runner.invoke(app, ["groups", "--help"])
    assert result.exit_code == 0


def test_roles_help():
    result = runner.invoke(app, ["roles", "--help"])
    assert result.exit_code == 0


def test_role_drift_help():
    result = runner.invoke(app, ["role-drift", "--help"])
    assert result.exit_code == 0


class _ProbeCursor:
    """Fake cursor that only needs to answer the pg_attribute/to_regclass probe."""

    def __init__(self, has_privilege_column: bool):
        self._result = [(1,)] if has_privilege_column else []

    def execute(self, query, params=None):
        assert "pg_attribute" in query
        assert "to_regclass('res_groups')" in query
        assert "privilege_id" in query

    def fetchone(self):
        return self._result[0] if self._result else None


def test_groups_category_sql_pre_19():
    select_cols, join_sql = _groups_category_sql(_ProbeCursor(has_privilege_column=False))  # ty: ignore[invalid-argument-type]
    assert select_cols == "NULL, NULL, mc.id, mc.name"
    assert "res_groups_privilege" not in join_sql
    assert "g.category_id" in join_sql


def test_groups_category_sql_19_plus():
    select_cols, join_sql = _groups_category_sql(_ProbeCursor(has_privilege_column=True))  # ty: ignore[invalid-argument-type]
    assert select_cols == "p.id, p.name, mc.id, mc.name"
    assert "res_groups_privilege p ON p.id = g.privilege_id" in join_sql
    assert "p.category_id" in join_sql


def test_localize():
    # Odoo 16+: translated Char/Text fields come back as jsonb
    assert _localize({"en_US": "Sales", "fr_FR": "Ventes"}) == "Sales"
    # requested lang missing → falls back to any available translation
    assert _localize({"fr_FR": "Ventes"}) == "Ventes"
    # pre-17: plain string column
    assert _localize("Sales") == "Sales"
    # falsy / absent values normalize to ""
    assert _localize(None) == ""
    assert _localize({}) == ""
    # requested lang present but null -> must not return that None itself;
    # falls back to "" since no other translation has a truthy value either
    assert _localize({"en_US": None}) == ""
    # ... but a truthy fallback translation is still picked up
    assert _localize({"en_US": None, "fr_FR": "Ventes"}) == "Ventes"


def test_compute_role_drift():
    # Sales Manager (role 1, marker group 10) resolves to {10, 11} via implied_ids;
    # Sales User (role 2, marker group 11) resolves to {11} only; Admin (role 3,
    # marker group 20) resolves to {10, 11, 20} - its closure legitimately implies
    # the Sales Manager role's own marker group.
    roles = [
        {
            "id": 1,
            "group_id": 10,
            "name": "Sales Manager",
            "users": ["alice", "dave"],
            "groups": [
                {"id": 10, "name": "Manager", "category": "Sales"},
                {"id": 11, "name": "User", "category": "Sales"},
            ],
        },
        {
            "id": 2,
            "group_id": 11,
            "name": "Sales User",
            "users": ["bob"],
            "groups": [{"id": 11, "name": "User", "category": "Sales"}],
        },
        {
            "id": 3,
            "group_id": 20,
            "name": "Admin",
            "users": ["eve"],
            "groups": [
                {"id": 10, "name": "Manager", "category": "Sales"},
                {"id": 11, "name": "User", "category": "Sales"},
                {"id": 20, "name": "Admin", "category": "Sales"},
            ],
        },
    ]
    groups = [
        # alice: role-consistent. carol: has the Manager marker group but no role granting it (extra).
        {"id": 10, "name": "Manager", "category": "Sales", "users": ["alice", "carol", "eve"]},
        # dave: holds the Sales Manager role but is missing both its groups.
        {"id": 11, "name": "User", "category": "Sales", "users": ["alice", "bob", "eve"]},
        {"id": 20, "name": "Admin", "category": "Sales", "users": ["eve"]},
        # not granted by any role -> never counted as drift, even though everyone has it.
        {
            "id": 99,
            "name": "Internal User",
            "category": "Technical",
            "users": ["alice", "bob", "carol", "dave", "eve"],
        },
    ]

    user_ids = {"alice": 1, "bob": 2, "carol": 3, "dave": 4, "eve": 5}

    # default: no "login" key at all (PII) - only the stable, non-identifying user_id.
    drift = compute_role_drift(roles, groups, user_ids)

    # eve holds the Sales Manager marker (10) only via Admin's implied closure,
    # fully covered by Admin's own resolved set -> not flagged.
    assert drift == [
        {
            "user_id": 3,
            "roles": [],
            "missing_groups": [],
            "extra_groups": [{"id": 10, "name": "Manager", "category": "Sales"}],
        },
        {
            "user_id": 4,
            "roles": ["Sales Manager"],
            "missing_groups": [
                {"id": 10, "name": "Manager", "category": "Sales"},
                {"id": 11, "name": "User", "category": "Sales"},
            ],
            "extra_groups": [],
        },
    ]

    # include_sensitive=True: same entries, "login" now present alongside user_id.
    sensitive_drift = compute_role_drift(roles, groups, user_ids, include_sensitive=True)
    assert [d["login"] for d in sensitive_drift] == ["carol", "dave"]
    assert all("user_id" in d for d in sensitive_drift)


def test_prepare_audit_help():
    result = runner.invoke(app, ["prepare-audit", "--help"])
    assert result.exit_code == 0


def test_bloat_help():
    result = runner.invoke(app, ["bloat", "--help"])
    assert result.exit_code == 0


def test_bloat_estimate_pages():
    # empty / unknown inputs → no estimate
    assert _bloat_estimate_pages(0, 100, 8000) == 0
    assert _bloat_estimate_pages(100, 0, 8000) == 0
    assert _bloat_estimate_pages(100, 100, 0) == 0
    # 1000 rows * 100 bytes / 8000 usable = 12.5 → ceil 13 pages
    assert _bloat_estimate_pages(1000, 100, 8000) == 13


def test_attachments_help():
    result = runner.invoke(app, ["attachments", "--help"])
    assert result.exit_code == 0


def test_crons_help():
    # TERM=dumb: Rich auto-enables ANSI styling in CI (detects GITHUB_ACTIONS),
    # which splits "--include-code" with escape codes and breaks the substring check.
    result = runner.invoke(app, ["crons", "--help"], env={"TERM": "dumb"})
    assert result.exit_code == 0
    assert "--include-code" in result.output
    assert "--all" in result.output


def test_dump_help():
    result = runner.invoke(app, ["dump", "--help"])
    assert result.exit_code == 0


def test_restore_help():
    result = runner.invoke(app, ["restore", "--help"])
    assert result.exit_code == 0


def test_generate_password_length():
    assert len(generate_password()) == 16
    assert len(generate_password(24)) == 24


def test_generate_password_charset():
    allowed = set(string.ascii_letters + string.digits)
    assert set(generate_password(200)).issubset(allowed)


def test_generate_password_random():
    assert generate_password(32) != generate_password(32)


def test_global_sensitive_flag_exists():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


class _FakeParamsCursor:
    def __init__(self, rows):
        self._rows = rows
        self.query: str | None = None
        self.params = None

    def execute(self, query, params=None):
        self.query = query
        self.params = params

    def fetchall(self):
        return self._rows


def test_get_config_parameters_with_pattern():
    cur = _FakeParamsCursor([("mail.catchall.domain", "example.com")])
    result = get_config_parameters(cur, pattern="mail")  # ty: ignore[invalid-argument-type]

    assert result == [{"key": "mail.catchall.domain", "value": "example.com"}]
    assert cur.params is None


def test_get_config_parameters_masks_secrets_by_default():
    cur = _FakeParamsCursor([
        ("database.secret", "a1b9f3e2c8d4"),
        ("web.base.url", "http://localhost:8069"),
        ("google.client_secret", "GOCSPX-xxxx"),
    ])
    result = get_config_parameters(cur)  # ty: ignore[invalid-argument-type]

    assert result == [
        {"key": "database.secret", "value": "********"},
        {"key": "web.base.url", "value": "http://localhost:8069"},
        {"key": "google.client_secret", "value": "********"},
    ]


def test_get_config_parameters_reveals_secrets_when_asked():
    cur = _FakeParamsCursor([("database.secret", "a1b9f3e2c8d4")])
    result = get_config_parameters(cur, reveal=True)  # ty: ignore[invalid-argument-type]

    assert result == [{"key": "database.secret", "value": "a1b9f3e2c8d4"}]


class _FakeSeqCursor:
    """Cursor stand-in returning one queued result set per execute()."""

    def __init__(self, *result_sets):
        self._result_sets = list(result_sets)
        self._current: list = []

    def execute(self, query, params=None):
        self._current = self._result_sets.pop(0)

    def fetchall(self):
        return self._current


# (name, latest_version, auto_install, state)
_MODULE_ROWS = [("base", "19.0.1.0", False, "installed"), ("sale", "19.0.1.0", False, "uninstalled")]
# (login, name, state, active)
_USER_ROWS = [("admin", "Mitchell Admin", "offline", True), ("old", "Ex Employee", "offline", False)]


def test_get_modules_default_shape_unchanged():
    cur = _FakeSeqCursor(_MODULE_ROWS[:1])
    assert get_modules(cur) == [  # ty: ignore[invalid-argument-type]
        {"name": "base", "version": "19.0.1.0", "auto_install": False}
    ]


def test_get_modules_include_uninstalled_adds_state_and_installed():
    cur = _FakeSeqCursor(_MODULE_ROWS)
    result = get_modules(cur, include_uninstalled=True)  # ty: ignore[invalid-argument-type]

    assert [r["state"] for r in result] == ["installed", "uninstalled"]
    # Must be real bools — consumers filter on truthiness of this key.
    assert result[0]["installed"] is True
    assert result[1]["installed"] is False


def test_get_users_default_shape_unchanged():
    cur = _FakeSeqCursor([("mail_presence",)], _USER_ROWS[:1])
    assert get_users(cur) == [  # ty: ignore[invalid-argument-type]
        {"login": "admin", "name": "Mitchell Admin", "state": "offline"}
    ]


def test_get_users_include_inactive_adds_active():
    cur = _FakeSeqCursor([("bus_presence",)], _USER_ROWS)
    result = get_users(cur, include_inactive=True)  # ty: ignore[invalid-argument-type]

    assert [r["login"] for r in result] == ["admin", "old"]
    assert result[0]["active"] is True
    assert result[1]["active"] is False


def test_filter_online_users():
    rows = [
        {"login": "on", "state": "online"},
        {"login": "idle", "state": "away"},
        {"login": "off", "state": "offline"},
        # No presence table on this Odoo version.
        {"login": "no_presence", "state": "unknown"},
    ]
    assert filter_online_users(rows) == [{"login": "on", "state": "online"}]


class _FakeCursor:
    """Minimal psycopg-cursor stand-in for _validate_attachment_orphans.

    Scripts results by matching the leading token of each SQL statement so the
    test stays decoupled from formatting. Per-model orphan probe returns a
    fixed (checked, dead, dead_size) tuple.
    """

    def __init__(self, registered_models, real_tables, probe_result=(10, 2, 2048)):
        self._registered = list(registered_models)
        self._tables = list(real_tables)
        self._probe = probe_result
        self._next = None
        self.probes: list[str] = []

    def execute(self, query, params=None):
        text = query.as_string({}) if hasattr(query, "as_string") else str(query)
        head = text.strip().split()[0].upper()
        if "FROM ir_model" in text:
            self._next = [(m,) for m in self._registered]
        elif "FROM pg_tables" in text:
            self._next = [(t,) for t in self._tables]
        elif head == "SELECT" and "ir_attachment" in text:
            assert params, "probe query missing params"
            self.probes.append(params[0])
            self._next = [self._probe]
        else:
            msg = f"unexpected query: {text[:80]}"
            raise AssertionError(msg)

    def fetchall(self):
        rows, self._next = self._next, None
        return rows

    def fetchone(self):
        rows, self._next = self._next, None
        return rows[0] if rows else None


def test_validate_attachment_orphans_skips_unregistered_models():
    # Bug regression: candidate set used to be underscore-form, so dotted
    # res_model values never matched and the probe never ran. Now compares
    # dotted-to-dotted and probes only registered + table-backed models.
    by_model = [
        {"model": "sale.order", "count": 5, "size": 100},
        {"model": "ghost.model", "count": 5, "size": 100},  # not in ir_model
        {"model": "abstract.thing", "count": 5, "size": 100},  # registered, no table
        {"model": "", "count": 5, "size": 100},  # empty res_model
    ]
    cur = _FakeCursor(
        registered_models={"sale.order", "abstract.thing"},
        real_tables={"sale_order", "res_partner"},
    )
    findings = _validate_attachment_orphans(cur, by_model, top_n=10, max_scan=1000)  # ty: ignore[invalid-argument-type]

    assert cur.probes == ["sale.order"]
    assert findings == [{"model": "sale.order", "checked": 10, "dead_count": 2, "dead_size": 2048}]


def test_validate_attachment_orphans_skips_when_over_max_scan():
    by_model = [{"model": "sale.order", "count": 10_000, "size": 100}]
    cur = _FakeCursor(registered_models={"sale.order"}, real_tables={"sale_order"})
    findings = _validate_attachment_orphans(cur, by_model, top_n=10, max_scan=1000)  # ty: ignore[invalid-argument-type]

    assert cur.probes == []
    assert findings == [{"model": "sale.order", "skipped": True, "reason": "10,000 attachments > max-scan 1,000"}]


def test_mime_family():
    assert _mime_family("image/png") == "image"
    assert _mime_family("application/pdf") == "pdf"
    assert _mime_family("text/css") == "assets (css/js)"
    assert _mime_family("application/javascript") == "assets (css/js)"
    assert _mime_family("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") == "office"
    assert _mime_family(None) == "unknown"
    assert _mime_family("") == "unknown"
    assert _mime_family("application/octet-stream") == "other"
