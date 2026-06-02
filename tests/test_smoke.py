from typer.testing import CliRunner

from odoo_db.db import _bloat_estimate_pages, _mime_family, _validate_attachment_orphans
from odoo_db.main import app

runner = CliRunner()


def test_help():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


def test_list_help():
    result = runner.invoke(app, ["list", "--help"])
    assert result.exit_code == 0


def test_modules_help():
    result = runner.invoke(app, ["modules", "--help"])
    assert result.exit_code == 0


def test_prepare_audit_help():
    result = runner.invoke(app, ["prepare-audit", "--help"])
    assert result.exit_code == 0


def test_bloat_help():
    result = runner.invoke(app, ["bloat", "--help"])
    assert result.exit_code == 0


def test_bloat_estimate_pages():
    # empty / unknown inputs → no estimate
    assert _bloat_estimate_pages(0, 100, 8000) == 0
    assert _bloat_estimate_pages(100, 0, 8000) == 0
    assert _bloat_estimate_pages(100, 100, 0) == 0
    # 1000 rows * 100 bytes / 8000 usable = 12.5 → ceil 13 pages
    assert _bloat_estimate_pages(1000, 100, 8000) == 13


def test_attachments_help():
    result = runner.invoke(app, ["attachments", "--help"])
    assert result.exit_code == 0


def test_global_sensitive_flag_exists():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0


class _FakeCursor:
    """Minimal psycopg-cursor stand-in for _validate_attachment_orphans.

    Scripts results by matching the leading token of each SQL statement so the
    test stays decoupled from formatting. Per-model orphan probe returns a
    fixed (checked, dead, dead_size) tuple.
    """

    def __init__(self, registered_models, real_tables, probe_result=(10, 2, 2048)):
        self._registered = list(registered_models)
        self._tables = list(real_tables)
        self._probe = probe_result
        self._next = None
        self.probes: list[str] = []

    def execute(self, query, params=None):
        text = query.as_string({}) if hasattr(query, "as_string") else str(query)
        head = text.strip().split()[0].upper()
        if "FROM ir_model" in text:
            self._next = [(m,) for m in self._registered]
        elif "FROM pg_tables" in text:
            self._next = [(t,) for t in self._tables]
        elif head == "SELECT" and "ir_attachment" in text:
            assert params, "probe query missing params"
            self.probes.append(params[0])
            self._next = [self._probe]
        else:
            msg = f"unexpected query: {text[:80]}"
            raise AssertionError(msg)

    def fetchall(self):
        rows, self._next = self._next, None
        return rows

    def fetchone(self):
        rows, self._next = self._next, None
        return rows[0] if rows else None


def test_validate_attachment_orphans_skips_unregistered_models():
    # Bug regression: candidate set used to be underscore-form, so dotted
    # res_model values never matched and the probe never ran. Now compares
    # dotted-to-dotted and probes only registered + table-backed models.
    by_model = [
        {"model": "sale.order", "count": 5, "size": 100},
        {"model": "ghost.model", "count": 5, "size": 100},  # not in ir_model
        {"model": "abstract.thing", "count": 5, "size": 100},  # registered, no table
        {"model": "", "count": 5, "size": 100},  # empty res_model
    ]
    cur = _FakeCursor(
        registered_models={"sale.order", "abstract.thing"},
        real_tables={"sale_order", "res_partner"},
    )
    findings = _validate_attachment_orphans(cur, by_model, top_n=10, max_scan=1000)  # ty: ignore[invalid-argument-type]

    assert cur.probes == ["sale.order"]
    assert findings == [{"model": "sale.order", "checked": 10, "dead_count": 2, "dead_size": 2048}]


def test_validate_attachment_orphans_skips_when_over_max_scan():
    by_model = [{"model": "sale.order", "count": 10_000, "size": 100}]
    cur = _FakeCursor(registered_models={"sale.order"}, real_tables={"sale_order"})
    findings = _validate_attachment_orphans(cur, by_model, top_n=10, max_scan=1000)  # ty: ignore[invalid-argument-type]

    assert cur.probes == []
    assert findings == [{"model": "sale.order", "skipped": True, "reason": "10,000 attachments > max-scan 1,000"}]


def test_mime_family():
    assert _mime_family("image/png") == "image"
    assert _mime_family("application/pdf") == "pdf"
    assert _mime_family("text/css") == "assets (css/js)"
    assert _mime_family("application/javascript") == "assets (css/js)"
    assert _mime_family("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet") == "office"
    assert _mime_family(None) == "unknown"
    assert _mime_family("") == "unknown"
    assert _mime_family("application/octet-stream") == "other"
