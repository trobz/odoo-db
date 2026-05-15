from typer.testing import CliRunner

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
