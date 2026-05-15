from __future__ import annotations

import logging
from contextlib import contextmanager
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
    log_file: Annotated[str, typer.Option("--log-file")] = "logs/odoo-db.log",
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
    with _handle_errors("postgres"):
        names = db.list_databases()
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
    with _handle_errors(db_name):
        rows_data = db.get_modules(db_name)

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
def crons(db_name: Annotated[str, typer.Argument(metavar="DB")]):
    """List active scheduled actions for a database."""
    with _handle_errors(db_name):
        rows_data = db.get_crons(db_name)

    with _writer() as w:
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
    with _handle_errors(db_name):
        rows_data = db.get_jobs(db_name)

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
    with _handle_errors(db_name):
        rows_data = db.get_users(db_name)

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
    with _handle_errors(db_name):
        data = db.get_locks(db_name)

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


if __name__ == "__main__":
    app()
