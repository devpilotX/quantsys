"""Operational CLI: where init-db finds the migrations."""

from __future__ import annotations

import pytest
from qsdash import cli


def _fake_migrations(root):
    (root / "alembic").mkdir(parents=True)
    (root / "alembic" / "env.py").write_text("")
    (root / "alembic.ini").write_text("[alembic]\n")
    return root


def test_a_checkout_uses_the_scripts_next_to_the_package(monkeypatch):
    monkeypatch.delenv("QSDASH_MIGRATIONS_DIR", raising=False)
    assert cli.migrations_dir() == cli.BACKEND_DIR
    assert (cli.migrations_dir() / "alembic" / "versions").is_dir()


def test_an_installed_package_reads_the_directory_the_image_names(monkeypatch, tmp_path):
    """Installed, BACKEND_DIR is site-packages, and site-packages/alembic is
    the Alembic library, so init-db failed with "Can't find Python file
    .../site-packages/alembic/env.py" inside the production image."""
    site = tmp_path / "site-packages"
    (site / "alembic").mkdir(parents=True)          # the library, no env.py
    monkeypatch.setattr(cli, "BACKEND_DIR", site)
    shipped = _fake_migrations(tmp_path / "app" / "backend")
    monkeypatch.setenv("QSDASH_MIGRATIONS_DIR", str(shipped))
    assert cli.migrations_dir() == shipped
    monkeypatch.delenv("QSDASH_MIGRATIONS_DIR")
    with pytest.raises(SystemExit, match="QSDASH_MIGRATIONS_DIR"):
        cli.migrations_dir()
