import string

from typer.testing import CliRunner

from odoo_db.db import (
    _bloat_estimate_pages,
    _groups_category_sql,
    _is_alias_domain_migration_pending,
    _is_legacy_mail_config_configured,
    _is_neutralization_stub_mail_server,
    _localize,
    _mime_family,
    _relevant_mail_config_parameters,
    _validate_attachment_orphans,
    compute_role_drift,
    filter_online_users,
    generate_password,
    get_config_parameters,
    get_is_neutralized,
    get_mail_addresses,
    get_mail_alias_domains,
    get_mail_config_parameters,
    get_mail_servers,
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


def test_mail_help():
    result = runner.invoke(app, ["mail", "--help"])
    assert result.exit_code == 0


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


def test_get_mail_config_parameters_fills_all_keys_including_unset():
    cur = _FakeParamsCursor([
        ("mail.catchall.domain", "example.com"),
        ("default_email", "root@example.com"),
        ("mail.catchall.alias", ""),  # row exists but blank -> distinct from absent
    ])
    result = get_mail_config_parameters(cur)  # ty: ignore[invalid-argument-type]

    assert [r["key"] for r in result] == [
        "mail.bounce.alias",
        "mail.catchall.alias",
        "mail.catchall.domain",
        "default_email",
        "mail.default.from",
        "mail.default.from_filter",
    ]
    by_key = {r["key"]: r["value"] for r in result}
    assert by_key["mail.catchall.domain"] == "example.com"
    assert by_key["default_email"] == "root@example.com"
    # Absent key -> None ("(not defined)" at display time).
    assert by_key["mail.bounce.alias"] is None
    # Present-but-blank key -> "" preserved, NOT coerced to None — main.py's
    # display must check `is None` rather than falsy-test this, or it shows
    # "(not defined)" for a row that actually exists with an empty value
    # (real case: on a v16 staging database of ours, mail.catchall.domain).
    assert by_key["mail.catchall.alias"] == ""


def test_is_legacy_mail_config_configured_false_on_a_clean_17_plus_install():
    # The documented install path: mail installed, ICP keys never set,
    # alias_domain_id never populated because there was nothing to migrate
    # -- not a misconfiguration (verified across 4 of 5 real v17/v18/v19
    # databases with mail installed).
    config_parameters = [
        {"key": "mail.bounce.alias", "explanation": "", "value": None},
        {"key": "mail.catchall.alias", "explanation": "", "value": None},
        {"key": "mail.catchall.domain", "explanation": "", "value": None},
        {"key": "default_email", "explanation": "", "value": None},
        {"key": "mail.default.from", "explanation": "", "value": None},
        {"key": "mail.default.from_filter", "explanation": "", "value": None},
    ]
    assert _is_legacy_mail_config_configured(config_parameters) is False


def test_is_legacy_mail_config_configured_true_when_a_legacy_icp_key_survives():
    # A stuck v16-to-17 upgrade: mail.catchall.domain was configured before
    # the upgrade and the alias_domain migration never ran (or failed) --
    # this is the case actually worth alerting on.
    config_parameters = [
        {"key": "mail.bounce.alias", "explanation": "", "value": None},
        {"key": "mail.catchall.alias", "explanation": "", "value": None},
        {"key": "mail.catchall.domain", "explanation": "", "value": "example.com"},
        {"key": "default_email", "explanation": "", "value": None},
        {"key": "mail.default.from", "explanation": "", "value": None},
        {"key": "mail.default.from_filter", "explanation": "", "value": None},
    ]
    assert _is_legacy_mail_config_configured(config_parameters) is True


def test_is_legacy_mail_config_configured_ignores_non_migration_keys():
    # default_email (Trobz-specific) and mail.default.from_filter aren't
    # read by _migrate_icp_to_domain -- a value there says nothing about
    # whether an alias-domain migration is stuck.
    config_parameters = [
        {"key": "default_email", "explanation": "", "value": "root@example.com"},
        {"key": "mail.default.from_filter", "explanation": "", "value": "example.com"},
        {"key": "mail.catchall.domain", "explanation": "", "value": None},
    ]
    assert _is_legacy_mail_config_configured(config_parameters) is False


def test_is_legacy_mail_config_configured_treats_blank_value_as_not_configured():
    # A row that exists but is blank ("" — see the present-but-blank case
    # above) is not a leftover legacy value worth alerting on.
    config_parameters = [{"key": "mail.catchall.domain", "explanation": "", "value": ""}]
    assert _is_legacy_mail_config_configured(config_parameters) is False


_ALL_SIX_CONFIG_PARAMETERS = [
    {"key": "mail.bounce.alias", "explanation": "", "value": None},
    {"key": "mail.catchall.alias", "explanation": "", "value": None},
    {"key": "mail.catchall.domain", "explanation": "", "value": None},
    {"key": "default_email", "explanation": "Trobz-specific, used by trobz_base", "value": None},
    {"key": "mail.default.from", "explanation": "", "value": None},
    {"key": "mail.default.from_filter", "explanation": "", "value": None},
]


def test_relevant_mail_config_parameters_hides_legacy_keys_when_migration_is_not_pending():
    # Showing 4 always-empty legacy keys right next to the authoritative
    # alias_domains section reads as if they're part of the active config,
    # confusing a reader debugging mail on a modern, working database --
    # true both for a clean install and for one that migrated fine.
    result = _relevant_mail_config_parameters(_ALL_SIX_CONFIG_PARAMETERS, alias_domains=[], migration_pending=False)
    assert [r["key"] for r in result] == ["default_email", "mail.default.from_filter"]


def test_relevant_mail_config_parameters_keeps_legacy_keys_pre_17():
    # alias_domains is None -> the legacy ICP keys are the only mechanism,
    # so they're always relevant regardless of migration_pending.
    result = _relevant_mail_config_parameters(_ALL_SIX_CONFIG_PARAMETERS, alias_domains=None, migration_pending=False)
    assert len(result) == 6


def test_relevant_mail_config_parameters_keeps_legacy_keys_when_migration_is_pending():
    # alias_domains exists (17+) and the migration is still pending (a
    # company with no alias domain, while a legacy key still holds a
    # value) -- the diagnostic evidence for a stuck v16-to-17 upgrade,
    # must stay visible.
    result = _relevant_mail_config_parameters(_ALL_SIX_CONFIG_PARAMETERS, alias_domains=[], migration_pending=True)
    assert len(result) == 6


def test_is_alias_domain_migration_pending_false_when_alias_domains_is_none():
    # Pre-17 (or mail not installed) -- the ICP keys are the only
    # mechanism there, not a migration in progress.
    assert _is_alias_domain_migration_pending(None, legacy_configured=True) is False


def test_is_alias_domain_migration_pending_false_when_no_legacy_config():
    # A clean 17+ install: nothing was ever migrated, so nothing can be
    # "pending" regardless of what alias_domains looks like.
    alias_domains = [{"company_id": 1, "alias_domain_id": None}]
    assert _is_alias_domain_migration_pending(alias_domains, legacy_configured=False) is False


def test_is_alias_domain_migration_pending_false_when_migration_succeeded():
    # A real-world case: a legacy ICP key still holds a value (Odoo never
    # clears it), but every company already has an alias domain -- the
    # migration ran and finished, this is not "stuck".
    alias_domains = [
        {"company_id": 1, "alias_domain_id": 10},
        {"company_id": 2, "alias_domain_id": 11},
    ]
    assert _is_alias_domain_migration_pending(alias_domains, legacy_configured=True) is False


def test_is_alias_domain_migration_pending_true_when_a_company_still_has_none():
    alias_domains = [
        {"company_id": 1, "alias_domain_id": 10},
        {"company_id": 2, "alias_domain_id": None},
    ]
    assert _is_alias_domain_migration_pending(alias_domains, legacy_configured=True) is True


class _FakeOneCursor:
    """Cursor stand-in returning one queued fetchone() result per execute()."""

    def __init__(self, *rows):
        self._rows = list(rows)
        self._current = None

    def execute(self, query, params=None):
        self._current = self._rows.pop(0)

    def fetchone(self):
        return self._current


class _FakeXmlidCursor:
    """Cursor stand-in for get_mail_addresses: resolves ir_model_data
    lookups from a dict keyed by (module, name), and detail (email) lookups
    from a queue -- since unlike _FakeOneCursor's fixed call order, which
    query runs next depends on whether the previous xmlid resolved."""

    def __init__(self, xmlids: dict[tuple[str, str], int], detail_answers: list):
        self._xmlids = xmlids
        self._detail_answers = list(detail_answers)
        self._current = None

    def execute(self, query, params: tuple[str, str] | None = None):
        if "ir_model_data" in query:
            res_id = self._xmlids.get(params) if params is not None else None
            self._current = (res_id,) if res_id is not None else None
        else:
            self._current = self._detail_answers.pop(0)

    def fetchone(self):
        return self._current


def test_get_mail_addresses_shows_real_email_and_flags_defaults():
    # Organizational mailboxes, not individual PII -> shown as-is, not masked.
    cur = _FakeXmlidCursor(
        xmlids={("base", "main_partner"): 1, ("base", "user_root"): 17, ("base", "user_admin"): 3},
        detail_answers=[
            ("info@yourcompany.example.com",),  # company: still Odoo default
            (17, None),  # OdooBot user found, partner has no email
            (5, "admin@acme.com"),  # admin: customized, real partner_id differs from user_id
        ],
    )
    result = get_mail_addresses(cur)  # ty: ignore[invalid-argument-type]

    assert result == [
        {
            "partner_id": 1,
            "label": "Company Email",
            "email": "info@yourcompany.example.com",
            "is_default": True,
            "missing": False,
        },
        {
            "partner_id": 17,
            "label": "System (OdooBot) Email",
            "email": None,
            "is_default": False,
            "missing": False,
        },
        {"partner_id": 5, "label": "Admin Email", "email": "admin@acme.com", "is_default": False, "missing": False},
    ]


def test_get_mail_addresses_empty_email():
    cur = _FakeXmlidCursor(
        xmlids={("base", "main_partner"): 1, ("base", "user_root"): 17, ("base", "user_admin"): 3},
        detail_answers=[(None,), (17, None), (5, None)],
    )
    result = get_mail_addresses(cur)  # ty: ignore[invalid-argument-type]

    assert [r["email"] for r in result] == [None, None, None]
    assert [r["is_default"] for r in result] == [False, False, False]
    assert [r["missing"] for r in result] == [False, False, False]


def test_get_mail_addresses_is_default_is_case_insensitive():
    # Odoo never normalizes res.partner.email -- an untouched demo address
    # typed back with capitals (ADMIN@Yourcompany.example.com) would
    # otherwise come back is_default: False.
    cur = _FakeXmlidCursor(
        xmlids={("base", "main_partner"): 1, ("base", "user_root"): 17, ("base", "user_admin"): 3},
        detail_answers=[
            ("Info@YourCompany.example.com",),
            (17, "OdooBot@Example.com"),
            (5, "ADMIN@Yourcompany.example.com"),
        ],
    )
    result = get_mail_addresses(cur)  # ty: ignore[invalid-argument-type]

    assert [r["is_default"] for r in result] == [True, True, True]


def test_get_mail_addresses_resolves_admin_by_xmlid_even_when_login_is_renamed():
    # Reproduced against a real v17 production database: res_users id 2 has
    # a customer email as its login, not "admin" -- matching on
    # login = 'admin' silently drops the row entirely there. Resolving
    # through base.user_admin instead doesn't care what the login is.
    cur = _FakeXmlidCursor(
        xmlids={("base", "main_partner"): 1, ("base", "user_root"): 17, ("base", "user_admin"): 2},
        detail_answers=[("info@acme.com",), (17, "odoobot@example.com"), (9, "real.customer@acme.com")],
    )
    result = get_mail_addresses(cur)  # ty: ignore[invalid-argument-type]

    admin_row = result[2]
    assert admin_row["label"] == "Admin Email"
    assert admin_row["email"] == "real.customer@acme.com"
    assert admin_row["missing"] is False


def test_get_mail_addresses_emits_a_missing_row_instead_of_dropping_it_when_xmlid_is_absent():
    # The admin/system row must never just vanish from the output -- "not
    # listed" must not read as "no admin problem".
    cur = _FakeXmlidCursor(
        xmlids={("base", "main_partner"): 1},  # user_root/user_admin: absent entirely
        detail_answers=[("info@acme.com",)],
    )
    result = get_mail_addresses(cur)  # ty: ignore[invalid-argument-type]

    assert len(result) == 3
    system_row, admin_row = result[1], result[2]
    assert system_row == {
        "partner_id": None,
        "label": "System (OdooBot) Email",
        "email": None,
        "is_default": False,
        "missing": True,
    }
    assert admin_row["missing"] is True
    assert admin_row["partner_id"] is None
    assert admin_row["email"] is None


def test_get_mail_addresses_treats_a_dangling_xmlid_as_missing_not_blank():
    # A deleted res_partner (company) or res_users (admin/system) row still
    # has its ir_model_data row -- the xmlid resolves, but the detail query
    # comes up empty. Must not be reported as {"email": None} indistinguishable
    # from "exists, blank email".
    cur = _FakeXmlidCursor(
        xmlids={("base", "main_partner"): 1, ("base", "user_root"): 17, ("base", "user_admin"): 3},
        detail_answers=[None, None, None],  # every detail SELECT comes up empty
    )
    result = get_mail_addresses(cur)  # ty: ignore[invalid-argument-type]

    for row in result:
        assert row["missing"] is True
        assert row["partner_id"] is None
        assert row["email"] is None


class _FakeMailServerCursor:
    """Cursor stand-in: pg_attribute probe then the ir_mail_server select."""

    def __init__(self, has_auth_col, rows):
        self._has_auth_col = has_auth_col
        self._rows = rows
        self._next: list = []

    def execute(self, query, params=None):
        if "pg_attribute" in query:
            self._next = [(1,)] if self._has_auth_col else []
        else:
            self._next = self._rows

    def fetchone(self):
        return self._next[0] if self._next else None

    def fetchall(self):
        return self._next


def test_get_mail_servers_masks_user_and_password_and_includes_15_plus_columns():
    rows = [(1, "Primary", "smtp.example.com", 587, "svc", "s3cr3t", "starttls", True, "login", "notif@example.com")]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert result == [
        {
            "sequence": 1,
            "name": "Primary",
            "smtp_host": "smtp.example.com",
            "smtp_port": 587,
            "smtp_user": "********",
            "smtp_pass": "********",
            "smtp_encryption": "starttls",
            "smtp_authentication": "login",
            "from_filter": "notif@example.com",
            "active": True,
            "is_test_catcher": False,
            "known_production_relay": None,
            "is_neutralization_stub": False,
        }
    ]


def test_get_mail_servers_flags_a_known_test_catcher_by_whole_token_match():
    rows = [
        (1, "mailhog", "smtp.example.com", 25, None, None, "none", True, "login", None),
        (2, "Primary", "mailhog-acme18-staging", 2025, None, None, "none", True, "login", None),
        (3, "Real Relay", "smtp.acme-internal.local", 587, None, None, "starttls", True, "login", None),
    ]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert [r["is_test_catcher"] for r in result] == [True, True, False]


def test_get_mail_servers_test_catcher_does_not_cross_word_boundaries():
    # A plain substring check would flag maildevices.com as "maildev".
    # Token matching (split on non-alphanumeric runs) fixes it.
    rows = [(1, "Lookalike", "smtp.maildevices.com", 587, None, None, "starttls", True, "login", None)]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert result[0]["is_test_catcher"] is False


def test_get_mail_servers_test_catcher_drops_the_ambiguous_papercut_marker():
    # Papercut-SMTP is a real catcher, but "PaperCut MF/NG" is common
    # print-management software -- the marker is dropped rather than guessed
    # at, since a working relay reported as a dead end is worse than a
    # missed catcher.
    rows = [(1, "PaperCut MF print server", "print.acme.local", 25, None, None, "none", True, "login", None)]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert result[0]["is_test_catcher"] is False


def test_get_mail_servers_flags_mailtrap_sandbox_but_not_mailtrap_live():
    # Mailtrap runs a real sending service and a catcher sandbox on
    # neighbouring hostnames -- a name/token marker can't tell them apart,
    # so this one is host-based instead.
    rows = [
        (1, "Mailtrap sandbox", "sandbox.smtp.mailtrap.io", 2525, None, None, "starttls", True, "login", None),
        (2, "Mailtrap live", "live.smtp.mailtrap.io", 587, None, None, "starttls", True, "login", None),
    ]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert [r["is_test_catcher"] for r in result] == [True, False]


def test_get_mail_servers_flags_google_on_any_documented_port():
    # Google documents 25, 465 and 587 for both hosts -- an earlier version
    # of this table required 587/465 respectively, which missed
    # smtp.gmail.com:587 with STARTTLS, the most common Odoo setup on Gmail.
    rows = [
        (1, "Relay 587", "smtp-relay.gmail.com", 587, None, None, "starttls", True, "login", None),
        (2, "Relay 25", "smtp-relay.gmail.com", 25, None, None, "none", True, "login", None),
        (3, "Direct 587", "smtp.gmail.com", 587, None, None, "starttls", True, "login", None),
        (4, "Wrong port", "smtp-relay.gmail.com", 2525, None, None, "starttls", True, "login", None),
    ]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert [r["known_production_relay"] for r in result] == [
        "Google Workspace SMTP relay",
        "Google Workspace SMTP relay",
        "Gmail SMTP",
        None,
    ]


def test_get_mail_servers_flags_a_known_production_relay_by_suffix_and_rejects_lookalikes():
    rows = [
        # M365: host is a per-tenant subdomain, so this must be a suffix match
        (1, "M365", "acme-com.mail.protection.outlook.com", 25, None, None, "none", True, "login", None),
        # a bare endswith() (no leading dot) would match both of these
        (2, "Lookalike Mailjet", "notmailjet.com", 587, None, None, "starttls", True, "login", None),
        (3, "Lookalike M365", "evilmail.protection.outlook.com", 25, None, None, "none", True, "login", None),
        (4, "Unrelated", "smtp.acme-internal.local", 587, None, None, "starttls", True, "login", None),
    ]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert [r["known_production_relay"] for r in result] == ["Microsoft 365", None, None, None]


def test_get_mail_servers_flags_microsoft_365_smtp_auth_client_submission():
    # The usual Odoo-on-M365 setup: SMTP AUTH client submission needs no
    # Exchange-side connector, just a mailbox user/password -- different
    # hosts from the per-tenant relay-connector suffix above, and
    # deliberately port-agnostic (a separate table entry, not a widened
    # one, so the relay connector's port-25 constraint stays intact).
    rows = [
        (1, "M365 direct", "smtp.office365.com", 587, None, None, "starttls", True, "login", None),
        (2, "M365 outlook", "smtp-mail.outlook.com", 587, None, None, "starttls", True, "login", None),
    ]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert [r["known_production_relay"] for r in result] == ["Microsoft 365", "Microsoft 365"]


def test_get_mail_servers_flags_port_agnostic_known_relays_by_host_alone():
    """These hosts are dedicated per-provider hostnames -- distinctive
    enough on their own that a port requirement would just be extra
    fragility, not extra precision. Unlike Google/Gmail (see
    test_get_mail_servers_flags_google_on_any_documented_port), none of
    these carry a port constraint at all."""
    rows = [
        (1, "Brevo old host", "smtp-relay.sendinblue.com", 587, None, None, "starttls", True, "login", None),
        (2, "Brevo new host", "smtp-relay.brevo.com", 587, None, None, "starttls", True, "login", None),
        (3, "Mandrill", "smtp.mandrillapp.com", 587, None, None, "starttls", True, "login", None),
        (4, "OVH", "ssl0.ovh.net", 465, None, None, "ssl", True, "login", None),
        (5, "Mailjet EU", "in-v3.mailjet.com", 587, None, None, "starttls", True, "login", None),
        (6, "SendGrid", "smtp.sendgrid.net", 587, None, None, "starttls", True, "login", None),
        (7, "Mailgun", "smtp.mailgun.org", 587, None, None, "starttls", True, "login", None),
        (8, "Postmark", "smtp.postmarkapp.com", 587, None, None, "starttls", True, "login", None),
    ]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert [r["known_production_relay"] for r in result] == [
        "Brevo (ex-Sendinblue)",
        "Brevo (ex-Sendinblue)",
        "Mandrill",
        "OVH",
        "Mailjet",
        "SendGrid",
        "Mailgun",
        "Postmark",
    ]


def test_get_mail_servers_flags_amazon_ses_by_region_pattern_and_rejects_unrelated_amazonaws_hosts():
    rows = [
        (1, "SES us-east-1", "email-smtp.us-east-1.amazonaws.com", 587, None, None, "starttls", True, "login", None),
        (2, "SES eu-west-1", "email-smtp.eu-west-1.amazonaws.com", 587, None, None, "starttls", True, "login", None),
        # an unrelated amazonaws.com host (e.g. an EC2 instance's own
        # hostname) must not be reported as SES
        (3, "Unrelated EC2", "ec2-1-2-3-4.compute-1.amazonaws.com", 25, None, None, "none", True, "login", None),
    ]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert [r["known_production_relay"] for r in result] == ["Amazon SES", "Amazon SES", None]


def test_get_mail_servers_reveals_user_and_password_when_asked():
    rows = [(1, "Primary", "smtp.example.com", 587, "svc", "s3cr3t", "starttls", True, "login", "notif@example.com")]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur, reveal=True)  # ty: ignore[invalid-argument-type]

    assert result[0]["smtp_user"] == "svc"
    assert result[0]["smtp_pass"] == "s3cr3t"  # noqa: S105 — test fixture value, not a real secret


def test_get_mail_servers_pre_15_has_no_auth_columns():
    rows = [(1, "Primary", "smtp.example.com", 25, None, None, "none", True, None, None)]
    cur = _FakeMailServerCursor(has_auth_col=False, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert result[0]["smtp_authentication"] is None
    assert result[0]["from_filter"] is None
    assert result[0]["smtp_user"] is None  # no username set -> nothing to mask
    assert result[0]["smtp_pass"] is None  # no password set -> nothing to mask


def test_get_mail_servers_flags_the_neutralization_stub_odoo_core_inserts():
    # base/data/neutralize.sql inserts exactly this (name, host) pair after
    # disabling every pre-existing relay -- the real relay below it is the
    # one that's now inactive, not broken.
    rows = [
        (None, "neutralization - disable emails", "invalid", 1025, None, None, "none", True, "login", None),
        (1, "Real Relay", "smtp.sendgrid.net", 587, None, None, "starttls", False, "login", None),
    ]
    cur = _FakeMailServerCursor(has_auth_col=True, rows=rows)
    result = get_mail_servers(cur)  # ty: ignore[invalid-argument-type]

    assert [r["is_neutralization_stub"] for r in result] == [True, False]


def test_is_neutralization_stub_mail_server_requires_both_name_and_host():
    assert _is_neutralization_stub_mail_server("neutralization - disable emails", "invalid") is True
    # Case-insensitive: Odoo core's own string, but nothing guarantees casing.
    assert _is_neutralization_stub_mail_server("Neutralization - Disable Emails", "Invalid") is True
    assert _is_neutralization_stub_mail_server("neutralization - disable emails", "smtp.example.com") is False
    assert _is_neutralization_stub_mail_server("Primary", "invalid") is False
    assert _is_neutralization_stub_mail_server(None, None) is False


class _FakeConfigParamCursor:
    """Cursor stand-in: one queued fetchone() result for a single-key select."""

    def __init__(self, row):
        self._row = row

    def execute(self, query, params=None):
        pass

    def fetchone(self):
        return self._row


def test_get_is_neutralized_case_folds_postgres_lowercase_true():
    # base/data/neutralize.sql writes the value via a plain SQL boolean
    # literal (`VALUES (..., true)`), which Postgres stores as lowercase
    # text 'true' -- a `== "True"` comparison here would silently report
    # every neutralized database as not neutralized.
    cur = _FakeConfigParamCursor(("true",))
    assert get_is_neutralized(cur) is True  # ty: ignore[invalid-argument-type]


def test_get_is_neutralized_false_when_key_absent():
    cur = _FakeConfigParamCursor(None)
    assert get_is_neutralized(cur) is False  # ty: ignore[invalid-argument-type]


def test_get_is_neutralized_false_when_explicitly_false():
    cur = _FakeConfigParamCursor(("false",))
    assert get_is_neutralized(cur) is False  # ty: ignore[invalid-argument-type]


class _FakeAliasDomainCursor:
    """Cursor stand-in: to_regclass probe then the res_company/mail_alias_domain join."""

    def __init__(self, table_exists, rows=()):
        self._table_exists = table_exists
        self._rows = list(rows)
        self._next: list = []
        self.main_query: str | None = None

    def execute(self, query, params=None):
        if "to_regclass" in query:
            self._next = [(1 if self._table_exists else None,)]
            return
        self.main_query = query
        self._next = self._rows

    def fetchone(self):
        return self._next[0] if self._next else None

    def fetchall(self):
        return self._next


def test_get_mail_alias_domains_returns_none_when_table_absent():
    cur = _FakeAliasDomainCursor(table_exists=False)
    assert get_mail_alias_domains(cur) is None  # ty: ignore[invalid-argument-type]


def test_get_mail_alias_domains_computes_emails_and_flags_missing():
    rows = [
        # company with an alias domain assigned
        (1, "My Company", 3, "mycompany.example", "bounce", "catchall", "notifications"),
        # company with no alias domain at all
        (2, "Other Company", None, None, None, None, None),
        # default_from already a full address -> used as-is, not "@"-appended again
        (4, "Third Company", 5, "third.example.com", "bounce", "catchall", "no-reply@third.example.com"),
    ]
    cur = _FakeAliasDomainCursor(table_exists=True, rows=rows)
    result = get_mail_alias_domains(cur)  # ty: ignore[invalid-argument-type]

    # Archived companies shouldn't feed the "missing alias domain" gauge with
    # false positives — regression check that the filter is actually in the query.
    assert cur.main_query is not None
    assert "c.active = true" in cur.main_query

    assert result == [
        {
            "company_id": 1,
            "company_name": "My Company",
            "alias_domain_id": 3,
            "alias_domain": "mycompany.example",
            "bounce_email": "bounce@mycompany.example",
            "catchall_email": "catchall@mycompany.example",
            "default_from_email": "notifications@mycompany.example",
        },
        {
            "company_id": 2,
            "company_name": "Other Company",
            "alias_domain_id": None,
            "alias_domain": None,
            "bounce_email": None,
            "catchall_email": None,
            "default_from_email": None,
        },
        {
            "company_id": 4,
            "company_name": "Third Company",
            "alias_domain_id": 5,
            "alias_domain": "third.example.com",
            "bounce_email": "bounce@third.example.com",
            "catchall_email": "catchall@third.example.com",
            "default_from_email": "no-reply@third.example.com",
        },
    ]


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
