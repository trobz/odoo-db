# CHANGELOG

<!-- version list -->

## v1.22.0 (2026-08-28)

### Features

- **sensitive**: Add check-sensitive-information command
  ([`55f4791`](https://github.com/trobz/odoo-db/commit/55f47919329dfe7cc56576d20429fc84070ad06b))

- **sensitive**: Lead the surfaces with base's own crons and relays
  ([`95c3436`](https://github.com/trobz/odoo-db/commit/95c34362da0c16547c6bf6fb0ea844e4cbe242de))

- **sensitive**: Report what neutralize should have cleared and did not
  ([`dec173d`](https://github.com/trobz/odoo-db/commit/dec173df905dfc642745b8f9617cf47723c1a884))


## v1.21.0 (2026-08-21)

### Features

- **mail**: Add mail configuration audit command
  ([`6d23317`](https://github.com/trobz/odoo-db/commit/6d233171087184fc60e07478a16735601f73c0a2))


## v1.20.0 (2026-08-17)

### Features

- Add dump and restore commands
  ([`039018d`](https://github.com/trobz/odoo-db/commit/039018d149a516bb7fc117834cfe91d815170699))


## v1.19.0 (2026-08-14)

### Features

- **modules,users**: Add --all flag to include uninstalled and archived rows
  ([`7a56d8f`](https://github.com/trobz/odoo-db/commit/7a56d8f461ac776ee7d1eab5222b302f64ff266a))

- **users**: Add --online flag to show only online users
  ([`b02c258`](https://github.com/trobz/odoo-db/commit/b02c25816008de7d6ebecc0b18fa027db31d72de))


## v1.18.0 (2026-07-27)

### Features

- Add groups, roles, and role-drift commands
  ([`0abb903`](https://github.com/trobz/odoo-db/commit/0abb9035b117bd9c418c273a0020c101c15efa36))


## v1.17.0 (2026-07-24)

### Features

- **params**: Add ir_config_parameter key/value dump command
  ([`4a08d2a`](https://github.com/trobz/odoo-db/commit/4a08d2adf144a0392451e01219e9b780a7357543))


## v1.16.0 (2026-07-24)

### Features

- **crons**: Add --all flag to include inactive crons
  ([`beb9843`](https://github.com/trobz/odoo-db/commit/beb9843d4fbcb44bc090c9c71a166dce6708a984))


## v1.15.0 (2026-07-22)

### Features

- **crons**: Add --include-code flag
  ([`5dcd8a9`](https://github.com/trobz/odoo-db/commit/5dcd8a9156ae76c8730a516daf7a8c463a85f1c1))


## v1.14.1 (2026-07-22)

### Bug Fixes

- **crons**: Resolve model name via ir_model join instead of ias.model_name
  ([`3253636`](https://github.com/trobz/odoo-db/commit/3253636c225aaa8986324fbeffe1a76ee25404b2))


## v1.14.0 (2026-06-19)

### Features

- Have option to show version
  ([`702e81d`](https://github.com/trobz/odoo-db/commit/702e81d2f7c991c1477ebac12edc3c6c919e5cf9))


## v1.13.0 (2026-06-02)

### Features

- **attachments**: Ir.attachment storage audit command
  ([`245bd06`](https://github.com/trobz/odoo-db/commit/245bd0627cda1431db46ef24028b8fdb9586deaa))


## v1.12.1 (2026-05-28)

### Bug Fixes

- **prepare-audit**: Enhance model filtering and include noupdate flag
  ([`2ff3e6c`](https://github.com/trobz/odoo-db/commit/2ff3e6c4d55c174086d93b36e8bf691f354c40f4))


## v1.12.0 (2026-05-28)

### Features

- **bloat**: Table + index bloat command (pgstattuple + estimate)
  ([`98129af`](https://github.com/trobz/odoo-db/commit/98129af6c590fdf107663b80ebffc4ac915b3201))


## v1.11.1 (2026-05-27)

### Bug Fixes

- **prepare-audit**: Guard ir_model_data.studio column in studio scan
  ([`e6a30ec`](https://github.com/trobz/odoo-db/commit/e6a30ec107c966099f6f7d32b0f2469f6d3e2755))


## v1.11.0 (2026-05-26)

### Features

- **prepare-audit**: Enrich audit bundle with orphan fields, customized records, and operational
  stats
  ([`3c8c2e7`](https://github.com/trobz/odoo-db/commit/3c8c2e772d7fc4abc4632abd9e7a0d3e0ac60fe2))


## v1.10.0 (2026-05-25)

### Features

- **studio**: Add studio command and enrich prepare-audit bundle
  ([`02c290e`](https://github.com/trobz/odoo-db/commit/02c290eaad5263a78098207eaadfca6be7d22f5f))


## v1.9.2 (2026-05-22)

### Bug Fixes

- **logging**: Log to console only by default, no file unless --log-file given
  ([`a1b42ff`](https://github.com/trobz/odoo-db/commit/a1b42ff8c28f2bfc10c10465e3c5effc50a2781c))


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
