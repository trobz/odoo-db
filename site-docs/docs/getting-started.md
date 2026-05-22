---
icon: lucide/rocket
description: Install odoo-db and run your first command in under a minute.
tags:
  - installation
  - quickstart
  - audit
---

# Getting Started

`odoo-db` is a CLI that introspects local Odoo databases via PostgreSQL's
Unix-socket peer authentication — no credentials, no network, no live-DB
impact. It is designed for developers and migration engineers who already
have shell access to the Odoo host.

## Installation

```bash
uv tool install git+https://github.com/trobz/odoo-db
odoo-db --version
```

Requires Python 3.10+ and [uv](https://docs.astral.sh/uv/).

## First command

List every Odoo database visible to your local PostgreSQL, with its version
and neutralization status:

```bash
odoo-db list
```

Add `--verbose` to also see module and user counts per database.

## Inspect a single database

```bash
odoo-db modules <db>      # installed modules with version
odoo-db users <db>        # active users + last connection
odoo-db crons <db>        # active scheduled actions
odoo-db crons <db> --running  # crons currently executing (transient debug)
odoo-db jobs <db>         # queue_job counts by state
odoo-db locks <db>        # active PostgreSQL locks
odoo-db stats <db>        # per-table record counts and sizes
odoo-db not-odoo <db>     # custom views, triggers, functions
```

## Generate an audit bundle

The `prepare-audit` command bundles everything Trobz needs to assess a
migration into a single JSON file written next to your shell:

```bash
odoo-db prepare-audit <db>
```

The resulting `<db>.json` contains:

- module list with versions,
- per-table record counts and sizes (grouped by functional area),
- `model_owners` and `orphan_tables` (tables not owned by any installed module),
- custom views, triggers, functions and stored procedures (tagged
  `recognized` vs `custom`),
- yearly aggregate user counts (no PII — the file ships without an NDA).

Drop the file at the [free migration audit form](https://migration.trobz.com/free-audit/)
and we'll come back to you with a written assessment.

## Output formats

Every command accepts `--output-format text|json|prometheus` and
`--output-file <path>`. Combine them to feed monitoring or downstream
tooling:

```bash
odoo-db --output-format json modules my_db --output-file modules.json
odoo-db --output-format prometheus list --output-file /tmp/odoo.prom
```

## Development

```bash
git clone https://github.com/trobz/odoo-db
cd odoo-db
make install              # deps + pre-commit hooks
uv tool install --editable .   # make `odoo-db` available globally
make check                # lint, format, type-check
make test                 # pytest
```
