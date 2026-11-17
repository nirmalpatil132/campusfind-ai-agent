"""Regression tests for the real browser bug (qwen2.5:3b, 17 Nov 2026, the "iQube scooter key" chat).

What the model did:
  turn 1  user: "Hi, I have lost my scooty key. It's a IQube key"
          model: create_lost_report(location="Main Canteen", lost_time="10:30", ...)  <- invented
          reply: "I have created a lost report ..."                                   <- false claim
  turn 2  user: "yes confirm"
          model: create_FOUND_report(...)                                              <- wrong type, saved!

These tests replay that exact model behaviour through the real orchestrator (run_turn) with a scripted
fake Ollama and an in-memory store, and check the server now prevents every one of those mistakes.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.agent import guard, loop
from app.agent.llm import OllamaClient
from app.agent.rule_agent import handle_message
from tests.conftest import TODAY
from tests.test_rule_agent import FakeTools

IQUBE_MSG = "Hi, I have lost my scooty key. It's a IQube key"
INVENTED_LOST_CALL = {"function": {"name": "create_lost_report", "arguments": {
    "category": "keys", "location": "Main Canteen", "item_name": "IQube key", "lost_date": "2026-11-17",
    "lost_time": "10:30", "description": "Scooty key"}}}
WRONG_TYPE_FOUND_CALL = {"function": {"name": "create_found_report", "arguments": {
    "category": "keys", "location": "Main Canteen", "item_name": "IQube key", "found_date": "2026-11-17",
    "found_time": "11:00", "description": "Scooty key"}}}


# ---------------------------------------------------------------------------- pure guard
def test_invented_location_date_time_are_dropped() -> None:
    state, dropped = guard.draft_from_llm_call("create_lost_report", INVENTED_LOST_CALL["function"]["arguments"],
                                               [IQUBE_MSG], TODAY)
    f = state["fields"]
    assert state["report_type"] == "lost"
    assert f.get("location") is None and f.get("event_date") is None and f.get("event_time") is None
    assert {"location", "date", "time"} <= set(dropped)
    assert f["item_name"] == "IQube key" and f["category"] == "keys" and f["brand"] == "iQube"
    reply = guard.start_reply(state)
    assert "Where did you last have it?" in reply and "saved" not in reply.lower()


def test_report_type_follows_the_students_words_not_the_model() -> None:
    state, dropped = guard.draft_from_llm_call("create_found_report", WRONG_TYPE_FOUND_CALL["function"]["arguments"],
                                               [IQUBE_MSG], TODAY)
    assert state["report_type"] == "lost"
    assert "report_type" in dropped


def test_grounded_values_are_kept() -> None:
    msg = "I lost my black HP laptop bag in the CSE block yesterday around 2pm"
    state, dropped = guard.draft_from_llm_call("create_lost_report", {
        "item_name": "laptop bag", "color": "black", "brand": "HP", "location": "CSE Block",
        "lost_date": "2026-11-16", "lost_time": "14:00"}, [msg], TODAY)
    f = state["fields"]
    assert (f["location"], f["event_date"], f["event_time"], f["color"], f["brand"]) == (
        "CSE Block", "2026-11-16", "14:00", "black", "HP")
    assert dropped == []
    assert "Shall I save it?" in guard.start_reply(state)  # everything known -> summary


def test_private_hint_must_come_from_the_finder() -> None:
    msg = "I found a blue bottle in the library today, it's with me"
    state, dropped = guard.draft_from_llm_call("create_found_report", {
        "item_name": "bottle", "verification_hint": "owner name written on the cap"}, [msg], TODAY)
    assert "verification_hint" not in state["fields"] and "verification_hint" in dropped
    assert state["fields"]["current_holding"] == "finder"


@pytest.mark.parametrize("text", ["I have created a lost report for your IQube key.",
                                  "Your report has been saved.", "I've submitted it successfully"])
def test_false_save_claims_are_replaced(text: str) -> None:
    out = guard.remove_false_save_claim(text, items_created=[])
    assert "Nothing has been saved yet" in out
    assert guard.remove_false_save_claim(text, items_created=["L-0001"]) == text


def test_draft_then_form_step_saves_a_lost_report_only_after_yes() -> None:
    state, _ = guard.draft_from_llm_call("create_lost_report", INVENTED_LOST_CALL["function"]["arguments"],
                                         [IQUBE_MSG], TODAY)
    guard.start_reply(state)
    tools = FakeTools()
    for msg in ("near the parking lot", "yesterday"):
        state = handle_message(msg, state, tools, TODAY, "Riya").state
        assert tools.calls == []  # nothing saved while collecting
    turn = handle_message("yes confirm", state, tools, TODAY, "Riya")
    assert tools.names == ["create_lost_report", "find_matching_items"]
    args = tools.calls[0][1]
    assert args["location"] == "Parking Lot" and args["lost_date"] == "2026-11-16"
    assert "lost_time" not in args  # never invented
    assert "Saved! Your report code is L-0001" in turn.reply


# ---------------------------------------------------------------------------- full orchestrator replay
class MemoryStore:
    """In-memory stand-in for app.agent.memory + tool logging (no database)."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.rows: list[dict[str, Any]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        m = loop.memory
        monkeypatch.setattr(m, "get_session", lambda sid, user: self.sessions.get(sid))
        monkeypatch.setattr(m, "save_session", lambda sid, user, mode, state: self.sessions.__setitem__(
            sid, {"session_id": sid, "mode": mode, "state": json.loads(json.dumps(state))}))
        monkeypatch.setattr(m, "append_message", lambda sid, user, role, content: self.rows.append(
            {"session_id": sid, "role": role, "content": json.loads(json.dumps(content, default=str))}))
        monkeypatch.setattr(m, "load_records", lambda sid, user, limit: [
            r for r in self.rows if r["session_id"] == sid][-limit:])
        monkeypatch.setattr(loop, "log_tool_call", lambda *a, **k: None)


def scripted_ollama(replies: list[dict[str, Any]], seen: list[dict[str, Any]]) -> OllamaClient:
    it = iter(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"message": next(it)})

    return OllamaClient(base_url="http://ollama.test", model="qwen2.5:3b", transport=httpx.MockTransport(handler))


def test_iqube_browser_conversation_replayed(monkeypatch: pytest.MonkeyPatch, student: Any) -> None:
    store = MemoryStore()
    store.install(monkeypatch)
    tools = FakeTools()
    monkeypatch.setattr(loop, "execute_tool", lambda name, args, ctx: (tools(name, args), None))
    seen: list[dict[str, Any]] = []
    client = scripted_ollama([
        # exactly what qwen2.5:3b did in the browser: invented details + a false "created" claim
        {"role": "assistant", "content": "I have created a lost report for your IQube key.",
         "tool_calls": [INVENTED_LOST_CALL]},
    ], seen)

    r1 = loop.run_turn(student, None, IQUBE_MSG, client=client, today=TODAY)
    assert r1.mode == "llm" and r1.reply_source == "server_check"
    assert "created" not in r1.reply.lower() and "saved" not in r1.reply.lower()
    assert "Where did you last have it?" in r1.reply
    assert tools.calls == [] and r1.items_created == []
    assert len(seen) == 1  # the server took over right after the tool call (one model call saved)
    assert r1.tool_calls[0]["summary"] == "draft prepared; details checked by the server"

    # the next turns are the deterministic form step, honestly labelled as such
    r2 = loop.run_turn(student, r1.session_id, "near the parking lot", client=client, today=TODAY)
    r3 = loop.run_turn(student, r1.session_id, "yesterday", client=client, today=TODAY)
    assert r2.mode == r3.mode == "rules" and tools.calls == []
    assert "LOST report" in r3.reply and "Main Canteen" not in r3.reply and "Shall I save it?" in r3.reply

    # "yes confirm" saves EXACTLY the confirmed LOST draft (the model is not even asked)
    r4 = loop.run_turn(student, r1.session_id, "yes confirm", client=client, today=TODAY)
    assert tools.names == ["create_lost_report", "find_matching_items"]
    assert "create_found_report" not in tools.names
    assert r4.items_created == ["L-0001"] and "L-0001" in r4.reply
    assert store.sessions[r1.session_id]["mode"] == "llm"  # the session goes back to the LLM afterwards
    assert len(seen) == 1


def test_llm_text_claiming_a_save_without_tool_is_corrected(monkeypatch: pytest.MonkeyPatch, student: Any) -> None:
    MemoryStore().install(monkeypatch)
    client = scripted_ollama([{"role": "assistant", "content": "Done! Your report has been saved as L-0099."}], [])
    r = loop.run_turn(student, None, "my wallet is missing", client=client, today=TODAY)
    assert "L-0099" not in r.reply and "Nothing has been saved yet" in r.reply


def test_ollama_timeout_falls_back_to_rules_with_a_friendly_note(monkeypatch: pytest.MonkeyPatch,
                                                                 student: Any) -> None:
    MemoryStore().install(monkeypatch)
    tools = FakeTools()
    monkeypatch.setattr(loop, "execute_tool", lambda name, args, ctx: (tools(name, args), None))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5:3b"}]})
        raise httpx.ReadTimeout("model too slow")

    client = OllamaClient(base_url="http://ollama.test", model="qwen2.5:3b", transport=httpx.MockTransport(handler))
    r = loop.run_turn(student, None, "I lost my black HP laptop bag in CSE block yesterday", client=client, today=TODAY)
    assert r.mode == "rules" and r.reply_source == "rules"
    assert r.reply.startswith("The AI model is temporarily unavailable. Switching to rule-based assistance.")
    assert "Shall I save it?" in r.reply or "?" in r.reply
    assert "Traceback" not in r.reply and "ReadTimeout" not in r.reply
    assert tools.calls == []


def test_tool_errors_are_friendly_in_chips() -> None:
    assert loop._summary("create_lost_report", {"error": "not_confirmed", "message": "internal text"}) == \
        "waiting for your 'yes'"
    assert loop._summary("search_items", {"error": "internal_error", "message": "boom"}) == "could not complete"
