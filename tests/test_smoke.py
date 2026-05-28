from typer.testing import CliRunner

from odoo_db.db import _bloat_estimate_pages
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
