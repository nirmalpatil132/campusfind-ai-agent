"""Human verification workflow (§9.2):

    match:  candidate → owner_confirmed → verified → closed      (or rejected)
    items:  open → matched → verified → returned

Owners confirm/decline their own candidates. ONLY admins verify, reject and mark returned.
None of these are agent tools, so an LLM mistake can never close a case.
"""

from __future__ import annotations

from typing import Any

import psycopg

from app import db
from app.campus import HOLDING_LABELS
from app.services import notifications
from app.services.errors import Conflict, Forbidden, InvalidInput, NotFound
from app.services.items import _FROM, _PUBLIC_COLUMNS, public_card
from app.services.matching import explain

_ACTIVE = ("candidate", "owner_confirmed", "verified")

_MATCH_SQL = """
select m.id, m.status, m.similarity_score, m.score_breakdown, m.owner_answer, m.reject_reason,
       m.created_at, m.lost_item_id, m.found_item_id,
       l.code as lost_code, l.item_name as lost_item, l.category as lost_category,
       l.color as lost_color, l.brand as lost_brand, l.description as lost_description,
       l.event_date as lost_date, l.created_by as owner_id, ll.name as lost_location,
       f.code as found_code, f.item_name as found_item, f.category as found_category,
       f.color as found_color, f.brand as found_brand, f.description as found_description,
       f.event_date as found_date, f.created_by as finder_id, fl.name as found_location,
       fr.current_holding, fr.verification_hint
from public.matches m
join public.items l on l.id = m.lost_item_id
join public.items f on f.id = m.found_item_id
left join public.locations ll on ll.id = l.location_id
left join public.locations fl on fl.id = f.location_id
left join public.found_reports fr on fr.item_id = f.id
"""


def _get(match_id: str, conn: psycopg.Connection) -> dict[str, Any]:
    row = db.fetch_one(_MATCH_SQL + " where m.id = %(id)s for update of m", {"id": match_id}, conn=conn)
    if not row:
        raise NotFound("Match not found.")
    return row


def _owner_view(row: dict[str, Any]) -> dict[str, Any]:
    """What the lost-item owner may see: the found report's public fields. No hint, no finder id."""
    breakdown = {k: v for k, v in (row["score_breakdown"] or {}).items() if isinstance(v, (int, float))
                 and not isinstance(v, bool)}
    return {
        "match_id": str(row["id"]),
        "status": row["status"],
        "total_score": float(row["similarity_score"]),
        "explanation": explain(breakdown, bool((row["score_breakdown"] or {}).get("unique_mark_bonus"))),
        "lost_code": row["lost_code"],
        "found": {
            "code": row["found_code"], "item_name": row["found_item"], "category": row["found_category"],
            "color": row["found_color"], "brand": row["found_brand"],
            "description": row["found_description"], "location": row["found_location"],
            "event_date": row["found_date"].isoformat(),
            "current_holding": HOLDING_LABELS.get(row["current_holding"] or "", None),
        },
        "created_at": row["created_at"].isoformat(),
    }


def _admin_view(row: dict[str, Any]) -> dict[str, Any]:
    view = _owner_view(row)
    view["lost"] = {
        "code": row["lost_code"], "item_name": row["lost_item"], "category": row["lost_category"],
        "color": row["lost_color"], "brand": row["lost_brand"], "description": row["lost_description"],
        "location": row["lost_location"], "event_date": row["lost_date"].isoformat(),
    }
    view["owner_answer"] = row["owner_answer"]
    view["verification_hint"] = row["verification_hint"]  # PRIVATE: admin view only
    view["reject_reason"] = row["reject_reason"]
    view["breakdown"] = row["score_breakdown"]
    return view


def _reopen_if_idle(item_ids: list[Any], conn: psycopg.Connection) -> None:
    """Put items back to 'open' when they no longer have any active match."""
    db.execute(
        """update public.items i set status = 'open'
           where i.id = any(%(ids)s) and i.status in ('matched', 'verified')
             and not exists (select 1 from public.matches m
                             where (m.lost_item_id = i.id or m.found_item_id = i.id)
                               and m.status = any(%(active)s))""",
        {"ids": item_ids, "active": list(_ACTIVE)}, conn=conn,
    )


def _log_admin(admin: Any, action: str, conn: psycopg.Connection, item_id: Any = None,
               match_id: Any = None, note: str | None = None) -> None:
    db.execute(
        "insert into public.admin_actions (admin_id, action, item_id, match_id, note) "
        "values (%(a)s, %(act)s, %(i)s, %(m)s, %(n)s)",
        {"a": admin.id, "act": action, "i": item_id, "m": match_id, "n": note}, conn=conn,
    )


# ---------------------------------------------------------------------------
# Owner actions
# ---------------------------------------------------------------------------
def list_for_owner(user: Any) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        _MATCH_SQL + " where l.created_by = %(u)s and m.status <> 'rejected' "
                     "order by m.created_at desc", {"u": user.id})
    return [_owner_view(r) for r in rows]


def confirm(user: Any, match_id: str, answer: str) -> dict[str, Any]:
    """Owner says 'might be mine' and answers the verification question."""
    answer = (answer or "").strip()
    if len(answer) < 3:
        raise InvalidInput("Please describe a detail only the owner would know.")
    with db.transaction() as conn:
        row = _get(match_id, conn)
        if str(row["owner_id"]) != str(user.id):
            raise Forbidden("Only the owner of the lost report can confirm this match.")
        if row["status"] != "candidate":
            raise Conflict(f"This match is already '{row['status']}'.")
        db.execute("update public.matches set status = 'owner_confirmed', owner_answer = %(a)s "
                   "where id = %(id)s", {"a": answer[:500], "id": match_id}, conn=conn)
        notifications.notify_admins(
            "match_to_verify",
            f"Owner of {row['lost_code']} believes {row['found_code']} is theirs. Please verify.",
            item_id=row["lost_item_id"], conn=conn)
    return {"match_id": match_id, "status": "owner_confirmed",
            "message": "Thanks! An admin will compare your answer with the finder's details."}


def decline(user: Any, match_id: str) -> dict[str, Any]:
    with db.transaction() as conn:
        row = _get(match_id, conn)
        if str(row["owner_id"]) != str(user.id):
            raise Forbidden("Only the owner of the lost report can decline this match.")
        if row["status"] not in ("candidate", "owner_confirmed"):
            raise Conflict(f"This match is already '{row['status']}'.")
        db.execute("update public.matches set status = 'rejected', reject_reason = 'Owner: not mine' "
                   "where id = %(id)s", {"id": match_id}, conn=conn)
        _reopen_if_idle([row["lost_item_id"], row["found_item_id"]], conn)
    return {"match_id": match_id, "status": "rejected"}


# ---------------------------------------------------------------------------
# Admin actions (routers guard these with require_admin)
# ---------------------------------------------------------------------------
def admin_queue(status: str = "owner_confirmed") -> list[dict[str, Any]]:
    if status not in ("candidate", "owner_confirmed", "verified", "rejected", "closed"):
        raise InvalidInput("Unknown match status.")
    rows = db.fetch_all(_MATCH_SQL + " where m.status = %(s)s order by m.created_at", {"s": status})
    return [_admin_view(r) for r in rows]


def verify(admin: Any, match_id: str, note: str | None = None) -> dict[str, Any]:
    with db.transaction() as conn:
        row = _get(match_id, conn)
        if row["status"] != "owner_confirmed":
            raise Conflict("Only owner-confirmed matches can be verified.")
        db.execute("update public.matches set status = 'verified' where id = %(id)s", {"id": match_id}, conn=conn)
        db.execute("update public.items set status = 'verified' where id = any(%(ids)s)",
                   {"ids": [row["lost_item_id"], row["found_item_id"]]}, conn=conn)
        _log_admin(admin, "verify_match", conn, item_id=row["lost_item_id"], match_id=match_id, note=note)
        where = HOLDING_LABELS.get(row["current_holding"] or "", "at the admin office")
        notifications.create(
            row["owner_id"], "match_verified",
            f"Good news: an admin verified {row['found_code']} for your {row['lost_code']}. "
            f"The item is {where}. Please collect it with your college ID.",
            item_id=row["lost_item_id"], conn=conn)
    return {"match_id": match_id, "status": "verified"}


def reject(admin: Any, match_id: str, reason: str) -> dict[str, Any]:
    reason = (reason or "").strip()
    if not reason:
        raise InvalidInput("A reason is required to reject a match.")
    with db.transaction() as conn:
        row = _get(match_id, conn)
        if row["status"] not in _ACTIVE:
            raise Conflict(f"This match is already '{row['status']}'.")
        db.execute("update public.matches set status = 'rejected', reject_reason = %(r)s where id = %(id)s",
                   {"r": reason[:300], "id": match_id}, conn=conn)
        _reopen_if_idle([row["lost_item_id"], row["found_item_id"]], conn)
        _log_admin(admin, "reject_match", conn, item_id=row["lost_item_id"], match_id=match_id, note=reason)
        notifications.create(
            row["owner_id"], "match_rejected",
            f"{row['found_code']} was checked by an admin and is not a match for {row['lost_code']}. "
            "Your report stays open.", item_id=row["lost_item_id"], conn=conn)
    return {"match_id": match_id, "status": "rejected"}


def mark_returned(admin: Any, item_code: str, note: str | None = None) -> dict[str, Any]:
    """Close the case: needs a VERIFIED match. Other candidates for both items are auto-rejected."""
    with db.transaction() as conn:
        item = db.fetch_one("select id, code from public.items where code = %(c)s",
                            {"c": item_code.strip().upper()}, conn=conn)
        if not item:
            raise NotFound(f"No report with code {item_code}.")
        m = db.fetch_one(
            "select id from public.matches where status = 'verified' "
            "and (lost_item_id = %(i)s or found_item_id = %(i)s) limit 1", {"i": item["id"]}, conn=conn)
        if not m:
            raise Conflict("This item has no verified match. Verify a match before marking it returned.")
        row = _get(str(m["id"]), conn)
        both = [row["lost_item_id"], row["found_item_id"]]
        db.execute("update public.matches set status = 'closed' where id = %(id)s", {"id": row["id"]}, conn=conn)
        db.execute("update public.items set status = 'returned' where id = any(%(ids)s)", {"ids": both}, conn=conn)
        others = db.fetch_all(
            """update public.matches set status = 'rejected', reject_reason = 'Auto-rejected: item returned'
               where id <> %(id)s and status = any(%(active)s)
                 and (lost_item_id = any(%(ids)s) or found_item_id = any(%(ids)s))
               returning lost_item_id, found_item_id""",
            {"id": row["id"], "active": list(_ACTIVE), "ids": both}, conn=conn)
        affected = {x for r in others for x in (r["lost_item_id"], r["found_item_id"])} - set(both)
        if affected:
            _reopen_if_idle(list(affected), conn)
        _log_admin(admin, "mark_returned", conn, item_id=item["id"], match_id=row["id"], note=note)
        notifications.create(row["owner_id"], "item_returned",
                             f"Case closed: {row['lost_code']} has been returned to you.",
                             item_id=row["lost_item_id"], conn=conn)
        notifications.create(row["finder_id"], "item_returned",
                             f"Thank you! The item you found ({row['found_code']}) was returned to its owner.",
                             item_id=row["found_item_id"], conn=conn)
    return {"lost_code": row["lost_code"], "found_code": row["found_code"], "status": "returned",
            "auto_rejected": len(others)}


def admin_stats() -> dict[str, Any]:
    by_status = db.fetch_all(
        "select report_type, status, count(*)::int as n from public.items group by report_type, status")
    matches_by = db.fetch_all("select status, count(*)::int as n from public.matches group by status")
    top = db.fetch_all(
        """select lo.name as location, count(*)::int as n from public.items i
           join public.locations lo on lo.id = i.location_id
           group by lo.name order by n desc limit 5""")
    items_summary: dict[str, dict[str, int]] = {"lost": {}, "found": {}}
    for r in by_status:
        items_summary[r["report_type"]][r["status"]] = r["n"]
    return {
        "items": items_summary,
        "total_lost": sum(items_summary["lost"].values()),
        "total_found": sum(items_summary["found"].values()),
        "returned": items_summary["lost"].get("returned", 0),
        "matches": {r["status"]: r["n"] for r in matches_by},
        "top_locations": top,
    }


def admin_items(status: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {}
    where = "true"
    if status:
        where = "i.status = %(s)s"
        params["s"] = status
    rows = db.fetch_all(f"select {_PUBLIC_COLUMNS} {_FROM} where {where} order by i.created_at desc limit 200",
                        params)
    return [public_card(r) for r in rows]
