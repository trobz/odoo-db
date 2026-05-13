# AGENTS.md

> Quick reference for AI coding agents.

## Project

`odoo-db` — CLI tool for Odoo database management. Connects to local PostgreSQL
via Unix socket (peer auth, no explicit credentials). Target: developers running
Odoo locally.

- **Type**: CLI (Typer)
- **Language**: Python 3.10+
- **Package manager**: [uv](https://docs.astral.sh/uv/)

## Entry Points

- `odoo_db/main.py` — CLI entry point (`odoo-db` command)

## CLI Structure

```
odoo-db [--output-file FILE] [--output-format FORMAT] COMMAND [ARGS]
```

**Global flags:**
- `--output-file` — default `-` (stdout)
- `--output-format` — `text` (default), `json`, `prometheus`

**Commands:**

| Command | Description |
|---------|-------------|
| `list` | All local Odoo DBs: name, version, neutralized status. `--verbose`: + module count, user count |
| `modules <db>` | Installed modules with version |
| `crons <db>` | Active scheduled actions |
| `jobs <db>` | Queue jobs (pg_queue_job) |
| `users <db>` | Users list |
| `locks <db>` | Active DB locks |

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
- `tests/` — Test suite (pytest)
