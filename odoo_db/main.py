from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
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


@app.callback()
def main(
    output_file: Annotated[str, typer.Option("--output-file")] = "-",
    output_format: Annotated[str, typer.Option("--output-format")] = "text",
    log_level: Annotated[str, typer.Option("--log-level")] = "WARNING",
    log_file: Annotated[str | None, typer.Option("--log-file")] = None,
):
    global _output_file, _output_format
    _output_file = None if output_file == "-" else output_file
    _output_format = output_format

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
def modules(db_name: Annotated[str, typer.Argument(metavar="DB")]):
    """List installed modules with version for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        rows_data = db.get_modules(cur)

    with _writer() as w:
        if w.fmt == "json":
            w.json(rows_data)
        elif w.fmt == "prometheus":
            lines = [
                "# HELP odoo_db_modules_installed Installed module count",
                "# TYPE odoo_db_modules_installed gauge",
                f'odoo_db_modules_installed{{db="{db_name}"}} {len(rows_data)}',
            ]
            w.prometheus(lines)
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
):
    """List active scheduled actions for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        rows_data = db.get_running_crons(cur) if running else db.get_crons(cur)

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
            lines = [
                "# HELP odoo_db_crons_active Active scheduled action count",
                "# TYPE odoo_db_crons_active gauge",
                f'odoo_db_crons_active{{db="{db_name}"}} {len(rows_data)}',
            ]
            w.prometheus(lines)
        else:
            w.table(
                ["name", "interval", "nextcall"],
                [[r["name"], r["interval"], r["nextcall"]] for r in rows_data],
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
def users(db_name: Annotated[str, typer.Argument(metavar="DB")]):
    """List active users for a database."""
    with _handle_errors(db_name), db.cursor(db_name) as cur:
        rows_data = db.get_users(cur)

    with _writer() as w:
        if w.fmt == "json":
            w.json(rows_data)
        elif w.fmt == "prometheus":
            connected = sum(1 for r in rows_data if r["state"] == "connected")
            lines = [
                "# HELP odoo_db_users_active Active user count",
                "# TYPE odoo_db_users_active gauge",
                f'odoo_db_users_active{{db="{db_name}"}} {len(rows_data)}',
                "# HELP odoo_db_users_connected Connected users (last 55s)",
                "# TYPE odoo_db_users_connected gauge",
                f'odoo_db_users_connected{{db="{db_name}"}} {connected}',
            ]
            w.prometheus(lines)
        else:
            w.table(
                ["login", "name", "state"],
                [[r["login"], r["name"], r["state"]] for r in rows_data],
            )


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


if __name__ == "__main__":
    app()
