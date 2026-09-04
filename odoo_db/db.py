from __future__ import annotations

import logging
import math
import re
import secrets
import string
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql
from rich.console import Console
from rich.progress import track

logger = logging.getLogger(__name__)

_progress_console = Console(stderr=True)

# Odoo core models whose `_table` attribute overrides the default
# `replace(model, '.', '_')` and therefore can't be recovered from `ir_model`
# alone. All seven live in `base`. Verified against odoo/addons/base/models/
# ir_actions.py and ir_actions_report.py — this list is stable across recent
# Odoo versions (14 → 19). Extend only if a new core override is observed.
_BASE_TABLE_NAME_OVERRIDES: dict[str, str] = {
    "ir_actions": "base",
    "ir_act_window": "base",
    "ir_act_window_view": "base",
    "ir_act_server": "base",
    "ir_act_report_xml": "base",
    "ir_act_client": "base",
    "ir_act_url": "base",
}

# Non-Odoo objects that are nonetheless legitimate / expected, so audit
# consumers can distinguish them from genuinely custom additions. Extend
# only with widely-deployed objects whose origin is unambiguous.
_RECOGNIZED_FUNCTIONS: dict[str, str] = {
    "unaccent": "Postgres unaccent extension wrapper (Odoo uses it for accent-insensitive search).",
}
_RECOGNIZED_TRIGGERS: dict[str, str] = {
    "queue_job_notify": "OCA queue_job module — NOTIFY trigger on queue_job inserts.",
}


@contextmanager
def connect(dbname: str):
    logger.debug("connecting to %s", dbname)
    with psycopg.connect(f"dbname={dbname}") as conn:
        yield conn


@contextmanager
def cursor(dbname: str):
    """Open a connection and yield a cursor. Single helper for one-shot queries.

    `prepare-audit` and other multi-query callers should open their own
    `connect()` once and reuse the cursor across calls instead of using this.
    """
    with connect(dbname) as conn, conn.cursor() as cur:
        yield cur


def _fetch_one(cur: psycopg.Cursor) -> tuple:
    """``cur.fetchone()`` that errors out if no row is returned.

    Use for queries that are *guaranteed* to produce exactly one row (e.g.
    ``SELECT count(*)``, ``SELECT EXTRACT(year FROM NOW())``). Keeps the
    type checker happy without scattering ``assert``s.
    """
    row = cur.fetchone()
    if row is None:
        raise RuntimeError
    return row


# Generic table-name prefixes that don't carry functional meaning on their
# own — when a table starts with one of these we fall through to the second
# component (e.g. ``wizard_payment_register`` → ``payment``).
_NOISE_PREFIXES: frozenset[str] = frozenset({
    "wizard",
    "validate",
    "report",
    "mixin",
    "abstract",
    "tmp",
    "temp",
    "x",
})


def _functional_group(table: str, model_owners: dict[str, str] | None = None) -> str:
    """Display-only functional bucket for a table name.

    The table name itself is the strongest functional signal — a custom module
    that extends MRP still uses ``mrp_*`` table names, so table-prefix is more
    informative than the owning module's name (which might be e.g.
    ``cpc_recycle_mrp``). Resolution order:

    1. Split the table name on ``_``. If the leading component is a noise
       prefix (``wizard``, ``report``, ``mixin``, …) and a second component
       exists, use the second component — those prefixes describe table
       *kind*, not functional area.
    2. Otherwise, the first underscore component.
    3. Only when the table has no underscore at all, fall back to the owner
       module's first prefix from ``model_owners`` if available — single-word
       tables carry no internal hint.

    Never used for owner attribution — that's ``model_owners`` only.
    """
    parts = table.split("_")
    if len(parts) > 1:
        if parts[0] in _NOISE_PREFIXES:
            return parts[1]
        return parts[0]
    if model_owners and table in model_owners:
        return model_owners[table].split("_", 1)[0]
    return parts[0]


def _is_odoo(cur: psycopg.Cursor) -> bool:
    cur.execute("SELECT 1 FROM pg_tables WHERE tablename='ir_module_module'")
    return bool(cur.fetchone())


def get_is_neutralized(cur: psycopg.Cursor) -> bool:
    """`base/data/neutralize.sql` (identical 16.0-19.0, absent on 14.0) sets
    `database.is_neutralized` via a plain SQL `VALUES (..., true)` — Postgres
    stores that as the lowercase text `'true'`, not `'True'`, so the check
    must be case-folded (a `row[0] == "True"` comparison here would silently
    report every neutralized database as not neutralized)."""
    cur.execute("SELECT value FROM ir_config_parameter WHERE key = 'database.is_neutralized'")
    row = cur.fetchone()
    return bool(row) and str(row[0]).strip().lower() in ("true", "t", "1")


def list_databases(cur: psycopg.Cursor) -> list[str]:
    cur.execute("""
            SELECT datname FROM pg_database
            WHERE NOT datistemplate AND datallowconn
              AND datname NOT IN ('postgres', 'template0', 'template1')
            ORDER BY datname
        """)
    return [row[0] for row in cur.fetchall()]


@dataclass
class DbSummary:
    name: str
    version: str
    neutralized: bool
    module_count: int | None = None
    user_count: int | None = None


def get_db_summary(dbname: str, verbose: bool = False) -> DbSummary | None:
    """Open its own connection — used in a per-DB loop where some DBs may fail."""
    try:
        with cursor(dbname) as cur:
            if not _is_odoo(cur):
                return None

            cur.execute("SELECT latest_version FROM ir_module_module WHERE name='base'")
            row = cur.fetchone()
            if not row or not row[0]:
                return None
            parts = row[0].split(".")
            version = ".".join(parts[:2]) if len(parts) >= 2 else row[0]

            neutralized = get_is_neutralized(cur)

            module_count = user_count = None
            if verbose:
                cur.execute("SELECT count(*) FROM ir_module_module WHERE state='installed'")
                module_count = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM res_users WHERE active=true")
                user_count = cur.fetchone()[0]

            return DbSummary(
                name=dbname,
                version=version,
                neutralized=neutralized,
                module_count=module_count,
                user_count=user_count,
            )
    except Exception as exc:
        logger.warning("skipping %s: %s", dbname, exc)
        return None


def get_modules(cur: psycopg.Cursor, *, include_uninstalled: bool = False) -> list[dict]:
    where_clause = sql.SQL("") if include_uninstalled else sql.SQL("WHERE state = 'installed'")
    cur.execute(
        sql.SQL("""
            SELECT name, latest_version, auto_install, state
            FROM ir_module_module
            {where}
            ORDER BY name
        """).format(where=where_clause)
    )
    return [
        {
            "name": row[0],
            "version": row[1] or "",
            "auto_install": bool(row[2]),
            **({"state": row[3], "installed": row[3] == "installed"} if include_uninstalled else {}),
        }
        for row in cur.fetchall()
    ]


def get_module_dependents(cur: psycopg.Cursor) -> dict[str, int]:
    """Return {module_name: count_of_installed_modules_that_depend_on_it}.

    Uses ir_module_module_dependency where `name` is the dependency string
    and module_id FK points to the module that declares the dependency.
    Only counts installed modules on both sides.
    """
    cur.execute("""
        SELECT d.name, count(*) AS cnt
        FROM ir_module_module_dependency d
        JOIN ir_module_module m ON m.id = d.module_id
        WHERE m.state = 'installed'
        GROUP BY d.name
    """)
    return {r[0]: r[1] for r in cur.fetchall()}


def get_model_owners(cur: psycopg.Cursor) -> dict[str, str]:
    """Return ``{table_name: owning_module}`` derived from Odoo's own registry.

    Sources, in precedence order (first wins):

    1. ``ir_model_data`` joined to ``ir_model`` — authoritative for regular
       models. When module X declares **or extends** a model, Odoo writes a
       row with ``model='ir.model'``, ``module=X``, ``res_id=<ir_model.id>``,
       and ``name='model_<table>'``. Multiple modules may therefore write
       rows for the same model. Since Odoo installs modules in topological
       dependency order (``base`` first, then everything else), the row with
       the smallest ``ir_model_data.id`` per model corresponds to the module
       that *originally declared* it — that's the one we keep, via
       ``DISTINCT ON``.
    2. ``ir_model_relation`` — many-to-many relation tables (which have no
       ``ir_model`` row of their own) are tracked here with a direct FK to
       ``ir_module_module``. When several modules declare the same relation,
       we keep the earliest one (smallest ``ir_model_relation.id``).

    Tables with no entry in either source (legacy / raw-SQL / orphans from
    uninstalled modules) are simply absent from the returned dict — callers
    decide how to handle them (typically fall back to a prefix heuristic).
    """
    owners: dict[str, str] = {}

    cur.execute("""
        SELECT DISTINCT ON (im.id)
            replace(im.model, '.', '_') AS tablename,
            imd.module
        FROM ir_model im
        JOIN ir_model_data imd
          ON imd.model = 'ir.model' AND imd.res_id = im.id
        ORDER BY im.id, imd.id
    """)
    for tablename, module in cur.fetchall():
        owners[tablename] = module

    cur.execute("""
        SELECT DISTINCT ON (r.name)
            r.name, m.name
        FROM ir_model_relation r
        JOIN ir_module_module m ON r.module = m.id
        ORDER BY r.name, r.id
    """)
    for tablename, module in cur.fetchall():
        owners.setdefault(tablename, module)

    for tablename, module in _BASE_TABLE_NAME_OVERRIDES.items():
        owners.setdefault(tablename, module)

    return owners


# Substring markers (case-insensitive) flagging an ir_config_parameter key whose
# value is a secret (session HMAC, oauth secret, api key, ...). Value is masked
# unless the caller passes reveal=True (global --include-sensitive-information).
_SENSITIVE_KEY_MARKERS = (
    "secret",
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "private_key",
    "client_secret",
    "enterprise_code",
    "dsn",
)
_SECRET_MASK = "********"  # noqa: S105 — mask placeholder, not a real secret


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    if re.search(r"(^|[._])key$", lowered):
        return True
    return any(marker in lowered for marker in _SENSITIVE_KEY_MARKERS)


def get_config_parameters(cur: psycopg.Cursor, *, pattern: str | None = None, reveal: bool = False) -> list[dict]:
    cur.execute("SELECT key, value FROM ir_config_parameter ORDER BY key")
    needle = pattern.lower() if pattern else None

    rows = []
    for key, value in cur.fetchall():
        if needle and needle not in key.lower():
            continue
        if not reveal and _is_sensitive_key(key):
            value = _SECRET_MASK
        rows.append({"key": key, "value": value})
    return rows


def _has_cron_failure_tracking(cur: psycopg.Cursor) -> bool:
    """Odoo 18+ only: ``ir_cron.failure_count``/``first_failure_date``, used to
    auto-deactivate a cron after enough consecutive failures. Probed via
    ``pg_attribute`` (not ``information_schema``) for the same reason as
    ``_groups_category_sql``: ``attrelid = to_regclass('ir_cron')`` pins the
    probe to the exact relation the surrounding query will resolve, whereas
    ``information_schema.columns`` matches on a bare table name and could
    report a same-named table in another schema.
    """
    cur.execute("""
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('ir_cron') AND attname = 'failure_count' AND NOT attisdropped
    """)
    return cur.fetchone() is not None


def get_crons(cur: psycopg.Cursor, *, include_code: bool = False, include_inactive: bool = False) -> list[dict]:
    where_clause = sql.SQL("") if include_inactive else sql.SQL("WHERE ic.active = true")
    has_failure_tracking = _has_cron_failure_tracking(cur)
    failure_cols = sql.SQL(", ic.failure_count, ic.first_failure_date") if has_failure_tracking else sql.SQL("")
    cur.execute(
        sql.SQL("""
            SELECT ic.cron_name, ic.interval_number, ic.interval_type, ic.nextcall, ias.code, ic.active
                {failure_cols}
            FROM ir_cron ic
            LEFT JOIN ir_act_server ias ON ias.id = ic.ir_actions_server_id
            {where}
            ORDER BY ic.nextcall
        """).format(where=where_clause, failure_cols=failure_cols)
    )
    return [
        {
            "name": row[0],
            "interval": f"{row[1]} {row[2]}",
            "nextcall": str(row[3]),
            **({"code": (row[4] or "").strip() or None} if include_code else {}),
            **({"active": row[5]} if include_inactive else {}),
            **(
                {"failure_count": row[6], "first_failure_date": str(row[7]) if row[7] else None}
                if has_failure_tracking
                else {}
            ),
        }
        for row in cur.fetchall()
    ]


def _has_cron_progress(cur: psycopg.Cursor) -> bool:
    """Odoo 18+ only: ``ir.cron.progress`` — a row created on every cron
    execution attempt (``ir_cron._add_progress()``), reporting ``done``/
    ``remaining``/``timed_out_counter`` for batched long-running jobs.
    Verified absent in 14.0-17.0 and present in 18.0/19.0 upstream source
    (github.com/odoo/odoo, ``odoo/addons/base/models/ir_cron.py``); GC'd by
    Odoo's own autovacuum after 1 week, so there's no unbounded history here.
    """
    cur.execute("SELECT to_regclass('public.ir_cron_progress')")
    return _fetch_one(cur)[0] is not None


def get_running_crons(cur: psycopg.Cursor) -> list[dict]:
    """List crons currently held by an Odoo worker (RowShareLock on ir_cron).

    Odoo cron acquires the row with `SELECT ... FOR NO KEY UPDATE`, which
    writes the locking txn id into the row's `xmax` system column. We join
    pg_locks → pg_stat_activity → ir_cron via `xmax = backend_xid` to resolve
    the exact cron being executed (the current `query` text has moved on to
    the cron's workload by the time we look).

    On Odoo 18+, also surfaces the cron's most recent ``ir_cron_progress``
    row (``done``/``remaining``/``timed_out_counter``) via a LATERAL join on
    the highest ``id`` per ``cron_id`` — mirrors the ``last_cron_progress``
    CTE Odoo's own ``_acquire_one_job`` uses to resume batched jobs. Gives a
    real completion signal for a long-running job instead of just "a worker
    holds the lock". The three keys are omitted entirely pre-18. On 18+ they
    are always present but ``None`` when the cron has no ``ir_cron_progress``
    row yet — the probe only tests that the table exists, and the LATERAL is
    a LEFT join, so a cron that never reached ``_add_progress()`` still gets
    the keys with null values.
    """
    has_progress = _has_cron_progress(cur)
    progress_join = (
        sql.SQL("""
            LEFT JOIN LATERAL (
                SELECT done, remaining, timed_out_counter
                FROM ir_cron_progress
                WHERE cron_id = ic.id
                ORDER BY id DESC
                LIMIT 1
            ) cp ON true
        """)
        if has_progress
        else sql.SQL("")
    )
    progress_cols = sql.SQL(", cp.done, cp.remaining, cp.timed_out_counter") if has_progress else sql.SQL("")
    cur.execute(
        sql.SQL("""
            SELECT
                a.pid,
                ic.id AS cron_id,
                ic.cron_name,
                im.model,
                ias.code,
                a.state,
                a.query_start,
                a.usename,
                a.application_name,
                a.query
                {progress_cols}
            FROM pg_locks l
            JOIN pg_class c ON l.relation = c.oid
            JOIN pg_stat_activity a ON a.pid = l.pid
            LEFT JOIN ir_cron ic ON ic.xmax = a.backend_xid
            LEFT JOIN ir_act_server ias ON ias.id = ic.ir_actions_server_id
            LEFT JOIN ir_model im ON im.id = ias.model_id
            {progress_join}
            WHERE c.relname = 'ir_cron' AND l.mode = 'RowShareLock'
        """).format(progress_cols=progress_cols, progress_join=progress_join)
    )
    results: list[dict] = []
    for row in cur.fetchall():
        pid, cron_id, name, model, code, state, query_start, usename, app_name, query = row[:10]
        entry = {
            "pid": pid,
            "cron_id": cron_id,
            "name": name,
            "model": model,
            "code": (code or "").strip() or None,
            "state": state,
            "query_start": str(query_start) if query_start else None,
            "user": usename,
            "application": app_name,
            "query": (query or "").strip(),
        }
        if has_progress:
            done, remaining, timed_out_counter = row[10:13]
            entry["done"] = done
            entry["remaining"] = remaining
            entry["timed_out_counter"] = timed_out_counter
        results.append(entry)
    return results


def has_cron_failure_data(rows: list[dict]) -> bool:
    """Any row carries a `failure_count` key at all (Odoo 18+ probe result),
    regardless of value. Gates the `crons` prometheus gauge so it's omitted
    -- not emitted as 0 -- on a pre-18 database, where "no failing crons"
    and "not tracked at all" must not read the same. Pure over the fetched
    rows, same shape as `filter_online_users`.
    """
    return any("failure_count" in r for r in rows)


def has_tracked_cron_failures(rows: list[dict]) -> bool:
    """Any row shows a nonzero `failure_count`.

    Gates whether the failure_count/first_failure_date columns earn a place
    in the `crons` text table: on a healthy Odoo 18+ database every row is
    0, and two all-empty columns crowd out name/nextcall into wrapping.
    JSON output is unaffected -- callers there read has_cron_failure_data
    (or the raw keys) instead, since a stable key set is what makes
    cross-version bundles diffable.
    """
    return any(r.get("failure_count") for r in rows)


def has_running_cron_progress(rows: list[dict]) -> bool:
    """Any `--running` row carries real `ir_cron_progress` data (`done`/
    `remaining` not None) -- gates the running-crons text table's progress
    columns, same reasoning as `has_tracked_cron_failures`: the LATERAL
    join attaches the keys to every row on Odoo 18+ even when no cron in
    flight has ever reported progress, and three empty columns are worse
    than none.
    """
    return any(r.get("done") is not None or r.get("remaining") is not None for r in rows)


def get_jobs(cur: psycopg.Cursor) -> list[dict] | None:
    """Returns None if queue_job module not installed."""
    cur.execute(
        "SELECT 1 FROM ir_module_module WHERE name = ANY(%s) AND state = %s",
        (["connector", "queue_job"], "installed"),
    )
    if not cur.fetchone():
        return None
    cur.execute("SELECT state, count(*) FROM queue_job GROUP BY state ORDER BY state")
    return [{"state": row[0], "count": row[1]} for row in cur.fetchall()]


def get_users(cur: psycopg.Cursor, *, include_inactive: bool = False) -> list[dict]:
    cur.execute("SELECT tablename FROM pg_tables WHERE tablename IN ('mail_presence', 'bus_presence')")
    presence_tables = {row[0] for row in cur.fetchall()}

    where_clause = sql.SQL("") if include_inactive else sql.SQL("WHERE ru.active = TRUE")

    if "mail_presence" in presence_tables:
        # Odoo 19+: mail.presence with direct status column
        query = sql.SQL("""
            SELECT ru.login, rp.name, COALESCE(mp.status, 'offline') AS state, ru.active
            FROM res_users ru
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            LEFT JOIN mail_presence mp ON mp.user_id = ru.id
            {where}
            ORDER BY ru.login
        """)
    elif "bus_presence" in presence_tables:
        # Odoo 14-18: bus.presence.status is updated in real-time (HTTP and WebSocket)
        query = sql.SQL("""
            SELECT ru.login, rp.name, COALESCE(bp.status, 'offline') AS state, ru.active
            FROM res_users ru
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            LEFT JOIN bus_presence bp ON bp.user_id = ru.id
            {where}
            ORDER BY ru.login
        """)
    else:
        query = sql.SQL("""
            SELECT ru.login, rp.name, 'unknown' AS state, ru.active
            FROM res_users ru
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            {where}
            ORDER BY ru.login
        """)
    cur.execute(query.format(where=where_clause))
    return [
        {
            "login": row[0],
            "name": row[1] or "",
            "state": row[2],
            **({"active": bool(row[3])} if include_inactive else {}),
        }
        for row in cur.fetchall()
    ]


def filter_online_users(rows: list[dict]) -> list[dict]:
    """Keep only rows whose presence state is ``online``"""
    return [r for r in rows if r["state"] == "online"]


def get_users_by_year(cur: psycopg.Cursor) -> dict[int, int]:
    """Return ``{year: count}`` for active users grouped by ``create_date`` year.

    Aggregate only — no PII (no login/name/email). Designed for audit export
    so leads can ship the file without an NDA.
    """
    cur.execute("""
        SELECT EXTRACT(year FROM create_date)::int AS yr, count(*)
        FROM res_users
        WHERE active = true AND create_date IS NOT NULL
        GROUP BY yr
        ORDER BY yr
    """)
    return {row[0]: row[1] for row in cur.fetchall()}


def _localize(value: dict[str, str | None] | str | None, lang: str = "en_US") -> str:
    """Normalize a translated Char/Text field value.

    Odoo 16+ stores translated fields as jsonb (``{"en_US": "...", ...}``);
    older versions return the plain string directly. Falls back to any
    available translation if ``lang`` is missing or its value is falsy
    (e.g. ``{"en_US": None}``) — ``next(iter(value.values()), "")`` alone
    would still yield that ``None`` (the fallback default only fires on an
    *empty* iterator, not a falsy first value), which callers expecting a
    ``str`` (like ``rich``'s table renderer) would choke on.
    """
    if isinstance(value, dict):
        return value.get(lang) or next((v for v in value.values() if v), "")
    return value or ""


def _trans_implied(cur: psycopg.Cursor, seed_group_ids: list[int]) -> dict[int, set[int]]:
    """Transitive closure of ``res.groups.implied_ids`` for each seed group id.

    ``trans_implied_ids`` is compute-only on the Odoo side (not a column), so
    this walks ``res_groups_implied_rel`` (a row ``(gid, hid)`` means "group
    ``gid`` implies group ``hid``") with a recursive CTE. Returns
    ``{seed_id: {implied group ids, direct + transitive}}``. Note: if a cycle
    in the implied graph loops back to the seed, the seed's own id *can*
    appear in its own set — this isn't filtered out. The sole caller
    (``get_roles``) is unaffected either way, since it re-adds the seed via
    ``{group_id} | trans_implied(group_id)``, a set union that's a no-op if
    the seed is already present.
    """
    if not seed_group_ids:
        return {}
    cur.execute(
        """
        WITH RECURSIVE implied(seed, gid) AS (
            SELECT gid, hid FROM res_groups_implied_rel WHERE gid = ANY(%s)
            UNION
            SELECT i.seed, r.hid
            FROM res_groups_implied_rel r
            JOIN implied i ON r.gid = i.gid
        )
        SELECT DISTINCT seed, gid FROM implied
        """,
        (seed_group_ids,),
    )
    result: dict[int, set[int]] = {gid: set() for gid in seed_group_ids}
    for seed, gid in cur.fetchall():
        result[seed].add(gid)
    return result


def _groups_category_sql(cur: psycopg.Cursor) -> tuple[str, str]:
    """``(select_cols, join_sql)`` for the group -> category link, version-branched.

    Odoo 19 dropped ``res_groups.category_id`` in favor of an intermediate
    ``res_groups.privilege_id`` -> ``res_groups_privilege.category_id`` ->
    ``ir_module_category`` chain. Both branches select the same 4 columns
    (privilege id/name, category id/name) so every call site unpacks rows the
    same way regardless of version; ``privilege`` is simply ``NULL`` on
    <= 18.0. Keeping ``category`` mapped to ``ir_module_category`` on both
    versions matters — it keeps the JSON comparable across a v16 -> v19
    migration audit, which is the point of the tool.

    The probe resolves ``res_groups`` via ``to_regclass`` (same name
    resolution — respecting ``search_path`` — that the unqualified
    ``FROM res_groups`` in the real queries below gets) and checks
    ``pg_attribute`` directly, rather than scanning
    ``information_schema.columns`` by bare table name across every schema on
    the search path. The latter lets an unrelated schema with its own
    ``res_groups(privilege_id)`` column flip a <= 18.0 database into the 19.0
    branch, which then queries a nonexistent ``res_groups_privilege`` and
    crashes — a plantable DoS in a database being audited.
    """
    cur.execute("""
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('res_groups') AND attname = 'privilege_id' AND NOT attisdropped
    """)
    if cur.fetchone():  # 19.0+
        return (
            "p.id, p.name, mc.id, mc.name",
            "LEFT JOIN res_groups_privilege p ON p.id = g.privilege_id "
            "LEFT JOIN ir_module_category mc ON mc.id = p.category_id",
        )
    return (  # <= 18.0
        "NULL, NULL, mc.id, mc.name",
        "LEFT JOIN ir_module_category mc ON mc.id = g.category_id",
    )


def get_groups(
    cur: psycopg.Cursor, include_users: bool = False, include_acls: bool = False
) -> list[dict] | dict[str, list[dict]]:
    """List all ``res.groups`` with category, optionally members and ACLs.

    Plain ``list[dict]`` unless ``include_acls=True``, in which case returns
    ``{"groups": [...], "global_acls": [...], "global_rules": [...]}``
    instead. An ``ir.model.access``/``ir.rule`` row with no group at all
    (``group_id IS NULL`` / ``ir.rule.global = true``) grants/restricts
    *every* user — the highest-value rows in a permission audit (a model
    readable by everyone would otherwise look unreachable in the report).
    They can't be attributed to a single group's ``acls``, hence the
    top-level siblings rather than duplicating them into every group.
    """
    # select_cols/join_sql are one of the two fixed literal pairs returned by
    # _groups_category_sql — never user or row data, so the f-string is safe.
    # ty can't see that: it wants a LiteralString, which a dynamically-built
    # (but still internally-fixed) query string can never satisfy.
    select_cols, join_sql = _groups_category_sql(cur)
    # No SQL ORDER BY on name/category: both are jsonb on Odoo 16+ (translate=True),
    # so postgres would sort by key-count then key/value pairs — deterministic but
    # unrelated to the localized string a reader actually sees. Sort in Python below,
    # after _localize(), instead.
    cur.execute(f"""
        SELECT g.id, g.name, {select_cols}, g.comment, g.share
        FROM res_groups g
        {join_sql}
    """)  # noqa: S608  # ty: ignore[no-matching-overload]
    groups: list[dict] = [
        {
            "id": row[0],
            "name": _localize(row[1]),
            "privilege_id": row[2],
            "privilege": _localize(row[3]) if row[3] else None,
            "category_id": row[4],
            "category": _localize(row[5]) if row[5] is not None else None,
            "comment": _localize(row[6]) if row[6] else "",
            "share": bool(row[7]),
        }
        for row in cur.fetchall()
    ]
    groups.sort(key=lambda g: (g["category"] or "", g["name"] or ""))

    if include_users:
        # WHERE u.active = true mirrors get_roles' member query: archived users
        # drop from both group membership and role membership, so compute_role_drift
        # never sees an inactive user holding a role's marker group with no matching
        # role row (which would surface as phantom extra_groups drift), and
        # `groups --include-users` counts don't overstate live membership.
        cur.execute("""
            SELECT r.gid, u.login
            FROM res_groups_users_rel r
            JOIN res_users u ON u.id = r.uid
            WHERE u.active = true
            ORDER BY r.gid, u.login
        """)
        users_by_group: dict[int, list[str]] = {}
        for gid, login in cur.fetchall():
            users_by_group.setdefault(gid, []).append(login)
        for g in groups:
            g["users"] = users_by_group.get(g["id"], [])

    if not include_acls:
        return groups

    # No group_id filter: an access row with group_id IS NULL applies to
    # every user, and ima.active excludes archived (no longer enforced) rows.
    cur.execute("""
        SELECT ima.group_id, im.model, ima.perm_read, ima.perm_write, ima.perm_create, ima.perm_unlink
        FROM ir_model_access ima
        JOIN ir_model im ON im.id = ima.model_id
        WHERE ima.active = true
        ORDER BY ima.group_id, im.model
    """)
    access_by_group: dict[int | None, list[dict]] = {}
    for gid, model, perm_read, perm_write, perm_create, perm_unlink in cur.fetchall():
        access_by_group.setdefault(gid, []).append({
            "model": model,
            "read": bool(perm_read),
            "write": bool(perm_write),
            "create": bool(perm_create),
            "unlink": bool(perm_unlink),
        })
    global_acls = access_by_group.pop(None, [])

    # LEFT JOIN from ir_rule (not rule_group_rel) so a rule with no linked
    # group at all (ir_rule.global = true — applies regardless of the user's
    # groups) still surfaces, as a single NULL-group_id row, instead of
    # silently disappearing the way the prior INNER JOIN did.
    cur.execute("""
        SELECT rgr.group_id, r.name, im.model, r.domain_force,
               r.perm_read, r.perm_write, r.perm_create, r.perm_unlink
        FROM ir_rule r
        JOIN ir_model im ON im.id = r.model_id
        LEFT JOIN rule_group_rel rgr ON rgr.rule_group_id = r.id
        WHERE r.active = true
        ORDER BY rgr.group_id, im.model
    """)
    rules_by_group: dict[int | None, list[dict]] = {}
    for gid, name, model, domain_force, perm_read, perm_write, perm_create, perm_unlink in cur.fetchall():
        # ir_rule.name is plain varchar (not translate=True) on every version
        # checked, unlike res.groups.name/comment above — no _localize() needed.
        rules_by_group.setdefault(gid, []).append({
            "name": name,
            "model": model,
            "domain": domain_force,
            "read": bool(perm_read),
            "write": bool(perm_write),
            "create": bool(perm_create),
            "unlink": bool(perm_unlink),
        })
    global_rules = rules_by_group.pop(None, [])

    for g in groups:
        g["acls"] = {
            "model_access": access_by_group.get(g["id"], []),
            "rules": rules_by_group.get(g["id"], []),
        }

    return {"groups": groups, "global_acls": global_acls, "global_rules": global_rules}


def get_roles(cur: psycopg.Cursor, include_users: bool = False, include_groups: bool = False) -> list[dict] | None:
    """List ``res.users.role`` (OCA ``base_user_role``). ``None`` if the module isn't installed.

    ``res_users_role`` delegates (``_inherits``) to ``res.groups`` via
    ``group_id`` — name/category live on the joined ``res_groups`` row, not
    on ``res_users_role`` itself. A role's full granted-group set is
    ``{group_id} | trans_implied(group_id)``.
    """
    cur.execute(
        "SELECT 1 FROM ir_module_module WHERE name = %s AND state = %s",
        ("base_user_role", "installed"),
    )
    if not cur.fetchone():
        return None

    # select_cols/join_sql are one of the two fixed literal pairs returned by
    # _groups_category_sql — never user or row data, so the f-string is safe.
    # ty can't see that: it wants a LiteralString, which a dynamically-built
    # (but still internally-fixed) query string can never satisfy.
    select_cols, join_sql = _groups_category_sql(cur)
    # No SQL ORDER BY: g.name (res_groups.name) is jsonb on Odoo 16+
    # (translate=True) — see the same note in get_groups. Sort in Python below.
    cur.execute(f"""
        SELECT ur.id, ur.group_id, g.name, {select_cols}, g.comment
        FROM res_users_role ur
        JOIN res_groups g ON g.id = ur.group_id
        {join_sql}
    """)  # noqa: S608  # ty: ignore[no-matching-overload]
    roles: list[dict] = [
        {
            "id": row[0],
            "group_id": row[1],
            "name": _localize(row[2]),
            "privilege_id": row[3],
            "privilege": _localize(row[4]) if row[4] else None,
            "category_id": row[5],
            "category": _localize(row[6]) if row[6] is not None else None,
            "comment": _localize(row[7]) if row[7] else "",
        }
        for row in cur.fetchall()
    ]
    roles.sort(key=lambda r: r["name"] or "")

    if include_users:
        cur.execute("""
            SELECT rl.role_id, u.login
            FROM res_users_role_line rl
            JOIN res_users u ON u.id = rl.user_id
            WHERE u.active = true
              AND (rl.date_from IS NULL OR rl.date_from <= CURRENT_DATE)
              AND (rl.date_to IS NULL OR rl.date_to >= CURRENT_DATE)
            ORDER BY rl.role_id, u.login
        """)
        users_by_role: dict[int, list[str]] = {}
        for role_id, login in cur.fetchall():
            users_by_role.setdefault(role_id, []).append(login)
        for r in roles:
            r["users"] = users_by_role.get(r["id"], [])

    if include_groups:
        closure = _trans_implied(cur, [r["group_id"] for r in roles])
        all_gids = {r["group_id"] for r in roles} | {gid for gids in closure.values() for gid in gids}
        # select_cols/join_sql are one of the two fixed literal pairs returned by
        # _groups_category_sql — never user or row data, so the f-string is safe.
        # ty can't see that: it wants a LiteralString, which a dynamically-built
        # (but still internally-fixed) query string can never satisfy.
        cur.execute(
            f"""
            SELECT g.id, g.name, {select_cols}
            FROM res_groups g
            {join_sql}
            WHERE g.id = ANY(%s)
            """,  # noqa: S608  # ty: ignore[invalid-argument-type]
            (list(all_gids),),
        )
        # Group names collide across categories (e.g. "Manager", "User" each
        # appear in a dozen+ categories), so category must travel alongside
        # name for any name-based matching downstream to be meaningful.
        group_info = {
            row[0]: {
                "id": row[0],
                "name": _localize(row[1]),
                "privilege_id": row[2],
                "privilege": _localize(row[3]) if row[3] else None,
                "category_id": row[4],
                "category": _localize(row[5]) if row[5] is not None else None,
            }
            for row in cur.fetchall()
        }
        for r in roles:
            gids = {r["group_id"]} | closure.get(r["group_id"], set())
            r["groups"] = sorted(
                (group_info[gid] for gid in gids if gid in group_info),
                key=lambda x: x["id"],
            )

    return roles


def compute_role_drift(
    roles: list[dict], groups: list[dict], user_ids: dict[str, int], include_sensitive: bool = False
) -> list[dict]:
    """Diff each user's assigned roles against their actual ``res.groups`` membership.

    Pure function over the JSON shapes returned by ``get_roles(include_users=True,
    include_groups=True)`` and ``get_groups(include_users=True)`` — kept separate
    from ``get_role_drift`` so the diff logic is unit-testable without a cursor.

    For every user who has at least one role, or who holds a group that is
    some role's own marker group (see below), compares:

    - ``expected`` = union of the resolved group sets of the user's currently
      assigned roles (each role's ``groups`` already includes its transitive
      ``implied_ids`` closure, see ``_trans_implied``) — this is what a
      correctly-synced user with those roles should hold, implied groups
      included.
    - ``actual`` = groups the user is actually a member of.

    ``missing_groups`` is ``expected - actual``: granted by a role the user
    holds, but absent from their actual membership — typically a
    ``base_user_role`` sync that never ran (broken cron, direct SQL write,
    module upgrade).

    ``extra_groups`` needs a narrower universe than "any group in some role's
    resolved set": that set sweeps in near-universal baseline groups (e.g.
    "Internal User") that virtually every role implies transitively and that
    virtually every employee holds regardless of role, which would flag almost
    the entire user base. Instead the universe is each role's own ``group_id``
    (its exclusive marker group, *not* its implied closure) — the one thing
    ``base_user_role`` actually writes/removes on ``res.users.groups_id``; Odoo
    core cascades implied groups onto real membership rows on top of that.
    ``extra_groups`` is then (``actual`` ∩ marker-group-ids) ``- expected``:
    a role's marker the user physically holds, not covered even by the full
    closure of roles they're currently assigned (so a role whose closure
    legitimately implies another role's marker — e.g. an admin role implying
    a cashier role's marker group — is correctly not flagged). What's left is
    a role's marker present with no assigned-role explanation for it at all:
    typically a role that was removed/expired without revoking its group, or
    a group granted by hand bypassing the role framework.

    Users with neither are omitted; the return list only carries drift.
    Identity: keyed by ``user_id`` (from ``user_ids``, a ``login -> id``
    lookup) rather than ``login`` (usually an email) by default — unlike
    ``groups``/``roles``, this report is inherently per-user with no
    ``--include-users``-style opt-out, so it needs its own gate. ``login``
    is added alongside ``user_id`` only when ``include_sensitive=True``
    (the global ``--include-sensitive-information`` switch), matching the
    PII-redaction convention ``attachments`` already uses for filenames.
    """
    group_by_id = {group["id"]: group for group in groups}

    user_actual_groups: dict[str, set[int]] = {}
    for group in groups:
        for login in group["users"]:
            user_actual_groups.setdefault(login, set()).add(group["id"])

    role_group_ids = {r["id"]: {group["id"] for group in r["groups"]} for r in roles}
    marker_group_ids: set[int] = {r["group_id"] for r in roles}

    user_role_ids: dict[str, set[int]] = {}
    for r in roles:
        for login in r["users"]:
            user_role_ids.setdefault(login, set()).add(r["id"])

    relevant_logins = set(user_role_ids) | {
        login for login, gids in user_actual_groups.items() if gids & marker_group_ids
    }

    def _group_info(gid: int) -> dict:
        group = group_by_id.get(gid)
        return {"id": gid, "name": group["name"] if group else None, "category": group["category"] if group else None}

    role_name_by_id = {r["id"]: r["name"] for r in roles}
    drift: list[dict] = []
    for login in sorted(relevant_logins):
        rids = user_role_ids.get(login, set())
        expected: set[int] = set()
        for rid in rids:
            expected |= role_group_ids.get(rid, set())
        actual = user_actual_groups.get(login, set())

        missing_ids = expected - actual
        extra_ids = (actual & marker_group_ids) - expected
        if not missing_ids and not extra_ids:
            continue

        entry: dict = {
            "user_id": user_ids.get(login),
            "roles": sorted(role_name_by_id[rid] for rid in rids),
            "missing_groups": [_group_info(gid) for gid in sorted(missing_ids)],
            "extra_groups": [_group_info(gid) for gid in sorted(extra_ids)],
        }
        if include_sensitive:
            entry["login"] = login
        drift.append(entry)

    return drift


def get_role_drift(cur: psycopg.Cursor, include_sensitive: bool = False) -> list[dict] | None:
    """Detect drift between assigned ``res.users.role`` and actual ``res.groups`` membership.

    ``None`` if ``base_user_role`` isn't installed (same pattern as
    ``get_roles``/``get_jobs``). See ``compute_role_drift`` for the diff rules
    and the ``include_sensitive`` login-vs-user_id gate.
    """
    roles = get_roles(cur, include_users=True, include_groups=True)
    if roles is None:
        return None
    # include_acls defaults to False, so this is always list[dict] — ty can't
    # narrow that from a plain bool default without an @overload per call site.
    groups = get_groups(cur, include_users=True)
    cur.execute("SELECT login, id FROM res_users")
    user_ids = dict(cur.fetchall())
    return compute_role_drift(roles, groups, user_ids, include_sensitive=include_sensitive)  # ty: ignore[invalid-argument-type]


def get_stats(
    cur: psycopg.Cursor,
    years: int = 3,
    top: int = 20,
    model_owners: dict[str, str] | None = None,
) -> dict:
    # All Odoo tables (have create_date)
    cur.execute("""
        SELECT c.relname
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid
        WHERE n.nspname = 'public' AND a.attname = 'create_date' AND c.relkind = 'r'
        ORDER BY c.relname
    """)
    all_tables = [row[0] for row in cur.fetchall()]

    # Map table -> model name
    cur.execute("SELECT replace(model, '.', '_') AS tbl, model FROM ir_model")
    table_to_model = {row[0]: row[1] for row in cur.fetchall()}

    # Table sizes (top N by total size; top=0 means no limit)
    size_query = """
        SELECT relname,
            pg_total_relation_size(relid) AS total_bytes,
            pg_relation_size(relid) AS table_bytes
        FROM pg_statio_user_tables
        WHERE relname = ANY(%s)
        ORDER BY total_bytes DESC
    """
    size_params: list = [all_tables]
    if top > 0:
        size_query += " LIMIT %s"
        size_params.append(top)
    cur.execute(size_query, size_params)
    size_rows = cur.fetchall()
    top_tables = [row[0] for row in size_rows]

    # Year columns for the report
    cur.execute("SELECT EXTRACT(year FROM NOW())::int")
    current_year = _fetch_one(cur)[0]
    year_cols = list(range(current_year - years + 1, current_year + 1))

    # Per-table queries: year_counts + total count, with progress bar on stderr.
    # Skip count queries when the heap is 0 bytes — assume empty, save the
    # round-trip on databases with many empty tables.
    table_bytes_by_relname = {row[0]: row[2] for row in size_rows}
    table_year_counts: dict[str, dict[int, int]] = {}
    total_counts: dict[str, int] = {}
    for table in track(
        top_tables,
        description="Scanning tables",
        console=_progress_console,
        transient=True,
    ):
        if table_bytes_by_relname.get(table, 0) == 0:
            table_year_counts[table] = {}
            total_counts[table] = 0
            continue
        cur.execute(
            sql.SQL("""
                SELECT EXTRACT(year FROM create_date)::int AS yr, count(*)
                FROM {}
                WHERE create_date >= NOW() - make_interval(years => %s)
                GROUP BY yr
            """).format(sql.Identifier(table)),
            (years,),
        )
        table_year_counts[table] = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table)))
        total_counts[table] = _fetch_one(cur)[0]

    # Index sizes per table (sum all indexes per table)
    cur.execute(
        """
        SELECT i.relname AS table_name, sum(pg_relation_size(i.indexrelid)) AS index_bytes
        FROM pg_stat_all_indexes i
        JOIN pg_class c ON i.relid = c.oid
        WHERE i.schemaname NOT IN ('information_schema', 'pg_catalog', 'pg_toast', 'pg_logical')
        AND i.relname = ANY(%s)
        GROUP BY i.relname
    """,
        (top_tables,),
    )
    index_sizes = {row[0]: int(row[1]) for row in cur.fetchall()}

    # Attachment sizes per model (dedup by checksum)
    cur.execute("""
        WITH unique_attachments AS (
            SELECT res_model, file_size,
                row_number() OVER (PARTITION BY checksum ORDER BY id) AS rowno
            FROM ir_attachment
        )
        SELECT res_model, sum(file_size)
        FROM unique_attachments
        WHERE rowno = 1
        GROUP BY res_model
    """)
    # keyed by model name (dotted), convert to table name for lookup
    attachment_by_model = {row[0]: int(row[1]) for row in cur.fetchall()}
    attachment_sizes = {tbl: attachment_by_model.get(mdl, 0) for tbl, mdl in table_to_model.items()}

    # Total DB size
    cur.execute("SELECT pg_size_pretty(pg_database_size(current_database()))")
    db_size = _fetch_one(cur)[0]

    tables = []
    for relname, total_bytes, table_bytes in size_rows:
        tables.append({
            "table": relname,
            "model": table_to_model.get(relname, ""),
            "functional_group": _functional_group(relname, model_owners),
            "total_records": total_counts.get(relname, 0),
            "total_size_bytes": total_bytes,
            "table_size_bytes": table_bytes,
            "index_size_bytes": index_sizes.get(relname, 0),
            "attachment_size_bytes": attachment_sizes.get(relname, 0),
            "year_counts": {yr: table_year_counts.get(relname, {}).get(yr, 0) for yr in year_cols},
        })

    return {
        "db_size": db_size,
        "years": year_cols,
        "tables": tables,
    }


def get_not_odoo(cur: psycopg.Cursor) -> dict:
    # Views not tracked in ir_model
    cur.execute("""
        SELECT viewname
        FROM pg_views
        WHERE schemaname = 'public'
          AND viewname NOT IN (
            SELECT replace(model, '.', '_') FROM ir_model
          )
        ORDER BY viewname
    """)
    views = [row[0] for row in cur.fetchall()]

    # Triggers (Odoo never creates triggers; aggregate events per trigger+table)
    cur.execute("""
        SELECT trigger_name, event_object_table,
               string_agg(DISTINCT event_manipulation, '/' ORDER BY event_manipulation) AS events,
               action_timing
        FROM information_schema.triggers
        WHERE trigger_schema = 'public'
        GROUP BY trigger_name, event_object_table, action_timing
        ORDER BY event_object_table, trigger_name
    """)
    triggers = [{"name": row[0], "table": row[1], "events": row[2], "timing": row[3]} for row in cur.fetchall()]

    # Custom functions and procedures (exclude extension-provided ones like crosstab, dblink)
    cur.execute("""
        SELECT DISTINCT p.proname, p.prokind
        FROM pg_proc p
        JOIN pg_namespace n ON p.pronamespace = n.oid
        LEFT JOIN pg_depend d ON d.objid = p.oid AND d.deptype = 'e'
        LEFT JOIN pg_extension e ON e.oid = d.refobjid
        WHERE n.nspname = 'public'
          AND e.extname IS NULL
          AND p.prokind IN ('f', 'p')
        ORDER BY p.proname
    """)
    routines = cur.fetchall()
    functions = [row[0] for row in routines if row[1] == "f"]
    procedures = [row[0] for row in routines if row[1] == "p"]

    recognized = {
        "functions": {n: _RECOGNIZED_FUNCTIONS[n] for n in functions if n in _RECOGNIZED_FUNCTIONS},
        "triggers": {t["name"]: _RECOGNIZED_TRIGGERS[t["name"]] for t in triggers if t["name"] in _RECOGNIZED_TRIGGERS},
    }

    return {
        "views": views,
        "triggers": triggers,
        "functions": functions,
        "procedures": procedures,
        "recognized": recognized,
    }


def get_orphan_tables(
    stats_tables: list[dict], model_owners: dict[str, str], installed_modules: list[dict]
) -> list[dict]:
    """Return tables not owned by any currently-installed module.

    Two distinct reasons land a table here, kept on the same field so audit
    consumers see one authoritative "orphan" list, with ``reason`` discriminating
    follow-up actions:

    - ``uninstalled_module`` — ``model_owners`` resolves the table to a module
      that is **not** in the installed-module list. Tables left over from a
      module that was uninstalled without dropping its schema. Primary
      migration-cleanup target. Entry includes ``owner_module``.
    - ``no_ownership_data`` — the table has **no** entry in ``model_owners``.
      Typically legacy / raw-SQL / custom tables created outside Odoo's ORM,
      or tables from an older Odoo version with stale ``ir_model_data``.
      Consumers may apply a longest-prefix heuristic to attribute them to a
      best-guess module. ``owner_module`` is omitted (unknown by definition).

    Each entry also carries a ``functional_group`` (first underscore component
    of the table name). Not an owner — purely a bucket so the audit report can
    group e.g. ``purchase_order`` + ``purchase_order_line`` under ``purchase``
    when reasoning about functional areas.
    """
    installed = {m["name"] for m in installed_modules}
    orphans: list[dict] = []
    for t in stats_tables:
        table = t["table"]
        entry: dict = {"table": table}
        if table in model_owners:
            owner = model_owners[table]
            if owner in installed:
                continue
            entry["owner_module"] = owner
            entry["reason"] = "uninstalled_module"
        else:
            entry["reason"] = "no_ownership_data"
        entry["functional_group"] = _functional_group(table, model_owners)
        entry["total_records"] = t.get("total_records", 0)
        entry["total_size_bytes"] = t.get("total_size_bytes", 0)
        orphans.append(entry)
    return orphans


# Mimetype → coarse family, for the attachment audit's "by mimetype family"
# repartition. Order matters: first matching test wins. Mirrors the families a
# migration lead reasons about (where do the bytes go: images, PDFs, web
# assets, office docs, …) rather than the raw mimetype long tail.
_MIME_FAMILIES = (
    ("image", lambda m: m.startswith("image/")),
    ("pdf", lambda m: m == "application/pdf"),
    ("assets (css/js)", lambda m: "javascript" in m or m.endswith("/css")),
    (
        "office",
        lambda m: any(
            k in m
            for k in (
                "spreadsheet",
                "wordprocessing",
                "presentation",
                "ms-excel",
                "msword",
                "ms-powerpoint",
                "officedocument",
            )
        ),
    ),
    ("archive", lambda m: any(k in m for k in ("zip", "x-tar", "x-gzip", "x-7z"))),
    ("text", lambda m: m.startswith("text/")),
    ("xml/json", lambda m: m in ("application/xml", "application/json")),
)


def _mime_family(mimetype: str | None) -> str:
    m = (mimetype or "").lower()
    if not m:
        return "unknown"
    for name, test in _MIME_FAMILIES:
        if test(m):
            return name
    return "other"


def get_attachments_audit(
    cur: psycopg.Cursor,
    *,
    top_files: int = 20,
    validate_orphans: bool = False,
    orphan_top_models: int = 15,
    orphan_max_scan: int = 50000,
    include_filenames: bool = False,
) -> dict:
    """Read-only ``ir.attachment`` storage audit, computed entirely in SQL.

    Where the attachment weight sits (repartition) and what can realistically
    be deleted, archived, or offloaded (cleanup candidates). Only metadata and
    ``file_size`` are read — attachment payloads (``db_datas`` / ``datas``) are
    never touched.

    Two facts make the raw-SQL version both correct *and* simpler than going
    through the ORM:

    - **Field-backed rows are visible for free.** ``ir.attachment._search``
      auto-injects ``res_field = False`` (hiding ``image_1920``, company logos,
      report logos, signatures) unless the domain references ``res_field`` —
      and that filter applies to ``read_group`` too. A plain ``SELECT`` over
      the table has no such filter, so totals naturally count the whole table.
    - **``file_size`` is a stored column**, set to ``len(data)`` at write time
      by ``_get_datas_related_values`` *before* the storage backend is chosen.
      It is reliable on any backend (filestore, DB, S3-offloaded); only
      ``type='url'`` rows leave it NULL. So a ``sum(file_size)`` is real bytes,
      not a disk estimate.

    Storage split follows Odoo's own field semantics: a row lives in the DB iff
    ``db_datas`` is set, on the filestore iff ``store_fname`` is set (mutually
    exclusive per ``_get_datas_related_values``); rows with neither carry no
    binary payload (url type, or empty).

    PII: ``include_filenames`` gates the per-file ``name`` column in
    ``top_files`` only — every other field is aggregate / non-identifying. Keep
    it ``False`` for any export that leaves the customer's box.
    """
    data: dict = {}

    cur.execute("SELECT count(*), COALESCE(sum(file_size), 0) FROM ir_attachment")
    total_count, total_size = _fetch_one(cur)
    data["total_count"] = total_count
    data["total_size_bytes"] = int(total_size)

    # by type (binary vs url vs empty)
    cur.execute("""
        SELECT COALESCE(type, 'unknown'), count(*), COALESCE(sum(file_size), 0)
        FROM ir_attachment GROUP BY type ORDER BY 3 DESC
    """)
    data["by_type"] = [{"type": r[0], "count": r[1], "size": int(r[2])} for r in cur.fetchall()]

    # storage location + public, in one pass
    cur.execute("""
        SELECT
            count(*) FILTER (WHERE db_datas IS NOT NULL),
            COALESCE(sum(file_size) FILTER (WHERE db_datas IS NOT NULL), 0),
            count(*) FILTER (WHERE store_fname IS NOT NULL),
            COALESCE(sum(file_size) FILTER (WHERE store_fname IS NOT NULL), 0),
            count(*) FILTER (WHERE db_datas IS NULL AND store_fname IS NULL),
            COALESCE(sum(file_size) FILTER (WHERE db_datas IS NULL AND store_fname IS NULL), 0),
            count(*) FILTER (WHERE public),
            COALESCE(sum(file_size) FILTER (WHERE public), 0)
        FROM ir_attachment
    """)
    r = _fetch_one(cur)
    data["storage"] = {
        "db": {"count": r[0], "size": int(r[1])},
        "filestore": {"count": r[2], "size": int(r[3])},
        "none": {"count": r[4], "size": int(r[5])},
    }
    data["public"] = {"count": r[6], "size": int(r[7])}

    # field-backed vs standalone
    cur.execute("""
        SELECT
            count(*) FILTER (WHERE res_field IS NOT NULL),
            COALESCE(sum(file_size) FILTER (WHERE res_field IS NOT NULL), 0),
            count(*) FILTER (WHERE res_field IS NULL),
            COALESCE(sum(file_size) FILTER (WHERE res_field IS NULL), 0)
        FROM ir_attachment
    """)
    r = _fetch_one(cur)
    data["by_field"] = {
        "field_backed": {"count": r[0], "size": int(r[1])},
        "standalone": {"count": r[2], "size": int(r[3])},
    }

    # by res_model (heaviest first)
    cur.execute("""
        SELECT COALESCE(res_model, ''), count(*), COALESCE(sum(file_size), 0)
        FROM ir_attachment GROUP BY res_model ORDER BY 3 DESC
    """)
    by_model = [{"model": r[0], "count": r[1], "size": int(r[2])} for r in cur.fetchall()]
    data["by_model"] = by_model

    # by mimetype, bucketed into families in Python
    cur.execute("""
        SELECT mimetype, count(*), COALESCE(sum(file_size), 0)
        FROM ir_attachment GROUP BY mimetype
    """)
    fam: dict[str, dict[str, int]] = {}
    for mimetype, cnt, size in cur.fetchall():
        agg = fam.setdefault(_mime_family(mimetype), {"count": 0, "size": 0})
        agg["count"] += cnt
        agg["size"] += int(size)
    data["by_mime"] = [{"family": k, **v} for k, v in sorted(fam.items(), key=lambda kv: kv[1]["size"], reverse=True)]

    # by create_date year
    cur.execute("""
        SELECT EXTRACT(year FROM create_date)::int AS yr, count(*), COALESCE(sum(file_size), 0)
        FROM ir_attachment WHERE create_date IS NOT NULL GROUP BY yr ORDER BY yr
    """)
    data["by_year"] = {str(r[0]): {"count": r[1], "size": int(r[2])} for r in cur.fetchall()}

    # size distribution — where the bytes physically concentrate (0-byte rows
    # excluded; they are url/empty placeholders)
    cur.execute("""
        SELECT
            count(*) FILTER (WHERE file_size > 0 AND file_size <= 10240),
            COALESCE(sum(file_size) FILTER (WHERE file_size > 0 AND file_size <= 10240), 0),
            count(*) FILTER (WHERE file_size > 10240 AND file_size <= 102400),
            COALESCE(sum(file_size) FILTER (WHERE file_size > 10240 AND file_size <= 102400), 0),
            count(*) FILTER (WHERE file_size > 102400 AND file_size <= 1048576),
            COALESCE(sum(file_size) FILTER (WHERE file_size > 102400 AND file_size <= 1048576), 0),
            count(*) FILTER (WHERE file_size > 1048576 AND file_size <= 10485760),
            COALESCE(sum(file_size) FILTER (WHERE file_size > 1048576 AND file_size <= 10485760), 0),
            count(*) FILTER (WHERE file_size > 10485760),
            COALESCE(sum(file_size) FILTER (WHERE file_size > 10485760), 0)
        FROM ir_attachment
    """)
    r = _fetch_one(cur)
    labels = ["<= 10 KB", "10-100 KB", "100 KB-1 MB", "1-10 MB", "> 10 MB"]
    data["size_buckets"] = [
        {"label": labels[i], "count": r[2 * i], "size": int(r[2 * i + 1])} for i in range(len(labels))
    ]

    # largest single attachments (file_size>0 to skip url/empty NULLs). The
    # name column is PII, so it is only selected when explicitly requested.
    if include_filenames:
        cur.execute(
            "SELECT name, res_model, mimetype, file_size FROM ir_attachment "
            "WHERE file_size > 0 ORDER BY file_size DESC LIMIT %s",
            (top_files,),
        )
    else:
        cur.execute(
            "SELECT res_model, mimetype, file_size FROM ir_attachment "
            "WHERE file_size > 0 ORDER BY file_size DESC LIMIT %s",
            (top_files,),
        )
    top = []
    for row in cur.fetchall():
        entry: dict = {}
        if include_filenames:
            entry["name"] = row[0]
            res_model, mimetype, file_size = row[1], row[2], row[3]
        else:
            res_model, mimetype, file_size = row[0], row[1], row[2]
        entry["res_model"] = res_model or ""
        entry["mimetype"] = (mimetype or "").split(";")[0]
        entry["size"] = int(file_size or 0)
        top.append(entry)
    data["top_files"] = top

    # config governing storage + image policy (missing key = Odoo default)
    cur.execute("""
        SELECT key, value FROM ir_config_parameter
        WHERE key IN (
            'ir_attachment.location',
            'base.image_autoresize_max_px',
            'base.image_autoresize_quality'
        )
    """)
    params = {row[0]: row[1] for row in cur.fetchall()}
    data["config"] = {
        "location": params.get("ir_attachment.location", "file (default)"),
        "image_max": params.get("base.image_autoresize_max_px", "1920x1920 (default)"),
        "image_quality": params.get("base.image_autoresize_quality", "80 (default)"),
    }

    data["candidates"] = _attachment_candidates(cur, by_model, data["by_year"], data["storage"])

    data["orphans_validated"] = None
    if validate_orphans:
        data["orphans_validated"] = _validate_attachment_orphans(
            cur, by_model, top_n=orphan_top_models, max_scan=orphan_max_scan
        )

    return data


def _dup_stats(cur: psycopg.Cursor, db_only: bool) -> dict:
    """Duplicate-checksum surplus: extra rows and the bytes they represent.

    Filestore keeps ONE physical file per checksum (``_file_write`` SHA1 path),
    so filestore duplicates reclaim ``ir_attachment`` rows but ~0 disk. Real
    byte reclaim comes from DB-stored duplicates only — hence the ``db_only``
    split, so the headline disk number stays honest. Per-group surplus bytes
    are ``group_size * (count - 1) / count`` (integer division, matching the
    reference implementation).
    """
    if db_only:
        cur.execute("""
            SELECT count(*), COALESCE(sum(c - 1), 0), COALESCE(sum((grp_size * (c - 1)) / c), 0)
            FROM (
                SELECT count(*) AS c, sum(file_size) AS grp_size
                FROM ir_attachment WHERE checksum IS NOT NULL AND db_datas IS NOT NULL
                GROUP BY checksum HAVING count(*) > 1
            ) g
        """)
    else:
        cur.execute("""
            SELECT count(*), COALESCE(sum(c - 1), 0), COALESCE(sum((grp_size * (c - 1)) / c), 0)
            FROM (
                SELECT count(*) AS c, sum(file_size) AS grp_size
                FROM ir_attachment WHERE checksum IS NOT NULL
                GROUP BY checksum HAVING count(*) > 1
            ) g
        """)
    groups, rows, size = _fetch_one(cur)
    return {"groups": groups, "rows": rows, "size": int(size)}


def _attachment_candidates(
    cur: psycopg.Cursor,
    by_model: list[dict],
    by_year: dict[str, dict],
    storage: dict,
) -> dict:
    """Cheap cleanup buckets, ordered safest → needs-judgement."""
    cand: dict = {}

    cur.execute("SELECT model FROM ir_model")
    valid = {row[0] for row in cur.fetchall()}

    # 1. uninstalled-model orphans: res_model not registered in ir_model
    orphan_models = [m for m in by_model if m["model"] and m["model"] not in valid]
    cand["orphan_uninstalled"] = {
        "count": sum(m["count"] for m in orphan_models),
        "size": sum(m["size"] for m in orphan_models),
        "rows": sorted(orphan_models, key=lambda m: m["size"], reverse=True),
        "note": (
            "res_model not registered in ir_model (module uninstalled or typo). "
            "Safe to delete once the module is confirmed gone for good."
        ),
    }

    # 2. regenerable web asset bundles — the exact domain Odoo core's own
    # ir.attachment.regenerate_assets_bundles() unlinks; the server rebuilds
    # them on demand.
    cur.execute("""
        SELECT count(*), COALESCE(sum(file_size), 0)
        FROM ir_attachment
        WHERE public = true AND url LIKE '/web/assets/%'
          AND res_model = 'ir.ui.view' AND res_id = 0
    """)
    a_count, a_size = _fetch_one(cur)
    cand["assets"] = {
        "count": a_count,
        "size": int(a_size),
        "note": (
            "Compiled /web/assets/* bundles. Exactly what Odoo's own "
            "regenerate_assets_bundles() deletes; the server rebuilds them on "
            "demand. Pure win."
        ),
    }

    # 3. duplicates (same checksum)
    overall = _dup_stats(cur, db_only=False)
    db_dups = _dup_stats(cur, db_only=True)
    cand["duplicates"] = {
        "count": overall["rows"],
        "size": db_dups["size"],
        "groups": overall["groups"],
        "logical_size": overall["size"],
        "db_reclaim": db_dups["size"],
        "db_rows": db_dups["rows"],
        "note": (
            "Rows sharing a checksum. Filestore stores one physical file per "
            "checksum, so filestore dups reclaim ir_attachment rows but ~0 disk. "
            "Real byte reclaim comes from db-stored dups only."
        ),
    }

    # cutoff = start of the second-newest year with data
    years_sorted = sorted((y for y in by_year if y.isdigit()), reverse=True)
    cutoff = f"{years_sorted[1]}-01-01" if len(years_sorted) >= 2 else None

    # 4. aged transient sets
    aged_rows: dict[str, dict] = {}
    if cutoff:
        cur.execute(
            "SELECT count(*), COALESCE(sum(file_size), 0) FROM ir_attachment "
            "WHERE res_model = 'mail.message' AND create_date < %s",
            (cutoff,),
        )
        c, s = _fetch_one(cur)
        aged_rows["mail.message"] = {"count": c, "size": int(s)}
        cur.execute(
            "SELECT count(*), COALESCE(sum(file_size), 0) FROM ir_attachment "
            "WHERE mimetype = 'application/pdf' AND create_date < %s",
            (cutoff,),
        )
        c, s = _fetch_one(cur)
        aged_rows["old PDFs"] = {"count": c, "size": int(s)}
    cand["aged"] = {
        "cutoff": cutoff,
        "rows": aged_rows,
        "note": (
            f"Created before {cutoff}. Candidates to archive off-box, not necessarily delete."
            if cutoff
            else "Not enough year history."
        ),
    }

    # 4b. archive-by-age per heaviest model — "archive?" turned into tonnage
    archive_rows: list[dict] = []
    if cutoff:
        heavy = [m["model"] for m in by_model[:10] if m["model"]]
        if heavy:
            cur.execute(
                "SELECT res_model, count(*), COALESCE(sum(file_size), 0) "
                "FROM ir_attachment WHERE res_model = ANY(%s) AND create_date < %s "
                "GROUP BY res_model",
                (heavy, cutoff),
            )
            aged_by_model = {r[0]: {"count": r[1], "size": int(r[2])} for r in cur.fetchall()}
            totals = {m["model"]: m["size"] for m in by_model}
            for model in heavy:
                a = aged_by_model.get(model)
                if a and a["size"] > 0:
                    archive_rows.append({
                        "model": model,
                        "count": a["count"],
                        "size": a["size"],
                        "total": totals.get(model, 0),
                    })
            archive_rows.sort(key=lambda r: r["size"], reverse=True)
    cand["archive_by_model"] = {
        "cutoff": cutoff,
        "rows": archive_rows,
        "note": (
            f"Of each heavy model's attachments, how much predates {cutoff} — "
            "the concrete tonnage movable to cold/off-box storage."
            if cutoff
            else "Not enough year history."
        ),
    }

    # 5. db-stored bulk (the location=db → file migration win)
    cand["db_bulk"] = {
        "count": storage["db"]["count"],
        "size": storage["db"]["size"],
        "note": (
            "Binaries stored in the DB (db_datas set). Migrating "
            "ir_attachment.location from 'db' to 'file' moves these to the "
            "filestore and is usually the single biggest DB-size reduction."
        ),
    }

    return cand


def _validate_attachment_orphans(
    cur: psycopg.Cursor,
    by_model: list[dict],
    *,
    top_n: int,
    max_scan: int,
) -> list[dict]:
    """For the top-N heaviest models, find attachments whose ``res_id`` no
    longer points to a live record (the strongest delete signal).

    Resolves each model's table via the ``replace('.', '_')`` convention and
    skips it if no such table exists (abstract model, table-name override) or
    if its attachment count exceeds ``max_scan``. The dead-row test is a single
    indexed ``LEFT JOIN ... WHERE t.id IS NULL`` per model — cheap in SQL,
    unlike the per-id existence batches the ORM version needs.
    """
    # Models registered in ir_model (dotted form, e.g. "sale.order"). Skip
    # res_model values that point nowhere — uninstalled module, typo, generic
    # placeholder. Resolving them to a table would either fail or hit the
    # wrong table.
    cur.execute("SELECT model FROM ir_model")
    registered_models = {row[0] for row in cur.fetchall()}
    cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    real_tables = {row[0] for row in cur.fetchall()}

    findings: list[dict] = []
    scanned = 0
    for m in by_model:
        if scanned >= top_n:
            break
        model = m["model"]
        if not model or model not in registered_models:
            continue
        table = model.replace(".", "_")
        if table not in real_tables:
            continue
        scanned += 1
        if m["count"] > max_scan:
            findings.append({
                "model": model,
                "skipped": True,
                "reason": f"{m['count']:,} attachments > max-scan {max_scan:,}",
            })
            continue
        cur.execute(
            sql.SQL("""
                SELECT
                    count(*) FILTER (WHERE a.res_id IS NOT NULL AND a.res_id <> 0),
                    count(*) FILTER (WHERE a.res_id IS NOT NULL AND a.res_id <> 0 AND t.id IS NULL),
                    COALESCE(sum(a.file_size) FILTER (
                        WHERE a.res_id IS NOT NULL AND a.res_id <> 0 AND t.id IS NULL), 0)
                FROM ir_attachment a
                LEFT JOIN {} t ON t.id = a.res_id
                WHERE a.res_model = %s
            """).format(sql.Identifier(table)),
            (model,),
        )
        checked, dead, dead_size = _fetch_one(cur)
        findings.append({"model": model, "checked": checked, "dead_count": dead, "dead_size": int(dead_size)})
    return findings


def _as_str(val: object) -> str:
    """Coerce a value to str, handling Odoo 16+ JSONB translated fields.

    Odoo 16+ stores translatable Char/Text fields as JSONB inline in the row
    (e.g. ``{"en_US": "Sale Order"}``). psycopg3 deserialises these as dicts.
    We take the first value; fall back to empty string when the dict is empty.
    """
    if isinstance(val, dict):
        return str(next(iter(val.values()), ""))
    return str(val) if val is not None else ""


def _build_menu_path(
    menu_id: int,
    all_menus: dict[int, tuple[object, int | None]],
    depth: int = 0,
) -> str:
    if depth > 12 or menu_id not in all_menus:
        return "?"
    name, parent_id = all_menus[menu_id]
    label = _as_str(name)
    if parent_id:
        return _build_menu_path(parent_id, all_menus, depth + 1) + " > " + label
    return label


def _studio_records_detail(
    cur: psycopg.Cursor,
) -> dict[str, list[dict]]:
    """Return per-record detail for every ir_model_data row where studio=true.

    Result: ``{odoo_model: [record_dict, ...]}`` ordered by model then record.
    Each record dict always contains ``xmlid``. Extra keys depend on model type;
    unknown model types fall back to ``{"xmlid": ..., "res_id": ...}``.
    """
    cur.execute("""
        SELECT model, res_id, module || '.' || name AS xmlid
        FROM ir_model_data
        WHERE studio = true
        ORDER BY model, id
    """)
    by_model: dict[str, list[tuple[int, str]]] = {}
    for model, res_id, xmlid in cur.fetchall():
        by_model.setdefault(model, []).append((res_id, xmlid))

    result: dict[str, list[dict]] = {}

    for odoo_model, rows in by_model.items():
        ids = [r[0] for r in rows]
        xmlids = {r[0]: r[1] for r in rows}

        if odoo_model == "ir.model.fields":
            cur.execute(
                """
                SELECT f.id, f.name, f.ttype, f.field_description, m.model
                FROM ir_model_fields f
                JOIN ir_model m ON f.model_id = m.id
                WHERE f.id = ANY(%s)
                ORDER BY m.model, f.name
                """,
                (ids,),
            )
            result[odoo_model] = [
                {
                    "xmlid": xmlids[r[0]],
                    "field": r[1],
                    "ttype": r[2],
                    "label": _as_str(r[3]),
                    "on_model": r[4],
                }
                for r in cur.fetchall()
            ]

        elif odoo_model == "ir.ui.view":
            cur.execute(
                """
                SELECT v.id, v.name, v.model, v.type, v.mode
                FROM ir_ui_view v
                WHERE v.id = ANY(%s)
                ORDER BY v.model, v.name
                """,
                (ids,),
            )
            result[odoo_model] = [
                {
                    "xmlid": xmlids[r[0]],
                    "name": r[1],
                    "model": r[2],
                    "view_type": r[3],
                    "mode": r[4],
                }
                for r in cur.fetchall()
            ]

        elif odoo_model == "ir.actions.act_window":
            cur.execute(
                """
                SELECT a.id, a.name, a.res_model, bm.model AS binding_model
                FROM ir_act_window a
                LEFT JOIN ir_model bm ON a.binding_model_id = bm.id
                WHERE a.id = ANY(%s)
                ORDER BY a.res_model, a.name
                """,
                (ids,),
            )
            result[odoo_model] = [
                {
                    "xmlid": xmlids[r[0]],
                    "name": _as_str(r[1]),
                    "model": r[2],
                    "binding": r[3],
                }
                for r in cur.fetchall()
            ]

        elif odoo_model == "ir.actions.server":
            cur.execute(
                """
                SELECT ias.id, ias.name, bm.model AS bound_to, ias.state
                FROM ir_act_server ias
                LEFT JOIN ir_model bm ON ias.binding_model_id = bm.id
                WHERE ias.id = ANY(%s)
                ORDER BY bm.model, ias.name
                """,
                (ids,),
            )
            result[odoo_model] = [
                {
                    "xmlid": xmlids[r[0]],
                    "name": _as_str(r[1]),
                    "bound_to": r[2],
                    "action_type": r[3],
                }
                for r in cur.fetchall()
            ]

        elif odoo_model == "ir.model":
            cur.execute(
                """
                SELECT m.id, m.model, m.name
                FROM ir_model m
                WHERE m.id = ANY(%s)
                ORDER BY m.model
                """,
                (ids,),
            )
            result[odoo_model] = [{"xmlid": xmlids[r[0]], "model": r[1], "name": _as_str(r[2])} for r in cur.fetchall()]

        elif odoo_model == "ir.ui.menu":
            # Fetch entire menu tree to build full paths in Python.
            cur.execute("SELECT id, name, parent_id FROM ir_ui_menu")
            all_menus: dict[int, tuple[object, int | None]] = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
            cur.execute(
                "SELECT id, name FROM ir_ui_menu WHERE id = ANY(%s) ORDER BY id",
                (ids,),
            )
            result[odoo_model] = [
                {
                    "xmlid": xmlids[r[0]],
                    "name": _as_str(r[1]),
                    "full_path": _build_menu_path(r[0], all_menus),
                }
                for r in cur.fetchall()
            ]

        elif odoo_model == "ir.model.access":
            cur.execute(
                """
                SELECT acc.id, acc.name, m.model, g.name AS group_name,
                    acc.perm_read, acc.perm_write, acc.perm_create, acc.perm_unlink
                FROM ir_model_access acc
                JOIN ir_model m ON acc.model_id = m.id
                LEFT JOIN res_groups g ON acc.group_id = g.id
                WHERE acc.id = ANY(%s)
                ORDER BY m.model, acc.name
                """,
                (ids,),
            )
            result[odoo_model] = [
                {
                    "xmlid": xmlids[r[0]],
                    "name": r[1],
                    "model": r[2],
                    "group": _as_str(r[3]) if r[3] is not None else None,
                    "perms": (
                        ("r" if r[4] else "") + ("w" if r[5] else "") + ("c" if r[6] else "") + ("d" if r[7] else "")
                    ),
                }
                for r in cur.fetchall()
            ]

        elif odoo_model == "ir.model.inherit":
            cur.execute("SELECT 1 FROM pg_tables WHERE tablename = 'ir_model_inherit'")
            if cur.fetchone():
                cur.execute(
                    """
                    SELECT inh.id, child.model AS child_model, parent.model AS inherits_from
                    FROM ir_model_inherit inh
                    JOIN ir_model child ON inh.model_id = child.id
                    JOIN ir_model parent ON inh.parent_id = parent.id
                    WHERE inh.id = ANY(%s)
                    ORDER BY child.model
                    """,
                    (ids,),
                )
                result[odoo_model] = [
                    {
                        "xmlid": xmlids[r[0]],
                        "child_model": r[1],
                        "inherits_from": r[2],
                    }
                    for r in cur.fetchall()
                ]
            else:
                result[odoo_model] = [{"xmlid": xmlids[i], "res_id": i} for i in ids]

        elif odoo_model == "ir.default":
            cur.execute(
                """
                SELECT d.id, m.model, f.name AS field_name, d.json_value
                FROM ir_default d
                JOIN ir_model_fields f ON d.field_id = f.id
                JOIN ir_model m ON f.model_id = m.id
                WHERE d.id = ANY(%s)
                ORDER BY m.model, f.name
                """,
                (ids,),
            )
            result[odoo_model] = [
                {
                    "xmlid": xmlids[r[0]],
                    "model": r[1],
                    "field": r[2],
                    "value": r[3],
                }
                for r in cur.fetchall()
            ]

        elif odoo_model == "base.automation":
            cur.execute(
                """
                SELECT ba.id, ba.name, m.model AS on_model, ba.trigger
                FROM base_automation ba
                JOIN ir_model m ON ba.model_id = m.id
                WHERE ba.id = ANY(%s)
                ORDER BY m.model, ba.name
                """,
                (ids,),
            )
            result[odoo_model] = [
                {
                    "xmlid": xmlids[r[0]],
                    "name": _as_str(r[1]),
                    "model": r[2],
                    "trigger": r[3],
                }
                for r in cur.fetchall()
            ]

        elif odoo_model == "ir.rule":
            cur.execute(
                """
                SELECT r.id, r.name, m.model AS on_model
                FROM ir_rule r
                JOIN ir_model m ON r.model_id = m.id
                WHERE r.id = ANY(%s)
                ORDER BY m.model, r.name
                """,
                (ids,),
            )
            result[odoo_model] = [{"xmlid": xmlids[r[0]], "name": _as_str(r[1]), "model": r[2]} for r in cur.fetchall()]

        elif odoo_model == "ir.module.module":
            cur.execute(
                """
                SELECT mod.id, mod.name, mod.state
                FROM ir_module_module mod
                WHERE mod.id = ANY(%s)
                ORDER BY mod.name
                """,
                (ids,),
            )
            result[odoo_model] = [{"xmlid": xmlids[r[0]], "module": r[1], "state": r[2]} for r in cur.fetchall()]

        else:
            result[odoo_model] = [{"xmlid": xmlids[i], "res_id": i} for i in ids]

    return result


def get_studio_customizations(cur: psycopg.Cursor) -> dict:
    """Return Studio customization stats via raw SQL.

    Four sub-queries:
    1. custom_models — ir_model WHERE state='manual', enriched with mixin list
    2. studio_records_by_type — detailed records from ir_model_data WHERE studio=true,
       grouped by Odoo model with per-record detail (only when web_studio installed)
    3. extended_models — custom fields (with full detail) added to existing models
    4. mixins — which abstract models each custom model inherits from (mail.thread, etc.)
    """
    cur.execute("""
        SELECT column_name FROM information_schema.columns
        WHERE table_name = 'ir_model_data' AND column_name = 'studio'
    """)
    has_studio_col = bool(cur.fetchone())

    # Custom models
    cur.execute("""
        SELECT m.model, m.name,
            count(f.id) FILTER (WHERE f.state = 'manual') AS custom_fields
        FROM ir_model m
        LEFT JOIN ir_model_fields f ON f.model_id = m.id
        WHERE m.state = 'manual'
        GROUP BY m.model, m.name
        ORDER BY m.model
    """)
    custom_models = [{"model": r[0], "name": _as_str(r[1]), "custom_fields": r[2]} for r in cur.fetchall()]

    # Mixins per custom model (mail.thread, mail.activity.mixin, etc.)
    cur.execute("""
        SELECT DISTINCT child.model, parent.model AS mixin
        FROM ir_model_inherit inh
        JOIN ir_model child ON inh.model_id = child.id
        JOIN ir_model parent ON inh.parent_id = parent.id
        WHERE child.state = 'manual'
        ORDER BY child.model, parent.model
    """)
    mixins_by_model: dict[str, list[str]] = {}
    for child_model, mixin in cur.fetchall():
        mixins_by_model.setdefault(child_model, []).append(mixin)
    for m in custom_models:
        m["mixins"] = mixins_by_model.get(m["model"], [])

    records_by_type: dict[str, list[dict]] = {}
    if has_studio_col:
        records_by_type = _studio_records_detail(cur)

    # Extended models — full field detail grouped by model.
    # has_studio_tracking: field is recorded in ir_model_data with studio=True
    # (fields without tracking were added outside Studio UI — RPC, XML, import…).
    # The ir_model_data.studio column only exists once web_studio is installed,
    # so fall back to a constant `false` when it is absent.
    if has_studio_col:
        cur.execute("""
            SELECT m.model, f.name, f.ttype, f.field_description,
                f.relation, f.required, f.readonly, f.store,
                EXISTS (
                    SELECT 1 FROM ir_model_data imd
                    WHERE imd.model = 'ir.model.fields'
                      AND imd.res_id = f.id
                      AND imd.studio = true
                ) AS has_studio_tracking
            FROM ir_model_fields f
            JOIN ir_model m ON f.model_id = m.id
            WHERE f.state = 'manual' AND m.state != 'manual'
            ORDER BY m.model, f.name
        """)
    else:
        cur.execute("""
            SELECT m.model, f.name, f.ttype, f.field_description,
                f.relation, f.required, f.readonly, f.store,
                false AS has_studio_tracking
            FROM ir_model_fields f
            JOIN ir_model m ON f.model_id = m.id
            WHERE f.state = 'manual' AND m.state != 'manual'
            ORDER BY m.model, f.name
        """)
    extended_by_model: dict[str, list[dict]] = {}
    for r in cur.fetchall():
        extended_by_model.setdefault(r[0], []).append({
            "name": r[1],
            "ttype": r[2],
            "label": _as_str(r[3]),
            "relation": r[4] or "",
            "required": bool(r[5]),
            "readonly": bool(r[6]),
            "store": bool(r[7]),
            "has_studio_tracking": bool(r[8]),
        })
    extended_models = [
        {"model": m, "added_fields": len(fields), "fields": fields} for m, fields in extended_by_model.items()
    ]

    return {
        "custom_model_count": len(custom_models),
        "custom_models": custom_models,
        "studio_records_by_type": records_by_type,
        "extended_model_count": len(extended_models),
        "extended_models": extended_models,
    }


def get_orphan_fields(cur: psycopg.Cursor) -> list[dict]:
    """Return DB columns that exist in Odoo model tables but have no ir_model_fields entry.

    These are ghost columns — left behind by uninstalled modules or direct SQL DDL.
    Only checks tables whose name matches an ir_model entry (replace('.','_')).
    Excludes ORM meta-columns always present in every table.
    """
    cur.execute("""
        WITH model_cols AS (
            SELECT replace(m.model, '.', '_') AS tbl, f.name AS col
            FROM ir_model_fields f
            JOIN ir_model m ON m.id = f.model_id
            WHERE f.ttype NOT IN ('one2many', 'many2many')
        ),
        odoo_tables AS (
            SELECT replace(model, '.', '_') AS tbl FROM ir_model
        )
        SELECT c.table_name, c.column_name, c.data_type
        FROM information_schema.columns c
        JOIN odoo_tables ot ON ot.tbl = c.table_name
        LEFT JOIN model_cols mc ON mc.tbl = c.table_name AND mc.col = c.column_name
        WHERE c.table_schema = 'public'
          AND mc.col IS NULL
          AND c.column_name NOT IN (
              'id', 'create_uid', 'create_date', 'write_uid', 'write_date'
          )
        ORDER BY c.table_name, c.column_name
    """)
    return [{"table": r[0], "column": r[1], "data_type": r[2]} for r in cur.fetchall()]


def get_customized_system_records(cur: psycopg.Cursor, exclude_logins: list[str] | None = None) -> list[dict]:
    """Return all records that have an XML ID from a module but were edited by a real user.

    Checks write_uid on the actual record table (not on ir_model_data). A record
    is considered customized when:
      - it has an ir_model_data entry from a real module (not studio/export/import)
      - its write_uid on the underlying table is not the system user (uid=1)

    Covers ALL models in ir_model_data, not just a curated list. Runs one query
    per model — skips tables that don't exist or lack write_uid (m2m relation
    tables, etc.). Returns per-record detail: module, model, xml_id, modified_by.
    """
    # All distinct models referenced by real modules (must exist in ir_module_module;
    # excludes fake import namespaces like 'import_hr' used during CSV imports)
    cur.execute("""
        SELECT DISTINCT model
        FROM ir_model_data imd
        WHERE module NOT IN ('__export__', '__import__', '__custom__', '__base__')
          AND module NOT LIKE '%studio%'
          AND EXISTS (SELECT 1 FROM ir_module_module m WHERE m.name = imd.module)
        ORDER BY model
    """)
    candidate_models = [r[0] for r in cur.fetchall()]
    candidate_tables = [m.replace(".", "_") for m in candidate_models]

    # Keep only tables that exist AND have write_uid
    cur.execute(
        """
        SELECT c.table_name
        FROM information_schema.columns c
        WHERE c.table_schema = 'public'
          AND c.column_name = 'write_uid'
          AND c.table_name = ANY(%s)
    """,
        (candidate_tables,),
    )
    queryable = {r[0] for r in cur.fetchall()}

    model_table_pairs = [(m, m.replace(".", "_")) for m in candidate_models if m.replace(".", "_") in queryable]

    excluded = list(exclude_logins) if exclude_logins else []

    results: list[dict] = []
    for model, table in track(
        model_table_pairs,
        description="Scanning customized records…",
        console=_progress_console,
    ):
        excl_clause = sql.SQL("AND u.login != ALL(%(excl)s)") if excluded else sql.SQL("")
        params: dict = {"model": model}
        if excluded:
            params["excl"] = excluded
        cur.execute(
            sql.SQL("""
            SELECT imd.module, imd.name, u.login, imd.noupdate
            FROM ir_model_data imd
            JOIN {tbl} t ON t.id = imd.res_id
            LEFT JOIN res_users u ON u.id = t.write_uid
            WHERE imd.model = %(model)s
              AND imd.module NOT IN ('__export__', '__import__', '__custom__', '__base__')
              AND imd.module NOT LIKE '%%studio%%'
              AND EXISTS (SELECT 1 FROM ir_module_module mm WHERE mm.name = imd.module)
              AND t.write_uid IS NOT NULL
              AND t.write_uid != 1
              {excl}
            ORDER BY imd.module, imd.name
            """).format(tbl=sql.Identifier(table), excl=excl_clause),
            params,
        )
        for r in cur.fetchall():
            results.append({
                "model": model,
                "module": r[0],
                "xml_id": r[1],
                "modified_by": r[2],
                "noupdate": bool(r[3]),
            })
    return results


def get_mail_message_stats(cur: psycopg.Cursor) -> dict[str, int] | None:
    """Breakdown of mail.message by message_type. Returns None if table absent."""
    cur.execute("SELECT to_regclass('public.mail_message')")
    row = _fetch_one(cur)
    if not row[0]:
        return None
    cur.execute("""
        SELECT message_type, count(*)::int
        FROM mail_message
        GROUP BY message_type
        ORDER BY count(*) DESC
    """)
    return {r[0]: r[1] for r in cur.fetchall()}


def get_attachment_stats(cur: psycopg.Cursor) -> dict | None:
    """ir.attachment breakdown: db_binary vs filestore, counts, sizes."""
    cur.execute("SELECT to_regclass('public.ir_attachment')")
    row = _fetch_one(cur)
    if not row[0]:
        return None
    cur.execute("""
        SELECT
            CASE WHEN store_fname IS NULL THEN 'db_binary' ELSE 'filestore' END AS storage,
            count(*)::int AS cnt,
            coalesce(sum(file_size), 0)::bigint AS total_size
        FROM ir_attachment
        GROUP BY 1
    """)
    return {r[0]: {"count": r[1], "total_size": r[2]} for r in cur.fetchall()}


def get_cron_inventory(cur: psycopg.Cursor) -> list[dict] | None:
    """List installed ir.cron entries with name, active, code_based flag."""
    cur.execute("SELECT to_regclass('public.ir_cron')")
    row = _fetch_one(cur)
    if not row[0]:
        return None
    cur.execute("""
        SELECT s.name AS name,
               c.active,
               coalesce(s.state = 'code', false) AS code_based,
               EXISTS (
                   SELECT 1 FROM ir_model_data imd
                   WHERE imd.model = 'ir.cron' AND imd.res_id = c.id
               ) AS has_xmlid
        FROM ir_cron c
        LEFT JOIN ir_act_server s ON s.id = c.ir_actions_server_id
        ORDER BY c.active DESC, s.name
    """)
    return [
        {"name": _as_str(r[0]), "active": bool(r[1]), "code_based": bool(r[2]), "has_xmlid": bool(r[3])}
        for r in cur.fetchall()
    ]


def get_company_count(cur: psycopg.Cursor) -> int:
    """Return total company count from res_company."""
    cur.execute("SELECT count(*) FROM res_company")
    return _fetch_one(cur)[0]


# ---------------------------------------------------------------------------
# mail
# ---------------------------------------------------------------------------

# (key, explanation) — the ir_config_parameter keys a mail-config audit cares
# about: who mail claims to be from, and where bounces/catch-alls land.
# `default_email` is Trobz-specific (read by trobz_base), not an Odoo core
# key, kept alongside the others since it answers the same question.
_MAIL_CONFIG_KEYS: tuple[tuple[str, str], ...] = (
    ("mail.bounce.alias", ""),
    ("mail.catchall.alias", ""),
    ("mail.catchall.domain", ""),
    ("default_email", "Trobz-specific, used by trobz_base"),
    ("mail.default.from", ""),
    ("mail.default.from_filter", ""),
)


def get_mail_config_parameters(cur: psycopg.Cursor) -> list[dict]:
    """``ir_config_parameter`` values relevant to outbound mail identity/bounces.

    None of ``_MAIL_CONFIG_KEYS`` match ``_is_sensitive_key`` so values are
    never masked here. Keys not set in the database still get a row with
    ``value: None``, matching the original API-based check's "(not defined)"
    — via ``dict.get``, which only falls through to that default when the
    key is genuinely absent. A key that exists with an empty-string value
    (distinct from absent — Odoo's own ``get_param`` treats them
    differently too) is preserved as ``""``, not coerced to ``None``; the
    caller (``main.py``) must check ``is None`` rather than falsy-test the
    value, or it collapses "never configured" and "configured blank" into
    the same "(not defined)" display (verified against real data: on a
    v16 staging database of ours, ``mail.catchall.domain``'s row exists
    with value ``""``).
    """
    keys = [k for k, _ in _MAIL_CONFIG_KEYS]
    cur.execute("SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)", (keys,))
    values = dict(cur.fetchall())
    return [{"key": k, "explanation": explanation, "value": values.get(k)} for k, explanation in _MAIL_CONFIG_KEYS]


# The 4 legacy ICP keys mail.alias_domain._migrate_icp_to_domain() actually
# reads (mail/models/mail_alias_domain.py) -- deliberately narrower than
# _MAIL_CONFIG_KEYS, which also includes Trobz's own default_email and
# mail.default.from_filter (never read by that migration).
_LEGACY_ALIAS_MIGRATION_KEYS = frozenset({
    "mail.catchall.domain",
    "mail.bounce.alias",
    "mail.catchall.alias",
    "mail.default.from",
})


def _is_legacy_mail_config_configured(config_parameters: list[dict]) -> bool:
    """Whether any of the pre-v17 ICP mail keys was ever actually set.

    True for *any* database that went through a v16-style config at some
    point, not just a stuck one: ``_migrate_icp_to_domain`` reads these 4
    keys but never clears them (``mail/models/mail_alias_domain.py``: it
    only ``get_param``s them and creates a record), so a leftover value is
    the permanent state of a *successfully* migrated database too. On its
    own this does not separate "migrated fine" from "migration still
    pending" — see ``_is_alias_domain_migration_pending`` for that. Still
    useful by itself for the gauge this was originally added for: without
    it, ``odoo_db_mail_companies_missing_alias_domain`` reads 1 on
    essentially every stock 17+ install and can't be alerted on — the
    exact "flags nearly everything" trap this file already documents for
    role-drift's ``extra_groups``.
    """
    return any(p["value"] for p in config_parameters if p["key"] in _LEGACY_ALIAS_MIGRATION_KEYS)


def _is_alias_domain_migration_pending(alias_domains: list[dict] | None, *, legacy_configured: bool) -> bool:
    """Whether the pre-17 ICP mail config still has an effect left to have.

    ``_migrate_icp_to_domain`` reads the 4 legacy keys but never clears
    them, so a leftover value is the permanent state of any *successfully*
    migrated database, not evidence of a stuck one — verified across real
    databases: one production v17 with the ICP keys still set has every
    company with an alias domain (migrated fine), distinct from a v16
    database with the same keys set and no ``mail_alias_domain`` table at
    all (pre-17, not migrated yet). What actually separates "still
    pending" from "done" is whether a company still has no alias domain:
    only then can those leftover keys still be picked up by a (re-)run of
    the migration.
    """
    if alias_domains is None or not legacy_configured:
        return False
    return any(a["alias_domain_id"] is None for a in alias_domains)


def _relevant_mail_config_parameters(
    config_parameters: list[dict], *, alias_domains: list[dict] | None, migration_pending: bool
) -> list[dict]:
    """Drop the 4 legacy ICP keys when they can no longer affect routing:
    Odoo 17+ (``alias_domains`` is not ``None``) and no migration left to
    run (see ``_is_alias_domain_migration_pending``). Showing them right
    next to the authoritative ``alias_domains`` section reads as if they
    were still part of the active config — the opposite of helpful for a
    reader debugging mail on a modern, working database. Kept whenever
    they ARE relevant: pre-17 (no ``mail_alias_domain`` table at all, so
    these are the only mechanism) or an unfinished v16-to-17 upgrade (a
    company with no alias domain, where they can still be read).
    """
    if alias_domains is not None and not migration_pending:
        return [p for p in config_parameters if p["key"] not in _LEGACY_ALIAS_MIGRATION_KEYS]
    return config_parameters


# Default addresses Odoo (or its demo data) ships with — an audit flags
# these as "still Odoo default". Sourced from the same check this command
# replaces (odooly-based, run against real client instances), plus
# "info@yourcompany.com" added after verifying against a v18 source tree
# (odoo/addons/base/data/res_users_demo.xml) — that's the value demo data
# actually writes to the main company partner; ".example.com" never appears
# there, so a demo-seeded DB would otherwise never trip this flag.
_MAIL_DEFAULT_COMPANY_EMAILS = frozenset({"info@yourcompany.example.com", "info@yourcompany.com"})
_MAIL_DEFAULT_SYSTEM_EMAILS = frozenset({
    "root@yourcompany.example.com",  # demo data
    "root@example.com",
    "odoobot@example.com",  # ships in base/data/res_partner_data.xml since 16.0 (verified: a v16
    # database with mass_mailing uninstalled still carries it)
})
_MAIL_DEFAULT_ADMIN_EMAILS = frozenset({"admin@yourcompany.example.com", "admin@example.com"})


def _resolve_xmlid(cur: psycopg.Cursor, module: str, name: str) -> int | None:
    """``res_id`` of a well-known singleton via its external id (``base.main_partner``,
    ``base.user_root``, ``base.user_admin``, ...) — resolves correctly
    regardless of a renamed login or a non-default row id, unlike matching
    on ``login = 'admin'`` or a hardcoded ``res_partner`` id (see
    ``get_mail_addresses``: both silently drop the row entirely on a
    database where the login had been renamed — the normal state on
    odoo.sh — or the row had been deleted, rather than reporting that the
    lookup came up empty).
    """
    cur.execute("SELECT res_id FROM ir_model_data WHERE module = %s AND name = %s", (module, name))
    row = cur.fetchone()
    return row[0] if row else None


def _mail_address_row(partner_id: int | None, label: str, email: str | None, defaults: frozenset[str]) -> dict:
    # Case-folded: Odoo never normalizes res.partner.email, so an untouched
    # demo address typed back with capitals (e.g. ADMIN@Yourcompany.example.com)
    # would otherwise pass the audit.
    return {
        "partner_id": partner_id,
        "label": label,
        "email": email,
        "is_default": (email or "").strip().lower() in defaults,
        # partner_id is None exactly when the xmlid didn't resolve at all,
        # or resolved to a row that's since been deleted — distinct from a
        # record that exists with no email set (email: None, missing:
        # False). Matching on login or a hardcoded company id instead
        # would either silently drop the row (no admin/system line at all,
        # read as "no admin problem") or, for the hardcoded company id,
        # report {"partner_id": 1, "email": None} for a partner that had
        # been deleted outright — indistinguishable from "exists, blank
        # email".
        "missing": partner_id is None,
    }


def get_mail_addresses(cur: psycopg.Cursor) -> list[dict]:
    """Company/system/admin partner emails, flagged if still Odoo default (never customized).

    Mirrors the addresses a mail-config audit script checked via the ORM API
    (the main company partner, the OdooBot user, the admin user) — ported to
    direct SQL since none of it needs auth. Unlike the ORM, raw SQL has no
    implicit ``active=True`` filter, so OdooBot (archived by design) is found
    without the API script's explicit ``active=False`` domain, and an
    archived admin/company record is included too rather than silently
    disappearing.

    All three are resolved through ``ir_model_data`` (``base.main_partner``/
    ``base.user_root``/``base.user_admin``, see ``_resolve_xmlid``) rather
    than a hardcoded ``res_partner`` id or ``login = 'admin'``/``'__system__'``:
    matching on login silently drops the row entirely once that login has
    been renamed (the normal state on odoo.sh, where the admin account
    routinely gets a real customer email as its login), and a hardcoded
    ``res_partner`` id 1 can't tell "deleted" from "exists with no email".
    A row that can't be resolved this way — the xmlid is missing, or points
    at a row that's since been deleted — is still emitted, with
    ``missing: True`` and ``email: None`` (never silently dropped: "not
    listed" must not read as "no admin problem").

    Not masked, unlike ``ir_mail_server.smtp_pass``: these are organizational
    mailboxes (company contact, the OdooBot service account, the admin
    account), not individual end-user PII — usually already public (e.g. a
    company's own contact address). Masking them would be friction over
    data that isn't sensitive.
    """
    results: list[dict] = []

    company_id = _resolve_xmlid(cur, "base", "main_partner")
    email = None
    if company_id is not None:
        cur.execute("SELECT email FROM res_partner WHERE id = %s", (company_id,))
        row = cur.fetchone()
        if row is None:
            company_id = None  # xmlid resolved but the partner row is gone -> missing, not "no email"
        else:
            email = row[0]
    results.append(_mail_address_row(company_id, "Company Email", email, _MAIL_DEFAULT_COMPANY_EMAILS))

    for xmlid_name, label, defaults in (
        ("user_root", "System (OdooBot) Email", _MAIL_DEFAULT_SYSTEM_EMAILS),
        ("user_admin", "Admin Email", _MAIL_DEFAULT_ADMIN_EMAILS),
    ):
        user_id = _resolve_xmlid(cur, "base", xmlid_name)
        partner_id = user_email = None
        if user_id is not None:
            cur.execute(
                """
                SELECT ru.partner_id, rp.email
                FROM res_users ru
                JOIN res_partner rp ON rp.id = ru.partner_id
                WHERE ru.id = %s
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if row is not None:
                partner_id, user_email = row
        results.append(_mail_address_row(partner_id, label, user_email, defaults))

    return results


def _mail_server_select_sql(cur: psycopg.Cursor) -> str:
    """Extra SELECT columns for ``ir_mail_server``, version-branched.

    ``smtp_authentication`` (login/certificate/cli) and ``from_filter``
    (per-server FROM allowlist) both landed in 15.0 — upstream commits
    ``a4d513034ea8`` and ``1b9dd118cb0f``, neither reachable from 14.0 and
    both reachable from 15.0, so one probe safely covers both. NULL on
    14.0 and earlier so every call site unpacks the same 10 columns
    regardless of version. Probed the same to_regclass/pg_attribute way as
    ``_groups_category_sql`` — avoids information_schema's bare-name
    matching across every schema on the search_path.
    """
    cur.execute("""
        SELECT 1 FROM pg_attribute
        WHERE attrelid = to_regclass('ir_mail_server') AND attname = 'smtp_authentication' AND NOT attisdropped
    """)
    if cur.fetchone():
        return "smtp_authentication, from_filter"
    return "NULL, NULL"


# Well-known SMTP test catchers. A server named/hosted after one of these
# accepts mail but never relays it anywhere real (by design, so a staging/dev
# environment can't leak test traffic to real inboxes), which looks identical
# to a working relay in both the raw data and Odoo's own UI (verified live: a
# real test send through mailhog landed in its own catch-all, `state='sent'`
# in Odoo with no error).
#
# A match is a hint to verify, not proof: these are product names, and
# `name` is free text an admin typed. Ambiguous markers are left out rather
# than guessed at — `papercut` matches Papercut-SMTP (a real catcher) but
# also PaperCut MF/NG, widely-deployed print-management software; telling an
# auditor a working relay is a dead end is worse than missing a catcher.
_TEST_MAIL_CATCHER_MARKERS = frozenset({
    "mailhog",
    "mailcatcher",
    "maildev",
    "mailpit",
    "smtp4dev",
    "inbucket",
    "mailslurp",
})

# Hosts that identify a catcher on their own, where a name/token marker
# can't: Mailtrap runs a sandbox (a catcher) and a real sending service on
# neighbouring hostnames — sandbox.smtp.mailtrap.io vs live.smtp.mailtrap.io
# — so "mailtrap" as a substring/token marker would flag real production
# traffic too.
_TEST_MAIL_CATCHER_HOSTS = ("sandbox.smtp.mailtrap.io", "ethereal.email")


def _is_test_mail_catcher(name: str | None, host: str | None) -> bool:
    """Whole-token match against `name`/`smtp_host`, case-insensitive.

    Tokens, not substrings: a plain `in` check on the old marker list
    flagged `maildevices.com` as `maildev`. Separators stay loose (any
    non-alphanumeric run splits a token), so `mailhog-acme18-staging`
    still matches on the `mailhog` token.
    """
    tokens = set(re.split(r"[^a-z0-9]+", f"{name or ''} {host or ''}".lower()))
    if tokens & _TEST_MAIL_CATCHER_MARKERS:
        return True
    lowered_host = (host or "").lower()
    return any(known in lowered_host for known in _TEST_MAIL_CATCHER_HOSTS)


# The positive counterpart to _TEST_MAIL_CATCHER_MARKERS/_HOSTS: (label,
# exact hosts, suffixes, allowed ports) for well-known managed relays, so a
# match is a real production signal rather than just "not flagged as a test
# catcher" (an absence, not a confirmation). `ports=None` means the host
# alone is already distinctive enough (a dedicated per-provider hostname,
# unlike a generic SMTP port reused everywhere).
#
# Data, not predicates: every entry here is either an exact-host or a
# suffix check, so a lookup table expresses it directly and adding a
# provider is a one-line change.
_KNOWN_PRODUCTION_RELAYS: tuple[tuple[str, frozenset[str], tuple[str, ...], frozenset[int] | None], ...] = (
    # Google documents ports 25, 465 and 587 for both hosts (an earlier
    # version of this table required 587/465 respectively — wrong, missed
    # smtp.gmail.com:587 with STARTTLS, the most common Odoo setup on Gmail).
    ("Google Workspace SMTP relay", frozenset({"smtp-relay.gmail.com"}), (), frozenset({25, 465, 587})),
    ("Gmail SMTP", frozenset({"smtp.gmail.com"}), (), frozenset({25, 465, 587})),
    # Per-tenant subdomain (direct send / relay connector), documented port 25.
    ("Microsoft 365", frozenset(), (".mail.protection.outlook.com",), frozenset({25})),
    # SMTP AUTH client submission: the usual Odoo-on-M365 setup, since it needs
    # no Exchange-side connector, just a mailbox user/password mapped onto
    # smtp_user/smtp_pass. Different hosts, so a separate entry rather than
    # widening the one above — loosening its ports to None would drop the
    # port-25 constraint that keeps the per-tenant suffix match honest.
    ("Microsoft 365", frozenset({"smtp.office365.com", "smtp-mail.outlook.com"}), (), None),
    # Brevo (renamed from Sendinblue); the old host still resolves.
    ("Brevo (ex-Sendinblue)", frozenset({"smtp-relay.brevo.com", "smtp-relay.sendinblue.com"}), (), None),
    ("Mandrill", frozenset({"smtp.mandrillapp.com"}), (), None),
    ("OVH", frozenset({"ssl0.ovh.net"}), (), None),
    ("Mailjet", frozenset(), (".mailjet.com",), None),
    ("SendGrid", frozenset({"smtp.sendgrid.net"}), (), None),
    ("Mailgun", frozenset({"smtp.mailgun.org", "smtp.eu.mailgun.org"}), (), None),
    ("Postmark", frozenset({"smtp.postmarkapp.com"}), (), None),
)

# email-smtp.<region>.amazonaws.com — a pattern, not a fixed host, so it
# needs its own check rather than a table row; anchored so an unrelated
# *.amazonaws.com host isn't reported as SES.
_AWS_SES_HOST_RE = re.compile(r"^email-smtp\.[a-z0-9-]+\.amazonaws\.com$")


def _known_production_relay(host: str | None, port: int | None) -> str | None:
    """Label for a well-known managed relay, or `None`.

    `None` means "not recognised", never "not a real relay" — this is a
    positive-confirmation signal, not the inverse of `is_test_catcher`.
    Suffix checks require a leading `.` — a bare
    `endswith("mailjet.com")`/`endswith("mail.protection.outlook.com")`
    would match lookalike domains like `notmailjet.com` and
    `evilmail.protection.outlook.com`.
    """
    lowered = (host or "").strip().lower().rstrip(".")
    if not lowered:
        return None
    if _AWS_SES_HOST_RE.match(lowered):
        return "Amazon SES"
    for label, exact_hosts, suffixes, ports in _KNOWN_PRODUCTION_RELAYS:
        if lowered not in exact_hosts and not any(lowered.endswith(suffix) for suffix in suffixes):
            continue
        if ports is None or port in ports:
            return label
    return None


# base/data/neutralize.sql (identical 16.0-19.0, absent on 14.0) inserts
# exactly this row — name and host both hardcoded in Odoo core — after
# disabling every pre-existing relay (see get_is_neutralized). Caught in
# review: without this, a neutralized database (every odoo.sh staging
# build) shows the stub as an ordinary active relay and the real relay as
# inactive with no explanation, which reads exactly like a broken/disabled
# relay rather than an intentional neutralization side effect.
_NEUTRALIZATION_STUB_NAME = "neutralization - disable emails"
_NEUTRALIZATION_STUB_HOST = "invalid"


def _is_neutralization_stub_mail_server(name: str | None, host: str | None) -> bool:
    return (name or "").strip().lower() == _NEUTRALIZATION_STUB_NAME and (
        host or ""
    ).strip().lower() == _NEUTRALIZATION_STUB_HOST


def get_mail_servers(cur: psycopg.Cursor, *, reveal: bool = False) -> list[dict]:
    """Outgoing SMTP relays (``ir.mail_server``), ordered by priority (sequence).

    ``smtp_user``/``smtp_pass`` are masked like any other secret
    (``_SECRET_MASK``) unless ``reveal`` is set — the original script
    printed both in cleartext, which this tool's existing secret-masking
    convention (see ``get_config_parameters``) deliberately does not repeat
    by default. Both, not just the password: the SMTP username is a
    credential too (may itself be a real mailbox address), unlike the
    organizational addresses in ``get_mail_addresses`` which aren't masked
    at all.

    ``is_test_catcher`` (see ``_is_test_mail_catcher``) flags a row whose
    name/host names a known test-mail catcher — the audit's answer to "why
    doesn't this email arrive", without requiring the reader to already
    know what that tool is. ``known_production_relay`` (see
    ``_known_production_relay``) is its positive counterpart: a label when
    host+port match a well-known managed relay (Google, Microsoft 365) —
    a real confirmation signal, not just the absence of the other flag.

    ``is_neutralization_stub`` (see ``_is_neutralization_stub_mail_server``)
    flags Odoo core's own db_neutralize placeholder row — the single most
    common reason mail never leaves an Odoo database, and not a test
    catcher at all (see ``get_is_neutralized`` for the accompanying
    top-level flag).
    """
    extra_sql = _mail_server_select_sql(cur)
    # extra_sql is one of the two fixed literal strings returned by
    # _mail_server_select_sql above — never user or row data, so the f-string
    # is safe. ty wants a LiteralString, which a dynamically-built (but still
    # internally-fixed) query string can never satisfy.
    cur.execute(f"""
        SELECT sequence, name, smtp_host, smtp_port, smtp_user, smtp_pass, smtp_encryption, active, {extra_sql}
        FROM ir_mail_server
        ORDER BY sequence, name
    """)  # noqa: S608  # ty: ignore[no-matching-overload]
    rows: list[dict] = []
    for seq, name, host, port, user, pwd, encryption, active, authentication, from_filter in cur.fetchall():
        rows.append({
            "sequence": seq,
            "name": name,
            "smtp_host": host,
            "smtp_port": port,
            "smtp_user": user if (reveal or not user) else _SECRET_MASK,
            "smtp_pass": pwd if (reveal or not pwd) else _SECRET_MASK,
            "smtp_encryption": encryption,
            "smtp_authentication": authentication,
            "from_filter": from_filter,
            "active": bool(active),
            "is_test_catcher": _is_test_mail_catcher(name, host),
            "known_production_relay": _known_production_relay(host, port),
            "is_neutralization_stub": _is_neutralization_stub_mail_server(name, host),
        })
    return rows


def get_mail_relevant_modules(cur: psycopg.Cursor) -> list[dict]:
    """State of modules that materially change mail behavior (currently: ``mass_mailing``)."""
    cur.execute(
        "SELECT name, state FROM ir_module_module WHERE name = ANY(%s) ORDER BY name",
        (["mass_mailing"],),
    )
    return [{"name": r[0], "state": r[1]} for r in cur.fetchall()]


def _mail_alias_local_email(local_part: str | None, domain_name: str | None) -> str | None:
    """Mirrors ``AliasDomain._compute_bounce_email``/``_compute_catchall_email``: always `local@domain`."""
    if not local_part or not domain_name:
        return None
    return f"{local_part}@{domain_name}"


def _mail_alias_default_from_email(default_from: str | None, domain_name: str | None) -> str | None:
    """Mirrors ``AliasDomain._compute_default_from_email``: keep as-is if already a full address."""
    if not default_from:
        return None
    if "@" in default_from:
        return default_from
    if not domain_name:
        return None
    return f"{default_from}@{domain_name}"


def get_mail_alias_domains(cur: psycopg.Cursor) -> list[dict] | None:
    """Per-company ``mail.alias.domain``, the *actual* runtime source (Odoo 17+)
    for catchall/bounce/default-from — supersedes the ``mail.catchall.domain``
    / ``mail.bounce.alias`` / ``mail.catchall.alias`` / ``mail.default.from``
    ``ir_config_parameter`` keys in ``get_mail_config_parameters``.

    Odoo's own model docstring says it plainly: "This replaces
    ``mail.alias.domain`` configuration parameter use until v16"
    (``odoo/addons/mail/models/mail_alias_domain.py``). Past v16, those ICP
    keys are read only by ``AliasDomain._migrate_icp_to_domain`` — a one-time
    compatibility helper for installing ``mail`` after ``base`` was already
    configured (e.g. the odoo.sh flow) — and are otherwise vestigial: a value
    sitting there does not mean Odoo is actually using it to route bounces or
    replies. Verified by grepping an Odoo 18 community + enterprise source
    tree: no runtime code in ``mail`` reads ``mail.catchall.domain``/
    ``mail.bounce.alias``/``mail.catchall.alias`` by key outside that
    migration helper — OCA or custom modules may still read
    ``mail.catchall.domain`` directly, so this doesn't generalize past core.

    Returns ``None`` if ``mail_alias_domain`` doesn't exist (pre-17, or the
    ``mail`` app not installed at all) — ``get_mail_config_parameters`` is
    then the only signal available, same as pre-17 in reality.

    A company with ``alias_domain_id IS NULL`` has *no* alias domain assigned
    — bounces/replies for that company aren't routed anywhere — but this is
    **not** on its own a misconfiguration: it's also the documented state of
    a clean 17+ install that never had v16-style ICP config to migrate
    (verified across real v17/v18/v19 databases: 4 of 5 with ``mail``
    installed have zero ``mail_alias_domain`` rows, all clean installs).
    Surfaced as ``alias_domain: None`` rather than silently omitted either
    way; ``_is_alias_domain_migration_pending`` (see ``get_mail_audit``) is
    what actually tells a clean or successfully-migrated install apart
    from a genuinely stuck one — a leftover ICP value alone
    (``_is_legacy_mail_config_configured``) does not, since
    ``_migrate_icp_to_domain`` never clears those keys even when it
    succeeds.

    Filters ``res_company.active = true`` — unlike ``get_mail_addresses``
    (where seeing past the ORM's implicit filter is the point), an archived
    company isn't sending real mail, so counting it here would just add
    false-positive noise to ``odoo_db_mail_companies_missing_alias_domain``.
    Same rationale ``get_groups(include_users=True)`` uses for filtering
    ``u.active = true`` on membership rows.
    """
    cur.execute("SELECT to_regclass('public.mail_alias_domain')")
    if not _fetch_one(cur)[0]:
        return None

    cur.execute("""
        SELECT c.id, c.name, mad.id, mad.name, mad.bounce_alias, mad.catchall_alias, mad.default_from
        FROM res_company c
        LEFT JOIN mail_alias_domain mad ON mad.id = c.alias_domain_id
        WHERE c.active = true
        ORDER BY c.id
    """)
    rows: list[dict] = []
    for company_id, company_name, domain_id, domain_name, bounce_alias, catchall_alias, default_from in cur.fetchall():
        rows.append({
            "company_id": company_id,
            "company_name": company_name,
            "alias_domain_id": domain_id,
            "alias_domain": domain_name,
            "bounce_email": _mail_alias_local_email(bounce_alias, domain_name),
            "catchall_email": _mail_alias_local_email(catchall_alias, domain_name),
            "default_from_email": _mail_alias_default_from_email(default_from, domain_name),
        })
    return rows


def get_mail_audit(cur: psycopg.Cursor, *, reveal: bool = False) -> dict:
    """Audit bundle for outbound mail configuration.

    Ported from a script that gathered the same data through the ORM API
    (odooly client) — none of it needs auth, so direct SQL replaces it,
    picking up raw-SQL's usual side benefit of seeing past the ORM's implicit
    ``active=True`` filter (see ``get_mail_addresses``). ``alias_domains``
    (Odoo 17+) is the authoritative counterpart to ``config_parameters`` —
    see ``get_mail_alias_domains`` for why both are kept rather than one
    replacing the other. ``reveal`` now only affects ``mail_servers``
    (``smtp_pass`` is a real credential) — ``addresses`` are organizational
    mailboxes, not masked regardless (see ``get_mail_addresses``).

    ``is_neutralized`` (see ``get_is_neutralized``) is the single most
    common reason mail never leaves an Odoo database — every odoo.sh
    staging build looks like this — read the same way ``odoo-db list``
    already reads the same ``database.is_neutralized`` key.

    ``is_legacy_mail_config_configured``/``is_alias_domain_migration_pending``
    (see ``_is_legacy_mail_config_configured``/
    ``_is_alias_domain_migration_pending``) together let a caller tell a
    clean 17+ install, a successfully migrated one, and a genuinely stuck
    v16-to-17 upgrade apart — all three otherwise look identical as just
    ``alias_domain_id IS NULL`` (the first two) or a leftover ICP value
    (the last two) taken alone.

    ``config_parameters`` here is always the full 6-key list — unlike
    ``odoo-db mail``'s own text output, which drops the 4 legacy ICP keys
    once ``is_alias_domain_migration_pending`` says they're no longer
    relevant (see ``_relevant_mail_config_parameters``, called from
    ``main.py``, not here). Kept complete in this dict deliberately: this
    is also what ``--output-format json`` returns, and this tool's main
    job is v16-to-v19 migration audits — comparing that JSON across
    versions, a leftover ``mail.catchall.domain`` present in one and
    silently dropped from the other would be indistinguishable from
    "never existed". Whether to hide an always-empty key is a
    presentation call for a human reading text/TUI output, not something
    the machine-readable artifact should also make.
    """
    config_parameters = get_mail_config_parameters(cur)
    alias_domains = get_mail_alias_domains(cur)
    legacy_configured = _is_legacy_mail_config_configured(config_parameters)
    migration_pending = _is_alias_domain_migration_pending(alias_domains, legacy_configured=legacy_configured)

    return {
        "is_neutralized": get_is_neutralized(cur),
        "config_parameters": config_parameters,
        "is_legacy_mail_config_configured": legacy_configured,
        "is_alias_domain_migration_pending": migration_pending,
        "alias_domains": alias_domains,
        "addresses": get_mail_addresses(cur),
        "mail_servers": get_mail_servers(cur, reveal=reveal),
        "modules": get_mail_relevant_modules(cur),
    }


# Per-row overhead used by the statistical bloat estimate. Deliberately
# coarse — the estimate is a triage signal, not a measurement (run with
# pgstattuple for exact numbers). Heap: 23-byte HeapTupleHeader rounded to 24
# + 4-byte line pointer. Btree: 8-byte IndexTupleData header + 4-byte line
# pointer; default btree fillfactor is 90.
_HEAP_TUPLE_OVERHEAD = 24 + 4
_BTREE_ENTRY_OVERHEAD = 8 + 4
_BTREE_FILLFACTOR = 0.90
_PAGE_HEADER = 24


def _bloat_estimate_pages(ntuples: int, entry_bytes: int, usable_per_page: float) -> int:
    if ntuples <= 0 or entry_bytes <= 0 or usable_per_page <= 0:
        return 0
    return math.ceil(ntuples * entry_bytes / usable_per_page)


def get_bloat(
    cur: psycopg.Cursor,
    *,
    top: int = 25,
    exact_max_scan_bytes: int = 2 * 1024**3,
) -> dict:
    """Table + index bloat for the heaviest relations, with a two-tier engine.

    Bloat = physical size held by a relation beyond what its live rows need —
    dead tuples not yet reclaimed plus half-empty pages VACUUM cannot compact.
    It is the space a ``VACUUM FULL`` / ``REINDEX`` or a dump+restore migration
    would give back. Autovacuum never returns it (it frees space *inside* the
    files for reuse but never shrinks them, and never re-indexes).

    Two engines, combined per the caller's privileges:

    - **Estimate (always):** a cheap statistical guess from ``pg_class``
      (``relpages``/``reltuples``) and ``pg_stats`` average column widths. No
      extension, read-only, runs as any role. Coarse — labelled ``est`` — and
      can be off when stats are stale (run ``ANALYZE`` first) or for fat-column
      / TOAST-heavy tables.
    - **Exact (overlay):** when the ``pgstattuple`` extension is installed and a
      relation is at or under ``exact_max_scan_bytes``, ``pgstattuple`` /
      ``pgstatindex`` measure real dead space — but they *full-scan* the
      relation, hence the size cap. Rows measured this way are labelled
      ``exact`` and override the estimate.

    Also surfaces cheap, exact health signals from ``pg_stat_user_tables`` /
    ``pg_stat_user_indexes``: dead-tuple ratio + ``last_autovacuum`` (is
    autovacuum keeping up / is an xmin holder blocking cleanup?) and
    ``idx_scan = 0`` (unused index — a different kind of dead weight).

    ``pgstattuple_available`` and the per-row ``method`` let the caller tell the
    user which strategy produced each number and recommend installing the
    extension for exactness.
    """
    cur.execute("SELECT 1 FROM pg_extension WHERE extname = 'pgstattuple'")
    has_pgstattuple = bool(cur.fetchone())

    cur.execute("SELECT current_setting('block_size')::int")
    bs = _fetch_one(cur)[0]
    usable = bs - _PAGE_HEADER

    # Context for judging the idx_scan ("unused") signal: how long counters
    # have accumulated and how busy the DB has been. idx_scan resets to 0 on
    # restore / pg_stat_reset, so on a fresh or low-traffic copy "unused" is
    # mostly false. stats_reset is NULL when never reset → fall back to the
    # cluster start time.
    cur.execute(
        """
        SELECT COALESCE(stats_reset, pg_postmaster_start_time()), stats_reset IS NOT NULL, xact_commit
        FROM pg_stat_database WHERE datname = current_database()
        """
    )
    scan_since, scan_since_is_reset, xact_commit = _fetch_one(cur)

    # heaviest regular tables (by heap size — bloat lives in the heap)
    cur.execute(
        """
        SELECT c.oid, c.relname, c.reltuples::bigint, pg_relation_size(c.oid)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public' AND c.relkind = 'r'
        ORDER BY pg_relation_size(c.oid) DESC
        LIMIT %s
        """,
        (top,),
    )
    table_rows = cur.fetchall()
    table_names = [r[1] for r in table_rows]

    # avg row width (sum of per-column avg_width) for those tables
    cur.execute(
        """
        SELECT tablename, COALESCE(sum(avg_width), 0)
        FROM pg_stats WHERE schemaname = 'public' AND tablename = ANY(%s)
        GROUP BY tablename
        """,
        (table_names,),
    )
    row_width = {r[0]: int(r[1]) for r in cur.fetchall()}

    # exact, cheap health signals
    cur.execute(
        """
        SELECT relname, n_live_tup, n_dead_tup, last_autovacuum
        FROM pg_stat_user_tables WHERE schemaname = 'public' AND relname = ANY(%s)
        """,
        (table_names,),
    )
    tstat = {r[0]: {"live": r[1], "dead": r[2], "last_autovacuum": r[3]} for r in cur.fetchall()}

    exact_count = estimate_count = 0
    tables: list[dict] = []
    for oid, name, reltuples, heap_bytes in table_rows:
        width = row_width.get(name, 0)
        est_pages = _bloat_estimate_pages(reltuples, width + _HEAP_TUPLE_OVERHEAD, usable)
        est_bytes = est_pages * bs
        bloat_bytes = max(0, heap_bytes - est_bytes) if width and reltuples > 0 else None
        method = "est" if bloat_bytes is not None else "n/a"

        if has_pgstattuple and heap_bytes <= exact_max_scan_bytes:
            try:
                cur.execute("SELECT dead_tuple_len, free_space FROM pgstattuple(%s)", (oid,))
                dead_len, free_space = _fetch_one(cur)
                bloat_bytes = int(dead_len) + int(free_space)
                method = "exact"
            except Exception as exc:
                logger.debug("pgstattuple failed for %s: %s", name, exc)

        exact_count += method == "exact"
        estimate_count += method == "est"
        st = tstat.get(name, {})
        dead = st.get("dead") or 0
        live = st.get("live") or 0
        tables.append({
            "table": name,
            "size_bytes": heap_bytes,
            "bloat_bytes": bloat_bytes,
            "bloat_pct": round(100 * bloat_bytes / heap_bytes, 1) if bloat_bytes and heap_bytes else 0.0,
            "method": method,
            "dead_tuples": dead,
            "dead_pct": round(100 * dead / (live + dead), 1) if (live + dead) else 0.0,
            "last_autovacuum": st.get("last_autovacuum"),
        })

    # btree indexes on those tables (estimate needs key column widths)
    cur.execute(
        """
        SELECT i.indexrelid, ic.relname, tc.relname, pg_relation_size(i.indexrelid),
               am.amname, COALESCE(st.idx_scan, 0), tc.reltuples::bigint,
               string_to_array(i.indkey::text, ' ')::int[]
        FROM pg_index i
        JOIN pg_class ic ON ic.oid = i.indexrelid
        JOIN pg_class tc ON tc.oid = i.indrelid
        JOIN pg_am am ON am.oid = ic.relam
        JOIN pg_namespace n ON n.oid = ic.relnamespace
        LEFT JOIN pg_stat_user_indexes st ON st.indexrelid = i.indexrelid
        WHERE n.nspname = 'public' AND tc.relname = ANY(%s)
        ORDER BY pg_relation_size(i.indexrelid) DESC
        LIMIT %s
        """,
        (table_names, top),
    )
    index_rows = cur.fetchall()

    # map (table, attnum) -> avg_width so we can size index key columns
    cur.execute(
        """
        SELECT s.tablename, a.attnum, s.avg_width
        FROM pg_stats s
        JOIN pg_class c ON c.relname = s.tablename
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = s.schemaname
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attname = s.attname
        WHERE s.schemaname = 'public' AND s.tablename = ANY(%s)
        """,
        (table_names,),
    )
    col_width: dict[tuple[str, int], int] = {(r[0], r[1]): int(r[2]) for r in cur.fetchall()}

    indexes: list[dict] = []
    for idxoid, idxname, tblname, idx_bytes, amname, idx_scan, reltuples, indkey in index_rows:
        bloat_bytes = None
        method = "n/a"
        if amname == "btree" and reltuples > 0 and indkey and 0 not in indkey:
            key_width = sum(col_width.get((tblname, att), 0) for att in indkey)
            if key_width:
                entry = key_width + _BTREE_ENTRY_OVERHEAD
                est_pages = _bloat_estimate_pages(reltuples, entry, usable * _BTREE_FILLFACTOR)
                est_bytes = est_pages * bs
                bloat_bytes = max(0, idx_bytes - est_bytes)
                method = "est"

        if has_pgstattuple and amname == "btree" and idx_bytes <= exact_max_scan_bytes:
            try:
                cur.execute("SELECT avg_leaf_density FROM pgstatindex(%s)", (idxoid,))
                density = float(_fetch_one(cur)[0] or 0)
                ratio = max(0.0, 1 - density / (100 * _BTREE_FILLFACTOR))
                bloat_bytes = int(idx_bytes * ratio)
                method = "exact"
            except Exception as exc:
                logger.debug("pgstatindex failed for %s: %s", idxname, exc)

        exact_count += method == "exact"
        estimate_count += method == "est"
        indexes.append({
            "index": idxname,
            "table": tblname,
            "access_method": amname,
            "size_bytes": idx_bytes,
            "bloat_bytes": bloat_bytes,
            "bloat_pct": round(100 * bloat_bytes / idx_bytes, 1) if bloat_bytes and idx_bytes else 0.0,
            "method": method,
            "idx_scan": idx_scan,
            "unused": idx_scan == 0,
        })

    return {
        "pgstattuple_available": has_pgstattuple,
        "exact_max_scan_bytes": exact_max_scan_bytes,
        "exact_count": exact_count,
        "estimate_count": estimate_count,
        "scan_stats_since": scan_since,
        "scan_stats_since_is_reset": scan_since_is_reset,
        "xact_commit": xact_commit,
        "tables": tables,
        "indexes": indexes,
    }


def get_locks(cur: psycopg.Cursor, dbname: str) -> dict:
    cur.execute(
        """
            SELECT blocked_locks.pid, blocking_locks.pid
            FROM pg_catalog.pg_locks blocked_locks
            JOIN pg_catalog.pg_stat_activity blocked_activity
                ON blocked_activity.pid = blocked_locks.pid
            JOIN pg_catalog.pg_locks blocking_locks
                ON blocking_locks.locktype = blocked_locks.locktype
                AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
                AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
                AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
                AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
                AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
                AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
                AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
                AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
                AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
                AND blocking_locks.pid != blocked_locks.pid
            JOIN pg_catalog.pg_stat_activity blocking_activity
                ON blocking_activity.pid = blocking_locks.pid
            WHERE NOT blocked_locks.granted
              AND blocked_activity.datname = %s
        """,
        (dbname,),
    )

    blocked_by: dict[int, list[int]] = {}
    for blocked_pid, blocking_pid in cur.fetchall():
        blocked_by.setdefault(blocked_pid, []).append(blocking_pid)

    blocked = set(blocked_by.keys())
    blocking = {pid for pids in blocked_by.values() for pid in pids}
    blocking_not_blocked = sorted(blocking - blocked)

    queries: dict[int, str] = {}
    all_pids = list(blocked | blocking)
    if all_pids:
        cur.execute(
            """
                SELECT pid, left(query, 120)
                FROM pg_stat_activity
                WHERE pid = ANY(%s)
            """,
            (all_pids,),
        )
        queries = {row[0]: row[1] for row in cur.fetchall()}

    return {
        "blocked_count": len(blocked),
        "blocking_count": len(blocking),
        "blocking_not_blocked": blocking_not_blocked,
        "details": [{"blocked_pid": bp, "blocking_pids": pids} for bp, pids in blocked_by.items()],
        "queries": {str(pid): q for pid, q in queries.items()},
    }


# ---------------------------------------------------------------------------
# sensitive information
# ---------------------------------------------------------------------------

# Table-name markers for objects a custom module is likely to have parked
# credentials in. Wildcarded on both sides deliberately: a custom module
# prefixes its tables (`x_api_config`, `trobz_api_instance`), so anchoring
# these would match almost nothing on a real database.
#
# Kept separate from _SENSITIVE_KEY_MARKERS, which describes
# ir_config_parameter *keys*: overlapping the two would drag in every
# `res_users.password` and `*_token` column Odoo core ships, burying the
# handful of rows a reviewer can act on. The markers here name integration
# tables, which core has none of -- so a match is nearly always custom.
_SENSITIVE_TABLE_MARKERS = ("api_key", "api_config", "api_instance", "api_url")


# `auth_signup.reset_password` is a checkbox, not a password -- and
# `_is_sensitive_key` matches it on the substring, as it should for masking
# (masking a boolean costs nothing). In a report whose whole purpose is to
# list the secrets, a false positive costs the reader's trust in the other
# rows, so a value that is only a boolean literal is dropped. Nothing wider:
# a short numeric value (`...duration = 90`) stays, since dropping by shape
# is how a real 4-digit credential would go missing.
_BOOLEAN_VALUES = frozenset(("true", "false", "0", "1"))

# Core keys `_is_sensitive_key` matches on a substring while holding no
# secret at all -- `auth_password_policy.minlength` is a policy number that
# says "password". Dropping them by *value shape* was rejected above (a
# 4-digit credential looks the same), so they are named instead: an exact
# key allowlist can only ever hide the key it names, and each entry is a
# core key whose meaning is fixed. A custom module's key never lands here.
#
# The second group is public *by design*, which the `_key` suffix hides:
# core hands `cf.turnstile_site_key` and `recaptcha_public_key` to the
# browser itself (`ir_http.py` puts them in the session payload), and the
# VAPID public key is the half of the pair a push subscription publishes --
# its `_private_key` sibling is the secret and stays listed. Matched as
# exact keys rather than a `*_public_key`/`*_site_key` suffix rule for the
# same reason as above: a rule hides keys nobody has read yet.
_NON_SECRET_CONFIG_KEYS = frozenset((
    "auth_password_policy.minlength",
    "cf.turnstile_site_key",
    "mail.web_push_vapid_public_key",
    "recaptcha_public_key",
    # a URL with a documented default (DEFAULT_MICROSOFT_TOKEN_ENDPOINT),
    # matched only on the word "token"
    "microsoft_account.token_endpoint",
))


def _sensitive_marker(name: str) -> str | None:
    """Which marker flagged `name`, for a reader who has to judge the hit."""
    lowered = name.lower()
    for marker in (*_SENSITIVE_KEY_MARKERS, *_SENSITIVE_TABLE_MARKERS):
        if marker in lowered:
            return marker
    return "key" if re.search(r"(^|[._])key$", lowered) else None


def get_sensitive_information(cur: psycopg.Cursor, *, reveal: bool = False) -> dict:
    """What secrets a database still carries — the question to answer before
    a dump leaves the building, or after a copy has been neutralized.

    Neutralization is the related but different question: it asks what a
    database can still *do* (mail servers disabled, crons off), while this
    asks what it still *holds*. A neutralized copy still has every API key
    its custom modules stored -- `base/data/neutralize.sql` only clears the
    credentials of modules that ship a neutralize.sql, and a client's own
    module never does.

    Three sections, each a different way a credential hides:

    - `config_parameters`: `ir_config_parameter` rows whose key looks
      secret-bearing (`_is_sensitive_key`) *and* actually hold a value. The
      empty ones are reported nowhere: a key with no value is not a leak,
      and listing it only pads the report a reviewer has to read.
    - `mail_servers`: `ir.mail_server` rows carrying real relay
      credentials. Odoo's own neutralization stub and the known test
      catchers are excluded -- neither has a credential to leak. Inactive
      rows are *included*, unlike the `mail` audit's: an archived relay
      cannot send anything, but its stored password is in the dump all the
      same.
    - `candidate_tables`: tables whose name matches
      `_SENSITIVE_TABLE_MARKERS`, with the row count and which of their
      columns look secret-bearing. `owner_module` is the module that owns
      the table (`get_model_owners`), or None when nothing claims it --
      those are the custom/orphan ones worth reading first.

    Values are masked unless `reveal`, the same rule `get_config_parameters`
    follows: the point is to say *where* the secrets are, and a report that
    prints them is itself the thing it warns about.
    """
    # fetched before anything else runs on this cursor: every execute
    # replaces the result set, so building the dict around a bare
    # `cur.fetchall()` would hand these rows to whichever query the
    # neighbouring value ran first.
    cur.execute("SELECT key, value FROM ir_config_parameter ORDER BY key")
    parameters = cur.fetchall()

    return {
        "is_neutralized": get_is_neutralized(cur),
        "config_parameters": filter_sensitive_parameters(parameters, reveal=reveal),
        "mail_servers": filter_credential_mail_servers(get_mail_servers(cur, reveal=reveal)),
        "live_surfaces": _live_neutralize_surfaces(cur),
        "candidate_tables": _sensitive_candidate_tables(cur),
    }


# What each module's own `data/neutralize.sql` clears, expressed as the
# rows that are still in the *un*-neutralized state. `(table, condition,
# what it can still reach)`, every condition read off that file (Odoo 19;
# the statements have been stable since 16, where neutralize.sql first
# ships).
#
# Only tables whose owning module also owns the column in the condition, so
# "the table is here" implies "the column is here". That leaves out the
# checks hanging off `res_company`/`res_users` (sms_twilio,
# microsoft_calendar, the l10n_* EDI credentials): those tables exist on
# every database while their columns come and go with the module, and a
# missing column is an error, not an empty result.
_NEUTRALIZE_SURFACES: tuple[tuple[str, str, str], ...] = (
    # base's own two statements, and the strongest signal in the list: both
    # tables exist on every Odoo database from 14 on, so unlike the module
    # rows below these are never skipped for want of a table. A copy whose
    # crons are still armed will act on its own, with nobody at the screen.
    # autovacuum is excluded exactly as neutralize.sql excludes it -- it is
    # the one cron Odoo deliberately leaves running.
    (
        "ir_cron",
        "active AND id NOT IN (SELECT res_id FROM ir_model_data "
        "WHERE model = 'ir.cron' AND name = 'autovacuum_job' AND module = 'base')",
        "still fires scheduled jobs (mail queue, sync, ...)",
    ),
    # neutralize.sql deactivates every relay and inserts the `invalid` stub
    # in their place. The stub itself is active by design, and a test
    # catcher accepts mail without ever relaying it, so neither is a way
    # out -- flagging them would only teach the reader to skim the section.
    (
        "ir_mail_server",
        "active AND coalesce(smtp_host, '') NOT IN ('invalid', 'mailhog', 'mailpit', 'maildev')",
        "can relay mail to the outside",
    ),
    ("payment_provider", "state NOT IN ('test', 'disabled')", "can charge a real card"),
    # the same table before Odoo 16 renamed it -- both are listed because a
    # missing table and a misspelled one look alike to the existence check,
    # so dropping the old name would retire the highest-value row in
    # silence on every 14/15 database
    ("payment_acquirer", "state NOT IN ('test', 'disabled')", "can charge a real card"),
    ("iap_account", "account_token NOT LIKE '%+disabled'", "bills the customer's IAP credits"),
    ("fetchmail_server", "active", "still fetches and processes real incoming mail"),
    ("whatsapp_account", "token <> 'dummy_token'", "sends real WhatsApp messages"),
    ("voip_provider", "mode <> 'demo'", "places real calls"),
    ("account_online_link", "client_id <> 'duplicate'", "keeps a live bank feed"),
    ("certificate_certificate", "pkcs12_password <> 'dummy'", "signs with a real certificate"),
    # the stub is excluded: a template pointing at Odoo's own dead-end relay
    # is exactly as harmless as one pointing nowhere
    (
        "mail_template",
        "mail_server_id IS NOT NULL AND mail_server_id NOT IN "
        "(SELECT id FROM ir_mail_server WHERE smtp_host = 'invalid')",
        "is pinned to a named relay",
    ),
)


def _live_neutralize_surfaces(cur: psycopg.Cursor) -> list[dict]:
    """Rows that `neutralize` should have cleared and did not.

    `base/data/neutralize.sql` disables the mail servers and the crons, and
    that much is easy to see. What it also does -- through each module's own
    `neutralize.sql` -- is strip the credentials that let a copy act on the
    outside world, and *that* is the part nothing was checking: a database
    flagged `is_neutralized` whose payment provider is still enabled is a
    staging copy one click away from charging a real card. Reported for a
    production database too, where the same list is simply what it can
    reach.

    Only surfaces still live are returned; a database with nothing left
    answers `[]`. `checked` is not tracked per row here because the table
    itself carries `installed`: a surface whose table does not exist is
    reported as such rather than silently skipped, so a typo in the table
    name and a module that isn't installed stay distinguishable.
    """
    tables = [table for table, _condition, _reach in _NEUTRALIZE_SURFACES]
    cur.execute(
        "SELECT t, to_regclass('public.' || t) IS NOT NULL FROM unnest(%s::text[]) t",
        (tables,),
    )
    present = dict(cur.fetchall())

    live = []
    for table, condition, reach in _NEUTRALIZE_SURFACES:
        if not present.get(table):
            continue
        rows = _count_rows(cur, table, where=condition)
        if rows:
            live.append({"table": table, "rows": rows, "reach": reach})
    return live


def filter_sensitive_parameters(rows: list[tuple], *, reveal: bool = False) -> list[dict]:
    """`(key, value)` rows down to the ones that actually hold a secret.

    Pure over the fetched rows so it is unit-testable without a cursor, the
    same shape `filter_online_users`/`compute_role_drift` use.
    """
    return [
        {"key": key, "value": value if reveal else _SECRET_MASK, "marker": _sensitive_marker(key)}
        for key, value in rows
        if value
        and _is_sensitive_key(key)
        and key not in _NON_SECRET_CONFIG_KEYS
        and str(value).strip().lower() not in _BOOLEAN_VALUES
    ]


def filter_credential_mail_servers(servers: list[dict]) -> list[dict]:
    """`get_mail_servers` rows down to the ones a dump would leak: a stored
    relay credential, or a known production relay host.

    The stub and the test catchers are dropped -- neither has a credential
    to leak. Inactive rows are kept, unlike the `mail` audit's active-only
    counting: an archived relay cannot send anything, but its stored
    password is in the dump all the same.

    A row matching a known production relay is kept even with no stored
    credential: such a relay commonly authenticates by IP allowlist or
    `from_filter` instead, so "no smtp_user" is not "no production
    config" -- the host itself is the finding, and a copy pointed at it
    is one cron away from mailing real customers.
    """
    return [
        {
            "name": row["name"],
            "smtp_host": row["smtp_host"],
            "smtp_port": row["smtp_port"],
            "smtp_user": row["smtp_user"],
            "has_password": bool(row["smtp_pass"]),
            "active": row["active"],
            "known_production_relay": row["known_production_relay"],
        }
        for row in servers
        if not row["is_neutralization_stub"]
        and not row["is_test_catcher"]
        and (row["smtp_user"] or row["smtp_pass"] or row["known_production_relay"])
    ]


def _sensitive_candidate_tables(cur: psycopg.Cursor) -> list[dict]:
    """Tables named after an integration credential store, with their row
    count and secret-looking columns.

    The counts are exact rather than `reltuples` estimates: these are a
    handful of tables, and "0 rows" is the answer that lets a reviewer drop
    a hit without opening it -- an estimate that says 0 on a
    never-analyzed table would drop real ones instead.
    """
    # `strpos`, not ILIKE: `_` is a single-character wildcard in a LIKE
    # pattern, so `%api_key%` also matches `apiXkey` -- fuzzy by accident,
    # never by design. A lowercased substring search says what it means.
    #
    # `relkind IN ('r', 'p')` plus `NOT relispartition`: a partitioned table
    # is 'p' and its partitions are 'r' children, so matching only 'r' misses
    # the parent the custom module actually declared, while matching both
    # without the flag reports the parent and every one of its partitions --
    # the same credentials counted once per year of data.
    cur.execute(
        """
        SELECT c.relname, array_agg(a.attname ORDER BY a.attname)
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
        WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') AND NOT c.relispartition
          AND (SELECT bool_or(strpos(lower(c.relname), m) > 0) FROM unnest(%s::text[]) m)
        GROUP BY c.relname
        ORDER BY c.relname
        """,
        (list(_SENSITIVE_TABLE_MARKERS),),
    )
    matches = cur.fetchall()
    if not matches:
        return []

    owners = get_model_owners(cur)
    tables = []
    for table, columns in matches:
        tables.append({
            "table": table,
            "owner_module": owners.get(table),
            "rows": _count_rows(cur, table),
            "marker": _sensitive_marker(table),
            "sensitive_columns": [column for column in columns if _is_sensitive_key(column)],
        })
    return tables


def _count_rows(cur: psycopg.Cursor, table: str, *, where: str | None = None) -> int | None:
    """`count(*)` on `table`, optionally filtered, or None if it can't be read.

    Behind a savepoint because a failed statement aborts the whole
    transaction in postgres: one table the connecting role has no SELECT on
    would otherwise take down the two sections that had already been
    gathered, turning a partial answer into no answer at all.
    """
    cur.execute("SAVEPOINT sensitive_count")
    try:
        # identifier, not a value: the table name came out of pg_class, so it
        # cannot be anything the caller chose.
        # `where` is a literal from _NEUTRALIZE_SURFACES in this file, never
        # anything a caller chose; the table name is an identifier from the
        # catalog, quoted as one.
        statement = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
        if where:
            statement = statement + sql.SQL(" WHERE ") + sql.SQL(where)  # ty: ignore[invalid-argument-type]
        cur.execute(statement)
        # read before RELEASE: executing anything on this cursor replaces the
        # result set, so fetching after it would read the RELEASE's own (empty) one
        count = _fetch_one(cur)[0]
    except psycopg.Error:
        cur.execute("ROLLBACK TO SAVEPOINT sensitive_count")
        return None

    cur.execute("RELEASE SAVEPOINT sensitive_count")
    return count


# ---------------------------------------------------------------------------
# dump / restore helpers
# ---------------------------------------------------------------------------


@contextmanager
def admin_connect():
    """Autocommit connection to `postgres` for DDL (CREATE/DROP DATABASE).

    CREATE/DROP DATABASE cannot run inside a transaction, so we force
    autocommit. Keep DDL statements narrow and short-lived.
    """
    with psycopg.connect("dbname=postgres", autocommit=True) as conn:
        yield conn


def db_exists(cur: psycopg.Cursor, name: str) -> bool:
    cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
    return cur.fetchone() is not None


def create_database(cur: psycopg.Cursor, name: str) -> None:
    cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def drop_database_if_exists(cur: psycopg.Cursor, name: str) -> None:
    cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name)))


def run_pg_dump(dbname: str, output: Path, verbose: bool = False) -> None:
    """Dump `dbname` to `output` using pg_dump custom format (`-Fc`).

    Custom format is a single self-contained file that `pg_restore -j` can
    read in parallel, so callers get parallel restore without needing the
    directory format.
    """
    cmd = ["pg_dump", "-Fc", "-f", str(output)]
    if verbose:
        cmd.append("-v")
    cmd.append(dbname)
    logger.debug("running: %s", " ".join(cmd))
    # Fixed argv[0] (pg_dump); DB name / output path are the wrapper's whole point.
    subprocess.run(cmd, check=True)  # noqa: S603


def run_pg_restore(dbname: str, backup: Path, jobs: int = 1, verbose: bool = False) -> int:
    """Restore `backup` into `dbname`. Returns pg_restore's exit code.

    Not raised on non-zero so the caller can decide whether to drop the
    freshly created database.
    """
    cmd = ["pg_restore", "--no-owner", "-x", "-j", str(jobs), "-d", dbname]
    if verbose:
        cmd.append("-v")
    cmd.append(str(backup))
    logger.debug("running: %s", " ".join(cmd))
    # Fixed argv[0] (pg_restore); DB name / backup path are the wrapper's whole point.
    return subprocess.run(cmd, check=False).returncode  # noqa: S603


def reset_all_user_passwords(cur: psycopg.Cursor, password: str) -> None:
    cur.execute("UPDATE res_users SET password = %s", (password,))


def generate_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))
