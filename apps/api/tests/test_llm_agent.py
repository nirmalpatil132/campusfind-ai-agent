"""Local-LLM engine with a scripted fake Ollama server (httpx.MockTransport). No real model needed."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.agent.llm import OllamaClient, OllamaModelMissing
from app.agent.loop import choose_mode, run_llm_turn
from app.config import settings


def fake_ollama(chat_replies: list[dict[str, Any]], models: list[str] | None = None,
                seen: list[dict[str, Any]] | None = None) -> OllamaClient:
    replies = iter(chat_replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": m} for m in (models or ["qwen2.5:3b"])]})
        if request.url.path == "/api/chat":
            body = json.loads(request.content)
            if seen is not None:
                seen.append(body)
            return httpx.Response(200, json={"message": next(replies)})
        return httpx.Response(404, json={"error": "not found"})

    return OllamaClient(base_url="http://ollama.test", model="qwen2.5:3b", transport=httpx.MockTransport(handler))


def test_tool_call_then_final_answer() -> None:
    seen: list[dict[str, Any]] = []
    client = fake_ollama([
        {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_my_reports", "arguments": {}}}]},
        {"role": "assistant", "content": "You have one report: L-0001."},
    ], seen=seen)
    executed: list[tuple[str, dict[str, Any]]] = []

    def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
        executed.append((name, args))
        return {"count": 1, "reports": [{"code": "L-0001"}]}

    turn = run_llm_turn(client, "SYSTEM", [], "what did I report?", execute)
    assert turn.reply == "You have one report: L-0001."
    assert executed == [("get_my_reports", {})]
    # 2nd request carried the tool result back to the model, tied to the tool name
    tool_msgs = [m for m in seen[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["tool_name"] == "get_my_reports"
    assert json.loads(tool_msgs[0]["content"])["reports"][0]["code"] == "L-0001"
    assert seen[0]["messages"][0] == {"role": "system", "content": "SYSTEM"}
    assert {t["function"]["name"] for t in seen[0]["tools"]} == {
        "create_lost_report", "create_found_report", "search_items", "find_matching_items",
        "get_item_details", "get_my_reports"}
    # the turn's messages are kept for history (assistant tool call + tool result + final answer)
    assert [m["role"] for m in turn.messages] == ["assistant", "tool", "assistant"]


def test_string_arguments_and_think_tags_are_normalised() -> None:
    client = fake_ollama([
        {"role": "assistant", "content": "<think>hmm</think>",
         "tool_calls": [{"function": {"name": "get_item_details", "arguments": "{\"item_code\": \"L-0001\"}"}}]},
        {"role": "assistant", "content": "<think>ok</think>It is open."},
    ])
    executed = []
    turn = run_llm_turn(client, "S", [], "details of L-0001",
                        lambda n, a: executed.append((n, a)) or {"code": "L-0001"})
    assert executed == [("get_item_details", {"item_code": "L-0001"})]
    assert turn.reply == "It is open."


def test_max_steps_stops_a_runaway_loop() -> None:
    call = {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "get_my_reports", "arguments": {}}}]}
    client = fake_ollama([call, call, call])
    turn = run_llm_turn(client, "S", [], "loop", lambda n, a: {}, max_steps=2)
    assert "couldn't finish" in turn.reply
    assert len(turn.tool_calls) == 2


def test_missing_model_on_chat_raises_clear_error() -> None:
    client = OllamaClient(base_url="http://ollama.test", model="qwen2.5:3b",
                          transport=httpx.MockTransport(lambda r: httpx.Response(404, json={"error": "model not found"})))
    with pytest.raises(OllamaModelMissing, match="ollama pull qwen2.5:3b"):
        client.chat([{"role": "user", "content": "hi"}])


def _unreachable() -> OllamaClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)
    return OllamaClient(base_url="http://ollama.test", transport=httpx.MockTransport(handler))


def test_auto_mode_uses_llm_when_model_installed() -> None:
    mode, reason = choose_mode(None, fake_ollama([]))
    assert mode == "llm" and "qwen2.5:3b" in reason


def test_auto_mode_falls_back_to_rules_when_ollama_is_down() -> None:
    mode, reason = choose_mode(None, _unreachable())
    assert mode == "rules"
    assert "not running" in reason and "rule-based fallback" in reason


def test_auto_mode_falls_back_when_model_not_pulled() -> None:
    mode, reason = choose_mode(None, fake_ollama([], models=["llama3.2:1b"]))
    assert mode == "rules" and "ollama pull qwen2.5:3b" in reason


def test_ollama_only_mode_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_PROVIDER", "ollama")
    mode, _ = choose_mode(None, _unreachable())
    assert mode == "unavailable"


def test_rules_mode_never_calls_ollama(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "LLM_PROVIDER", "rules")
    mode, _ = choose_mode(None, _unreachable())
    assert mode == "rules"


def test_session_that_started_in_rules_stays_in_rules() -> None:
    mode, reason = choose_mode("rules", fake_ollama([]))
    assert mode == "rules" and "started in rule mode" in reason
