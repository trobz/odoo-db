# CHANGELOG

<!-- version list -->

## v1.9.1 (2026-05-22)

### Bug Fixes

- **docs**: Repair broken main.html override + auto-generate CLI reference
  ([`fe4d700`](https://github.com/trobz/odoo-db/commit/fe4d700659e1a4977c51398c4887adf2879dd946))

### Documentation

- **agents**: Trim CLAUDE.md/AGENTS.md commands table → point at auto-gen reference
  ([`fd2e938`](https://github.com/trobz/odoo-db/commit/fd2e93826597c5a07c317e269e37c0bfebf79933))


## v1.9.0 (2026-05-21)

### Features

- **crons**: Add --running flag to list currently executing scheduled actions
  ([`0a4c34a`](https://github.com/trobz/odoo-db/commit/0a4c34a058398a7b51ec1f7cd4bc08e0e05edb3e))


## v1.8.0 (2026-05-20)

### Features

- **docs**: Add VHS terminal demo for landing page
  ([`f64e6f0`](https://github.com/trobz/odoo-db/commit/f64e6f0b5a1b32df23e58a784edc55dad3a4ca35))

- **docs**: Scaffold site-docs landing + make docs targets
  ([`d1b834e`](https://github.com/trobz/odoo-db/commit/d1b834e9fe55cad22214d8ce2e8b759cf1ac0506))


## v1.7.0 (2026-05-19)

### Features

- Add functional_group to every table; table-prefix primary
  ([`e1e250e`](https://github.com/trobz/odoo-db/commit/e1e250ed8c7cc7e08fa99ca02d3dfa9763231401))

- Add users_by_year and skip count on empty tables
  ([`316c26b`](https://github.com/trobz/odoo-db/commit/316c26ba34224c92ffe57e223bf7dacc7d74d76a))

### Refactoring

- Pass cursor into db helpers, share one conn in prepare-audit
  ([`7b76006`](https://github.com/trobz/odoo-db/commit/7b760063977da0cef3ce74247c13a4d7e1552002))


## v1.6.0 (2026-05-18)

### Features

- Add orphan_tables to prepare-audit bundle
  ([`a71b066`](https://github.com/trobz/odoo-db/commit/a71b0667b78ace8c85c2e801a00baccbc91bf091))

- Tag recognized infra in not-odoo output
  ([`c794da7`](https://github.com/trobz/odoo-db/commit/c794da742de53dadd82c966dc503099a0b5fb8e9))


## v1.5.0 (2026-05-18)

### Features

- Add authoritative model_owners map to prepare-audit bundle
  ([`b487d20`](https://github.com/trobz/odoo-db/commit/b487d20142647eb92cbeaedbdd24d1d3eb13a83a))

### Refactoring

- Write prepare-audit output to the current directory by default
  ([`5762b18`](https://github.com/trobz/odoo-db/commit/5762b184e28989275bfd93817b4b9fe428a7d2e1))


## v1.4.0 (2026-05-15)

### Features

- Add prepare-audit command — bundle $db.json for /odoo-dev:audit-db
  ([`1d744ad`](https://github.com/trobz/odoo-db/commit/1d744ad3be69cbf0d7539a89711e00b084a93fc3))

- Progress bar for per-table stats queries
  ([`159c482`](https://github.com/trobz/odoo-db/commit/159c482d0872c1f4acbf46729b0ad67465ae2551))


## v1.3.0 (2026-05-15)

### Documentation

- Add not-odoo examples and update command descriptions
  ([`328e12f`](https://github.com/trobz/odoo-db/commit/328e12f562ae07247e3c479c6393aa6607bb1301))

### Features

- Add not-odoo command — detect custom views, triggers, functions, and procedures
  ([`04a5408`](https://github.com/trobz/odoo-db/commit/04a540802f45ec09745350ba7691173c62f0c189))


## v1.2.0 (2026-05-15)

### Features

- Add stats command — per-table record counts, sizes, and year breakdown
  ([`21fce9e`](https://github.com/trobz/odoo-db/commit/21fce9e0f5264fe4d52a9574af07bcb3f307776c))


## v1.1.0 (2026-05-14)

- Initial Release
