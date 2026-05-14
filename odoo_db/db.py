from __future__ import annotations

import logging
from contextlib import contextmanager
from dataclasses import dataclass

import psycopg
from psycopg import sql

logger = logging.getLogger(__name__)


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

        # Table sizes (top N by total size)
        cur.execute(
            """
            SELECT relname,
                pg_total_relation_size(relid) AS total_bytes,
                pg_relation_size(relid) AS table_bytes
            FROM pg_statio_user_tables
            WHERE relname = ANY(%s)
            ORDER BY total_bytes DESC
            LIMIT %s
        """,
            (all_tables, top),
        )
        size_rows = cur.fetchall()
        top_tables = [row[0] for row in size_rows]

        # Year columns for the report
        cur.execute("SELECT EXTRACT(year FROM NOW())::int")
        current_year = cur.fetchone()[0]
        year_cols = list(range(current_year - years + 1, current_year + 1))

        # Records per year per table
        table_year_counts: dict[str, dict[int, int]] = {}
        for table in top_tables:
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

        # Total record count per table
        total_counts: dict[str, int] = {}
        for table in top_tables:
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
        index_sizes = {row[0]: row[1] for row in cur.fetchall()}

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
        attachment_by_model = {row[0]: row[1] for row in cur.fetchall()}
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
