"""Server-side guard for reports proposed by the local LLM: "the LLM proposes, the server checks".

Problem seen in real browser testing with qwen2.5:3b (the "iQube scooter key" chat):
  * the model invented a location ("Main Canteen") and a time the student never gave,
  * it said "I have created a lost report" although nothing was saved,
  * after "yes confirm" it called create_FOUND_report for a LOST key.

Fix: in LLM mode a create_* tool call never saves anything. Instead the server turns it into a
DRAFT:
  1. Report type = what the STUDENT said ("I lost" / "I found"), not what the model picked.
  2. Every field must be grounded in the student's own words. Values only the model produced
     (location, date, time, colour, brand, private hint) are dropped, and the student is asked.
  3. The draft becomes a normal rule-engine state, so the deterministic, tested form step asks
     the missing questions one at a time, shows the summary, and saves ONLY after "yes".
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from app.agent import rule_agent
from app.agent.extractor import extract, mask_sensitive, match_location, parse_date, parse_holding, parse_time
from app.agent.intent import detect_intent
from app.campus import CATEGORIES, LOCATION_ALIASES, LOCATIONS

CREATE_TOOLS = ("create_lost_report", "create_found_report")
_WORD_RE = re.compile(r"[a-z0-9]+")
_SAVED_CLAIM_RE = re.compile(
    r"\b(i\s*(have|'ve)?\s*(created|saved|submitted|registered|recorded|filed|logged)|"
    r"(report|it)\s+(has been|is|was)\s+(created|saved|submitted|registered|recorded|filed)|"
    r"successfully\s+(created|saved|submitted|registered))\b", re.I)


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) >= 3}


def report_texts(user_texts: list[str]) -> list[str]:
    """The student's messages from the most recent 'I lost / I found' message onwards."""
    for i in range(len(user_texts) - 1, -1, -1):
        if detect_intent(user_texts[i]) in ("report_lost", "report_found"):
            return user_texts[i:]
    return user_texts


def user_report_type(user_texts: list[str]) -> str | None:
    """'lost' or 'found' from the student's latest report message, or None if they never said."""
    for text in reversed(user_texts):
        intent = detect_intent(text)
        if intent == "report_lost":
            return "lost"
        if intent == "report_found":
            return "found"
    return None


def mentioned_locations(text: str) -> set[str]:
    t = text.lower()
    found = set()
    for loc in LOCATIONS:
        for alias in [loc.lower(), *LOCATION_ALIASES.get(loc, [])]:
            if re.search(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])", t):
                found.add(loc)
                break
    return found


def _grounded_text(value: Any, user_words: set[str], min_share: float = 1.0) -> str | None:
    """Keep a short text value only if (most of) its words appear in what the student wrote."""
    if not isinstance(value, str) or not value.strip():
        return None
    words = _words(value)
    if not words:
        return None
    share = len(words & user_words) / len(words)
    return value.strip() if share >= min_share else None


def draft_from_llm_call(tool_name: str, args: dict[str, Any], user_texts: list[str],
                        today: date, last_code: str | None = None) -> tuple[dict[str, Any], list[str]]:
    """Turn the model's create_* arguments into a rule-engine state holding ONLY grounded fields.

    Returns (state, dropped) where `dropped` lists the fields the model invented.
    """
    texts = report_texts([t for t in user_texts if t.strip()])
    joined = "\n".join(texts)
    user_words = _words(joined)
    model_type = "found" if tool_name == "create_found_report" else "lost"
    rtype = user_report_type(texts) or model_type
    dropped: list[str] = []
    if rtype != model_type:
        dropped.append("report_type")

    # 1) What the deterministic extractor reads from the student's own messages (in order).
    fields: dict[str, Any] = {"description": mask_sensitive(joined)[:1000]}
    for t in texts:
        rule_agent._merge(fields, extract(t, today), rtype, override=True)

    # 2) Model values are accepted only when the student actually said them.
    item = _grounded_text(args.get("item_name"), user_words)
    if item and len(item) <= 80:
        fields["item_name"] = item
        cat = str(args.get("category") or "").lower().replace(" ", "_")
        if not fields.get("category") and cat in CATEGORIES:
            fields["category"] = cat
    elif args.get("item_name") and not fields.get("item_name"):
        dropped.append("item_name")
    for key in ("color", "brand"):
        value = _grounded_text(args.get(key), user_words)
        if value and len(value) <= 40:
            fields[key] = value
        elif args.get(key) and not fields.get(key):
            dropped.append(key)

    model_loc = match_location(str(args.get("location") or "")) if args.get("location") else None
    said = mentioned_locations(joined)
    if model_loc and model_loc in said:
        fields["location"] = model_loc
    elif args.get("location") and model_loc != fields.get("location"):
        dropped.append("location")

    date_key, time_key = ("lost_date", "lost_time") if model_type == "lost" else ("found_date", "found_time")
    said_dates = {d.isoformat() for d in (parse_date(t, today) for t in texts) if d}
    if args.get(date_key) and str(args[date_key])[:10] not in said_dates:
        dropped.append("date")
    said_times = {t.strftime("%H:%M") for t in (parse_time(x) for x in texts) if t}
    if args.get(time_key) and str(args[time_key])[:5] not in said_times:
        dropped.append("time")

    if rtype == "found":
        if not fields.get("current_holding"):
            fields["current_holding"] = next(
                (h for h in (parse_holding(t) for t in texts) if h), None)
        hint = _grounded_text(args.get("verification_hint"), user_words, min_share=0.6)
        if hint:
            fields["verification_hint"] = mask_sensitive(hint)[:300]
        elif args.get("verification_hint"):
            dropped.append("verification_hint")
    else:
        fields.pop("current_holding", None)

    state = rule_agent.new_state(last_code)
    state["stage"], state["report_type"], state["fields"] = "collecting", rtype, fields
    return state, dropped


def start_reply(state: dict[str, Any]) -> str:
    """The next question, or the summary + 'Shall I save it?' (mutates state like the rule engine)."""
    intro = ("Sorry to hear that. Let's report it." if state["report_type"] == "lost"
             else "Thanks for reporting a found item!")
    return f"{intro} {rule_agent._ask_next_or_summarize(state)}"


def claims_saved(reply: str) -> bool:
    return bool(_SAVED_CLAIM_RE.search(reply or ""))


def remove_false_save_claim(reply: str, items_created: list[str]) -> str:
    """If the model says something was saved but the database created nothing, replace the reply."""
    if items_created or not claims_saved(reply):
        return reply
    return ("Nothing has been saved yet. Tell me what you lost or found, where and when, and I'll show you "
            "a summary to confirm before saving.")
