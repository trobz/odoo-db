#!/usr/bin/env bash
#
# build_fixtures.sh — generate REAL per-version Odoo fixtures.
#
# The checked-in v1 fixtures are synthetic SQL (tests/fixtures/versions/*.sql)
# and need no Odoo to run — see tests/fixtures/versions/README.md. This script
# is the documented upgrade path for when faithfulness to a real Odoo schema is
# worth the weight: it stands up each Odoo version, neutralizes + shrinks a
# demo database, dumps it with `pg_dump -Fc`, and records a manifest row
# (pinned image digest + sha256 + expected shape).
#
# It is deliberately NOT run by CI (standing up 8.0 → 19.0 is the expensive part
# the spec calls out) — run it by hand to refresh a fixture, then commit the
# dump (via LFS or an object store, per the storage decision) alongside its
# manifest row. Requires each Odoo version available as a container image.
#
# Status: scaffold. The per-version loop below is a TODO, intentionally fenced
# off so the script cannot silently produce an empty or non-neutralized dump.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FIXTURES_DIR="${SCRIPT_DIR}/../tests/fixtures/versions"

VERSIONS=("${@:-8.0 12.0 14.0 16.0 17.0 18.0 19.0}")

echo "build_fixtures.sh is a scaffold for real-dump generation and is not wired up yet."
echo "The runnable v1 fixtures are the synthetic SQL in: ${FIXTURES_DIR}"
echo
echo "To implement, per version in: ${VERSIONS[*]}"
cat <<'STEPS'
  1. Boot that Odoo version with demo data + the module set
     (-i base,web,auth_totp[,auth_passkey] --stop-after-init), pinned by image digest.
  2. Neutralize it; assert database.is_neutralized.
  3. Strip the bulky tables (mail_message, ir_attachment, logs) — reuse
     odoo-db's own shrink/anonymize path so the corpus exercises it too.
  4. pg_dump -Fc to tests/fixtures/versions/<version>.pgdump.
  5. Write the manifest row: version, kind=pgdump, dump, image digest, sha256, expected_shape.
STEPS
exit 1
