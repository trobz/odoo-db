# AGENTS.md

> Quick reference for AI coding agents.

## Agent Discipline

**After every change, always:**
1. Update `README.md` and `AGENTS.md` to reflect changes (new flags, commands, behavior).
2. Run `make check` (lint + format + type-check) before committing.
3. Run pre-commit: `uv run pre-commit run -a` or via `make check`.

Never skip these steps. They catch regressions and keep docs in sync.

---

## Project

`odoo-db` — CLI tool for Odoo database management. Connects to local PostgreSQL
via Unix socket (peer auth, no explicit credentials). Target: developers running
Odoo locally.

- **Type**: CLI (Typer)
- **Language**: Python 3.10+
- **Package manager**: [uv](https://docs.astral.sh/uv/)

## Entry Points

- `odoo_db/main.py` — CLI entry point (`odoo-db` command)
- `odoo_db/db.py` — all DB queries (psycopg3)
- `odoo_db/output.py` — output formatting (text/json/prometheus)

## CLI Structure

```
odoo-db [--output-file FILE] [--output-format FORMAT] [--log-level LEVEL] [--log-file FILE] COMMAND [ARGS]
```

**Global flags:**
- `--output-file` — default `-` (stdout)
- `--output-format` — `text` (default), `json`, `prometheus`
- `--log-level` — `DEBUG`, `INFO`, `WARNING` (default), `ERROR`
- `--log-file` — default `logs/odoo-db.log` (auto-created, gitignored)

**Commands:**

| Command | Description |
|---------|-------------|
| `list` | All local Odoo DBs: name, version, neutralized status. `--verbose`: + module count, user count |
| `modules <db>` | Installed modules with version |
| `crons <db>` | Active scheduled actions |
| `jobs <db>` | Queue job counts by state (returns message if queue_job not installed) |
| `users <db>` | Active users with connection status (via bus_presence if available) |
| `locks <db>` | Active DB locks (blocked/blocking PIDs + queries) |
| `stats <db>` | Per-table record counts and sizes by year; `--years N` (default 3), `--top N` (default 20) |
| `not-odoo <db>` | Show non-Odoo objects: custom views (not in ir_model), triggers, functions, and stored procedures |

**Key SQL for `list`:**
```sql
-- Odoo version (keep first 2 numbers)
SELECT latest_version FROM ir_module_module WHERE name = 'base';
-- Neutralized?
SELECT value FROM ir_config_parameter WHERE key = 'database.is_neutralized';
-- Verbose: installed module count
SELECT count(*) FROM ir_module_module WHERE state = 'installed';
-- Verbose: user count
SELECT count(*) FROM res_users WHERE active = true;
```

## DB Connection

Connect via psycopg3 (Unix socket, peer auth):
```python
psycopg.connect(dbname=db_name)  # no host/user needed for local socket
```

## Logging

- Console handler always active.
- File handler writes to `--log-file` (default `logs/odoo-db.log`), parent dir auto-created.
- `logs/*.log` is gitignored; `logs/.gitkeep` tracks the directory.

## Dev Commands

Run `make help` for all commands. Key ones:

```
make install   # Install deps + pre-commit hooks
make check     # Lint, format, type-check
make test      # Run pytest
```

## Key Files

- `Makefile` — Project commands
- `pyproject.toml` — Dependencies and build config
- `ruff.toml` — Linter/formatter rules
- `logs/` — Log output directory (`.gitkeep` tracked, `*.log` gitignored)
- `tests/` — Test suite (pytest)
