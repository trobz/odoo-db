# odoo-db

CLI tool for Odoo database management. Connects to local PostgreSQL via Unix
socket (peer auth — no credentials needed). Designed for developers running
Odoo locally.

## Installation

```bash
uv tool install git+https://github.com/trobz/odoo-db
```

Or for development:

```bash
git clone https://github.com/trobz/odoo-db
cd odoo-db
make install                       # install deps + pre-commit hooks
uv tool install --editable .       # make `odoo-db` available globally
```

## Usage

```
odoo-db [OPTIONS] COMMAND [DB]
```

**Global options:**

| Option | Default | Description |
|--------|---------|-------------|
| `--output-file` | `-` (stdout) | Write output to file |
| `--output-format` | `text` | Output format: `text`, `json`, `prometheus` |
| `--log-level` | `WARNING` | Logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `--log-file` | `logs/odoo-db.log` | Log file path (auto-created) |
| `--include-sensitive-information` | off | PII master switch: unmask identifying data (e.g. attachment filenames) in any command that redacts it by default |

**Commands:**

| Command | Description |
|---------|-------------|
| `list` | List all Odoo DBs with version and neutralization status |
| `modules <db>` | List installed modules with version |
| `crons <db>` | List active scheduled actions (`--all` also includes inactive ones). `--running` shows crons currently held by an Odoo worker (RowShareLock on `ir_cron`) — transient debug data, not bundled into `prepare-audit`. `--include-code` adds the python source of each `state='code'` cron (ignored with `--running`, which already always shows it) |
| `jobs <db>` | Queue job counts by state (requires `queue_job` module) |
| `params <db> [pattern]` | Show `ir_config_parameter` keys and values. Optional `pattern` narrows to keys containing it (case-insensitive substring). Values of secret-bearing keys are masked `********` by default; the global `--include-sensitive-information` reveals them |
| `users <db>` | List active users with connection status |
| `groups <db>` | List `res.groups` (category, name, share flag). `--include-users` adds each group's member logins. `--include-acls` adds per-group model access rights and record rules, plus top-level `global_acls`/`global_rules` for rows with no group at all (apply to every user — excluded from prior output, now the highest-value rows in a permission audit) |
| `roles <db>` | List `res.users.role` (requires OCA `base_user_role`; prints a message if not installed). `--include-users` adds currently-enabled assigned users' logins. `--include-groups` adds the role's full resolved group set (its own group plus all directly and transitively implied groups) |
| `role-drift <db>` | Detect drift between assigned `res.users.role` and actual `res.groups` membership (requires OCA `base_user_role`). Reports `missing_groups` (role grants a group the user doesn't actually have — sync gap) and `extra_groups` (user physically holds a role's own marker group but no role assignment — active or via another assigned role's implied closure — explains it: stale privilege from a removed/expired role, or a group granted by hand), per user. Baseline groups implied by roles but not exclusive to any of them (e.g. "Internal User") never count as drift. Each user is keyed by `user_id`; `login` (PII) is included only with the global `--include-sensitive-information` flag. `--output-format prometheus` exposes a `odoo_db_role_drift_users` gauge for alerting |
| `locks <db>` | Show active PostgreSQL locks |
| `stats <db>` | Per-table record counts and sizes by year (`--years N`, `--top N`). Tables with 0-byte heap are reported as empty without running `count(*)` |
| `bloat <db>` | Estimate table + index bloat — space reclaimable by `VACUUM FULL` / `REINDEX` / a dump+restore migration (autovacuum never returns it). Uses `pgstattuple` for exact figures when the extension is installed and the relation fits under `--exact-max-scan` (MB), else a cheap statistical estimate; each row is tagged `exact`/`est`. Also flags high dead-tuple ratios, stale autovacuum, and unused indexes (`idx_scan = 0`) |
| `studio <db>` | Show Studio customizations: custom models, models extended with Studio fields, and studio-flagged record counts by type |
| `not-odoo <db>` | Show non-Odoo database objects: custom views, triggers, functions, and stored procedures. Triggers/functions are tagged `recognized` (known infra: `unaccent`, `queue_job_notify`, …) or `custom` |
| `attachments <db>` | Read-only `ir.attachment` storage audit: repartition (storage location, by-model, mimetype family, size distribution, growth by year, largest files) plus cleanup/archive candidates (uninstalled-model orphans, regenerable asset bundles, duplicate checksums, aged transient, DB-stored bulk). `--validate-orphans` adds dead-`res_id` detection for the heaviest models. Sizes are `file_size` sums (reliable on any backend; payloads never read). Filenames are redacted by default — pass `--include-individual-filenames` (or the global `--include-sensitive-information`) to show them |
| `prepare-audit <db>` | Bundle summary + modules + `model_owners` + `orphan_tables` + `users_by_year` + stats + not-odoo + `studio_customizations` into `<db>.json` (in the current directory) for `/odoo-dev:audit-db` (`--years N`, `--top N`; `--top 0` means all tables). `orphan_tables` flags tables not owned by any installed module (reason: `uninstalled_module` or `no_ownership_data`). Every table in `stats.tables` and `orphan_tables` carries `functional_group` (first underscore component) for display-time grouping by functional area. `users_by_year` is an aggregate `{year: count}` of active users by `create_date` year — zero PII so the file can ship without an NDA. `studio_customizations` includes custom model list, extended model list, and studio-flagged record counts by type. The deep attachment audit is intentionally a separate command (`attachments`), not bundled here |

## Examples

```bash
# List all local Odoo databases
odoo-db list

# Verbose: also show module count and user count
odoo-db list --verbose

# Output as JSON
odoo-db --output-format json list

# Export prometheus metrics to file
odoo-db --output-format prometheus --output-file /tmp/odoo.prom list

# Show installed modules for a specific database
odoo-db modules my_db

# Show queue jobs
odoo-db jobs my_db

# Show all config parameters
odoo-db params my_db

# Show config parameters with "mail" in the key
odoo-db params my_db mail

# Reveal masked secret values (database.secret, api keys, ...)
odoo-db --include-sensitive-information params my_db

# List access groups, with members and ACLs
odoo-db groups my_db --include-users --include-acls

# List user roles (base_user_role), with resolved group sets
odoo-db --output-format json roles my_db --include-users --include-groups

# Per-table stats: record counts and sizes for last 3 years
odoo-db stats my_db

# Top 10 tables, last 5 years
odoo-db stats my_db --top 10 --years 5

# Estimate table + index bloat (reclaimable space)
odoo-db bloat my_db

# Debug mode with full logging
odoo-db --log-level debug list

# Show Studio customizations (custom models, extended models, flagged records)
odoo-db studio my_db

# Show non-Odoo objects: custom views, triggers, functions, stored procedures
odoo-db not-odoo my_db

# Export not-odoo report as JSON
odoo-db --output-format json not-odoo my_db

# Audit ir.attachment storage (repartition + cleanup candidates)
odoo-db attachments my_db

# Also validate dead-record orphans, and show real filenames (PII)
odoo-db --include-sensitive-information attachments my_db --validate-orphans

# Full attachment audit as JSON
odoo-db --output-format json attachments my_db

# Prepare an audit bundle (writes ./my_db.json in the current directory)
odoo-db prepare-audit my_db

# Custom output path
odoo-db --output-file /tmp/audit.json prepare-audit my_db
```

## Dev

```bash
make install   # Install deps + pre-commit hooks
make check     # Lint, format, type-check
make test      # Run tests
make docs      # Build the Zensical documentation site (site-docs/)
make docs-serve  # Serve the docs locally
```
