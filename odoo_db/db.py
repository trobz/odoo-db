from __future__ import annotations

import logging
import math
from contextlib import contextmanager
from dataclasses import dataclass

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

            cur.execute("SELECT value FROM ir_config_parameter WHERE key='database.is_neutralized'")
            row = cur.fetchone()
            neutralized = row is not None and row[0] == "True"

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


def get_modules(cur: psycopg.Cursor) -> list[dict]:
    cur.execute("""
        SELECT name, latest_version, auto_install
        FROM ir_module_module
        WHERE state = 'installed'
        ORDER BY name
    """)
    return [{"name": row[0], "version": row[1] or "", "auto_install": bool(row[2])} for row in cur.fetchall()]


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


def get_crons(cur: psycopg.Cursor, *, include_code: bool = False, include_inactive: bool = False) -> list[dict]:
    where_clause = sql.SQL("") if include_inactive else sql.SQL("WHERE ic.active = true")
    cur.execute(
        sql.SQL("""
            SELECT ic.cron_name, ic.interval_number, ic.interval_type, ic.nextcall, ias.code, ic.active
            FROM ir_cron ic
            LEFT JOIN ir_act_server ias ON ias.id = ic.ir_actions_server_id
            {where}
            ORDER BY ic.nextcall
        """).format(where=where_clause)
    )
    return [
        {
            "name": row[0],
            "interval": f"{row[1]} {row[2]}",
            "nextcall": str(row[3]),
            **({"code": (row[4] or "").strip() or None} if include_code else {}),
            **({"active": row[5]} if include_inactive else {}),
        }
        for row in cur.fetchall()
    ]


def get_running_crons(cur: psycopg.Cursor) -> list[dict]:
    """List crons currently held by an Odoo worker (RowShareLock on ir_cron).

    Odoo cron acquires the row with `SELECT ... FOR NO KEY UPDATE`, which
    writes the locking txn id into the row's `xmax` system column. We join
    pg_locks → pg_stat_activity → ir_cron via `xmax = backend_xid` to resolve
    the exact cron being executed (the current `query` text has moved on to
    the cron's workload by the time we look).
    """
    cur.execute("""
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
        FROM pg_locks l
        JOIN pg_class c ON l.relation = c.oid
        JOIN pg_stat_activity a ON a.pid = l.pid
        LEFT JOIN ir_cron ic ON ic.xmax = a.backend_xid
        LEFT JOIN ir_act_server ias ON ias.id = ic.ir_actions_server_id
        LEFT JOIN ir_model im ON im.id = ias.model_id
        WHERE c.relname = 'ir_cron' AND l.mode = 'RowShareLock'
    """)
    results: list[dict] = []
    for pid, cron_id, name, model, code, state, query_start, usename, app_name, query in cur.fetchall():
        results.append({
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
        })
    return results


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


def get_users(cur: psycopg.Cursor) -> list[dict]:
    cur.execute("SELECT tablename FROM pg_tables WHERE tablename IN ('mail_presence', 'bus_presence')")
    presence_tables = {row[0] for row in cur.fetchall()}

    if "mail_presence" in presence_tables:
        # Odoo 19+: mail.presence with direct status column
        cur.execute("""
            SELECT ru.login, rp.name, COALESCE(mp.status, 'offline') AS state
            FROM res_users ru
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            LEFT JOIN mail_presence mp ON mp.user_id = ru.id
            WHERE ru.active = TRUE
            ORDER BY ru.login
        """)
    elif "bus_presence" in presence_tables:
        # Odoo 14-18: bus.presence.status is updated in real-time (HTTP and WebSocket)
        cur.execute("""
            SELECT ru.login, rp.name, COALESCE(bp.status, 'offline') AS state
            FROM res_users ru
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            LEFT JOIN bus_presence bp ON bp.user_id = ru.id
            WHERE ru.active = TRUE
            ORDER BY ru.login
        """)
    else:
        cur.execute("""
            SELECT ru.login, rp.name, 'unknown' AS state
            FROM res_users ru
            LEFT JOIN res_partner rp ON ru.partner_id = rp.id
            WHERE ru.active = TRUE
            ORDER BY ru.login
        """)
    return [{"login": row[0], "name": row[1] or "", "state": row[2]} for row in cur.fetchall()]


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
