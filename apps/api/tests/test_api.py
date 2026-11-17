"""HTTP API: auth, admin guard, safe failure without a database."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import auth
from app.auth import get_current_user
from app.config import settings
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _clear_overrides() -> Iterator[None]:
    yield
    app.dependency_overrides.clear()


def test_health_needs_no_auth_and_reports_status() -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["database"] == "not_configured"
    assert body["chat_engine"] == "rules"          # Ollama unreachable in tests → fallback
    assert body["ollama"]["available"] is False


def test_chat_without_token_is_401() -> None:
    assert client.post("/api/agent/chat", json={"message": "hi"}).status_code == 401


def test_garbage_token_is_401(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setattr(auth, "_jwks_client", None)
    r = client.post("/api/agent/chat", json={"message": "hi"}, headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_chat_without_database_is_503_with_setup_steps(student) -> None:
    app.dependency_overrides[get_current_user] = lambda: student
    r = client.post("/api/agent/chat", json={"message": "I lost my wallet"})
    assert r.status_code == 503
    assert any("schema.sql" in step for step in r.json()["setup"])


def test_chat_message_too_long_is_422(student) -> None:
    app.dependency_overrides[get_current_user] = lambda: student
    assert client.post("/api/agent/chat", json={"message": "x" * 2001}).status_code == 422


@pytest.mark.parametrize("method, path", [
    ("get", "/api/admin/matches"),
    ("get", "/api/admin/items"),
    ("get", "/api/admin/stats"),
    ("post", "/api/admin/matches/00000000-0000-0000-0000-000000000000/verify"),
    ("post", "/api/admin/items/F-0001/returned"),
])
def test_students_get_403_on_admin_routes(student, method: str, path: str) -> None:
    app.dependency_overrides[get_current_user] = lambda: student
    assert getattr(client, method)(path).status_code == 403


def test_public_config_exposes_only_public_values() -> None:
    body = client.get("/api/public-config").json()
    assert set(body) == {"supabase_url", "supabase_publishable_key", "college_name"}


def test_locations_for_forms() -> None:
    body = client.get("/api/locations").json()
    assert len(body["locations"]) == 14 and "wallet" in body["categories"]


def test_form_rejects_unknown_category(student) -> None:
    app.dependency_overrides[get_current_user] = lambda: student
    r = client.post("/api/items/lost", json={"category": "spaceship", "item_name": "x", "description": "x",
                                             "location": "CSE Block", "event_date": "2026-11-16"})
    assert r.status_code == 422


def test_web_page_is_served() -> None:
    r = client.get("/")
    assert r.status_code == 200 and "CampusFind AI" in r.text
