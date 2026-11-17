"""Shared test fixtures. No test here needs a database, Ollama or the internet
(except tests marked `integration`, which are skipped unless RUN_DB_TESTS=1)."""

from __future__ import annotations

from datetime import date

import pytest

from app.auth import CurrentUser
from app.config import settings

TODAY = date(2026, 11, 17)  # a Tuesday


@pytest.fixture
def today() -> date:
    return TODAY


@pytest.fixture
def student() -> CurrentUser:
    return CurrentUser(id="11111111-1111-1111-1111-111111111111", email="riya@test.local",
                       name="Riya Patil", role="student")


@pytest.fixture
def admin() -> CurrentUser:
    return CurrentUser(id="22222222-2222-2222-2222-222222222222", email="admin@test.local",
                       name="Admin Office", role="admin")


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never touch the real database or a real Ollama, whatever apps/api/.env says."""
    from app import db
    from app.agent import llm

    monkeypatch.setattr(settings, "DATABASE_URL", "")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://127.0.0.1:9")  # nothing listens here
    monkeypatch.setattr(settings, "EMBEDDING_PROVIDER", "none")
    monkeypatch.setattr(settings, "LLM_PROVIDER", "auto")
    monkeypatch.setattr(db, "_pool", None)
    llm._status_cache.update(at=0.0, value=None)
