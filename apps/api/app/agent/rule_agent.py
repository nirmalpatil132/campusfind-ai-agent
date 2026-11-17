"""Deterministic fallback engine (used when no local LLM is available).

This is NOT an LLM. It is a rule-based state machine:

    idle ──report intent──▶ collecting ──all fields known──▶ confirming ──"yes"──▶ save + match ──▶ idle
                              │  asks ONE question per turn          │ "no"/correction → re-summarise
                              └─ 3 unanswered questions → give up    └─ "cancel" → idle (nothing saved)

It uses intent.py (what does the user want?) and extractor.py (which fields did they give?), and
calls the SAME tool registry as the LLM engine (passed in as `run_tool`), so both engines save,
search and match in exactly the same way. Every reply is a fixed template.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.agent.extractor import (
    Extraction, extract, is_skip, mask_sensitive, match_item, match_location, parse_date, parse_holding,
)
from app.agent.intent import detect_intent, find_item_code, is_affirmative, is_negative
from app.agent.prompts import POTENTIAL_MATCH_SENTENCE
from app.campus import HOLDING_LABELS

RunTool = Callable[[str, dict[str, Any]], dict[str, Any]]

MAX_MISSES = 3
REQUIRED = ("item", "location", "date")
SIDE_INTENTS = ("search", "check_status", "find_matches", "item_details")

QUESTIONS: dict[str, dict[str, str]] = {
    "lost": {
        "item": "What did you lose? (for example: 'black HP laptop bag')",
        "location": "Where did you last have it? (e.g. CSE Block, Central Library, Main Canteen, Auditorium)",
        "date": "When did you lose it? (e.g. 'today', 'yesterday', 'last Monday', '28 Sept')",
        "detail": "Any colour, brand or unique mark (sticker, scratch, keychain)? Say 'skip' if not.",
    },
    "found": {
        "item": "What did you find? (for example: 'blue water bottle')",
        "location": "Where did you find it? (e.g. CSE Block, Central Library, Main Canteen, Auditorium)",
        "date": "When did you find it? (e.g. 'today', 'yesterday', '28 Sept')",
        "holding": "Where is the item now: with you, at the admin office, or with security?",
        "hint": "Tell me one private detail only the real owner would know (only admins will see it). "
                "Say 'skip' if there's nothing.",
    },
}

HELP = (
    "I can help you:\n"
    "• report a lost item: 'I lost my black wallet in the canteen yesterday'\n"
    "• report a found item: 'I found a blue bottle in the library'\n"
    "• search: 'has anyone found a calculator?'\n"
    "• see your reports: 'my reports'\n"
    "• check matches: 'matches for L-0001'"
)


@dataclass
class RuleTurn:
    reply: str
    state: dict[str, Any]
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def new_state(last_code: str | None = None) -> dict[str, Any]:
    return {"stage": "idle", "report_type": None, "fields": {}, "asked": [], "pending": None,
            "misses": 0, "last_code": last_code}


# ---------------------------------------------------------------------------
# Field helpers
# ---------------------------------------------------------------------------
def _merge(fields: dict[str, Any], ex: Extraction, report_type: str, override: bool = False) -> list[str]:
    """Copy extracted values into fields. Returns the keys that changed."""
    changed = []
    values = {
        "category": ex.category, "item_name": ex.item_name, "color": ex.color, "brand": ex.brand,
        "location": ex.location,
        "event_date": ex.event_date.isoformat() if ex.event_date else None,
        "event_time": ex.event_time.strftime("%H:%M") if ex.event_time else None,
        "current_holding": ex.current_holding if report_type == "found" else None,
    }
    for key, value in values.items():
        if value is None:
            continue
        if fields.get(key) is None or (override and fields[key] != value):
            fields[key] = value
            changed.append(key)
    return changed


def _next_missing(st: dict[str, Any]) -> str | None:
    f, rt, asked = st["fields"], st["report_type"], st["asked"]
    if not f.get("category"):
        return "item"
    if not f.get("location"):
        return "location"
    if not f.get("event_date"):
        return "date"
    if rt == "lost" and "detail" not in asked and not (f.get("color") or f.get("brand")):
        return "detail"
    if rt == "found" and "holding" not in asked and not f.get("current_holding"):
        return "holding"
    if rt == "found" and "hint" not in asked:
        return "hint"
    return None


def _summary(st: dict[str, Any]) -> str:
    f, rt = st["fields"], st["report_type"]
    brand = f.get("brand")
    if brand and brand.lower() in (f.get("item_name") or "").lower():
        brand = None  # "iQube" + "IQube key" would read "iQube IQube key"
    what = " ".join(x for x in (f.get("color"), brand, f.get("item_name")) if x)
    when = f["event_date"] + (f" around {f['event_time']}" if f.get("event_time") else "")
    lines = [f"Here's your {rt.upper()} report:",
             f"• Item: {what} ({f['category'].replace('_', ' ')})",
             f"• Where: {f['location']}",
             f"• When: {when}"]
    if rt == "found":
        lines.append(f"• Item is now: {HOLDING_LABELS.get(f.get('current_holding') or '', 'not specified')}")
        if f.get("verification_hint"):
            lines.append("• Private owner detail: saved (only admins will see it)")
    lines.append(f"• Details: \"{f['description']}\"")
    lines.append("Shall I save it? Reply yes to save, or tell me what to change.")
    return "\n".join(lines)


def _ask_next_or_summarize(st: dict[str, Any]) -> str:
    nxt = _next_missing(st)
    if nxt:
        st["pending"] = nxt
        if nxt not in st["asked"]:
            st["asked"].append(nxt)
        return QUESTIONS[st["report_type"]][nxt]
    st["stage"], st["pending"] = "confirming", None
    return _summary(st)


def _short_name(text: str) -> str:
    t = re.sub(r"^(i\s+(lost|found)\s+)?(my|a|an|the|some)\s+", "", text.strip(), flags=re.I)
    return t[:60] or "item"


# ---------------------------------------------------------------------------
# Formatting tool results
# ---------------------------------------------------------------------------
def _card_line(c: dict[str, Any]) -> str:
    what = " ".join(x for x in (c.get("color"), c.get("brand"), c.get("item_name")) if x)
    line = f"• {c['code']}: {what} · {c.get('location') or 'unknown place'} · {c['event_date']}"
    if c.get("report_type") == "found" and c.get("current_holding"):
        line += f" ({c['current_holding']})"
    return line + f" [{c['status']}]"


def format_candidates(out: dict[str, Any], report_type: str, code: str) -> str:
    if "error" in out:
        return f"I couldn't check for matches right now: {out['message']} Try 'matches for {code}' later."
    cands = out.get("candidates") or []
    if not cands:
        if report_type == "lost":
            return ("No potentially matching found reports yet. If someone reports a similar item you'll get a "
                    f"notification, or ask me 'matches for {code}' any time.")
        return ("No lost report matches this yet. If the owner reports it, the system will match it. "
                "Please keep the item safe or hand it to the admin office.")
    lines = [POTENTIAL_MATCH_SENTENCE]
    for c in cands:
        what = " ".join(x for x in (c.get("color"), c.get("brand"), c.get("item_name")) if x)
        band = "strong candidate" if c["band"] == "strong_candidate" else "possible candidate"
        lines.append(f"• {c['item_code']}: {what} at {c.get('location')} on {c['event_date']}. "
                     f"{band} (score {c['total_score']:.2f}): {c['explanation']}.")
    if report_type == "lost":
        lines.append("Next step: click 'Might be mine' on a candidate (or open the Matches tab) and answer the "
                     "verification question. An admin checks before anything is handed over.")
    else:
        lines.append("The owner will be asked to verify. Please keep the item safe or hand it to the admin office.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Intent handlers
# ---------------------------------------------------------------------------
def _start(report_type: str, text: str, st: dict[str, Any], today: date) -> str:
    last = st.get("last_code")
    st.clear()
    st.update(new_state(last))
    st["stage"], st["report_type"] = "collecting", report_type
    st["fields"]["description"] = mask_sensitive(text.strip())[:1000]
    _merge(st["fields"], extract(text, today), report_type)
    intro = ("Sorry to hear that. Let's report it." if report_type == "lost"
             else "Thanks for reporting a found item!")
    return f"{intro} {_ask_next_or_summarize(st)}"


def _collect(text: str, st: dict[str, Any], today: date) -> str | None:
    """Handle an answer while collecting. Returns None if we gave up (caller resets)."""
    f, rt, pending = st["fields"], st["report_type"], st["pending"]
    understood = True
    if pending == "item":
        item = match_item(text)
        if item:
            f["category"], f["item_name"] = item
        elif text.strip() and not is_skip(text):
            f["category"], f["item_name"] = "other", _short_name(text)
        else:
            understood = False
    elif pending == "location":
        loc = match_location(text)
        if loc:
            f["location"] = loc
        else:
            understood = False
    elif pending == "date":
        d = parse_date(text, today)
        if d:
            f["event_date"] = d.isoformat()
        else:
            understood = False
    elif pending == "detail":
        if not is_skip(text):
            f["description"] = (f["description"] + ". " + mask_sensitive(text.strip()))[:1000]
    elif pending == "holding":
        f["current_holding"] = parse_holding(text, answer=True)  # may stay None; that's allowed
    elif pending == "hint":
        if not is_skip(text):
            f["verification_hint"] = mask_sensitive(text.strip())[:300]

    if pending != "hint":  # the private hint must never leak into public fields
        _merge(f, extract(text, today), rt)

    if pending in REQUIRED and not understood:
        st["misses"] += 1
        if st["misses"] >= MAX_MISSES:
            return None
        tip = ""
        if pending == "location":
            tip = " Please name a campus place, e.g. 'Main Canteen' or 'CSE Block'."
        elif pending == "date":
            tip = " Try 'today', 'yesterday' or a date like '28 Sept'."
        return f"Sorry, I didn't catch that.{tip} {QUESTIONS[rt][pending]}"
    st["misses"] = 0
    return _ask_next_or_summarize(st)


def _save(st: dict[str, Any], run: RunTool) -> str:
    f, rt = st["fields"], st["report_type"]
    args: dict[str, Any] = {"category": f["category"], "item_name": f["item_name"],
                            "description": f["description"], "location": f["location"],
                            "color": f.get("color"), "brand": f.get("brand")}
    if rt == "lost":
        args.update(lost_date=f["event_date"], lost_time=f.get("event_time"))
    else:
        args.update(found_date=f["event_date"], found_time=f.get("event_time"),
                    current_holding=f.get("current_holding"), verification_hint=f.get("verification_hint"))
    args = {k: v for k, v in args.items() if v is not None}

    out = run(f"create_{rt}_report", args)
    if out.get("duplicate_of"):
        dup = out["duplicate_of"]
        st.clear()
        st.update(new_state(dup))
        return (f"It looks like you already reported this as {dup}, so I didn't create a second report. "
                f"Say 'matches for {dup}' to check for candidates.")
    if "error" in out:
        return f"Sorry, I couldn't save the report: {out['message']} Reply 'yes' to try again, or 'cancel'."

    code = out["code"]
    matches = run("find_matching_items", {"item_code": code})
    st.clear()
    st.update(new_state(code))
    return f"Saved! Your report code is {code}.\n\n" + format_candidates(matches, rt, code)


def _confirm(text: str, st: dict[str, Any], today: date, run: RunTool) -> str:
    if is_affirmative(text):
        return _save(st, run)
    changed = _merge(st["fields"], extract(text, today), st["report_type"], override=True)
    if changed:
        return "Updated.\n" + _summary(st)
    if is_negative(text):
        return ("No problem. What should I change? For example 'it was in the Main Canteen' or "
                "'it was yesterday'. Or say 'cancel'.")
    return "Please reply yes to save this report, tell me what to change, or say 'cancel'."


def _side(intent: str, text: str, st: dict[str, Any], today: date, run: RunTool) -> str:
    if intent == "search":
        ex = extract(text, today)
        rt = "lost" if re.search(r"\b(anyone|someone|somebody) lost\b|\blost (items|reports)\b", text.lower()) else "found"
        if not (ex.category or ex.color or ex.brand or ex.location):
            return "What are you looking for? For example: 'has anyone found a blue bottle?'"
        args: dict[str, Any] = {"report_type": rt}
        if ex.category:
            args["category"] = ex.category
        if ex.location:
            args["location"] = ex.location
        query = " ".join(x for x in (ex.color, ex.brand) if x)
        if query:
            args["query"] = query
        out = run("search_items", args)
        if "error" in out:
            return f"Sorry, I couldn't search right now: {out['message']}"
        if not out["results"]:
            return (f"No open {rt} reports match that yet. If it's yours, report it as lost and "
                    "I'll run matching and notify you when something similar is found.")
        lines = [f"I found {out['count']} open {rt} report(s):"]
        lines += [_card_line(c) for c in out["results"]]
        lines.append("These are search results, not confirmed matches. To get scored candidates, report your "
                     "lost item and I'll run matching.")
        return "\n".join(lines)

    if intent == "check_status":
        out = run("get_my_reports", {})
        if "error" in out:
            return f"Sorry, I couldn't load your reports: {out['message']}"
        if not out["reports"]:
            return "You haven't reported anything yet. " + HELP
        lines = ["Your reports:"]
        lines += [f"• {r['code']}: {r['report_type']} {r['item_name']} · {r['location']} · "
                  f"{r['event_date']} [{r['status']}]" for r in out["reports"]]
        lines.append("Say 'matches for <code>' to check candidates for one of them.")
        return "\n".join(lines)

    if intent == "find_matches":
        code = find_item_code(text) or st.get("last_code")
        if not code:
            return "Which report? Tell me its code, e.g. 'matches for L-0001'. Say 'my reports' to see your codes."
        out = run("find_matching_items", {"item_code": code})
        return format_candidates(out, "lost" if code.startswith("L") else "found", code)

    # item_details
    code = find_item_code(text) or ""
    out = run("get_item_details", {"item_code": code})
    if "error" in out:
        return f"Sorry: {out['message']}"
    return _card_line(out) + f"\nDetails: \"{out['description']}\""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def handle_message(message: str, state: dict[str, Any] | None, run_tool: RunTool, today: date,
                   first_name: str = "there") -> RuleTurn:
    st = copy.deepcopy(state) if state and state.get("stage") else new_state()
    calls: list[dict[str, Any]] = []

    def run(name: str, args: dict[str, Any]) -> dict[str, Any]:
        out = run_tool(name, args)
        calls.append({"name": name, "input": args, "output": out})
        return out

    text = message.strip()
    intent = detect_intent(text)
    stage = st["stage"]

    if intent == "cancel":
        if stage != "idle":
            st = new_state(st.get("last_code"))
            reply = "Okay, I've cancelled that report. Nothing was saved."
        else:
            reply = "There's nothing to cancel. " + HELP
    elif stage == "confirming":
        reply = _confirm(text, st, today, run)
    elif stage == "collecting":
        rt = st["report_type"]
        new_type = {"report_lost": "lost", "report_found": "found"}.get(intent)
        if intent in SIDE_INTENTS and st["pending"] not in ("detail", "hint"):
            reply = (_side(intent, text, st, today, run)
                     + f"\n\n(Your {rt} report isn't saved yet. {QUESTIONS[rt][st['pending']]})")
        elif new_type and new_type != rt:
            reply = f"Okay, starting a new {new_type} report instead. " + _start(new_type, text, st, today)
        else:
            collected = _collect(text, st, today)
            if collected is None:
                st = new_state(st.get("last_code"))
                reply = ("I'm having trouble understanding, so I've stopped this report (nothing was saved). "
                         "You can start again, or visit the admin office in the Admin Building for help.")
            else:
                reply = collected
    elif intent in ("report_lost", "report_found"):
        reply = _start("lost" if intent == "report_lost" else "found", text, st, today)
    elif intent in SIDE_INTENTS:
        reply = _side(intent, text, st, today, run)
    elif intent == "self_found":
        reply = ("Great news that you found it! Closing a report from chat isn't available yet, so please "
                 "tell the admin office and they will close it.")
    elif intent in ("greeting", "help"):
        reply = (f"Hi {first_name}! I'm CampusFind's assistant, running in rule-based mode "
                 "(no AI model is active, so I follow fixed rules).\n" + HELP)
    elif intent in ("yes", "no"):
        reply = "There's nothing waiting for a yes/no right now.\n" + HELP
    else:
        reply = "I can only help with campus lost-and-found. Did you lose or find something?\n" + HELP

    return RuleTurn(reply=reply, state=st, tool_calls=calls)
