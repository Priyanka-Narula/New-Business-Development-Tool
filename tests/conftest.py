"""Keep pytest on isolated SQLite even when .env points at Neon."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_database_url(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite:///{(tmp_path / 'pytest.db').as_posix()}",
    )
