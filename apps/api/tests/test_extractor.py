from datetime import date, time

import pytest

from app.agent.extractor import (
    extract, mask_sensitive, match_color, match_item, match_location, parse_date, parse_holding, parse_time,
)
from tests.conftest import TODAY


def test_full_sentence() -> None:
    ex = extract("I lost my black HP laptop bag with a red keychain in CSE block yesterday around 2pm", TODAY)
    assert (ex.category, ex.item_name) == ("bag", "laptop bag")
    assert ex.color == "black"
    assert ex.brand == "HP"
    assert ex.location == "CSE Block"
    assert ex.event_date == date(2026, 11, 16)
    assert ex.event_time == time(14, 0)


@pytest.mark.parametrize("text, expected", [
    ("yesterday", date(2026, 11, 16)),
    ("day before yesterday", date(2026, 11, 15)),
    ("today morning", TODAY),
    ("3 days ago", date(2026, 11, 14)),
    ("last monday", date(2026, 11, 16)),
    ("on Tuesday", TODAY),
    ("last tuesday", date(2026, 11, 10)),
    ("on 28 sept", date(2026, 9, 28)),
    ("September 28th", date(2026, 9, 28)),
    ("28/09/2026", date(2026, 9, 28)),
    ("2026-09-28", date(2026, 9, 28)),
    ("kal", date(2026, 11, 16)),
    ("25 december", date(2025, 12, 25)),  # no year + would be in the future -> last year
])
def test_dates(text: str, expected: date) -> None:
    assert parse_date(text, TODAY) == expected


def test_future_date_rejected() -> None:
    assert parse_date("2026-12-25", TODAY) is None
    assert parse_date("no date here", TODAY) is None


@pytest.mark.parametrize("text, expected", [
    ("around 2pm", time(14, 0)), ("at 2:30 pm", time(14, 30)), ("14:05", time(14, 5)),
    ("12 am", time(0, 0)), ("in the evening", time(18, 0)), ("no time", None),
])
def test_times(text: str, expected: time | None) -> None:
    assert parse_time(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("library", "Central Library"), ("near the canteen", "Main Canteen"), ("E&TC", "E&TC Block"),
    ("boys hostel", "Boys Hostel A"), ("somewhere", None),
    # prefers the place after a preposition, not "CSE" in the student's year
    ("my ID card, 2nd year CSE, lost in the canteen", "Main Canteen"),
    # "gave it to the admin office" is where it is NOW, not where it was found
    ("found a wallet in the auditorium and gave it to the admin office", "Auditorium"),
])
def test_locations(text: str, expected: str | None) -> None:
    assert match_location(text) == expected


def test_items_and_colours() -> None:
    assert match_item("Keys on a blue keyring") == ("keys", "keys")
    assert match_item("boAt Airdopes") == ("electronics", "earbuds")
    assert match_item("a smartwatch") == ("electronics", "smartwatch")
    assert match_item("nothing") is None
    assert match_color("Navy blue hoodie") == "navy blue"
    assert match_color("grey case") == "grey"
    assert match_color("gray case") == "grey"


def test_holding() -> None:
    assert parse_holding("I handed it to security", answer=False) == "security"
    assert parse_holding("found it near the admin office", answer=False) is None  # a place, not holding
    assert parse_holding("it's with me", answer=False) == "finder"
    assert parse_holding("office", answer=True) == "admin_office"
    assert parse_holding("me", answer=True) == "finder"


def test_mask_sensitive() -> None:
    assert mask_sensitive("call me on 98765 43210") == "call me on [number hidden]"
    assert mask_sensitive("Casio fx-991ES, lost 2026-09-28") == "Casio fx-991ES, lost 2026-09-28"
