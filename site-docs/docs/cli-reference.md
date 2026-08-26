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

* `-V, --version`: Display the odoo-db version.
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
* `modules`: List modules with version for a database.
* `crons`: List scheduled actions for a database.
* `params`: Show ir_config_parameter keys and values...
* `mail`: Audit outbound mail configuration: config...
* `jobs`: List queue job counts by state for a...
* `users`: List users for a database.
* `groups`: List res.groups for a database.
* `roles`: List res.users.role (OCA base_user_role)...
* `role-drift`: Detect drift between assigned...
* `locks`: Show active database locks for a database.
* `stats`: Show per-table record counts and sizes for...
* `bloat`: Estimate table + index bloat (reclaimable...
* `attachments`: Audit ir.attachment storage: repartition +...
* `studio`: Show Studio customizations: custom models,...
* `check-sensitive-information`: Surface secrets a database still carries:...
* `not-odoo`: Show non-Odoo database objects: custom...
* `prepare-audit`: Combine summary + modules + stats +...
* `dump`: Dump an Odoo database using pg_dump custom...
* `restore`: Restore a pg_dump backup into a new...

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

List modules with version for a database.

**Usage**:

```console
$ odoo-db modules [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--all`: Also include modules that are not installed.
* `--help`: Show this message and exit.

## `odoo-db crons`

List scheduled actions for a database.

**Usage**:

```console
$ odoo-db crons [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--running`: List crons currently running (RowShareLock on ir_cron).
* `--include-code`: Show the python source of each cron&#x27;s server action, if any (populated only for state=&#x27;code&#x27; actions).
* `--all`: Also include inactive crons, adding an &#x27;active&#x27; column (ignored with --running).
* `--help`: Show this message and exit.

## `odoo-db params`

Show ir_config_parameter keys and values for a database.

Values of secret-bearing keys (database.secret, *.client_secret, *.api_key,
tokens, ...) are masked as ******** by default; pass the global
--include-sensitive-information to reveal them. Keys are always shown.

Masking is best-effort by key name and not exhaustive — verify output
before sharing.

**Usage**:

```console
$ odoo-db params [OPTIONS] DB [PATTERN]
```

**Arguments**:

* `DB`: \[required\]
* `[PATTERN]`: Case-insensitive substring match on key.

**Options**:

* `--help`: Show this message and exit.

## `odoo-db mail`

Audit outbound mail configuration: config keys, alias domains, addresses, relays, mass_mailing.

Ports a script that checked the same things through the Odoo API
(odooly) to direct SQL — none of this data needs auth. Company/system
(OdooBot)/admin email addresses are organizational mailboxes, not
individual PII, so they&#x27;re shown as-is; `ir_mail_server.smtp_user`/
`smtp_pass` are real credentials and stay masked — pass the global
--include-sensitive-information to reveal them.

On Odoo 17+, four of the config parameters above —
`mail.catchall.domain`/`mail.bounce.alias`/`mail.catchall.alias`/
`mail.default.from` — are legacy (read only by a one-time migration
helper) and shown alongside the per-company `mail.alias.domain` records
that actually control bounce/catchall/default-from routing now.
`mail.default.from_filter` is not part of that migration and stays
live at runtime on 17.0-19.0 (`IrMailServer._get_default_from_filter`).

Flags a neutralized database (`database.is_neutralized`, set by
`base/data/neutralize.sql` on 16.0-19.0) up front — the single most
common reason mail never leaves an Odoo database — and marks the
stub relay it inserts (`is_neutralization_stub`) so it isn&#x27;t mistaken
for a real, working server.

**Usage**:

```console
$ odoo-db mail [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

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

List users for a database.

**Usage**:

```console
$ odoo-db users [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--all`: Also include archived users.
* `--online`: Only show users currently online.
* `--help`: Show this message and exit.

## `odoo-db groups`

List res.groups for a database.

**Usage**:

```console
$ odoo-db groups [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--include-users`: Include group members&#x27; logins.
* `--include-acls`: Include model access rights and record rules per group.
* `--help`: Show this message and exit.

## `odoo-db roles`

List res.users.role (OCA base_user_role) for a database.

**Usage**:

```console
$ odoo-db roles [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `--include-users`: Include assigned users&#x27; logins.
* `--include-groups`: Include the role&#x27;s full resolved group set.
* `--help`: Show this message and exit.

## `odoo-db role-drift`

Detect drift between assigned res.users.role and actual res.groups membership.

**Usage**:

```console
$ odoo-db role-drift [OPTIONS] DB
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

## `odoo-db check-sensitive-information`

Surface secrets a database still carries: config keys, mail relay credentials, custom credential tables.

The question to answer before a dump leaves the building, and the one
neutralizing a copy does *not* answer: `neutralize` clears what a
database can still do, and only for modules that ship a neutralize.sql
— a client&#x27;s own module never does, so its API keys survive intact.

Values are masked by default; pass the global
--include-sensitive-information to reveal them. Detection is by name
(`_is_sensitive_key`, `_SENSITIVE_TABLE_MARKERS`) and best-effort in
both directions: a credential in a table named nothing like one is not
found, and a hit is a candidate to read, not a verdict.

**Usage**:

```console
$ odoo-db check-sensitive-information [OPTIONS] DB
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

## `odoo-db dump`

Dump an Odoo database using pg_dump custom format (-Fc).

**Usage**:

```console
$ odoo-db dump [OPTIONS] DB
```

**Arguments**:

* `DB`: \[required\]

**Options**:

* `-f, --file PATH`: Output path (default: ./&lt;db&gt;.pgdump).
* `--force / --no-force`: Overwrite the output file if it already exists.  \[default: no-force\]
* `-v, --verbose`: Pass -v to pg_dump.
* `--help`: Show this message and exit.

## `odoo-db restore`

Restore a pg_dump backup into a new database using pg_restore.

**Usage**:

```console
$ odoo-db restore [OPTIONS] BACKUP
```

**Arguments**:

* `BACKUP`: \[required\]

**Options**:

* `--db TEXT`: Target database name (default: derived from backup filename).
* `--force / --no-force`: Drop an existing database with the same name before restoring.  \[default: no-force\]
* `-j, --jobs INTEGER`: Parallel restore jobs.  \[default: 1\]
* `-v, --verbose`: Pass -v to pg_restore.
* `--reset-passwords`: After restore, reset every res_users password. Skipped with a warning on non-Odoo DBs.
* `-P, --password TEXT`: Password used with --reset-passwords (default: random 16 chars).
* `--help`: Show this message and exit.
