"""Rate limiting, request-size limit, LLM concurrency and friendly error responses."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app import ratelimit
from app.agent import llm
from app.agent.llm import OllamaClient, OllamaUnavailable
from app.auth import get_current_user
from app.config import settings
from app.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_limiter() -> Iterator[None]:
    ratelimit.limiter.reset()
    yield
    ratelimit.limiter.reset()
    app.dependency_overrides.clear()


def test_parse_limits() -> None:
    assert ratelimit.parse_limits("10/minute;200/day") == [(10, 60.0), (200, 86400.0)]
    assert ratelimit.parse_limits("5/seconds") == [(5, 1.0)]
    assert ratelimit.parse_limits("junk;;0/minute") == []


def test_sliding_window_with_fake_clock() -> None:
    now = [1000.0]
    lim = ratelimit.RateLimiter(clock=lambda: now[0])
    assert [lim.hit("k", "3/minute") for _ in range(3)] == [None, None, None]
    retry = lim.hit("k", "3/minute")
    assert retry is not None and 59 <= retry <= 60
    assert lim.hit("other", "3/minute") is None  # keys are independent
    now[0] += 61
    assert lim.hit("k", "3/minute") is None  # window slid


def test_chat_is_limited_per_user_with_retry_after(monkeypatch: pytest.MonkeyPatch, student: Any) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_CHAT", "2/minute")
    app.dependency_overrides[get_current_user] = lambda: student
    codes = [client.post("/api/agent/chat", json={"message": "hi"}).status_code for _ in range(2)]
    assert codes == [503, 503]  # passes the limiter; fails later only because tests have no database
    r = client.post("/api/agent/chat", json={"message": "hi"})
    assert r.status_code == 429
    assert r.json()["detail"] == "Too many requests. Please try again shortly."
    assert r.json()["code"] == "rate_limited"
    assert int(r.headers["Retry-After"]) >= 1


def test_report_creation_limit(monkeypatch: pytest.MonkeyPatch, student: Any) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_REPORTS", "1/minute")
    app.dependency_overrides[get_current_user] = lambda: student
    body = {"category": "bag", "item_name": "bag", "description": "black bag", "location": "CSE Block",
            "event_date": "2026-11-16"}
    assert client.post("/api/items/lost", json=body).status_code == 503
    assert client.post("/api/items/lost", json=body).status_code == 429


def test_global_ip_flood_guard_but_health_is_never_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_GLOBAL_IP", "3/minute")
    assert [client.get("/api/locations").status_code for _ in range(3)] == [200, 200, 200]
    assert client.get("/api/locations").status_code == 429
    assert all(client.get("/api/health").status_code == 200 for _ in range(5))


def test_limits_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_GLOBAL_IP", "1/minute")
    assert all(client.get("/api/locations").status_code == 200 for _ in range(3))


def test_oversized_request_is_413(student: Any) -> None:
    app.dependency_overrides[get_current_user] = lambda: student
    r = client.post("/api/agent/chat", content=b"x" * (settings.MAX_REQUEST_BYTES + 1),
                    headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and r.json()["code"] == "request_too_large"


def test_error_responses_have_a_consistent_shape() -> None:
    r = client.get("/api/me")
    assert r.status_code == 401 and set(r.json()) >= {"detail", "code"} and r.json()["code"] == "unauthorized"


def test_llm_busy_slot_fails_fast_instead_of_hanging(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "OLLAMA_QUEUE_TIMEOUT", 0.05)
    slots = llm._llm_slots()
    held = [slots.acquire(blocking=False) for _ in range(settings.OLLAMA_MAX_CONCURRENT)]
    try:
        with pytest.raises(OllamaUnavailable, match="busy"):
            OllamaClient(base_url="http://127.0.0.1:9").chat([{"role": "user", "content": "hi"}])
    finally:
        for ok in held:
            if ok:
                slots.release()
