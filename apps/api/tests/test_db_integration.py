"""Real-database smoke test. Skipped unless RUN_DB_TESTS=1 and DATABASE_URL is set in apps/api/.env.

    PowerShell:  $env:RUN_DB_TESTS = "1"; python -m pytest tests/test_db_integration.py
"""

from __future__ import annotations

import os

import pytest

from app.config import Settings

pytestmark = pytest.mark.integration
REAL_URL = Settings().DATABASE_URL  # read .env directly (the autouse fixture blanks settings)


@pytest.mark.skipif(os.environ.get("RUN_DB_TESTS") != "1" or not REAL_URL,
                    reason="set RUN_DB_TESTS=1 and DATABASE_URL to run")
def test_schema_is_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    from app import db
    from app.config import settings

    monkeypatch.setattr(settings, "DATABASE_URL", REAL_URL)
    try:
        assert db.db_status() == "ok"
        assert db.fetch_one("select count(*) as n from public.locations")["n"] >= 14
        for table in ("items", "matches", "agent_sessions", "agent_logs", "chat_messages"):
            assert db.fetch_one("select to_regclass(%(t)s) as t", {"t": f"public.{table}"})["t"] is not None
        assert db.fetch_one("select 1 from pg_extension where extname = 'vector'") is not None
    finally:
        db.close_pool()
