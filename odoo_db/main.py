from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Annotated

import psycopg
import typer

from odoo_db import db, output

app = typer.Typer(no_args_is_help=True)


@contextmanager
def _handle_errors(db_name: str):
    try:
        yield
    except psycopg.OperationalError as e:
        raw = str(e).strip()
        # Extract the FATAL/ERROR reason from the pg error chain
        for part in reversed(raw.split(":")):
            part = part.strip()
            if part:
                msg = part
                break
        else:
            msg = raw
        typer.echo(f"Error [{db_name}]: {msg}", err=True)
        raise typer.Exit(1) from None
    except Exception as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1) from e


_output_file: str | None = None
_output_format: str = "text"
_include_sensitive: bool = False


def version_callback(value: bool):
    if value:
        typer.echo(f"odoo-db {version('odoo-db')}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            callback=version_callback,
            is_eager=True,
            help="Display the odoo-db version.",
        ),
    ] = False,
    output_file: Annotated[str, typer.Option("--output-file")] = "-",
    output_format: Annotated[str, typer.Option("--output-format")] = "text",
    log_level: Annotated[str, typer.Option("--log-level")] = "WARNING",
    log_file: Annotated[str | None, typer.Option("--log-file")] = None,
    include_sensitive: Annotated[
        bool,
        typer.Option(
            "--include-sensitive-information",
            help="Global PII master switch: unmask identifying data (e.g. attachment filenames) "
            "in any command that would otherwise redact it. Off by default so output is safe to ship.",
        ),
    ] = False,
):
    global _output_file, _output_format, _include_sensitive
    _output_file = None if output_file == "-" else output_file
    _output_format = output_format
    _include_sensitive = include_sensitive

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(log_level.upper())

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    if log_file is not None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_path)
        fh.setFormatter(fmt)
        root.addHandler(fh)


def _writer() -> output.Writer:
    return output.Writer(_output_file, _output_format)


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@app.command(name="list")
def cmd_list(
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
):
    """List all Odoo databases: name, version, neutralized status."""
    with _handle_errors("postgres"), db.cursor("postgres") as cur:
        names = db.list_databases(cur)
    summaries = [s for name in names if (s := db.get_db_summary(name, verbose=verbose))]

    with _writer() as w:
        if w.fmt == "json":
            w.json([
                {
                    "db": s.name,
                    "version": s.version,
                    "neutralized": s.neutralized,
                    **({"modules": s.module_count, "users": s.user_count} if verbose else {}),
                }
                for s in summaries
            ])
        elif w.fmt == "prometheus":
            lines: list[str] = []
            lines.append("# HELP odoo_db_info Odoo database metadata")
            lines.append("# TYPE odoo_db_info gauge")
            for s in summaries:
                labels = f'db="{s.name}",version="{s.version}",neutralized="{str(s.neutralized).lower()}"'
                lines.append(f"odoo_db_info{{{labels}}} 1")
                if verbose:
                    lines.append(f'odoo_db_modules_installed{{db="{s.name}"}} {s.module_count}')
                    lines.append(f'odoo_db_users_active{{db="{s.name}"}} {s.user_count}')
            w.prometheus(lines)
        else:
            headers = ["database", "version", "neutralized"]
            if verbose:
                headers += ["modules", "users"]
            rows = [
                [
                    s.name,
                    s.version,
                    "yes" if s.neutralized else "no",
                    *([str(s.module_count), str(s.user_count)] if verbose else []),
                ]
                for s in summaries
            ]
            w.table(headers, rows, empty_msg="No Odoo databases found.")


# ---------------------------------------------------------------------------
# modules
# ---------------------------------------------------------------------------


@app.command()
def modules(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    include_uninstalled: Annotated[
        bool,
        typer.Option("--all", help="Also include modules that are not installed."),
    ] = False,
):
    """List modules with version for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        rows_data = db.get_modules(cur, include_uninstalled=include_uninstalled)

    with _writer() as w:
        if w.fmt == "json":
            w.json(rows_data)
        elif w.fmt == "prometheus":
            # Gauge counts installed only, even with --all.
            installed_count = sum(1 for r in rows_data if r["installed"]) if include_uninstalled else len(rows_data)
            lines = [
                "# HELP odoo_db_modules_installed Installed module count",
                "# TYPE odoo_db_modules_installed gauge",
                f'odoo_db_modules_installed{{db="{db_name}"}} {installed_count}',
            ]
            w.prometheus(lines)
        elif include_uninstalled:
            w.table(
                ["module", "version", "installed"],
                [[r["name"], r["version"], str(r["installed"])] for r in rows_data],
            )
        else:
            w.table(["module", "version"], [[r["name"], r["version"]] for r in rows_data])


# ---------------------------------------------------------------------------
# crons
# ---------------------------------------------------------------------------


@app.command()
def crons(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    running: Annotated[
        bool, typer.Option("--running", help="List crons currently running (RowShareLock on ir_cron).")
    ] = False,
    include_code: Annotated[
        bool,
        typer.Option(
            "--include-code",
            help=(
                "Show the python source of each cron's server action, if any (populated only for state='code' actions)."
            ),
        ),
    ] = False,
    include_inactive: Annotated[
        bool,
        typer.Option(
            "--all",
            help="Also include inactive crons, adding an 'active' column (ignored with --running).",
        ),
    ] = False,
):
    """List scheduled actions for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        rows_data = (
            db.get_running_crons(cur)
            if running
            else db.get_crons(cur, include_code=include_code, include_inactive=include_inactive)
        )

    with _writer() as w:
        if running:
            if w.fmt == "json":
                w.json(rows_data)
            elif w.fmt == "prometheus":
                lines = [
                    "# HELP odoo_db_crons_running Currently running cron count",
                    "# TYPE odoo_db_crons_running gauge",
                    f'odoo_db_crons_running{{db="{db_name}"}} {len(rows_data)}',
                ]
                w.prometheus(lines)
            else:
                w.table(
                    ["pid", "cron_id", "name", "model", "code", "query_start"],
                    [
                        [
                            str(r["pid"]),
                            str(r["cron_id"]) if r["cron_id"] is not None else "",
                            r["name"] or "",
                            r["model"] or "",
                            r["code"] or "",
                            r["query_start"] or "",
                        ]
                        for r in rows_data
                    ],
                )
            return

        if w.fmt == "json":
            w.json(rows_data)
        elif w.fmt == "prometheus":
            active_count = sum(1 for r in rows_data if r.get("active", True)) if include_inactive else len(rows_data)
            lines = [
                "# HELP odoo_db_crons_active Active scheduled action count",
                "# TYPE odoo_db_crons_active gauge",
                f'odoo_db_crons_active{{db="{db_name}"}} {active_count}',
            ]
            w.prometheus(lines)
        else:
            header = ["name", "interval", "nextcall"]
            if include_code:
                header.append("code")
            if include_inactive:
                header.append("active")

            def row_cells(r: dict) -> list[str]:
                cells = [r["name"], r["interval"], r["nextcall"]]
                if include_code:
                    cells.append(r.get("code") or "")
                if include_inactive:
                    cells.append(str(r.get("active")))
                return cells

            w.table(header, [row_cells(r) for r in rows_data])


# ---------------------------------------------------------------------------
# params
# ---------------------------------------------------------------------------


@app.command()
def params(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    pattern: Annotated[
        str | None,
        typer.Argument(metavar="[PATTERN]", help="Case-insensitive substring match on key."),
    ] = None,
):
    """Show ir_config_parameter keys and values for a database.

    Values of secret-bearing keys (database.secret, *.client_secret, *.api_key,
    tokens, ...) are masked as ******** by default; pass the global
    --include-sensitive-information to reveal them. Keys are always shown.

    Masking is best-effort by key name and not exhaustive — verify output
    before sharing.
    """
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        rows_data = db.get_config_parameters(cur, pattern=pattern, reveal=_include_sensitive)

    with _writer() as w:
        if w.fmt == "json":
            w.json(rows_data)
        elif w.fmt == "prometheus":
            lines = [
                "# HELP odoo_db_config_parameters Config parameter count",
                "# TYPE odoo_db_config_parameters gauge",
                f'odoo_db_config_parameters{{db="{db_name}"}} {len(rows_data)}',
            ]
            w.prometheus(lines)
        else:
            w.table(
                ["key", "value"],
                [[r["key"], r["value"] or ""] for r in rows_data],
                empty_msg="No parameters found.",
            )


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------


@app.command()
def jobs(db_name: Annotated[str, typer.Argument(metavar="DB")]):
    """List queue job counts by state for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        rows_data = db.get_jobs(cur)

    with _writer() as w:
        if rows_data is None:
            w.text("queue_job module not installed.")
            return

        if w.fmt == "json":
            w.json(rows_data)
        elif w.fmt == "prometheus":
            lines = [
                "# HELP odoo_db_queue_jobs Queue job count by state",
                "# TYPE odoo_db_queue_jobs gauge",
            ]
            for r in rows_data:
                lines.append(f'odoo_db_queue_jobs{{db="{db_name}",state="{r["state"]}"}} {r["count"]}')
            w.prometheus(lines)
        else:
            if not rows_data:
                w.text("No jobs.")
                return
            w.table(["state", "count"], [[r["state"], str(r["count"])] for r in rows_data])


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


@app.command()
def users(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    include_inactive: Annotated[
        bool,
        typer.Option("--all", help="Also include archived users."),
    ] = False,
    online: Annotated[
        bool,
        typer.Option("--online", help="Only show users currently online."),
    ] = False,
):
    """List users for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        rows_data = db.get_users(cur, include_inactive=include_inactive)

    # The gauges below count the full set, so --online only narrows the listing.
    display_rows = db.filter_online_users(rows_data) if online else rows_data

    with _writer() as w:
        if w.fmt == "json":
            w.json(display_rows)
        elif w.fmt == "prometheus":
            # Both gauges count active users only, with or without --all.
            active_rows = [r for r in rows_data if r["active"]] if include_inactive else rows_data
            connected = sum(1 for r in active_rows if r["state"] == "connected")
            lines = [
                "# HELP odoo_db_users_active Active user count",
                "# TYPE odoo_db_users_active gauge",
                f'odoo_db_users_active{{db="{db_name}"}} {len(active_rows)}',
                "# HELP odoo_db_users_connected Connected users (last 55s)",
                "# TYPE odoo_db_users_connected gauge",
                f'odoo_db_users_connected{{db="{db_name}"}} {connected}',
            ]
            w.prometheus(lines)
        elif include_inactive:
            w.table(
                ["login", "name", "state", "active"],
                [[r["login"], r["name"], r["state"], str(r["active"])] for r in display_rows],
            )
        else:
            w.table(
                ["login", "name", "state"],
                [[r["login"], r["name"], r["state"]] for r in display_rows],
            )


# ---------------------------------------------------------------------------
# groups
# ---------------------------------------------------------------------------


@app.command()
def groups(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    include_users: Annotated[bool, typer.Option("--include-users", help="Include group members' logins.")] = False,
    include_acls: Annotated[
        bool, typer.Option("--include-acls", help="Include model access rights and record rules per group.")
    ] = False,
):
    """List res.groups for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        result = db.get_groups(cur, include_users=include_users, include_acls=include_acls)

    # With --include-acls, get_groups returns {"groups", "global_acls", "global_rules"}
    # instead of a plain list — acl/rule rows with no group apply to everyone and can't
    # be attributed to a single group's per-row acls.
    if isinstance(result, dict):
        rows_data = result["groups"]
        global_acls = result["global_acls"]
        global_rules = result["global_rules"]
    else:
        rows_data = result
        global_acls = []
        global_rules = []

    with _writer() as w:
        if w.fmt == "json":
            w.json(result)
        elif w.fmt == "prometheus":
            lines = [
                "# HELP odoo_db_groups Access group count",
                "# TYPE odoo_db_groups gauge",
                f'odoo_db_groups{{db="{db_name}"}} {len(rows_data)}',
            ]
            if include_acls:
                lines += [
                    "# HELP odoo_db_groups_global_acls Model access rules granted to every user (no group)",
                    "# TYPE odoo_db_groups_global_acls gauge",
                    f'odoo_db_groups_global_acls{{db="{db_name}"}} {len(global_acls)}',
                    "# HELP odoo_db_groups_global_rules Record rules applied to every user (no group)",
                    "# TYPE odoo_db_groups_global_rules gauge",
                    f'odoo_db_groups_global_rules{{db="{db_name}"}} {len(global_rules)}',
                ]
            w.prometheus(lines)
        else:
            headers = ["id", "category", "name", "share"]
            if include_users:
                headers.append("users")
            if include_acls:
                # Two columns, not a summed total: model access rights (ir.model.access,
                # grants) and record rules (ir.rule, restricts) have opposite security
                # meaning, and collapsing them hides which one a given count refers to.
                # "(group)" flags these as per-group only - global_acls/global_rules
                # (rows with no group at all) are reported separately, see the summary
                # line below the table.
                headers += ["access (group)", "rules (group)"]
            rows = []
            for r in rows_data:
                row = [str(r["id"]), r["category"] or "", r["name"], "yes" if r["share"] else "no"]
                if include_users:
                    row.append(str(len(r["users"])))
                if include_acls:
                    row += [str(len(r["acls"]["model_access"])), str(len(r["acls"]["rules"]))]
                rows.append(row)
            w.table(headers, rows)
            if include_acls and (global_acls or global_rules):
                w.text(
                    f"\nGlobal (no group — applies to every user): "
                    f"{len(global_acls)} model access right(s), {len(global_rules)} record rule(s)."
                )


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


@app.command()
def roles(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    include_users: Annotated[bool, typer.Option("--include-users", help="Include assigned users' logins.")] = False,
    include_groups: Annotated[
        bool, typer.Option("--include-groups", help="Include the role's full resolved group set.")
    ] = False,
):
    """List res.users.role (OCA base_user_role) for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        rows_data = db.get_roles(cur, include_users=include_users, include_groups=include_groups)

    with _writer() as w:
        if rows_data is None:
            # json/prometheus consumers need valid output, not the human message —
            # the message still goes to stderr so an interactive run explains why.
            if w.fmt == "json":
                w.json([])
                typer.echo("base_user_role module not installed.", err=True)
            elif w.fmt == "prometheus":
                w.prometheus([
                    "# HELP odoo_db_roles_module_installed Whether the base_user_role module is installed",
                    "# TYPE odoo_db_roles_module_installed gauge",
                    f'odoo_db_roles_module_installed{{db="{db_name}"}} 0',
                ])
                typer.echo("base_user_role module not installed.", err=True)
            else:
                w.text("base_user_role module not installed.")
            return

        if w.fmt == "json":
            w.json(rows_data)
        elif w.fmt == "prometheus":
            lines = [
                "# HELP odoo_db_roles User role count",
                "# TYPE odoo_db_roles gauge",
                f'odoo_db_roles{{db="{db_name}"}} {len(rows_data)}',
            ]
            w.prometheus(lines)
        else:
            headers = ["id", "category", "name"]
            if include_users:
                headers.append("users")
            if include_groups:
                headers.append("groups")
            rows = []
            for r in rows_data:
                row = [str(r["id"]), r["category"] or "", r["name"]]
                if include_users:
                    row.append(str(len(r["users"])))
                if include_groups:
                    row.append(str(len(r["groups"])))
                rows.append(row)
            w.table(headers, rows)


# ---------------------------------------------------------------------------
# role-drift
# ---------------------------------------------------------------------------


@app.command(name="role-drift")
def role_drift(db_name: Annotated[str, typer.Argument(metavar="DB")]):
    """Detect drift between assigned res.users.role and actual res.groups membership."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        rows_data = db.get_role_drift(cur, include_sensitive=_include_sensitive)

    with _writer() as w:
        if rows_data is None:
            # json/prometheus consumers need valid output, not the human message —
            # the message still goes to stderr so an interactive run explains why.
            if w.fmt == "json":
                w.json([])
                typer.echo("base_user_role module not installed.", err=True)
            elif w.fmt == "prometheus":
                w.prometheus([
                    "# HELP odoo_db_role_drift_module_installed Whether the base_user_role module is installed",
                    "# TYPE odoo_db_role_drift_module_installed gauge",
                    f'odoo_db_role_drift_module_installed{{db="{db_name}"}} 0',
                ])
                typer.echo("base_user_role module not installed.", err=True)
            else:
                w.text("base_user_role module not installed.")
            return

        if w.fmt == "json":
            w.json(rows_data)
        elif w.fmt == "prometheus":
            lines = [
                "# HELP odoo_db_role_drift_users Users whose actual groups diverge from their assigned roles",
                "# TYPE odoo_db_role_drift_users gauge",
                f'odoo_db_role_drift_users{{db="{db_name}"}} {len(rows_data)}',
            ]
            w.prometheus(lines)
        else:
            if not rows_data:
                w.text("No drift detected.")
                return

            def _fmt_groups(gs: list[dict]) -> str:
                return "; ".join(f"{g['name']} ({g['category']})" if g["category"] else g["name"] for g in gs) or "-"

            # "login" is only present when --include-sensitive-information is set
            # (see db.compute_role_drift); user_id is always a safe, stable fallback.
            rows = []
            for r in rows_data:
                rows.append([
                    r.get("login", f"user_id={r['user_id']}"),
                    ", ".join(r["roles"]) or "-",
                    _fmt_groups(r["missing_groups"]),
                    _fmt_groups(r["extra_groups"]),
                ])
            w.table(["user", "roles", "missing_groups", "extra_groups"], rows)


# ---------------------------------------------------------------------------
# locks
# ---------------------------------------------------------------------------


@app.command()
def locks(db_name: Annotated[str, typer.Argument(metavar="DB")]):
    """Show active database locks for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        data = db.get_locks(cur, db_name)

    with _writer() as w:
        if w.fmt == "json":
            w.json(data)
        elif w.fmt == "prometheus":
            lines = [
                "# HELP odoo_db_locks_blocked Blocked process count",
                "# TYPE odoo_db_locks_blocked gauge",
                f'odoo_db_locks_blocked{{db="{db_name}"}} {data["blocked_count"]}',
                "# HELP odoo_db_locks_blocking Blocking process count",
                "# TYPE odoo_db_locks_blocking gauge",
                f'odoo_db_locks_blocking{{db="{db_name}"}} {data["blocking_count"]}',
            ]
            w.prometheus(lines)
        else:
            w.text(f"Blocked:  {data['blocked_count']}")
            w.text(f"Blocking: {data['blocking_count']}")
            w.text(f"Blocking (not blocked) PIDs: {data['blocking_not_blocked'] or 'none'}")
            if data["details"]:
                w.text("")
                rows = [[str(d["blocked_pid"]), ", ".join(str(p) for p in d["blocking_pids"])] for d in data["details"]]
                w.table(["blocked_pid", "blocking_pids"], rows)
            if data["queries"]:
                w.text("")
                w.text("Queries involved:")
                for pid, query in data["queries"].items():
                    w.text(f"  [{pid}] {query}")


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------


def _fmt_bytes(b: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}"
        b //= 1024
    return f"{b:.0f} TB"


@app.command()
def stats(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    years: Annotated[int, typer.Option("--years", "-y", help="Number of years to show")] = 3,
    top: Annotated[int, typer.Option("--top", "-n", help="Number of top tables to show")] = 20,
):
    """Show per-table record counts and sizes for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        model_owners = db.get_model_owners(cur)
        data = db.get_stats(cur, years=years, top=top, model_owners=model_owners)

    year_cols = data["years"]
    tables = data["tables"]

    with _writer() as w:
        if w.fmt == "json":
            w.json(data)
        elif w.fmt == "prometheus":
            lines = [
                "# HELP odoo_db_table_size_bytes Table total size in bytes",
                "# TYPE odoo_db_table_size_bytes gauge",
            ]
            for t in tables:
                lines.append(f'odoo_db_table_size_bytes{{db="{db_name}",table="{t["table"]}"}} {t["total_size_bytes"]}')
            lines += [
                "# HELP odoo_db_table_records Total record count per table",
                "# TYPE odoo_db_table_records gauge",
            ]
            for t in tables:
                lines.append(f'odoo_db_table_records{{db="{db_name}",table="{t["table"]}"}} {t["total_records"]}')
            w.prometheus(lines)
        else:
            w.text(f"Total DB size: {data['db_size']}\n")
            w.text("Columns:")
            w.text("  size    = total table size (heap + indexes + toast)")
            w.text("  indexes = sum of all index sizes")
            w.text("  attach  = attachment file sizes linked to this model (dedup by checksum)")
            w.text(f"  {'/'.join(str(y) for y in year_cols)} = records created that year")
            w.text("")
            headers = ["table", "model", "records", "size", "indexes", "attach"] + [str(y) for y in year_cols]
            rows = [
                [
                    t["table"],
                    t["model"],
                    f"{t['total_records']:,}",
                    _fmt_bytes(t["total_size_bytes"]),
                    _fmt_bytes(t["index_size_bytes"]),
                    _fmt_bytes(t["attachment_size_bytes"]),
                    *[f"{t['year_counts'].get(y, 0):,}" for y in year_cols],
                ]
                for t in tables
            ]
            footer = [
                f"TOP {len(tables)}",
                "",
                f"{sum(t['total_records'] for t in tables):,}",
                _fmt_bytes(sum(t["total_size_bytes"] for t in tables)),
                _fmt_bytes(sum(t["index_size_bytes"] for t in tables)),
                _fmt_bytes(sum(t["attachment_size_bytes"] for t in tables)),
                *[f"{sum(t['year_counts'].get(y, 0) for t in tables):,}" for y in year_cols],
            ]
            w.table(headers, rows, footer=footer)


# ---------------------------------------------------------------------------
# bloat
# ---------------------------------------------------------------------------


def _fmt_dt(value: object) -> str:
    return str(value)[:16] if value else "never"


@app.command()
def bloat(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    top: Annotated[int, typer.Option("--top", "-n", help="Top relations by size to inspect")] = 25,
    exact_max_scan_mb: Annotated[
        int,
        typer.Option(
            "--exact-max-scan",
            help="Max relation size (MB) to measure exactly with pgstattuple; larger ones are estimated.",
        ),
    ] = 2048,
):
    """Estimate table + index bloat (reclaimable by VACUUM FULL / REINDEX / dump+restore).

    Uses pgstattuple for exact numbers when the extension is installed (and the
    relation fits under --exact-max-scan), otherwise a cheap statistical
    estimate. Each row is tagged `exact` or `est`. Also flags high dead-tuple
    ratios, stale autovacuum, and unused indexes.
    """
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        data = db.get_bloat(cur, top=top, exact_max_scan_bytes=exact_max_scan_mb * 1024 * 1024)

    tables = data["tables"]
    indexes = data["indexes"]

    with _writer() as w:
        if w.fmt == "json":
            w.json(data)
        elif w.fmt == "prometheus":
            lines = [
                "# HELP odoo_db_bloat_bytes Estimated/measured reclaimable bytes per relation",
                "# TYPE odoo_db_bloat_bytes gauge",
            ]
            for t in tables:
                if t["bloat_bytes"] is not None:
                    lines.append(
                        f'odoo_db_bloat_bytes{{db="{db_name}",kind="table",'
                        f'relation="{t["table"]}",method="{t["method"]}"}} {t["bloat_bytes"]}'
                    )
            for i in indexes:
                if i["bloat_bytes"] is not None:
                    lines.append(
                        f'odoo_db_bloat_bytes{{db="{db_name}",kind="index",'
                        f'relation="{i["index"]}",method="{i["method"]}"}} {i["bloat_bytes"]}'
                    )
            w.prometheus(lines)
        else:
            if data["pgstattuple_available"]:
                w.text(
                    f"Engine: pgstattuple (exact ≤ {exact_max_scan_mb} MB) + estimate fallback  ·  "
                    f"{data['exact_count']} exact, {data['estimate_count']} estimated"
                )
            else:
                w.text("Engine: estimate only (pgstattuple not installed). Figures marked `est` are approximate.")
                w.text("  → For exact numbers: connect as superuser and run  CREATE EXTENSION pgstattuple;")
            w.text("  bloat   = dead tuples + free space, reclaimable by VACUUM FULL / REINDEX / dump+restore")
            w.text("            (autovacuum frees space inside files but never shrinks them, so bloat persists)")
            w.text("  dead %  = tuples already dead but not yet vacuumed — separate from bloat's free space")
            w.text("  scans   = index uses since last stats reset; UNUSED = 0 (drop candidate, but not PK/unique;")
            w.text("            counter resets on restore, so a fresh DB shows false UNUSED)")
            w.text("  via n/a = bloat not measurable (non-btree e.g. GIN/GiST, or expression index) → shown as —")
            since = str(data["scan_stats_since"])[:16] if data["scan_stats_since"] else "unknown"
            since_kind = "stats_reset" if data["scan_stats_since_is_reset"] else "cluster start"
            w.text(
                f"  scan stats since {since} ({since_kind}) · {data['xact_commit']:,} txns committed "
                "— UNUSED unreliable on freshly restored / low-traffic DBs"
            )
            w.text("")

            table_bloat = sum(t["bloat_bytes"] or 0 for t in tables)
            index_bloat = sum(i["bloat_bytes"] or 0 for i in indexes)
            w.text(f"Top {len(tables)} tables by size:")
            w.table(
                ["table", "size", "bloat", "bloat %", "via", "dead %", "last autovac"],
                [
                    [
                        t["table"],
                        _fmt_bytes(t["size_bytes"]),
                        _fmt_bytes(t["bloat_bytes"]) if t["bloat_bytes"] is not None else "—",
                        f"{t['bloat_pct']:.1f}%" if t["bloat_bytes"] is not None else "—",
                        t["method"],
                        f"{t['dead_pct']:.1f}%",
                        _fmt_dt(t["last_autovacuum"]),
                    ]
                    for t in tables
                ],
            )
            w.text("")
            w.text(f"Top {len(indexes)} indexes by size:")
            w.table(
                ["index", "table", "size", "bloat", "bloat %", "via", "scans"],
                [
                    [
                        i["index"],
                        i["table"],
                        _fmt_bytes(i["size_bytes"]),
                        _fmt_bytes(i["bloat_bytes"]) if i["bloat_bytes"] is not None else "—",
                        f"{i['bloat_pct']:.1f}%" if i["bloat_bytes"] is not None else "—",
                        i["method"],
                        "UNUSED" if i["unused"] else f"{i['idx_scan']:,}",
                    ]
                    for i in indexes
                ],
            )
            w.text("")
            w.text(
                f"Reclaimable in shown relations: ~{_fmt_bytes(table_bloat)} table + "
                f"~{_fmt_bytes(index_bloat)} index = ~{_fmt_bytes(table_bloat + index_bloat)}."
            )
            w.text("  Fix in place: REINDEX INDEX CONCURRENTLY <name> / VACUUM (FULL) <table>.")
            w.text("  Or reclaim all at once via a dump+restore migration (rebuilds every relation compactly).")


# ---------------------------------------------------------------------------
# attachments
# ---------------------------------------------------------------------------


@app.command()
def attachments(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    top_models: Annotated[int, typer.Option("--top-models", "-n", help="Heaviest models to show (text)")] = 25,
    top_files: Annotated[int, typer.Option("--top-files", help="Largest single attachments to list")] = 20,
    validate_orphans: Annotated[
        bool,
        typer.Option("--validate-orphans", help="Find attachments whose res_id no longer exists (heaviest models)."),
    ] = False,
    orphan_top_models: Annotated[
        int, typer.Option("--orphan-top-models", help="How many heaviest models to validate.")
    ] = 15,
    orphan_max_scan: Annotated[
        int, typer.Option("--orphan-max-scan", help="Skip orphan validation for models above this attachment count.")
    ] = 50000,
    include_individual_filenames: Annotated[
        bool,
        typer.Option(
            "--include-individual-filenames",
            help="Show real filenames in the largest-attachments list (PII). "
            "Off by default; also enabled by the global --include-sensitive-information.",
        ),
    ] = False,
):
    """Audit ir.attachment storage: repartition + cleanup/archive candidates.

    Read-only — only metadata and file_size are read, never the payload. Sizes
    are file_size sums (reliable on any storage backend). Filenames are
    redacted by default; pass --include-individual-filenames (or the global
    --include-sensitive-information) to include them.
    """
    include_filenames = _include_sensitive or include_individual_filenames
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        data = db.get_attachments_audit(
            cur,
            top_files=top_files,
            validate_orphans=validate_orphans,
            orphan_top_models=orphan_top_models,
            orphan_max_scan=orphan_max_scan,
            include_filenames=include_filenames,
        )

    st = data["storage"]
    cand = data["candidates"]
    dup = cand["duplicates"]

    with _writer() as w:
        if w.fmt == "json":
            w.json(data)
        elif w.fmt == "prometheus":
            lbl = f'db="{db_name}"'
            lines = [
                "# HELP odoo_db_attachments_total Total ir.attachment count",
                "# TYPE odoo_db_attachments_total gauge",
                f"odoo_db_attachments_total{{{lbl}}} {data['total_count']}",
                "# HELP odoo_db_attachments_size_bytes Total attachment size (file_size sum)",
                "# TYPE odoo_db_attachments_size_bytes gauge",
                f"odoo_db_attachments_size_bytes{{{lbl}}} {data['total_size_bytes']}",
                "# HELP odoo_db_attachments_storage_bytes Attachment size by storage location",
                "# TYPE odoo_db_attachments_storage_bytes gauge",
                f'odoo_db_attachments_storage_bytes{{{lbl},location="db"}} {st["db"]["size"]}',
                f'odoo_db_attachments_storage_bytes{{{lbl},location="filestore"}} {st["filestore"]["size"]}',
                f'odoo_db_attachments_storage_bytes{{{lbl},location="none"}} {st["none"]["size"]}',
                "# HELP odoo_db_attachments_public Public (URL-downloadable) attachment count",
                "# TYPE odoo_db_attachments_public gauge",
                f"odoo_db_attachments_public{{{lbl}}} {data['public']['count']}",
                "# HELP odoo_db_attachments_reclaim_bytes Reclaimable attachment bytes by bucket",
                "# TYPE odoo_db_attachments_reclaim_bytes gauge",
            ]
            reclaim = {
                "assets": cand["assets"]["size"],
                "orphan_uninstalled": cand["orphan_uninstalled"]["size"],
                "duplicates_db": dup["db_reclaim"],
            }
            for bucket, size in reclaim.items():
                lines.append(f'odoo_db_attachments_reclaim_bytes{{{lbl},bucket="{bucket}"}} {size}')
            w.prometheus(lines)
        else:
            total = data["total_size_bytes"]
            cfg = data["config"]
            w.text(f"Attachments: {data['total_count']:,}   Total size: {_fmt_bytes(total)}")
            w.text(
                f"Storage: db {_fmt_bytes(st['db']['size'])} ({st['db']['count']:,})  ·  "
                f"filestore {_fmt_bytes(st['filestore']['size'])} ({st['filestore']['count']:,})  ·  "
                f"no payload {st['none']['count']:,}"
            )
            w.text(f"Config: location={cfg['location']}  image_max={cfg['image_max']}  quality={cfg['image_quality']}")
            if data["public"]["count"]:
                w.text(
                    f"Public (URL-downloadable): {data['public']['count']:,} "
                    f"({_fmt_bytes(data['public']['size'])}) — review for sensitive data"
                )
            w.text("")
            shown = data["by_model"][:top_models]
            w.text(f"Top {len(shown)} models by attachment size:")
            w.text("  attach size = sum of file_size of attachments linked to the model (not the table's own size)")
            w.text("  avg/file = attach size / files   ·   % total = share of all attachment bytes")
            model_rows = [
                [
                    m["model"] or "(unattached)",
                    f"{m['count']:,}",
                    _fmt_bytes(m["size"]),
                    _fmt_bytes(m["size"] // m["count"]) if m["count"] else "—",
                    f"{(100 * m['size'] / total) if total else 0:.1f}%",
                ]
                for m in shown
            ]
            shown_count = sum(m["count"] for m in shown)
            shown_size = sum(m["size"] for m in shown)
            footer = [
                f"top {len(shown)}",
                f"{shown_count:,}",
                _fmt_bytes(shown_size),
                _fmt_bytes(shown_size // shown_count) if shown_count else "—",
                f"{(100 * shown_size / total) if total else 0:.1f}%",
            ]
            w.table(["res_model", "files", "attach size", "avg/file", "% total"], model_rows, footer=footer)
            w.text("")
            w.text("Largest single attachments (one file each):")
            headers = ["file size", "model", "mimetype"] + (["name"] if include_filenames else [])
            w.table(
                headers,
                [
                    [_fmt_bytes(t["size"]), t["res_model"] or "—", t["mimetype"] or "—"]
                    + ([t.get("name") or ""] if include_filenames else [])
                    for t in data["top_files"]
                ],
            )
            w.text("")
            w.text("Reclaimable — at a glance:")
            ou = cand["orphan_uninstalled"]
            scorecard = [
                [
                    "Regenerable asset bundles",
                    f"{cand['assets']['count']:,}",
                    _fmt_bytes(cand["assets"]["size"]),
                    "delete (auto-rebuilt)",
                ],
                [
                    "Uninstalled-model orphans",
                    f"{ou['count']:,}",
                    _fmt_bytes(ou["size"]),
                    "delete (verify module gone)",
                ],
                [
                    "DB->filestore migration",
                    f"{cand['db_bulk']['count']:,}",
                    _fmt_bytes(cand["db_bulk"]["size"]),
                    "move (shrinks DB)",
                ],
                [
                    "Duplicate rows",
                    f"{dup['count']:,}",
                    _fmt_bytes(dup["db_reclaim"]),
                    "DB rows; disk only from db dups",
                ],
            ]
            dead_sz = 0
            if data["orphans_validated"] is not None:
                dead_n = sum(f.get("dead_count", 0) for f in data["orphans_validated"])
                dead_sz = sum(f.get("dead_size", 0) for f in data["orphans_validated"])
                scorecard.insert(2, ["Dead-record orphans", f"{dead_n:,}", _fmt_bytes(dead_sz), "delete"])
            w.table(["action", "rows", "disk reclaim", "type"], scorecard)

            # Adaptive recommendation: safe delete-now reclaim = assets + orphans
            # (+ validated dead records). When that is a small slice of the total,
            # deletion is not the lever — storage tiering / archiving is.
            safe_disk = cand["assets"]["size"] + ou["size"] + dead_sz
            safe_pct = (100 * safe_disk / total) if total else 0
            w.text("")
            w.text(f"Safe delete-now reclaim (assets + orphans): {_fmt_bytes(safe_disk)} = {safe_pct:.1f}% of total.")
            if safe_pct < 10:
                w.text(
                    "  → Deletion is not the lever here. Offload the filestore to cheaper object storage "
                    "(S3-compatible via ir_attachment.location) and archive aged data per the buckets above."
                )
            else:
                w.text("  → Delete the safe buckets first, then weigh archiving aged data.")
            if data["orphans_validated"] is None:
                w.text("  (run with --validate-orphans to fold dead-record orphans into this number)")


# ---------------------------------------------------------------------------
# studio
# ---------------------------------------------------------------------------


@app.command()
def studio(db_name: Annotated[str, typer.Argument(metavar="DB")]):
    """Show Studio customizations: custom models, extended models, studio-flagged records."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        data = db.get_studio_customizations(cur)

    with _writer() as w:
        if w.fmt == "json":
            w.json(data)
        elif w.fmt == "prometheus":
            lines = [
                "# HELP odoo_db_studio_custom_models Studio custom model count",
                "# TYPE odoo_db_studio_custom_models gauge",
                f'odoo_db_studio_custom_models{{db="{db_name}"}} {data["custom_model_count"]}',
                "# HELP odoo_db_studio_extended_models Models extended via Studio",
                "# TYPE odoo_db_studio_extended_models gauge",
                f'odoo_db_studio_extended_models{{db="{db_name}"}} {data["extended_model_count"]}',
            ]
            w.prometheus(lines)
        else:
            w.text(f"Custom models (state=manual): {data['custom_model_count']}")
            if data["custom_models"]:
                w.table(
                    ["model", "name", "custom_fields"],
                    [[m["model"], m["name"], str(m["custom_fields"])] for m in data["custom_models"]],
                )
            w.text(f"\nModels extended via Studio: {data['extended_model_count']}")
            if data["extended_models"]:
                w.table(
                    ["model", "added_fields"],
                    [[m["model"], str(m["added_fields"])] for m in data["extended_models"]],
                )
            rbt = data["studio_records_by_type"]
            if rbt:
                total = sum(len(v) for v in rbt.values())
                w.text(f"\nStudio-flagged records (ir_model_data.studio=true): {total}")
                w.table(
                    ["type", "count"],
                    [[k, str(len(v))] for k, v in sorted(rbt.items(), key=lambda x: len(x[1]), reverse=True)],
                )
            elif not rbt and not data["custom_models"]:
                w.text("No Studio customizations detected.")


# ---------------------------------------------------------------------------
# not-odoo
# ---------------------------------------------------------------------------


@app.command(name="not-odoo")
def cmd_not_odoo(db_name: Annotated[str, typer.Argument(metavar="DB")]):
    """Show non-Odoo database objects: custom views, triggers, and functions."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        data = db.get_not_odoo(cur)

    views = data["views"]
    triggers = data["triggers"]
    functions = data["functions"]
    procedures = data["procedures"]
    recognized = data.get("recognized", {"functions": {}, "triggers": {}})
    rec_fn = recognized.get("functions", {})
    rec_tr = recognized.get("triggers", {})

    with _writer() as w:
        if w.fmt == "json":
            w.json(data)
        elif w.fmt == "prometheus":
            custom_fn = sum(1 for n in functions if n not in rec_fn)
            custom_tr = sum(1 for t in triggers if t["name"] not in rec_tr)
            lines = [
                "# HELP odoo_db_custom_views Custom (non-Odoo) view count",
                "# TYPE odoo_db_custom_views gauge",
                f'odoo_db_custom_views{{db="{db_name}"}} {len(views)}',
                "# HELP odoo_db_custom_triggers Custom trigger count (excludes recognized infra)",
                "# TYPE odoo_db_custom_triggers gauge",
                f'odoo_db_custom_triggers{{db="{db_name}"}} {custom_tr}',
                "# HELP odoo_db_custom_functions Custom function count (excludes recognized infra)",
                "# TYPE odoo_db_custom_functions gauge",
                f'odoo_db_custom_functions{{db="{db_name}"}} {custom_fn}',
                "# HELP odoo_db_custom_procedures Custom stored procedure count",
                "# TYPE odoo_db_custom_procedures gauge",
                f'odoo_db_custom_procedures{{db="{db_name}"}} {len(procedures)}',
            ]
            w.prometheus(lines)
        else:
            w.text(f"=== Views not in ir_model ({len(views)}) ===")
            if views:
                w.table(["view"], [[v] for v in views])
            else:
                w.text("(none)")

            w.text("")
            w.text(f"=== Triggers ({len(triggers)}) ===")
            if triggers:
                w.table(
                    ["table", "trigger", "timing", "events", "kind"],
                    [
                        [
                            t["table"],
                            t["name"],
                            t["timing"],
                            t["events"],
                            "recognized" if t["name"] in rec_tr else "custom",
                        ]
                        for t in triggers
                    ],
                )
            else:
                w.text("(none)")

            w.text("")
            w.text(f"=== Functions ({len(functions)}) ===")
            if functions:
                w.table(
                    ["function", "kind"],
                    [[f, "recognized" if f in rec_fn else "custom"] for f in functions],
                )
            else:
                w.text("(none)")

            w.text("")
            w.text(f"=== Stored Procedures ({len(procedures)}) ===")
            if procedures:
                w.table(["procedure"], [[p] for p in procedures])
            else:
                w.text("(none)")


# ---------------------------------------------------------------------------
# prepare-audit
# ---------------------------------------------------------------------------


def _compact_stats(stats_data: dict) -> dict:
    """Shrink stats payload for audit export.

    - empty tables (records == 0): keep only `table`, `model`,
      `functional_group`, `total_size_bytes`
    - non-empty tables: drop `table_size_bytes` (redundant); drop
      `index_size_bytes`, `attachment_size_bytes`, and `year_counts` entries
      whose values are zero; drop `year_counts` entirely when all years zero
    """
    compact_tables = []
    for t in stats_data["tables"]:
        if t["total_records"] == 0:
            compact_tables.append({
                "table": t["table"],
                "model": t["model"],
                "functional_group": t["functional_group"],
                "total_size_bytes": t["total_size_bytes"],
            })
            continue
        entry = {
            "table": t["table"],
            "model": t["model"],
            "functional_group": t["functional_group"],
            "total_records": t["total_records"],
            "total_size_bytes": t["total_size_bytes"],
        }
        if t["index_size_bytes"]:
            entry["index_size_bytes"] = t["index_size_bytes"]
        if t["attachment_size_bytes"]:
            entry["attachment_size_bytes"] = t["attachment_size_bytes"]
        year_counts = {y: c for y, c in t["year_counts"].items() if c > 0}
        if year_counts:
            entry["year_counts"] = year_counts
        compact_tables.append(entry)
    return {
        "db_size": stats_data["db_size"],
        "years": stats_data["years"],
        "tables": compact_tables,
    }


@app.command(name="prepare-audit")
def cmd_prepare_audit(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    years: Annotated[int, typer.Option("--years", "-y", help="Years for stats breakdown")] = 3,
    top: Annotated[int, typer.Option("--top", "-n", help="Top tables by size to include (0 = all)")] = 0,
    admin_users: Annotated[
        list[str] | None,
        typer.Option(
            "--admin-user",
            help="Login to exclude from customized-records scan (repeat for multiple). "
            "Use when the project admin uses a personal account instead of 'admin'.",
        ),
    ] = None,
):
    """Combine summary + modules + stats + not-odoo into a $db.json audit export.

    Output goes to ./$db.json by default; override with --output-file. Always
    written as JSON regardless of --output-format. Intended as input for the
    /odoo-dev:audit-db skill.

    Stats payload is compacted: empty tables drop year_counts/index/attachment
    fields; non-empty tables drop zero year entries. Consumers should use
    `.get(key, 0)` for the dropped fields.
    """
    with _handle_errors(db_name):
        summary = db.get_db_summary(db_name, verbose=True)
        if summary is None:
            typer.echo(f"Error [{db_name}]: not an Odoo database", err=True)
            raise typer.Exit(1)
        exclude_logins = list(admin_users) if admin_users else []
        with db.cursor(db_name) as cur:
            modules_data = db.get_modules(cur)
            module_dependents = db.get_module_dependents(cur)
            model_owners = db.get_model_owners(cur)
            stats_data = db.get_stats(cur, years=years, top=top, model_owners=model_owners)
            not_odoo_data = db.get_not_odoo(cur)
            users_by_year = db.get_users_by_year(cur)
            studio_data = db.get_studio_customizations(cur)
            orphan_fields = db.get_orphan_fields(cur)
            customized_records = db.get_customized_system_records(cur, exclude_logins=exclude_logins)
            mail_message_stats = db.get_mail_message_stats(cur)
            attachment_stats = db.get_attachment_stats(cur)
            cron_inventory = db.get_cron_inventory(cur)
            company_count = db.get_company_count(cur)
        for m in modules_data:
            m["dependent_count"] = module_dependents.get(m["name"], 0)
        orphan_tables = db.get_orphan_tables(stats_data["tables"], model_owners, modules_data)

    payload = {
        "db": summary.name,
        "version": summary.version,
        "neutralized": summary.neutralized,
        "module_count": summary.module_count,
        "user_count": summary.user_count,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "modules": modules_data,
        "model_owners": model_owners,
        "orphan_tables": orphan_tables,
        "users_by_year": users_by_year,
        "stats": _compact_stats(stats_data),
        "not_odoo": not_odoo_data,
        "studio_customizations": studio_data,
        "orphan_fields": orphan_fields,
        "customized_records": customized_records,
        "customized_records_excluded": exclude_logins if exclude_logins else [],
        "mail_message_stats": mail_message_stats,
        "attachment_stats": attachment_stats,
        "cron_inventory": cron_inventory,
        "company_count": company_count,
    }

    target = _output_file if _output_file is not None else f"{db_name}.json"
    parent = Path(target).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w") as f:
        json.dump(payload, f, indent=2, default=str)
    typer.echo(
        f"Wrote {target} "
        f"(modules={len(modules_data)}, owners={len(model_owners)}, "
        f"tables={len(stats_data['tables'])}, orphans={len(orphan_tables)}, "
        f"user_years={len(users_by_year)}, "
        f"views={len(not_odoo_data['views'])}, triggers={len(not_odoo_data['triggers'])}, "
        f"functions={len(not_odoo_data['functions'])}, procedures={len(not_odoo_data['procedures'])}, "
        f"studio_models={studio_data['custom_model_count']}, "
        f"studio_extended={studio_data['extended_model_count']}, "
        f"orphan_fields={len(orphan_fields)}, "
        f"customized_records={len(customized_records)}, "
        f"companies={company_count}, "
        f"crons={len(cron_inventory) if cron_inventory is not None else 'N/A'})"
    )


# ---------------------------------------------------------------------------
# dump
# ---------------------------------------------------------------------------


@app.command()
def dump(
    db_name: Annotated[str, typer.Argument(metavar="DB")],
    file: Annotated[
        Path | None,
        typer.Option("--file", "-f", help="Output path (default: ./<db>.pgdump)."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force/--no-force", help="Overwrite the output file if it already exists."),
    ] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Pass -v to pg_dump.")] = False,
):
    """Dump an Odoo database using pg_dump custom format (-Fc)."""
    output = file if file is not None else Path(f"{db_name}.pgdump")
    if output.exists() and not force:
        typer.echo(f"Error: {output} already exists. Pass --force to overwrite.", err=True)
        raise typer.Exit(1)
    parent = output.parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    with _handle_errors(db_name):
        db.run_pg_dump(db_name, output, verbose=verbose)
    typer.echo(f"Dumped {db_name} -> {output}")


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


@app.command()
def restore(
    backup_path: Annotated[Path, typer.Argument(metavar="BACKUP")],
    db_name: Annotated[
        str | None,
        typer.Option("--db", help="Target database name (default: derived from backup filename)."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force/--no-force", help="Drop an existing database with the same name before restoring."),
    ] = False,
    jobs: Annotated[int, typer.Option("--jobs", "-j", help="Parallel restore jobs.")] = 1,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Pass -v to pg_restore.")] = False,
    reset_passwords: Annotated[
        bool,
        typer.Option(
            "--reset-passwords",
            help="After restore, reset every res_users password. Skipped with a warning on non-Odoo DBs.",
        ),
    ] = False,
    password: Annotated[
        str | None,
        typer.Option("--password", "-P", help="Password used with --reset-passwords (default: random 16 chars)."),
    ] = None,
):
    """Restore a pg_dump backup into a new database using pg_restore."""
    if not backup_path.exists():
        typer.echo(f"Error: backup file {backup_path} not found.", err=True)
        raise typer.Exit(1)

    target = db_name if db_name is not None else backup_path.stem.replace("-", "_")

    with _handle_errors(target):
        with db.admin_connect() as conn, conn.cursor() as cur:
            if force:
                db.drop_database_if_exists(cur, target)
            elif db.db_exists(cur, target):
                typer.echo(
                    f"Error: database {target!r} already exists. Use --force to drop it, or --db to choose a name.",
                    err=True,
                )
                raise typer.Exit(1)
            db.create_database(cur, target)

        typer.echo(f"Restoring {backup_path} -> {target}...")
        rc = db.run_pg_restore(target, backup_path, jobs=jobs, verbose=verbose)
        if rc != 0:
            typer.echo(
                f"pg_restore exited with code {rc}. The database may be incomplete.",
                err=True,
            )
            if typer.confirm(f"Drop the newly created database {target!r}?", default=False):
                with db.admin_connect() as conn, conn.cursor() as cur:
                    db.drop_database_if_exists(cur, target)
                typer.echo(f"Dropped {target}.")
            raise typer.Exit(rc)

        if reset_passwords:
            with db.cursor(target) as cur:
                if not db._is_odoo(cur):
                    typer.echo(
                        f"Warning: {target!r} does not look like an Odoo database "
                        "(no ir_module_module table); skipping password reset.",
                        err=True,
                    )
                else:
                    pwd = password if password else db.generate_password()
                    db.reset_all_user_passwords(cur, pwd)
                    cur.connection.commit()
                    typer.echo(f"Reset all res_users passwords to: {pwd}")

    typer.echo(f"Restore complete -> {target}")


if __name__ == "__main__":
    app()
