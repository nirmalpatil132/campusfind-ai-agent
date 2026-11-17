"""Rule-based engine: scripted multi-turn conversations against a fake tool registry."""

from __future__ import annotations

from typing import Any

from app.agent.prompts import POTENTIAL_MATCH_SENTENCE
from app.agent.rule_agent import handle_message
from tests.conftest import TODAY


class FakeTools:
    """Stands in for tools.execute_tool: records every call, returns canned outputs."""

    def __init__(self, **responses: Any) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.responses = responses

    def __call__(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, args))
        if name in self.responses:
            r = self.responses[name]
            return r(args) if callable(r) else r
        return {
            "create_lost_report": {"item_id": "x", "code": "L-0001", "status": "open"},
            "create_found_report": {"item_id": "y", "code": "F-0001", "status": "open"},
            "find_matching_items": {"item_code": args.get("item_code"), "candidates": []},
            "search_items": {"count": 0, "results": []},
            "get_my_reports": {"count": 0, "reports": []},
        }.get(name, {"error": "unknown_tool", "message": "?"})

    @property
    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def talk(tools: FakeTools, *messages: str) -> tuple[list[str], dict[str, Any]]:
    state, replies = None, []
    for m in messages:
        turn = handle_message(m, state, tools, TODAY, "Riya")
        state = turn.state
        replies.append(turn.reply)
    return replies, state


def test_asks_one_question_at_a_time_and_confirms_before_saving() -> None:
    tools = FakeTools()
    r, state = talk(tools, "I lost my wallet", "Main canteen", "yesterday",
                    "brown leather, Wildcraft, bus pass inside")
    assert "Where did you last have it?" in r[0]
    assert "When did you lose it?" in r[1]
    assert "colour, brand or unique mark" in r[2]
    assert "Shall I save it?" in r[3]
    assert state["stage"] == "confirming"
    assert tools.calls == []  # nothing saved before "yes"

    r2, state = talk_continue(tools, state, "yes")
    assert tools.names == ["create_lost_report", "find_matching_items"]
    args = tools.calls[0][1]
    assert args["category"] == "wallet"
    assert args["location"] == "Main Canteen"
    assert args["lost_date"] == "2026-11-16"  # "yesterday" resolved against TODAY
    assert args["color"] == "brown" and args["brand"] == "Wildcraft"
    assert "L-0001" in r2 and state["stage"] == "idle"


def talk_continue(tools: FakeTools, state: dict[str, Any], message: str) -> tuple[str, dict[str, Any]]:
    turn = handle_message(message, state, tools, TODAY, "Riya")
    return turn.reply, turn.state


def test_everything_in_one_message_then_correction() -> None:
    tools = FakeTools()
    r, state = talk(tools, "I lost my black HP laptop bag in CSE block yesterday around 2pm", "no",
                    "it was in the library")
    assert "Shall I save it?" in r[0]          # all fields + colour known: straight to summary
    assert "What should I change?" in r[1]     # plain "no": nothing saved
    assert "Updated." in r[2] and "Central Library" in r[2]
    assert tools.calls == []
    reply, state = talk_continue(tools, state, "yes")
    assert tools.calls[0][1]["location"] == "Central Library"
    assert tools.calls[0][1]["lost_time"] == "14:00"


def test_found_flow_asks_holding_and_private_hint_then_cancel_saves_nothing() -> None:
    tools = FakeTools()
    r, state = talk(tools, "I found a blue bottle in the library today", "I gave it to the admin office",
                    "skip", "cancel")
    assert "Where is the item now" in r[0]
    assert "private detail" in r[1]
    assert "Shall I save it?" in r[2] and "at the admin office" in r[2]
    assert "Nothing was saved" in r[3]
    assert tools.calls == [] and state["stage"] == "idle"


def test_private_hint_is_kept_out_of_public_description() -> None:
    tools = FakeTools()
    _, state = talk(tools, "I found a black wallet in the auditorium today", "with me",
                    "it has a photo of a dog inside", "yes")
    args = tools.calls[0][1]
    assert args["verification_hint"] == "it has a photo of a dog inside"
    assert "dog" not in args["description"]
    assert args["current_holding"] == "finder"


def test_vague_message_asks_what_item() -> None:
    tools = FakeTools()
    r, state = talk(tools, "I lost something", "my calculator")
    assert "What did you lose?" in r[0]
    assert state["fields"]["category"] == "electronics"
    assert "Where did you last have it?" in r[1]


def test_gives_up_after_three_unanswered_questions() -> None:
    tools = FakeTools()
    r, state = talk(tools, "I lost my keys", "dunno", "no idea", "somewhere")
    assert "didn't catch that" in r[1] and "didn't catch that" in r[2]
    assert "trouble understanding" in r[3]
    assert state["stage"] == "idle" and tools.calls == []


def test_search_uses_search_tool() -> None:
    results = {"count": 1, "results": [{"code": "F-0002", "report_type": "found", "item_name": "water bottle",
                                        "color": "navy", "brand": "Milton", "location": "Central Library",
                                        "event_date": "2026-11-16", "status": "open",
                                        "current_holding": "with the finder"}]}
    tools = FakeTools(search_items=results)
    r, _ = talk(tools, "has anyone found a blue bottle?")
    assert tools.calls == [("search_items", {"report_type": "found", "category": "bottle", "query": "blue"})]
    assert "F-0002" in r[0] and "not confirmed matches" in r[0]


def test_duplicate_is_reported_not_saved_twice() -> None:
    tools = FakeTools(create_lost_report={"duplicate_of": "L-0007", "message": "dup"})
    r, _ = talk(tools, "I lost my black HP laptop bag in CSE block yesterday", "yes")
    assert "L-0007" in r[1]
    assert tools.names == ["create_lost_report"]  # no matching for a duplicate


def test_candidates_are_never_presented_as_certain() -> None:
    cand = {"item_code": "F-0001", "report_type": "found", "item_name": "laptop bag", "color": "black",
            "brand": "HP", "location": "CSE Block", "event_date": "2026-11-16", "total_score": 0.93,
            "band": "strong_candidate", "explanation": "same category, same colour, same place",
            "match_id": "m1", "match_status": "candidate"}
    tools = FakeTools(find_matching_items={"item_code": "L-0001", "candidates": [cand]})
    r, _ = talk(tools, "I lost my black HP laptop bag in CSE block yesterday", "yes")
    assert POTENTIAL_MATCH_SENTENCE in r[1]
    assert "Might be mine" in r[1]
    assert "is yours" not in r[1].lower()


def test_tool_error_keeps_report_for_retry() -> None:
    tools = FakeTools(create_lost_report={"error": "database_not_configured", "message": "The database is not set up."})
    r, state = talk(tools, "I lost my black HP laptop bag in CSE block yesterday", "yes")
    assert "couldn't save" in r[1]
    assert state["stage"] == "confirming"


def test_side_question_while_collecting_keeps_the_report() -> None:
    tools = FakeTools()
    r, state = talk(tools, "I lost my wallet", "my reports")
    assert tools.names == ["get_my_reports"]
    assert state["stage"] == "collecting" and "isn't saved yet" in r[1]


def test_help_says_it_is_rule_based() -> None:
    r, _ = talk(FakeTools(), "hi")
    assert "rule-based mode" in r[0]
