"""Item reports: create, read, list, search. Shared by agent tools AND form endpoints."""

from __future__ import annotations

import re
from typing import Any

from app import db
from app.agent.extractor import mask_sensitive, match_location
from app.campus import HOLDING_LABELS, LOCATIONS
from app.schemas.items import FoundReportIn, LostReportIn
from app.services import embeddings
from app.services.errors import Conflict, InvalidInput, NotFound
from app.timeutil import today_ist

_PUBLIC_COLUMNS = """
    i.id, i.code, i.report_type, i.category, i.item_name, i.brand, i.color, i.description,
    lo.name as location, i.event_date, i.event_time, i.status, i.created_at, i.created_by,
    fr.current_holding
"""
_FROM = """
    from public.items i
    left join public.locations lo on lo.id = i.location_id
    left join public.found_reports fr on fr.item_id = i.id
"""

_DUPLICATE_SQL = """
select code from public.items
where created_by = %(uid)s and report_type = %(rt)s and category = %(cat)s
  and status in ('open', 'matched')
  and abs(event_date - %(d)s::date) <= 3
  and ((%(emb)s::text is not null and embedding is not null
        and 1 - (embedding <=> %(emb)s::vector) >= 0.85)
    or (location_id = %(loc)s and lower(item_name) = lower(%(name)s)))
order by created_at desc
limit 1
"""

_INSERT_ITEM_SQL = """
insert into public.items (code, report_type, category, item_name, brand, color, description,
                          location_id, event_date, event_time, embedding, created_by)
values (%(code)s, %(rt)s, %(cat)s, %(name)s, %(brand)s, %(color)s, %(desc)s,
        %(loc)s, %(d)s, %(t)s, %(emb)s::vector, %(uid)s)
returning id, code, status
"""

_STOP_WORDS = {"a", "an", "the", "my", "any", "anyone", "has", "have", "found", "lost", "is", "was",
               "of", "in", "at", "with", "and", "or", "for", "near", "someone", "please", "i", "me"}


def resolve_location(text: str) -> str:
    """Exact or alias match to a canonical campus location, else InvalidInput."""
    for name in LOCATIONS:
        if name.lower() == text.strip().lower():
            return name
    found = match_location(text)
    if found:
        return found
    raise InvalidInput(f"Unknown campus location '{text}'. Known locations: {', '.join(LOCATIONS)}.")


def public_card(row: dict[str, Any]) -> dict[str, Any]:
    """Fields anyone may see. NEVER includes created_by, names, emails or the verification hint."""
    return {
        "code": row["code"],
        "report_type": row["report_type"],
        "category": row["category"],
        "item_name": row["item_name"],
        "brand": row.get("brand"),
        "color": row.get("color"),
        "description": row["description"],
        "location": row.get("location"),
        "event_date": row["event_date"].isoformat(),
        "event_time": row["event_time"].strftime("%H:%M") if row.get("event_time") else None,
        "status": row["status"],
        "current_holding": HOLDING_LABELS.get(row.get("current_holding") or "", None),
    }


def create_report(user: Any, report_type: str, data: LostReportIn | FoundReportIn) -> dict[str, Any]:
    """Save a lost/found report. Raises Conflict(duplicate_of=...) for a probable duplicate."""
    location = resolve_location(data.location)
    if data.event_date > today_ist():
        raise InvalidInput("The date can't be in the future.")
    description = mask_sensitive(data.description)
    vector = embeddings.embed_item(data.category, data.item_name, data.color, data.brand, description)
    emb = embeddings.to_pgvector(vector)

    with db.transaction() as conn:
        loc = db.fetch_one("select id from public.locations where name = %(n)s", {"n": location}, conn=conn)
        if not loc:
            raise InvalidInput(f"Location '{location}' is missing from the database. Re-run db/schema.sql.")
        dup = db.fetch_one(_DUPLICATE_SQL, {
            "uid": user.id, "rt": report_type, "cat": data.category, "d": data.event_date,
            "emb": emb, "loc": loc["id"], "name": data.item_name,
        }, conn=conn)
        if dup:
            raise Conflict(f"You already have a similar open report: {dup['code']}.", duplicate_of=dup["code"])

        # Two fixed statements (sequence names can't be query parameters).
        if report_type == "lost":
            n = db.fetch_one("select nextval('public.lost_code_seq') as n", conn=conn)["n"]
            code = f"L-{n:04d}"
        else:
            n = db.fetch_one("select nextval('public.found_code_seq') as n", conn=conn)["n"]
            code = f"F-{n:04d}"

        row = db.fetch_one(_INSERT_ITEM_SQL, {
            "code": code, "rt": report_type, "cat": data.category, "name": data.item_name,
            "brand": data.brand, "color": data.color, "desc": description, "loc": loc["id"],
            "d": data.event_date, "t": data.event_time, "emb": emb, "uid": user.id,
        }, conn=conn)
        if report_type == "lost":
            db.execute("insert into public.lost_reports (item_id, last_seen_location, last_seen_time) "
                       "values (%(id)s, %(loc)s, %(t)s)",
                       {"id": row["id"], "loc": location, "t": data.event_time}, conn=conn)
        else:
            assert isinstance(data, FoundReportIn)
            db.execute("insert into public.found_reports "
                       "(item_id, found_location, found_time, current_holding, verification_hint) "
                       "values (%(id)s, %(loc)s, %(t)s, %(h)s, %(hint)s)",
                       {"id": row["id"], "loc": location, "t": data.event_time,
                        "h": data.current_holding,
                        "hint": mask_sensitive(data.verification_hint) if data.verification_hint else None},
                       conn=conn)

    return {"item_id": str(row["id"]), "code": row["code"], "status": row["status"],
            "embedding_saved": vector is not None}


def get_by_code(user: Any, code: str) -> dict[str, Any]:
    row = db.fetch_one(
        f"select {_PUBLIC_COLUMNS}, fr.verification_hint {_FROM} where i.code = %(code)s",
        {"code": code.strip().upper()},
    )
    if not row:
        raise NotFound(f"No report with code {code}.")
    card = public_card(row)
    is_owner = str(row["created_by"]) == str(user.id)
    card["is_mine"] = is_owner
    if is_owner or user.role == "admin":  # private fields: owner/admin only
        card["verification_hint"] = row.get("verification_hint")
    return card


def list_mine(user: Any, status: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"uid": user.id}
    where = "i.created_by = %(uid)s"
    if status:
        where += " and i.status = %(status)s"
        params["status"] = status
    rows = db.fetch_all(f"select {_PUBLIC_COLUMNS} {_FROM} where {where} order by i.created_at desc", params)
    return [public_card(r) for r in rows]


def search(user: Any, query: str = "", report_type: str | None = None, category: str | None = None,
           location: str | None = None, date_from: Any = None, date_to: Any = None,
           limit: int = 10) -> list[dict[str, Any]]:
    """Search unresolved reports. Only fixed SQL fragments are joined; every value is a parameter."""
    clauses = ["i.status in ('open', 'matched')"]
    params: dict[str, Any] = {"lim": max(1, min(limit, 10))}
    if report_type:
        clauses.append("i.report_type = %(rt)s")
        params["rt"] = report_type
    if category:
        clauses.append("i.category = %(cat)s")
        params["cat"] = category
    if location:
        clauses.append("lo.name = %(loc)s")
        params["loc"] = resolve_location(location)
    if date_from:
        clauses.append("i.event_date >= %(df)s")
        params["df"] = date_from
    if date_to:
        clauses.append("i.event_date <= %(dt)s")
        params["dt"] = date_to
    tokens = [w for w in re.findall(r"[a-z0-9&]+", (query or "").lower())
              if w not in _STOP_WORDS and len(w) > 1][:5]
    for n, token in enumerate(tokens):
        key = f"t{n}"  # placeholder NAME only; the value goes in params
        clauses.append(
            f"(i.item_name ilike %({key})s or i.description ilike %({key})s or i.category ilike %({key})s"
            f" or coalesce(i.brand, '') ilike %({key})s or coalesce(i.color, '') ilike %({key})s)")
        params[key] = f"%{token}%"
    rows = db.fetch_all(
        f"select {_PUBLIC_COLUMNS} {_FROM} where {' and '.join(clauses)} "
        "order by i.event_date desc limit %(lim)s", params)
    return [public_card(r) for r in rows]
