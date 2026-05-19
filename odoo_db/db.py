from __future__ import annotations

import logging
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


def _is_odoo(cur: psycopg.Cursor) -> bool:
    cur.execute("SELECT 1 FROM pg_tables WHERE tablename='ir_module_module'")
    return bool(cur.fetchone())


def list_databases() -> list[str]:
    with connect("postgres") as conn, conn.cursor() as cur:
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
    try:
        with connect(dbname) as conn, conn.cursor() as cur:
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


def get_modules(dbname: str) -> list[dict]:
    with connect(dbname) as conn, conn.cursor() as cur:
        cur.execute("""
                SELECT name, latest_version
                FROM ir_module_module
                WHERE state = 'installed'
                ORDER BY name
            """)
        return [{"name": row[0], "version": row[1] or ""} for row in cur.fetchall()]


def get_model_owners(dbname: str) -> dict[str, str]:
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
    with connect(dbname) as conn, conn.cursor() as cur:
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


def get_crons(dbname: str) -> list[dict]:
    with connect(dbname) as conn, conn.cursor() as cur:
        cur.execute("""
                SELECT cron_name, interval_number, interval_type, nextcall
                FROM ir_cron
                WHERE active = true
                ORDER BY nextcall
            """)
        return [
            {
                "name": row[0],
                "interval": f"{row[1]} {row[2]}",
                "nextcall": str(row[3]),
            }
            for row in cur.fetchall()
        ]


def get_jobs(dbname: str) -> list[dict] | None:
    """Returns None if queue_job module not installed."""
    with connect(dbname) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM ir_module_module WHERE name = ANY(%s) AND state = %s",
            (["connector", "queue_job"], "installed"),
        )
        if not cur.fetchone():
            return None
        cur.execute("SELECT state, count(*) FROM queue_job GROUP BY state ORDER BY state")
        return [{"state": row[0], "count": row[1]} for row in cur.fetchall()]


def get_users(dbname: str) -> list[dict]:
    with connect(dbname) as conn, conn.cursor() as cur:
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


def get_users_by_year(dbname: str) -> dict[int, int]:
    """Return ``{year: count}`` for active users grouped by ``create_date`` year.

    Aggregate only — no PII (no login/name/email). Designed for audit export
    so leads can ship the file without an NDA.
    """
    with connect(dbname) as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT EXTRACT(year FROM create_date)::int AS yr, count(*)
            FROM res_users
            WHERE active = true AND create_date IS NOT NULL
            GROUP BY yr
            ORDER BY yr
        """)
        return {row[0]: row[1] for row in cur.fetchall()}


def get_stats(dbname: str, years: int = 3, top: int = 20) -> dict:
    with connect(dbname) as conn, conn.cursor() as cur:
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
        current_year = cur.fetchone()[0]
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
            total_counts[table] = cur.fetchone()[0]

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
        db_size = cur.fetchone()[0]

        tables = []
        for relname, total_bytes, table_bytes in size_rows:
            tables.append({
                "table": relname,
                "model": table_to_model.get(relname, ""),
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


def get_not_odoo(dbname: str) -> dict:
    with connect(dbname) as conn, conn.cursor() as cur:
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
            "triggers": {
                t["name"]: _RECOGNIZED_TRIGGERS[t["name"]] for t in triggers if t["name"] in _RECOGNIZED_TRIGGERS
            },
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
        entry["total_records"] = t.get("total_records", 0)
        entry["total_size_bytes"] = t.get("total_size_bytes", 0)
        orphans.append(entry)
    return orphans


def get_locks(dbname: str) -> dict:
    with connect(dbname) as conn, conn.cursor() as cur:
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
