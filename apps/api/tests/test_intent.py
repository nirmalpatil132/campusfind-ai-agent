import pytest

from app.agent.intent import detect_intent, find_item_code, is_affirmative, is_negative


@pytest.mark.parametrize("text, expected", [
    ("I lost my wallet", "report_lost"),
    ("I lost my wallet, has anyone found it?", "report_lost"),
    ("mera wallet kho gaya", "report_lost"),
    ("I can't find my calculator", "report_lost"),
    ("I found a blue water bottle in the library", "report_found"),
    ("Found a black umbrella near the bus stop", "report_found"),
    ("mujhe ek bottle mili", "report_found"),
    ("Has anyone found a calculator?", "search"),
    ("show me found bottles", "search"),
    ("what did I report?", "check_status"),
    ("my reports", "check_status"),
    ("any matches for L-0001?", "find_matches"),
    ("details of F-0002", "item_details"),
    ("L-0003", "item_details"),
    ("hi", "greeting"),
    ("what can you do", "help"),
    ("cancel", "cancel"),
    ("I found it myself, got it back", "self_found"),
    ("write my assignment on thermodynamics", "other"),
])
def test_detect_intent(text: str, expected: str) -> None:
    assert detect_intent(text) == expected


@pytest.mark.parametrize("text", ["yes", "Yes!", "y", "ok", "haan", "sure, save it", "yes please", "correct"])
def test_affirmative(text: str) -> None:
    assert is_affirmative(text)


@pytest.mark.parametrize("text", ["no", "nahi", "yes but change the location", "ok but it was yesterday",
                                  "I lost my wallet near the canteen yesterday around 2pm please help", ""])
def test_not_affirmative(text: str) -> None:
    assert not is_affirmative(text)


def test_negative() -> None:
    assert is_negative("no, it was in the library")
    assert not is_negative("yes")


def test_item_code_normalised() -> None:
    assert find_item_code("matches for l-1") == "L-0001"
    assert find_item_code("F0017 please") == "F-0017"
    assert find_item_code("no code here") is None
