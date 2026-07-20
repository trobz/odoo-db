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
* `--include-sensitive-information`: Global PII master switch: unmask identifying data (e.g. attachment filenames) in any command that would otherwise redact it. Off by default so output is safe to ship.
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
* `bloat`: Estimate table + index bloat (reclaimable...
* `attachments`: Audit ir.attachment storage: repartition +...
* `studio`: Show Studio customizations: custom models,...
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
* `--include-code`: Show the python source of each cron&#x27;s server action, if any (populated only for state=&#x27;code&#x27; actions).
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

## `odoo-db bloat`

Estimate table + index bloat (reclaimable by VACUUM FULL / REINDEX / dump+restore).

Uses pgstattuple for exact numbers when the extension is installed (and the
relation fits under --exact-max-scan), otherwise a cheap statistical
estimate. Each row is tagged `exact` or `est`. Also flags high dead-tuple
ratios, stale autovacuum, and unused indexes.

**Usage**:

```console
$ odoo-db bloat [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `-n, --top INTEGER`: Top relations by size to inspect  \[default: 25\]
* `--exact-max-scan INTEGER`: Max relation size (MB) to measure exactly with pgstattuple; larger ones are estimated.  \[default: 2048\]
* `--help`: Show this message and exit.

## `odoo-db attachments`

Audit ir.attachment storage: repartition + cleanup/archive candidates.

Read-only — only metadata and file_size are read, never the payload. Sizes
are file_size sums (reliable on any storage backend). Filenames are
redacted by default; pass --include-individual-filenames (or the global
--include-sensitive-information) to include them.

**Usage**:

```console
$ odoo-db attachments [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `-n, --top-models INTEGER`: Heaviest models to show (text)  \[default: 25\]
* `--top-files INTEGER`: Largest single attachments to list  \[default: 20\]
* `--validate-orphans`: Find attachments whose res_id no longer exists (heaviest models).
* `--orphan-top-models INTEGER`: How many heaviest models to validate.  \[default: 15\]
* `--orphan-max-scan INTEGER`: Skip orphan validation for models above this attachment count.  \[default: 50000\]
* `--include-individual-filenames`: Show real filenames in the largest-attachments list (PII). Off by default; also enabled by the global --include-sensitive-information.
* `--help`: Show this message and exit.

## `odoo-db studio`

Show Studio customizations: custom models, extended models, studio-flagged records.

**Usage**:

```console
$ odoo-db studio [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

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
* `--admin-user TEXT`: Login to exclude from customized-records scan (repeat for multiple). Use when the project admin uses a personal account instead of &#x27;admin&#x27;.
* `--help`: Show this message and exit.
