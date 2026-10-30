# Per-version Odoo fixtures

Tiny, synthetic, per-version databases that pin odoo-db's version-shape
assumptions so they are checked by CI on every push instead of by hand against
a live database. Background and rationale: the
[per-version fixtures spec][spec] (companion to the
[cross-version robustness post][post]).

## What these are (and are not)

odoo-db is mostly version-shape logic: which table or column exists on which
Odoo version, and what to do when it does not (`res_users_log` vs a
`login_date` column, `totp_secret`, `auth_passkey_key`, the 18.0 cron columns,
`bus_presence` → `mail_presence`, `res_groups.category_id` → `privilege_id`).
Each fixture here is a **synthetic** database that exhibits exactly one
version's shape, so a probe that silently flips turns a test red and names the
version.

These are **not** real Odoo dumps. They are hand-written SQL that creates only
the tables, columns and handful of rows the probes and the shape-sensitive
commands actually read. That makes them a strong regression guard that runs in
CI today with nothing but a `postgres` service — but it means the shape is
asserted *from the documented matrix*, not proven to match what Odoo really
ships. Swapping in real neutralized `pg_dump` files later is a drop-in change
(see "Upgrading to real dumps").

## Layout

| File | Role |
| --- | --- |
| `manifest.json` | The single source of truth. One entry per version: `version`, `kind`, `fixture` file, `modules`, and the `expected_shape` the tests assert. No second hardcoded copy lives in the tests. |
| `_base.sql` | Shared schema + seed rows every version has (users, partners, company, a cron, config params). Loaded first. |
| `<version>.sql` | The overlay that makes a database look like that Odoo version: adds/removes the columns and tables that distinguish it, plus its `ir_module_module` rows (which set the detected version). Loaded after `_base.sql`. |

The shapes are **cumulative**: each `<version>.sql` only adds what first
appears on that version (or replaces what changed — `bus_presence` →
`mail_presence`, `category_id` → `privilege_id`). Everything an earlier version
exhibits still holds unless a later overlay changes it.

## Shape matrix

| Version | First-appearing shape |
| --- | --- |
| 8.0 | `res_users.login_date` real column; **no** `res_users_log`; no presence table |
| 9.0 | `res_users_log` appears (no more `login_date` column); `bus_presence` |
| 12.0, 13.0, 14.0 | same probe-shape as 9.0 (`res_users_log`, `bus_presence`, `category_id`) |
| 16.0, 17.0 | `auth_totp` → `res_users.totp_secret` |
| 18.0 | `ir_cron.failure_count`/`first_failure_date` + `ir_cron_progress`; `auth_passkey` → `auth_passkey_key` |
| 19.0, 20.0 | `bus_presence` → `mail_presence`; `res_groups.category_id` → `privilege_id` → `res_groups_privilege` |

## Running

The tests ([`tests/test_fixtures.py`](../../test_fixtures.py)) are parametrized
over `manifest.json`. The loader ([`tests/conftest.py`](../../conftest.py))
creates a throwaway database per version, loads `_base.sql` + the overlay, and
drops it afterwards. It **skips** the whole suite when no PostgreSQL server is
reachable, so `make test` stays green on a machine with no database — only the
dedicated `version-fixtures` CI job (which runs a `postgres` service) exercises
them.

```bash
# needs a reachable PostgreSQL with CREATEDB rights (PGHOST/PGUSER as usual)
make test-fixtures
# or directly, picking the role:
PGUSER=postgres uv run python -m pytest tests/test_fixtures.py -q
```

## Adding a newly supported version

1. Add `<version>.sql` overlaying `_base.sql` with that version's shape.
2. Add a `manifest.json` entry with its `expected_shape`.
3. Run `make test-fixtures`.

When Odoo moves a shape mid-series, the failing test names the version and the
probe; update the overlay and the manifest's `expected_shape` together.

## Upgrading to real dumps

If faithfulness to a real Odoo schema becomes worth the weight, generate real
neutralized dumps with [`scripts/build_fixtures.sh`](../../../scripts/build_fixtures.sh)
(needs each Odoo version available as an image) and set the entry's `kind` to
`pgdump` with a `dump` filename, a pinned `image` digest and a `sha256`. The
loader would then `pg_restore` that file instead of loading SQL, and the CI job
becomes the per-version matrix the spec describes. The `expected_shape` oracle
and the tests stay exactly as they are.

[spec]: https://internal.trobz.com/blog/odoo-db-version-fixtures-spec/
[post]: https://internal.trobz.com/blog/odoo-db-cross-version-robustness/
