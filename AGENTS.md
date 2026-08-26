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
- `--log-file` — optional; if omitted, logs go to console only
- `--include-sensitive-information` — global PII master switch (default off)

**Commands:** see `site-docs/docs/cli-reference.md` — auto-generated from
the Typer app, exhaustive, always current. Read that file (or run
`odoo-db <cmd> --help`) instead of relying on a hand-maintained list here.

Non-obvious behavior worth keeping in agent context (the bits a `--help`
dump won't tell you):

- `groups`/`roles`: `res.groups.name`/`comment` and `ir.module.category.name`
  are `translate=True` — Odoo 16+ stores them as jsonb (`{"en_US": "...",
  ...}`) since `ir.translation` was dropped for model fields that version;
  older versions store plain text. `db._localize()` normalizes both
  (picks `en_US`, falls back to any available translation). `roles` reads
  `res.users.role` (OCA `base_user_role`); the table `res_users_role`
  delegates (`_inherits`) to `res.groups` via `group_id` — name/category live
  on the joined `res_groups` row, not on `res_users_role` itself. Returns
  `None` (command prints a message) if `base_user_role` isn't installed, same
  pattern as `jobs`/`queue_job`. `--include-groups` resolves each role's full
  granted-group set (`{group_id} | trans_implied(group_id)`) via
  `db._trans_implied()`, a recursive CTE over `res_groups_implied_rel`
  (`(gid, hid)` = "group `gid` implies group `hid`") — `trans_implied_ids` is
  compute-only on the Odoo side, not a column, so there's no shortcut query.
  Odoo 19 dropped `res_groups.category_id`; the category is now reached via
  `res_groups.privilege_id` -> `res_groups_privilege.category_id` ->
  `ir_module_category`. `db._groups_category_sql(cur)` probes
  `information_schema.columns` for `privilege_id` once per `get_groups`/
  `get_roles` call and returns the right `(select_cols, join_sql)` pair for
  either schema — both branches select the same 4 columns (privilege id/name,
  category id/name) so row-unpacking stays single-path; `privilege`/
  `privilege_id` are simply `None` pre-19. `category` still maps to
  `ir_module_category` on both versions so the JSON stays comparable across a
  v16 -> v19 migration audit. Verified against two live v18 databases
  (`odoo_db`, `v18c_pos_container_deposit`) — no v14/v16/v19 database was
  available to verify those branches directly.
- `groups --include-acls` returns a plain `list[dict]` when `include_acls` is
  unset (the default `get_groups()` shape everything else relies on, e.g.
  `role-drift`'s `get_groups(include_users=True)`), but with
  `include_acls=True` returns `{"groups": [...], "global_acls": [...],
  "global_rules": [...]}` instead — two `@overload` signatures on
  `db.get_groups()` express this so callers get the right static type back.
  An `ir_model_access` row with `group_id IS NULL`, or an `ir_rule` with no
  linked group at all (`ir_rule.global = true`), grants/restricts *every*
  user — previously silently dropped (`WHERE group_id IS NOT NULL` / an INNER
  JOIN through `rule_group_rel` that only matches group-linked rules), which
  hid the highest-value rows in a permission audit: a model readable by
  everyone looked unreachable in the report. They can't be attributed to a
  single group's `acls`, hence top-level siblings instead of duplicating them
  into every group. Both queries also now filter `active = true`
  (`ima.active`, `r.active`) — archived acls/rules were previously reported
  as if still in force. Verified end-to-end against `odoo_db`: 7 global acls
  + 10 global rules surfaced that the old query silently dropped.
- `role-drift` diffs each user's assigned roles (`get_roles(include_users=True,
  include_groups=True)`) against their actual group membership
  (`get_groups(include_users=True)`) via `db.compute_role_drift()`, a pure
  function over those two JSON shapes (unit-tested without a cursor —
  see `tests/test_smoke.py::test_compute_role_drift`). `expected` = union of
  the resolved (implied-closure-included) group sets of the user's *currently
  assigned* roles; `missing_groups` = `expected - actual` (role grants it,
  user doesn't actually have it — `base_user_role` sync gap: broken cron,
  direct SQL write, module upgrade). `extra_groups` deliberately does *not*
  use "any group in some role's resolved set" as its universe — that sweeps
  in near-universal implied baseline groups (e.g. "Internal User") that
  almost every role transitively implies and almost every employee holds
  regardless of role, flagging nearly the whole user base as verified against
  real data (`lalouve_staging`: ~90 of ~120 users). Instead the universe is
  each role's own `group_id` (its exclusive marker group, not its implied
  closure) — the one thing `base_user_role` actually writes/removes on
  `groups_id`, since Odoo core cascades implied groups onto real membership
  on top of that. `extra_groups` = (`actual` ∩ marker-group-ids) `-
  expected` — note: subtracted against the *full* closure `expected`, not
  just the user's own marker ids, so a role whose closure legitimately
  implies another role's marker (e.g. an admin role implying a cashier role's
  marker) is correctly not flagged. Verified against `lalouve_staging` this
  way: 24 of ~120 users flagged, all either seed/demo accounts named after a
  role (`cashier@example.com` holding the `Cashier` marker with no role line)
  or real users holding a marker with no active `res_users_role_line` for
  it — zero false positives from implied-baseline noise. Users with neither
  kind of drift are omitted. Returns `None` if `base_user_role` isn't
  installed, same pattern as `roles`. `--output-format prometheus` exposes
  `odoo_db_role_drift_users` (count of users with drift) for alerting.
  Unlike `groups`/`roles` (which gate exposing logins behind their own
  `--include-users` flag), `role-drift` is inherently per-user with no
  such opt-out, so each entry is keyed by `user_id` (from a `login -> id`
  lookup built in `get_role_drift`, passed into `compute_role_drift` as
  `user_ids`) rather than `login` by default; `login` is added alongside
  `user_id` only when the global `--include-sensitive-information` flag is
  set, threaded through as `compute_role_drift(..., include_sensitive=...)`
  — same PII-redaction convention `attachments` uses for filenames.
- `prepare-audit` writes `<db>.json` to the current working directory.
  `model_owners` is derived from `ir_model_data` + `ir_model_relation`
  (authoritative, not heuristic). `orphan_tables` carries a `reason`:
  `uninstalled_module` or `no_ownership_data`. `users_by_year` is
  `{year: count}` — zero PII so the file can ship without an NDA.
  `studio_customizations` carries custom models (with `mixins` list),
  extended models (with full `fields` detail), and studio-flagged records
  by type (menu records include `full_path`).
  `--admin-user LOGIN` (repeatable) excludes a specific login from the
  `customized_records` scan — useful when the primary admin uses a personal
  account instead of the system `admin` user.
  Additional bundle fields: `orphan_fields` (list of `{table, column, reason}`),
  `customized_records` (list of `{module, name, login}`),
  `customized_records_excluded` (list of excluded logins, empty if none),
  `mail_message_stats` (`{message_type: count}` or null if table missing),
  `attachment_stats` (`{storage: {count, total_size}}` or null),
  `cron_inventory` (list of `{name, active, code_based}` or null),
  `company_count` (int).
  Module entries in `modules` include `dependent_count` (number of installed
  modules that depend on it) added at `prepare-audit` time.
- Every table in `stats.tables` and `orphan_tables` gets a
  `functional_group` = first underscore component of the table name
  (`purchase_order_line` → `purchase`). Display-only bucket, **not** an
  owner attribution — owner lookup goes through `model_owners`.
- `stats` skips `count(*)` on tables whose heap is 0 bytes
  (`total_records=0`, empty `year_counts`) to avoid scanning empty
  partitions.
- `not-odoo` tags each trigger/function as `recognized` (known infra like
  `unaccent`, `queue_job_notify`) or `custom`. The allowlist lives in
  `odoo_db/db.py` under `_RECOGNIZED_FUNCTIONS` / `_RECOGNIZED_TRIGGERS`.
- `crons --running` is transient debug data, intentionally excluded from
  `prepare-audit`. `crons --all` additionally lists inactive crons.
- `crons`, `modules` and `users` share one `--all` convention: default output
  shows only in-use rows (active crons/users, installed modules) and the flag
  drops the SQL filter *and* adds the status key (`active` for crons/users,
  `state` + `installed` for modules). The key is added **only** when the flag
  is set — `odoo-activity`'s TUI and MCP `db_query` read the default shape, so
  it must stay byte-identical. Prometheus gauges keep counting installed/active
  rows only, with or without `--all`, so alert thresholds don't move.
  `prepare-audit` calls `get_modules(cur)` with no kwargs, so its bundle is
  unaffected.
- `users --online` narrows to `state == 'online'`. `state` comes from
  `mail_presence.status` (Odoo 19+) / `bus_presence.status` (14-18), whose
  selection is `online`/`away`/`offline` — `away` is an idle session so it is
  *not* online, and the no-presence-table fallback (`unknown`) never matches.
  The filter is `db.filter_online_users()` (a pure list-of-dicts function,
  unit-tested without a cursor) applied in `main.py` *after* the fetch, so the
  prometheus branch still counts every row and the gauges don't move — same
  rule as `--all`.
- `params` prints `ir_config_parameter` key/value pairs; it's a local debug
  tool, not part of `prepare-audit`. Optional `[PATTERN]` arg does a
  case-insensitive substring match on `key` (`ILIKE '%pattern%'`). Values of
  secret-bearing keys are **masked** (`********`) by default — keys always show.
  A key is sensitive if it contains any `_SENSITIVE_KEY_MARKERS` substring
  (`secret`, `password`, `token`, `api_key`, `dsn`, ...) in `odoo_db/db.py`.
  The global `--include-sensitive-information` reveals values (no per-command
  flag). Masking applies to `text` and `json`; `prometheus` emits only a count.
- `mail` audits outbound mail config (`get_mail_audit`). Ports a script that
  checked the same data via the Odoo API (`odooly`) to direct SQL — none of
  it needs auth. Dict keys are unordered; CLI `text` leads with
  `config_parameters`, odoo-activity's TUI (`panes/mail.py`) instead leads
  with `mail_servers` (see that repo's AGENTS.md/README). Top-level: 3 bool
  flags — `is_neutralized` (`get_is_neutralized`),
  `is_legacy_mail_config_configured` (`_is_legacy_mail_config_configured`:
  was any of the 4 pre-v17 ICP mail keys ever set — permanently true once
  it has been, migrated or not) and `is_alias_domain_migration_pending`
  (`_is_alias_domain_migration_pending`: of those, is the leftover config
  still live — true only when a legacy key is set **and** a company still
  has no alias domain; this is the one that separates "migrated fine" from
  "still stuck") — plus 5 sections: `config_parameters`
  (`get_mail_config_parameters`), `alias_domains`
  (`get_mail_alias_domains`, Odoo 17+ only, `None` pre-17), `addresses`
  (`get_mail_addresses`), `mail_servers` (`get_mail_servers`, `ir.mail_server`
  ordered by `sequence`, each row flagged `is_test_catcher`/
  `known_production_relay`/`is_neutralization_stub` — see
  `_is_test_mail_catcher`/`_known_production_relay`/
  `_is_neutralization_stub_mail_server`), `modules`
  (`get_mail_relevant_modules`, currently just `mass_mailing`).

  `mail_servers[].smtp_user`/`smtp_pass` are masked (`_SECRET_MASK`) like any
  other secret; `--include-sensitive-information` reveals both.
  `addresses` (company/OdooBot/admin) are organizational mailboxes, not
  individual PII, and are deliberately never masked — see `get_mail_addresses`.

  CLI `text` output (not `get_mail_audit()` itself, which always returns the
  full 6-key `config_parameters` list) drops the 4 legacy ICP keys once
  `is_alias_domain_migration_pending` is `False` — see
  `_relevant_mail_config_parameters`, called from `main.py`. `json` never
  filters, so the two formats diverge on those 4 keys by design (see that
  function's docstring for why: cross-version JSON diffing needs the full
  list).

  Rationale for individual checks (upstream commit hashes for
  `smtp_authentication`/`from_filter`, the neutralization stub row, the
  test-catcher marker/host lists incl. the `papercut` exclusion and the
  Mailtrap sandbox-vs-live split, the known-production-relay table, the
  demo-data default addresses) lives in the corresponding function
  docstrings/comments in `db.py`, not here.
- `check-sensitive-information` answers "what secrets does this database
  still hold", the complement of neutralization's "what can it still do".
  `db.get_sensitive_information()` returns 3 sections. `config_parameters`
  reuses `_is_sensitive_key` (same matcher `params` masks with) but drops
  rows whose value is only a boolean literal (`_BOOLEAN_VALUES`):
  `auth_signup.reset_password` is a checkbox, and a false positive costs
  more in a report that exists to name secrets than in masking, where it
  costs nothing. Deliberately nothing wider — a short numeric value stays,
  since dropping by shape is how a real 4-digit credential would go
  missing; core keys that match the substring while holding no secret
  (`auth_password_policy.minlength`) are named in `_NON_SECRET_CONFIG_KEYS`
  instead, an exact-key allowlist hiding only what it names — including the
  ones that are public *by design* behind a `_key` suffix
  (`cf.turnstile_site_key`, `recaptcha_public_key`,
  `mail.web_push_vapid_public_key`: core hands each to the browser in the
  session payload / push subscription), while every one of their secret
  siblings stays listed.
  `live_surfaces` is the neutralization counterpart: what each module's own
  `data/neutralize.sql` clears, expressed as the rows still in the
  *un*-neutralized state (`_NEUTRALIZE_SURFACES`) — an enabled
  `payment_provider`, an `iap_account` token without `+disabled`, an active
  `fetchmail_server`, ... `base`'s own neutralize (mail servers off, crons
  off) is easy to see; the per-module credential stripping is the part
  nothing was checking, and a database flagged `is_neutralized` with an
  enabled payment provider is a staging copy one click from charging a real
  card. `payment_acquirer` is listed beside `payment_provider` (renamed in
  16) since the existence probe cannot tell a missing table from a
  misspelled one; `mail_template` excludes rows pinned to the stub relay,
  which is as harmless as pinning to nothing. The top-level
  `is_neutralized` rides along because it is what makes the section
  readable — the same list is a leftover on a database claiming
  neutralization and an inventory on a production one.
  `mail_servers` keeps **inactive** rows, unlike the `mail`
  audit's active-only gauges: an archived relay sends nothing but its
  password is still in the dump; the stub and test catchers are dropped
  (no credential to leak). A row with **no** stored credential is kept
  when `known_production_relay` matches — such a relay commonly
  authenticates by IP allowlist/`from_filter`, so the host is the
  finding; the text table carries a `relay` column so that row doesn't
  read as an empty-credential false positive.
  `candidate_tables` matches table names against
  `_SENSITIVE_TABLE_MARKERS` (`api_key`, `api_config`, `api_instance`,
  `api_url`, wildcarded both sides since a custom module prefixes its
  tables) — kept separate from `_SENSITIVE_KEY_MARKERS` on purpose:
  overlapping them drags in every core `*_token`/`password` column and
  buries the handful of rows a reviewer can act on. Core ships no table
  named that way, so a match is nearly always custom. Row counts are exact
  `count(*)`, not `reltuples`: "0 rows" is what lets a reviewer dismiss a
  hit, and a never-analyzed table estimates 0. Each count runs behind a
  savepoint (`_count_rows`): a failed statement aborts the whole
  transaction in postgres, so one table the role cannot read would
  otherwise take down the two sections already gathered — it reports
  `rows: null` instead. `filter_sensitive_parameters`
  and `filter_credential_mail_servers` are pure over fetched rows
  (unit-tested without a cursor, same as `filter_online_users`).
- `attachments` audits `ir.attachment` storage in pure SQL — no ORM, so it
  sees field-backed rows (`image_1920`, logos, signatures) natively. The ORM's
  `_search` auto-injects `res_field = False` and hides them; raw SQL has no
  such filter, so totals count the whole table without the `res_field` OR
  trick the odooly version needed. `file_size` is a stored column
  (`len(data)` set at write time), reliable on any backend incl. S3-offloaded;
  payloads (`db_datas`/`datas`) are never read. Storage split: DB =
  `db_datas` set, filestore = `store_fname` set (mutually exclusive). Asset
  bucket is the exact domain core's `regenerate_assets_bundles()` deletes.
  Duplicate reclaim separates *logical* duplicated bytes from *real* disk
  reclaim (filestore dedups by SHA1, so only db-stored dups reclaim disk).
  Filenames are PII: `--include-individual-filenames` (or global
  `--include-sensitive-information`) gates the `top_files` `name` column only.
  Deliberately a standalone command, **not** folded into `prepare-audit` (per
  maintainer steer) — the bundle keeps only rough per-table
  `stats.attachment_size_bytes`; deep analysis lives here.
- `--include-sensitive-information` is a global PII master switch on the root
  callback (stored in `_include_sensitive`); a command's own opt-in flag is
  OR'd with it (e.g. `attachments` filenames show if either is set).
- `dump` / `restore` are the only write-side commands and the only ones that
  shell out to `pg_dump` / `pg_restore`. They share the peer-auth Unix-socket
  connection model of the rest of the CLI — pg_dump/pg_restore use libpq too,
  so `PGHOST`/`PGUSER` env vars work the same way. `dump` always uses custom
  format (`-Fc`) so restore can parallelise with `-j` from a single file.
  `restore` creates the target DB itself via `admin_connect()` (an autocommit
  connection to `postgres` — CREATE/DROP DATABASE can't run in a transaction);
  if pg_restore exits non-zero it prompts to drop the newly created DB so a
  half-restored shell doesn't linger. `--reset-passwords` runs
  `UPDATE res_users` and is silently skipped (with a warning) on non-Odoo DBs
  detected via `_is_odoo`.
- `bloat` uses a two-tier engine. **Estimate (always):** statistical guess
  from `pg_class` (`relpages`/`reltuples`) + `pg_stats` avg column widths — no
  extension, any role, but coarse and stale-stats-sensitive; per-row overhead
  constants live in `db.py` (`_HEAP_TUPLE_OVERHEAD`, `_BTREE_ENTRY_OVERHEAD`,
  `_BTREE_FILLFACTOR`). **Exact overlay:** `pgstattuple`/`pgstatindex` when the
  extension exists *and* the relation is ≤ `--exact-max-scan` (they full-scan,
  hence the cap). Each row carries `method` = `exact`/`est`/`n/a`; `n/a` = btree
  estimate impossible (expression index, no column stats) or non-btree. Index
  exact bloat is derived from `avg_leaf_density` vs the 90% btree fillfactor.
  Cheap exact signals (`dead_pct`, `last_autovacuum`, `idx_scan = 0` unused)
  come straight from `pg_stat_user_*`. Autovacuum never reclaims bloat — only
  `VACUUM FULL`/`REINDEX`/dump+restore do; the report frames the number as that
  win. Standalone command, not in `prepare-audit`.

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
- File handler only added when `--log-file PATH` is explicitly passed; parent dir auto-created.
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
