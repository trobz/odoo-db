# AGENTS.md

> Quick reference for AI coding agents.

## Agent Discipline

**After every change, always:**
1. Update `README.md` and `AGENTS.md` to reflect changes (new flags, commands, behavior).
2. Run `make check` (lint + format + type-check) before committing.
3. Run pre-commit: `uv run pre-commit run -a` or via `make check`.
4. If you touched the CLI surface (new command/flag/option), run `make cli-docs` and commit the regenerated `site-docs/docs/cli-reference.md`. The Documentation CI workflow also regenerates it on every push to `main`, but committing keeps PR diffs honest.

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

**Commands:** see `site-docs/docs/cli-reference.md` — auto-generated from
the Typer app, exhaustive, always current. Read that file (or run
`odoo-db <cmd> --help`) instead of relying on a hand-maintained list here.

Non-obvious behavior worth keeping in agent context (the bits a `--help`
dump won't tell you):

- `prepare-audit` writes `<db>.json` to the current working directory and
  bundles summary + modules + `model_owners` + `orphan_tables` +
  `users_by_year` + stats + not-odoo. `model_owners` is derived from
  `ir_model_data` + `ir_model_relation` (authoritative, not heuristic).
  `orphan_tables` carries a `reason`: `uninstalled_module` (owner
  uninstalled) or `no_ownership_data` (legacy / raw-SQL / custom).
  `users_by_year` is an aggregate `{year: count}` — zero PII so the file
  can ship without an NDA.
- Every table in `stats.tables` and `orphan_tables` gets a
  `functional_group` = first underscore component of the table name
  (`purchase_order_line` → `purchase`). Display-only bucket, **not** an
  owner attribution — owner lookup goes through `model_owners`.
- `stats` skips `count(*)` on tables whose heap is 0 bytes
  (`total_records=0`, empty `year_counts`) to avoid scanning empty
  partitions.
- `not-odoo` tags each trigger/function as `recognized` (known infra like
  `unaccent`, `queue_job_notify`) or `custom`. The allowlist lives in
  `odoo_db/db.py` under `recognized.functions` / `recognized.triggers`.
- `crons --running` is transient debug data, intentionally excluded from
  `prepare-audit`.

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
make docs      # Build Zensical docs (site-docs/) → docs site at site-docs/site
make docs-serve  # Serve docs locally with live reload
```

The documentation site lives under `site-docs/` (Zensical, enabled via
`enable_docs_site: true` in `.copier-answers.yml`). The landing page is
overridden in `site-docs/overrides/landing.html`; `getting-started.md`
documents the CLI for the free-audit landing on `migration.trobz.com`.

## Key Files

- `Makefile` — Project commands
- `pyproject.toml` — Dependencies and build config
- `ruff.toml` — Linter/formatter rules
- `logs/` — Log output directory (`.gitkeep` tracked, `*.log` gitignored)
- `prepare-audit` writes `<db>.json` to the current working directory; `/*.json` at the repo root is gitignored so local audit dumps stay untracked when running from the source tree.
- `tests/` — Test suite (pytest)
