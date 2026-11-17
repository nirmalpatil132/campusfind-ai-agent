"""Matching engine (PROJECT_GUIDE.md §14). Plain Python + SQL — the LLM is NOT involved.

Stage 1 (SQL):    shortlist opposite-type reports: same/related category, date window,
                  different reporter, still unresolved, pair not already rejected.
Stage 2 (Python): weighted score

    score = 0.30·semantic + 0.20·category + 0.15·colour + 0.15·location + 0.10·time + 0.10·brand

Fallback when embeddings are missing (Ollama not running): the semantic signal is dropped and
the other five weights are divided by their sum (0.70), so they still add up to 1.0.

Hard rules: conflicting brands cap the score at 0.50; a unique mark on both sides
("red keychain") adds +0.05 (max 1.0).
Bands: >= 0.75 strong_candidate · >= 0.55 possible_candidate · below that hidden.

The score is a ranking aid, NOT a probability. A match is never verified automatically:
only the owner (confirm) and then an admin (verify) can move it forward.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any

from app import db
from app.campus import HOLDING_LABELS
from app.services import notifications
from app.services.errors import Forbidden, NotFound

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WEIGHTS: dict[str, float] = {
    "semantic": 0.30, "category": 0.20, "color": 0.15,
    "location": 0.15, "time": 0.10, "brand": 0.10,
}
STRONG_THRESHOLD = 0.75
CANDIDATE_THRESHOLD = 0.55
# Without embeddings the 5 structured signals are inflated by renormalisation (÷0.70), so a
# stricter bar is used (evaluation v2: removes the L14–F14 umbrella trap; recall stays 10/10).
CANDIDATE_THRESHOLD_NO_SEMANTIC = 0.70
BRAND_CONFLICT_CAP = 0.50
UNIQUE_MARK_BONUS = 0.05

RELATED_CATEGORIES: set[frozenset[str]] = {
    frozenset({"electronics", "bag"}),        # laptop bags / laptop sleeves
    frozenset({"wallet", "id_card"}),         # card holders, bus passes
    frozenset({"electronics", "jewellery"}),  # smartwatch vs watch
    frozenset({"electronics", "stationery"}), # calculators
    frozenset({"book", "stationery"}),
}

COLOR_FAMILIES: dict[str, set[str]] = {
    "blue": {"blue", "navy", "navy blue", "dark blue", "light blue", "sky blue", "royal blue"},
    "red": {"red", "maroon", "crimson"},
    "gold": {"gold", "golden"},
    "grey": {"grey", "dark grey", "light grey"},
    "white": {"white", "off white", "cream"},
    "green": {"green", "dark green", "light green", "olive"},
    "brown": {"brown", "tan", "beige"},
    "purple": {"purple", "violet"},
}

# Words that describe a distinguishing mark. Values are the normalised stem.
MARK_WORDS: dict[str, str] = {
    "keychain": "keychain", "key chain": "keychain", "keyring": "keyring", "key ring": "keyring",
    "sticker": "sticker", "stickers": "sticker",
    "scratch": "scratch", "scratched": "scratch", "scratches": "scratch",
    "crack": "crack", "cracked": "crack", "dent": "dent", "dented": "dent",
    "stain": "stain", "ink": "ink", "lanyard": "lanyard",
    "engraving": "engraving", "engraved": "engraving",
    "highlighter": "highlight", "highlighting": "highlight", "highlighted": "highlight",
    "name": "name", "label": "label", "tag": "tag", "charm": "charm",
}

_ZERO_TIME = time(12, 0)


# ---------------------------------------------------------------------------
# Pure scoring functions (unit-tested with hand-calculated values)
# ---------------------------------------------------------------------------
def _norm(s: str | None) -> str | None:
    if s is None:
        return None
    s = s.strip().lower().replace("gray", "grey").replace("-", " ")
    return s or None


def color_family(color: str | None) -> str | None:
    c = _norm(color)
    if not c:
        return None
    for family, members in COLOR_FAMILIES.items():
        if c in members:
            return family
    last = c.split()[-1]
    for family, members in COLOR_FAMILIES.items():
        if last in members:
            return family
    return c


def category_score(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return 0.6 if frozenset({a, b}) in RELATED_CATEGORIES else 0.0


def color_score(a: str | None, b: str | None) -> float:
    """1.0 same · 0.8 same family (navy≈blue, maroon≈red, golden≈gold) · 0 conflicting · 0.5 missing."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.5
    if na == nb:
        return 1.0
    return 0.8 if color_family(na) == color_family(nb) else 0.0


def location_score(loc_a: str | None, zone_a: str | None, loc_b: str | None, zone_b: str | None) -> float:
    """1.0 same place · 0.6 same zone · 0.2 otherwise · 0.5 if a location is missing."""
    if not loc_a or not loc_b:
        return 0.5
    if loc_a == loc_b:
        return 1.0
    return 0.6 if zone_a and zone_a == zone_b else 0.2


def time_score(lost_date: date, lost_time: time | None, found_date: date, found_time: time | None) -> float:
    """1.0 if found within 24 h after the loss, falling linearly to 0.2 at 14 days.

    Found BEFORE the reported loss (people misremember dates; Stage 1 allows 1 day):
    1.0 at 0 h falling to 0.2 at 24 h before.
    """
    if lost_time is None or found_time is None:  # only dates known: compare whole days
        hours = (found_date - lost_date).days * 24.0
    else:
        delta = datetime.combine(found_date, found_time) - datetime.combine(lost_date, lost_time)
        hours = delta.total_seconds() / 3600
    if hours < 0:
        return max(0.2, 1.0 - 0.8 * (-hours / 24.0))
    if hours <= 24:
        return 1.0
    if hours >= 14 * 24:
        return 0.2
    return 1.0 - 0.8 * (hours - 24) / (14 * 24 - 24)


def brand_score(a: str | None, b: str | None) -> float:
    """1.0 same · 0 conflicting · 0.5 if either is missing."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.5
    return 1.0 if na == nb else 0.0


def brands_conflict(a: str | None, b: str | None) -> bool:
    na, nb = _norm(a), _norm(b)
    return bool(na and nb and na != nb)


def extract_marks(description: str | None) -> dict[str, str | None]:
    """{'keychain': 'red', 'sticker': None} — mark stem -> colour family right before it (if any)."""
    if not description:
        return {}
    t = description.lower()
    marks: dict[str, str | None] = {}
    for word, stem in sorted(MARK_WORDS.items(), key=lambda kv: -len(kv[0])):
        for m in re.finditer(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", t):
            before = re.findall(r"[a-z]+", t[max(0, m.start() - 25): m.start()])[-2:]
            colour = next((color_family(w) for w in reversed(before) if color_family(w) in COLOR_FAMILIES), None)
            if stem not in marks or (marks[stem] is None and colour):
                marks[stem] = colour
    return marks


def shares_unique_mark(desc_a: str | None, desc_b: str | None) -> bool:
    """Same mark on both sides with compatible colours ('red lanyard' vs 'blue lanyard' does NOT count)."""
    ma, mb = extract_marks(desc_a), extract_marks(desc_b)
    for stem in ma.keys() & mb.keys():
        ca, cb = ma[stem], mb[stem]
        if ca is None or cb is None or ca == cb:
            return True
    return False


def candidate_threshold(semantic_used: bool) -> float:
    return CANDIDATE_THRESHOLD if semantic_used else CANDIDATE_THRESHOLD_NO_SEMANTIC


def band_for(score: float, semantic_used: bool = True) -> str:
    if score >= STRONG_THRESHOLD:
        return "strong_candidate"
    if score >= candidate_threshold(semantic_used):
        return "possible_candidate"
    return "hidden"


def rank_key(result: dict[str, Any]) -> tuple[float, float]:
    """Sort key: total score, then the score before the 1.0 cap (breaks ties such as
    L01: F01 with the shared red keychain vs F11 without it, both capped at 1.0)."""
    return (result["total_score"], result["rank_score"])


@dataclass(frozen=True)
class ItemFeatures:
    """Everything the scorer needs about one report."""
    category: str
    color: str | None
    brand: str | None
    location: str | None
    zone: str | None
    event_date: date
    event_time: time | None
    description: str | None = None


def weighted_score(signals: dict[str, float]) -> float:
    """Weighted sum, renormalised over the signals present (semantic may be missing)."""
    used = {k: WEIGHTS[k] for k in signals}
    total_weight = sum(used.values())
    return sum(used[k] * signals[k] for k in signals) / total_weight


def score_pair(lost: ItemFeatures, found: ItemFeatures, cosine: float | None) -> dict[str, Any]:
    """Score one lost/found pair. Returns {total_score, band, breakdown, ...}."""
    signals: dict[str, float] = {}
    if cosine is not None:
        signals["semantic"] = max(0.0, min(1.0, float(cosine)))
    signals["category"] = category_score(lost.category, found.category)
    signals["color"] = color_score(lost.color, found.color)
    signals["location"] = location_score(lost.location, lost.zone, found.location, found.zone)
    signals["time"] = time_score(lost.event_date, lost.event_time, found.event_date, found.event_time)
    signals["brand"] = brand_score(lost.brand, found.brand)

    raw = weighted_score(signals)
    mark = shares_unique_mark(lost.description, found.description)
    if mark:
        raw += UNIQUE_MARK_BONUS
    conflict = brands_conflict(lost.brand, found.brand)
    if conflict:
        raw = min(raw, BRAND_CONFLICT_CAP)
    total = round(min(1.0, raw), 3)
    return {
        "total_score": total,
        "rank_score": round(raw, 4),
        "band": band_for(total, cosine is not None),
        "breakdown": {k: round(v, 3) for k, v in signals.items()},
        "unique_mark_bonus": mark,
        "brand_conflict": conflict,
        "semantic_used": cosine is not None,
    }


def explain(breakdown: dict[str, float], unique_mark: bool = False) -> str:
    """Plain-language reasons, e.g. 'same category, same colour, same place, within a day'."""
    parts: list[str] = []
    b = breakdown
    if b.get("category") == 1.0:
        parts.append("same category")
    elif b.get("category", 0) > 0:
        parts.append("related category")
    if b.get("color") == 1.0:
        parts.append("same colour")
    elif b.get("color") == 0.8:
        parts.append("similar colour")
    elif b.get("color") == 0.0:
        parts.append("different colour")
    if b.get("location") == 1.0:
        parts.append("same place")
    elif b.get("location") == 0.6:
        parts.append("same area of campus")
    if b.get("time", 0) >= 0.99:
        parts.append("within about a day")
    elif b.get("time", 0) >= 0.6:
        parts.append("within a week")
    if b.get("brand") == 1.0:
        parts.append("same brand")
    elif b.get("brand") == 0.0:
        parts.append("different brand")
    if "semantic" in b and b["semantic"] >= 0.75:
        parts.append("very similar description")
    if unique_mark:
        parts.append("same distinguishing mark")
    return ", ".join(parts) if parts else "some details overlap"


def stage1_filter(item: ItemFeatures, item_type: str, other: ItemFeatures) -> bool:
    """Python mirror of the Stage-1 SQL filter (used by the offline evaluation script)."""
    if category_score(item.category, other.category) == 0.0:
        return False
    if item_type == "lost":
        days = (other.event_date - item.event_date).days
    else:
        days = (item.event_date - other.event_date).days
    return -1 <= days <= 30


def is_probable_duplicate(a: dict[str, Any], b: dict[str, Any], cosine: float | None = None) -> bool:
    """Duplicate rule (§14): same user + type + category, within 3 days, AND
    (embedding similarity >= 0.85 OR same location and same item name).
    Python mirror of the SQL in services/items.py (used by the offline evaluation)."""
    if (a["created_by"], a["report_type"], a["category"]) != (b["created_by"], b["report_type"], b["category"]):
        return False
    if abs((a["event_date"] - b["event_date"]).days) > 3:
        return False
    if cosine is not None and cosine >= 0.85:
        return True
    return a["location"] == b["location"] and a["item_name"].strip().lower() == b["item_name"].strip().lower()


# ---------------------------------------------------------------------------
# Database part
# ---------------------------------------------------------------------------
_STAGE1_SQL = """
select o.id, o.code, o.report_type, o.category, o.item_name, o.brand, o.color, o.description,
       o.event_date, o.event_time, o.status, o.created_by, lo.name as location, lo.zone as zone,
       fr.current_holding,
       case when o.embedding is not null and x.embedding is not null
            then 1 - (o.embedding <=> x.embedding) end as cosine
from public.items x
join public.items o on o.report_type <> x.report_type
left join public.locations lo on lo.id = o.location_id
left join public.found_reports fr on fr.item_id = o.id
where x.id = %(item_id)s
  and o.status in ('open', 'matched')
  and o.created_by <> x.created_by
  and o.category = any(%(categories)s)
  and ((x.report_type = 'lost'  and o.event_date between x.event_date - 1  and x.event_date + 30)
    or (x.report_type = 'found' and o.event_date between x.event_date - 30 and x.event_date + 1))
  and not exists (
      select 1 from public.matches m
      where m.status = 'rejected'
        and ((x.report_type = 'lost'  and m.lost_item_id = x.id and m.found_item_id = o.id)
          or (x.report_type = 'found' and m.found_item_id = x.id and m.lost_item_id = o.id)))
order by cosine desc nulls last, o.event_date
limit 20
"""

_UPSERT_SQL = """
insert into public.matches (lost_item_id, found_item_id, similarity_score, score_breakdown)
values (%(lost_id)s, %(found_id)s, %(score)s, %(breakdown)s)
on conflict (lost_item_id, found_item_id) do update
   set similarity_score = excluded.similarity_score,
       score_breakdown  = excluded.score_breakdown
   where public.matches.status = 'candidate'
returning id, status, (xmax = 0) as inserted
"""


def _related(category: str) -> list[str]:
    out = {category}
    for pair in RELATED_CATEGORIES:
        if category in pair:
            out |= pair
    return sorted(out)


def _features(row: dict[str, Any]) -> ItemFeatures:
    return ItemFeatures(
        category=row["category"], color=row.get("color"), brand=row.get("brand"),
        location=row.get("location"), zone=row.get("zone"), event_date=row["event_date"],
        event_time=row.get("event_time"), description=row.get("description"),
    )


def find_matches(user: Any, item_code: str, limit: int = 5) -> dict[str, Any]:
    """Run matching for one report. Only its owner or an admin may run it.

    Candidates with score >= 0.55 are saved in `matches` (status 'candidate'), both items are
    marked 'matched', and the lost-item owner is notified about NEW candidates.
    Nothing is ever verified here.
    """
    limit = max(1, min(int(limit or 5), 10))
    with db.transaction() as conn:
        item = db.fetch_one(
            """select i.id, i.code, i.report_type, i.category, i.item_name, i.brand, i.color,
                      i.description, i.event_date, i.event_time, i.status, i.created_by,
                      lo.name as location, lo.zone as zone
               from public.items i left join public.locations lo on lo.id = i.location_id
               where i.code = %(code)s""",
            {"code": item_code.strip().upper()}, conn=conn,
        )
        if not item:
            raise NotFound(f"No report with code {item_code}.")
        if str(item["created_by"]) != str(user.id) and user.role != "admin":
            raise Forbidden("You can only run matching for your own reports.")
        if item["status"] not in ("open", "matched"):
            return {"item_code": item["code"], "candidates": [],
                    "note": f"This report is '{item['status']}', so matching is not run."}

        rows = db.fetch_all(_STAGE1_SQL, {"item_id": item["id"], "categories": _related(item["category"])},
                            conn=conn)
        me = _features(item)
        scored = []
        for row in rows:
            other = _features(row)
            lost, found = (me, other) if item["report_type"] == "lost" else (other, me)
            cos = float(row["cosine"]) if row["cosine"] is not None else None
            result = score_pair(lost, found, cos)
            if result["band"] != "hidden":
                scored.append((result, row))
        scored.sort(key=lambda pair: rank_key(pair[0]), reverse=True)
        scored = scored[:limit]

        candidates = []
        for result, row in scored:
            lost_id, found_id = ((item["id"], row["id"]) if item["report_type"] == "lost"
                                 else (row["id"], item["id"]))
            saved = db.fetch_one(_UPSERT_SQL, {
                "lost_id": lost_id, "found_id": found_id, "score": result["total_score"],
                "breakdown": db.jsonb({**result["breakdown"],
                                       "unique_mark_bonus": result["unique_mark_bonus"],
                                       "brand_conflict": result["brand_conflict"],
                                       "semantic_used": result["semantic_used"]}),
            }, conn=conn)
            if saved is None:  # pair exists with a non-candidate status: keep it, just read it
                saved = db.fetch_one(
                    "select id, status, false as inserted from public.matches "
                    "where lost_item_id = %(l)s and found_item_id = %(f)s",
                    {"l": lost_id, "f": found_id}, conn=conn)
            db.execute("update public.items set status = 'matched' "
                       "where id = any(%(ids)s) and status = 'open'",
                       {"ids": [lost_id, found_id]}, conn=conn)
            lost_code = item["code"] if item["report_type"] == "lost" else row["code"]
            found_code = row["code"] if item["report_type"] == "lost" else item["code"]
            if saved["inserted"]:
                owner_id = item["created_by"] if item["report_type"] == "lost" else row["created_by"]
                notifications.create(
                    owner_id, "match_candidate",
                    f"A potentially matching found report {found_code} was found for your {lost_code}. "
                    "Please verify the details before we treat this as a match.",
                    item_id=lost_id, conn=conn,
                )
            candidates.append({
                "item_code": row["code"],
                "report_type": row["report_type"],
                "item_name": row["item_name"],
                "category": row["category"],
                "color": row["color"],
                "brand": row["brand"],
                "location": row["location"],
                "event_date": row["event_date"].isoformat(),
                "current_holding": HOLDING_LABELS.get(row["current_holding"] or "", None),
                "total_score": result["total_score"],
                "band": result["band"],
                "breakdown": result["breakdown"],
                "explanation": explain(result["breakdown"], result["unique_mark_bonus"]),
                "match_id": str(saved["id"]),
                "match_status": saved["status"],
                "lost_code": lost_code,
                "found_code": found_code,
            })

    return {
        "item_code": item["code"],
        "candidates": candidates,
        "semantic_used": any(c["breakdown"].get("semantic") is not None for c in candidates),
        "note": "Candidates require owner and admin verification.",
    }
