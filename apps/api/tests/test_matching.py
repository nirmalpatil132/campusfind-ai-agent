"""Scoring functions with hand-calculated expected values (§14)."""

from datetime import date, time

import pytest

from app.services.matching import (
    ItemFeatures, band_for, brand_score, category_score, color_score, is_probable_duplicate,
    location_score, score_pair, shares_unique_mark, time_score, weighted_score,
)


def test_worked_example_from_guide() -> None:
    # §14: 0.30×0.8 + 0.20×1.0 + 0.15×0.8 + 0.15×1.0 + 0.10×1.0 + 0.10×0.5 = 0.86
    signals = {"semantic": 0.8, "category": 1.0, "color": 0.8, "location": 1.0, "time": 1.0, "brand": 0.5}
    assert weighted_score(signals) == pytest.approx(0.86)


def _bottles() -> tuple[ItemFeatures, ItemFeatures]:
    lost = ItemFeatures("bottle", "blue", None, "Central Library", "academic", date(2026, 9, 28), time(11, 30),
                        "Blue water bottle with stickers")
    found = ItemFeatures("bottle", "navy", "Milton", "Central Library", "academic", date(2026, 9, 28), time(16, 0),
                         "Navy blue Milton bottle with some stickers")
    return lost, found


def test_worked_example_full_pair_with_unique_mark() -> None:
    lost, found = _bottles()
    r = score_pair(lost, found, cosine=0.8)
    assert r["breakdown"] == {"semantic": 0.8, "category": 1.0, "color": 0.8, "location": 1.0, "time": 1.0,
                              "brand": 0.5}
    assert r["unique_mark_bonus"] is True          # "stickers" on both sides: +0.05
    assert r["total_score"] == pytest.approx(0.91)
    assert r["band"] == "strong_candidate"


def test_renormalised_without_embeddings() -> None:
    lost, found = _bottles()
    r = score_pair(lost, found, cosine=None)
    # (0.20 + 0.12 + 0.15 + 0.10 + 0.05) / 0.70 = 0.8857 ; +0.05 mark = 0.9357
    assert "semantic" not in r["breakdown"]
    assert r["semantic_used"] is False
    assert r["total_score"] == pytest.approx(0.936, abs=0.001)


def test_brand_conflict_caps_score() -> None:
    lost = ItemFeatures("bag", "black", "Dell", "CSE Block", "academic", date(2026, 9, 29), time(15, 0))
    found = ItemFeatures("bag", "black", "HP", "CSE Block", "academic", date(2026, 9, 29), time(17, 0))
    r = score_pair(lost, found, cosine=None)
    assert r["brand_conflict"] is True
    assert r["total_score"] == 0.5
    assert r["band"] == "hidden"


@pytest.mark.parametrize("a, b, expected", [
    ("blue", "blue", 1.0), ("navy", "blue", 0.8), ("maroon", "red", 0.8), ("gold", "golden", 0.8),
    ("dark blue", "navy", 0.8), ("black", "white", 0.0), (None, "red", 0.5),
])
def test_color_score(a: str | None, b: str | None, expected: float) -> None:
    assert color_score(a, b) == expected


def test_category_location_brand() -> None:
    assert category_score("bag", "bag") == 1.0
    assert category_score("bag", "electronics") == 0.6
    assert category_score("bag", "keys") == 0.0
    assert location_score("CSE Block", "academic", "CSE Block", "academic") == 1.0
    assert location_score("CSE Block", "academic", "Mech Block", "academic") == 0.6
    assert location_score("CSE Block", "academic", "Main Canteen", "common") == 0.2
    assert brand_score("boAt", "BOAT") == 1.0
    assert brand_score("HP", "Dell") == 0.0
    assert brand_score(None, "HP") == 0.5


def test_time_decay() -> None:
    d = date(2026, 9, 1)
    assert time_score(d, time(10, 0), d, time(20, 0)) == 1.0                    # same day
    assert time_score(d, None, date(2026, 9, 15), None) == pytest.approx(0.2)   # 14 days
    # 7.5 days later = 180 h: 1 - 0.8 × (180-24)/(336-24) = 0.6
    assert time_score(d, time(0, 0), date(2026, 9, 8), time(12, 0)) == pytest.approx(0.6)
    # found 12 h BEFORE the reported loss: 1 - 0.8 × 0.5 = 0.6
    assert time_score(d, time(22, 0), d, time(10, 0)) == pytest.approx(0.6)


def test_unique_mark_respects_colour() -> None:
    assert shares_unique_mark("red keychain on zip", "bag with a red keychain")
    assert not shares_unique_mark("College ID card, red lanyard", "ID card on blue lanyard")
    assert shares_unique_mark("ink stain on left sleeve", "ink mark on one sleeve")


def test_bands() -> None:
    assert band_for(0.75) == "strong_candidate"
    assert band_for(0.6) == "possible_candidate"
    assert band_for(0.54) == "hidden"
    # stricter bar when the semantic signal is missing (renormalised scores run higher)
    assert band_for(0.69, semantic_used=False) == "hidden"
    assert band_for(0.70, semantic_used=False) == "possible_candidate"


def test_umbrella_trap_hidden_without_embeddings() -> None:
    lost = ItemFeatures("other", "black", None, "Bus Stop", "common", date(2026, 9, 22), time(8, 30),
                        "Black folding umbrella with wooden handle")
    found = ItemFeatures("other", "black", None, "Girls Hostel B", "hostel", date(2026, 9, 30), time(18, 0),
                         "Black umbrella with plastic handle")
    r = score_pair(lost, found, cosine=None)
    assert r["total_score"] == pytest.approx(0.692, abs=0.001)
    assert r["band"] == "hidden"


def test_tie_broken_by_unique_mark() -> None:
    from app.services.matching import rank_key
    lost = ItemFeatures("bag", "black", "HP", "CSE Block", "academic", date(2026, 9, 29), time(14, 0),
                        "Black HP laptop bag, red keychain on zip")
    f01 = ItemFeatures("bag", "black", "HP", "CSE Block", "academic", date(2026, 9, 29), time(15, 10),
                       "Black laptop bag with a red keychain")
    f11 = ItemFeatures("bag", "black", "HP", "CSE Block", "academic", date(2026, 9, 29), time(17, 0),
                       "Black HP laptop bag, empty")
    a, b = score_pair(lost, f01, None), score_pair(lost, f11, None)
    assert a["total_score"] == b["total_score"] == 1.0
    assert rank_key(a) > rank_key(b)


def test_duplicate_rule() -> None:
    a = {"created_by": "u1", "report_type": "lost", "category": "bag", "event_date": date(2026, 9, 29),
         "location": "CSE Block", "item_name": "laptop bag"}
    assert is_probable_duplicate(a, {**a, "event_date": date(2026, 9, 30)})
    assert not is_probable_duplicate(a, {**a, "created_by": "u2"})
    assert not is_probable_duplicate(a, {**a, "event_date": date(2026, 10, 5)})
    assert is_probable_duplicate(a, {**a, "location": "Mech Block"}, cosine=0.9)
