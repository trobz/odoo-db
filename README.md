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

**Commands:**

| Command | Description |
|---------|-------------|
| `list` | List all Odoo DBs with version and neutralization status |
| `modules <db>` | List installed modules with version |
| `crons <db>` | List active scheduled actions |
| `jobs <db>` | Queue job counts by state (requires `queue_job` module) |
| `users <db>` | List active users with connection status |
| `locks <db>` | Show active PostgreSQL locks |
| `stats <db>` | Per-table record counts and sizes by year (`--years N`, `--top N`) |
| `not-odoo <db>` | Show non-Odoo database objects: custom views, triggers, functions, and stored procedures |
| `prepare-audit <db>` | Bundle summary + modules + `model_owners` + `orphan_tables` + stats + not-odoo into `<db>.json` (in the current directory) for `/odoo-dev:audit-db` (`--years N`, `--top N`; `--top 0` means all tables). `orphan_tables` flags tables not owned by any installed module (reason: `uninstalled_module` or `no_ownership_data`) |

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

# Per-table stats: record counts and sizes for last 3 years
odoo-db stats my_db

# Top 10 tables, last 5 years
odoo-db stats my_db --top 10 --years 5

# Debug mode with full logging
odoo-db --log-level debug list

# Show non-Odoo objects: custom views, triggers, functions, stored procedures
odoo-db not-odoo my_db

# Export not-odoo report as JSON
odoo-db --output-format json not-odoo my_db

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
```
