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

# Debug mode with full logging
odoo-db --log-level debug list
```

## Dev

```bash
make install   # Install deps + pre-commit hooks
make check     # Lint, format, type-check
make test      # Run tests
```
