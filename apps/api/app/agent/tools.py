"""The agent's tool registry, shared by BOTH engines (local LLM and rule-based fallback).

Golden rules (§10):
  * The logged-in user comes from ToolContext (set by the server from the verified JWT).
    It is never a tool parameter; unknown arguments such as "user_id" or "role" are ignored.
  * Tools call service functions; there is no SQL in this file.
  * create_* tools only run if the user's latest message is a plain "yes" (confirmation guard),
    so neither a small LLM nor a rule bug can save a report the user didn't approve.
  * Results are small JSON with no other user's identity and no verification hint.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.agent.extractor import match_item, parse_date, parse_time
from app.agent.intent import find_item_code, is_affirmative
from app.campus import CATEGORIES, LOCATIONS
from app.db import DatabaseNotConfigured, DatabaseUnavailable
from app.schemas.items import FoundReportIn, LostReportIn
from app.services import items, matching
from app.services.errors import Conflict, InvalidInput, ServiceError

log = logging.getLogger(__name__)


@dataclass
class ToolContext:
    user: Any                # app.auth.CurrentUser — injected by the server, never by the model
    session_id: str
    last_user_message: str   # used by the confirmation guard
    today: date


class NotConfirmed(Exception):
    pass


# ---------------------------------------------------------------------------
# Tool schemas (Ollama / OpenAI-style function format). This is ALL the model sees.
# ---------------------------------------------------------------------------
def _fn(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": required}}}


_CATEGORY = {"type": "string", "enum": list(CATEGORIES)}
_LOCATION = {"type": "string", "description": "One of: " + ", ".join(LOCATIONS)}
_REPORT_PROPS = {
    "category": _CATEGORY,
    "item_name": {"type": "string", "description": "Short name, e.g. 'laptop bag'"},
    "color": {"type": "string"},
    "brand": {"type": "string"},
    "description": {"type": "string", "description": "Distinguishing details in the user's words"},
    "location": _LOCATION,
}

TOOLS: list[dict[str, Any]] = [
    _fn("create_lost_report",
        "Start a report for an item the user LOST. Pass ONLY details the user said; leave unknown fields "
        "out (never guess location, date or time). The server asks for missing details and confirmation.",
        {**_REPORT_PROPS,
         "lost_date": {"type": "string", "description": "YYYY-MM-DD"},
         "lost_time": {"type": "string", "description": "HH:MM 24-hour, approximate is fine"}},
        ["item_name"]),
    _fn("create_found_report",
        "Start a report for an item the user FOUND. Pass ONLY details the user said; leave unknown fields "
        "out (never guess location, date or time). The server asks for missing details and confirmation.",
        {**_REPORT_PROPS,
         "found_date": {"type": "string", "description": "YYYY-MM-DD"},
         "found_time": {"type": "string", "description": "HH:MM 24-hour"},
         "current_holding": {"type": "string", "enum": ["finder", "admin_office", "security"]},
         "verification_hint": {"type": "string",
                               "description": "PRIVATE detail only the owner would know (admins only)"}},
        ["item_name"]),
    _fn("search_items",
        "Search open lost/found reports, e.g. when the user asks 'has anyone found a blue bottle?'.",
        {"query": {"type": "string", "description": "Keywords, e.g. 'blue bottle'"},
         "report_type": {"type": "string", "enum": ["lost", "found"]},
         "category": _CATEGORY, "location": _LOCATION,
         "date_from": {"type": "string", "description": "YYYY-MM-DD"},
         "date_to": {"type": "string", "description": "YYYY-MM-DD"}},
        []),
    _fn("find_matching_items",
        "Run the matching engine for one of the user's reports. ALWAYS call it right after creating a "
        "report. Returns ranked CANDIDATES with a score breakdown (never certain matches).",
        {"item_code": {"type": "string", "description": "e.g. L-0042"},
         "limit": {"type": "integer", "description": "max candidates, default 5"}},
        ["item_code"]),
    _fn("get_item_details", "Read one report by its code (e.g. L-0042 or F-0017).",
        {"item_code": {"type": "string"}}, ["item_code"]),
    _fn("get_my_reports", "List the current user's own reports.",
        {"status": {"type": "string", "enum": ["open", "matched", "verified", "returned", "cancelled", "closed"]}},
        []),
]


# ---------------------------------------------------------------------------
# Argument models: validate AND forgive sloppy small-model output.
# ---------------------------------------------------------------------------
class _Args(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)  # drops user_id/role/etc.

    @field_validator("*", mode="before")
    @classmethod
    def _blank_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and v.strip().lower() in {"", "null", "none", "unknown", "n/a", "na", "-"}:
            return None
        return v


class _ReportArgs(_Args):
    category: str | None = None
    item_name: str = Field(min_length=1, max_length=80)
    color: str | None = Field(default=None, max_length=40)
    brand: str | None = Field(default=None, max_length=40)
    description: str | None = Field(default=None, max_length=1000)
    location: str = Field(min_length=1, max_length=80)


class CreateLostArgs(_ReportArgs):
    lost_date: str
    lost_time: str | None = None


class CreateFoundArgs(_ReportArgs):
    found_date: str
    found_time: str | None = None
    current_holding: str | None = None
    verification_hint: str | None = Field(default=None, max_length=300)


class SearchArgs(_Args):
    query: str | None = Field(default=None, max_length=200)
    report_type: Literal["lost", "found"] | None = None
    category: str | None = None
    location: str | None = None
    date_from: str | None = None
    date_to: str | None = None


class ItemCodeArgs(_Args):
    item_code: str = Field(min_length=2, max_length=12)
    limit: int = Field(default=5, ge=1, le=10)


class MyReportsArgs(_Args):
    status: str | None = None


def _category(raw: str | None, *texts: str | None) -> str:
    c = (raw or "").strip().lower().replace(" ", "_").replace("jewelry", "jewellery")
    if c in CATEGORIES:
        return c
    guess = match_item(" ".join(t for t in texts if t))
    return guess[0] if guess else "other"


def _date(raw: str, today: date) -> date:
    try:
        return date.fromisoformat(raw.strip()[:10])
    except ValueError:
        parsed = parse_date(raw, today)  # "yesterday", "28 Sept"...
        if parsed is None:
            raise InvalidInput(f"Could not understand the date '{raw}'. Ask the user and use YYYY-MM-DD.") from None
        return parsed


def _time(raw: str | None) -> time | None:
    if not raw:
        return None
    try:
        return time.fromisoformat(raw.strip())
    except ValueError:
        return parse_time(raw)


def _holding(raw: str | None) -> str | None:
    if not raw:
        return None
    r = raw.lower()
    if "admin" in r or "office" in r:
        return "admin_office"
    if "security" in r or "guard" in r:
        return "security"
    if "finder" in r or "me" in r.split() or "with" in r:
        return "finder"
    return None


def _code(raw: str) -> str:
    return find_item_code(raw) or raw.strip().upper()


# ---------------------------------------------------------------------------
# Tool functions
# ---------------------------------------------------------------------------
def _require_confirmation(ctx: ToolContext) -> None:
    if not is_affirmative(ctx.last_user_message):
        raise NotConfirmed()


def create_lost_report(ctx: ToolContext, **raw: Any) -> dict[str, Any]:
    _require_confirmation(ctx)
    a = CreateLostArgs(**raw)
    data = LostReportIn(
        category=_category(a.category, a.item_name, a.description), item_name=a.item_name,
        color=a.color, brand=a.brand, description=a.description or a.item_name,
        location=a.location, event_date=_date(a.lost_date, ctx.today), event_time=_time(a.lost_time))
    try:
        return items.create_report(ctx.user, "lost", data)
    except Conflict as exc:
        return {"duplicate_of": exc.extra.get("duplicate_of"), "message": exc.message}


def create_found_report(ctx: ToolContext, **raw: Any) -> dict[str, Any]:
    _require_confirmation(ctx)
    a = CreateFoundArgs(**raw)
    data = FoundReportIn(
        category=_category(a.category, a.item_name, a.description), item_name=a.item_name,
        color=a.color, brand=a.brand, description=a.description or a.item_name,
        location=a.location, event_date=_date(a.found_date, ctx.today), event_time=_time(a.found_time),
        current_holding=_holding(a.current_holding), verification_hint=a.verification_hint)
    try:
        return items.create_report(ctx.user, "found", data)
    except Conflict as exc:
        return {"duplicate_of": exc.extra.get("duplicate_of"), "message": exc.message}


def search_items(ctx: ToolContext, **raw: Any) -> dict[str, Any]:
    a = SearchArgs(**raw)
    category = _category(a.category) if a.category else None
    results = items.search(
        ctx.user, query=a.query or "", report_type=a.report_type, category=category,
        location=a.location,
        date_from=_date(a.date_from, ctx.today) if a.date_from else None,
        date_to=_date(a.date_to, ctx.today) if a.date_to else None)
    return {"count": len(results), "results": results,
            "note": "Search results are not matches. Report the lost item to get scored candidates."}


def find_matching_items(ctx: ToolContext, **raw: Any) -> dict[str, Any]:
    a = ItemCodeArgs(**raw)
    return matching.find_matches(ctx.user, _code(a.item_code), a.limit)


def get_item_details(ctx: ToolContext, **raw: Any) -> dict[str, Any]:
    a = ItemCodeArgs(**raw)
    return items.get_by_code(ctx.user, _code(a.item_code))


def get_my_reports(ctx: ToolContext, **raw: Any) -> dict[str, Any]:
    a = MyReportsArgs(**raw)
    reports = items.list_mine(ctx.user, a.status)
    return {"count": len(reports), "reports": [
        {k: r[k] for k in ("code", "report_type", "item_name", "location", "event_date", "status")}
        for r in reports[:20]]}


TOOL_FUNCS: dict[str, Callable[..., dict[str, Any]]] = {
    "create_lost_report": create_lost_report,
    "create_found_report": create_found_report,
    "search_items": search_items,
    "find_matching_items": find_matching_items,
    "get_item_details": get_item_details,
    "get_my_reports": get_my_reports,
}


def execute_tool(name: str, args: Any, ctx: ToolContext) -> tuple[dict[str, Any], str | None]:
    """Run one tool safely. Returns (output, error_message_or_None). Never raises."""
    func = TOOL_FUNCS.get(name)
    if func is None:
        out: dict[str, Any] = {"error": "unknown_tool", "message": f"There is no tool called '{name}'."}
        return out, out["message"]
    if not isinstance(args, dict):
        args = {}
    try:
        out = func(ctx, **args)
    except NotConfirmed:
        out = {"error": "not_confirmed",
               "message": "The user has not confirmed yet. Show a one-line summary of the report and ask "
                          "them to reply 'yes'. Call this tool only after they say yes."}
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:4])
        out = {"error": "invalid_input", "message": problems}
    except ServiceError as exc:
        out = {"error": type(exc).__name__.lower(), "message": exc.message}
    except DatabaseNotConfigured:
        out = {"error": "database_not_configured",
               "message": "The database is not set up yet, so reports can't be saved or searched. "
                          "Ask the project admin to configure Supabase (see README)."}
    except DatabaseUnavailable:
        out = {"error": "database_unavailable",
               "message": "The database can't be reached right now. Please try again in a minute."}
    except Exception:  # noqa: BLE001 — never crash the agent loop
        log.exception("Tool %s crashed", name)
        out = {"error": "internal_error", "message": "Something went wrong while running this tool."}
    error = out.get("message", out["error"]) if "error" in out else None
    return out, error
