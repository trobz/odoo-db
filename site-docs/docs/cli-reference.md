---
icon: lucide/book
description: Complete reference for every odoo-db command, auto-generated from --help.
tags:
  - reference
  - cli
---

!!! info "Auto-generated"
    This page is regenerated from `odoo-db --help` by `make cli-docs`.
    Do not edit by hand — your changes will be overwritten on the next build.

# `odoo-db`

**Usage**:

```console
$ odoo-db [OPTIONS] COMMAND [ARGS]...
```

**Options**:

* `--output-file TEXT`: \[default: -\]
* `--output-format TEXT`: \[default: text\]
* `--log-level TEXT`: \[default: WARNING\]
* `--log-file TEXT`
* `--install-completion`: Install completion for the current shell.
* `--show-completion`: Show completion for the current shell, to copy it or customize the installation.
* `--help`: Show this message and exit.

**Commands**:

* `list`: List all Odoo databases: name, version,...
* `modules`: List installed modules with version for a...
* `crons`: List active scheduled actions for a database.
* `jobs`: List queue job counts by state for a...
* `users`: List active users for a database.
* `locks`: Show active database locks for a database.
* `stats`: Show per-table record counts and sizes for...
* `not-odoo`: Show non-Odoo database objects: custom...
* `prepare-audit`: Combine summary + modules + stats +...

## `odoo-db list`

List all Odoo databases: name, version, neutralized status.

**Usage**:

```console
$ odoo-db list [OPTIONS]
```

**Options**:

* `-v, --verbose`
* `--help`: Show this message and exit.

## `odoo-db modules`

List installed modules with version for a database.

**Usage**:

```console
$ odoo-db modules [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--help`: Show this message and exit.

## `odoo-db crons`

List active scheduled actions for a database.

**Usage**:

```console
$ odoo-db crons [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--running`: List crons currently running (RowShareLock on ir_cron).
* `--help`: Show this message and exit.

## `odoo-db jobs`

List queue job counts by state for a database.

**Usage**:

```console
$ odoo-db jobs [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--help`: Show this message and exit.

## `odoo-db users`

List active users for a database.

**Usage**:

```console
$ odoo-db users [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--help`: Show this message and exit.

## `odoo-db locks`

Show active database locks for a database.

**Usage**:

```console
$ odoo-db locks [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--help`: Show this message and exit.

## `odoo-db stats`

Show per-table record counts and sizes for a database.

**Usage**:

```console
$ odoo-db stats [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `-y, --years INTEGER`: Number of years to show  \[default: 3\]
* `-n, --top INTEGER`: Number of top tables to show  \[default: 20\]
* `--help`: Show this message and exit.

## `odoo-db not-odoo`

Show non-Odoo database objects: custom views, triggers, and functions.

**Usage**:

```console
$ odoo-db not-odoo [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--help`: Show this message and exit.

## `odoo-db prepare-audit`

Combine summary + modules + stats + not-odoo into a $db.json audit export.

Output goes to ./$db.json by default; override with --output-file. Always
written as JSON regardless of --output-format. Intended as input for the
/odoo-dev:audit-db skill.

Stats payload is compacted: empty tables drop year_counts/index/attachment
fields; non-empty tables drop zero year entries. Consumers should use
`.get(key, 0)` for the dropped fields.

**Usage**:

```console
$ odoo-db prepare-audit [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `-y, --years INTEGER`: Years for stats breakdown  \[default: 3\]
* `-n, --top INTEGER`: Top tables by size to include (0 = all)  \[default: 0\]
* `--help`: Show this message and exit.
