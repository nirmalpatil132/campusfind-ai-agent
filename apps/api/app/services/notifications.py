"""In-app notifications."""

from __future__ import annotations

from typing import Any

import psycopg

from app import db
from app.services.errors import NotFound


def create(user_id: Any, type_: str, message: str, item_id: Any = None,
           conn: psycopg.Connection | None = None) -> None:
    db.execute(
        "insert into public.notifications (user_id, type, message, item_id) "
        "values (%(u)s, %(t)s, %(m)s, %(i)s)",
        {"u": user_id, "t": type_, "m": message[:500], "i": item_id}, conn=conn,
    )


def notify_admins(type_: str, message: str, item_id: Any = None,
                  conn: psycopg.Connection | None = None) -> None:
    db.execute(
        "insert into public.notifications (user_id, type, message, item_id) "
        "select id, %(t)s, %(m)s, %(i)s from public.users where role = 'admin'",
        {"t": type_, "m": message[:500], "i": item_id}, conn=conn,
    )


def list_for(user: Any, limit: int = 30) -> list[dict[str, Any]]:
    rows = db.fetch_all(
        """select n.id, n.type, n.message, n.status, n.created_at, i.code as item_code
           from public.notifications n left join public.items i on i.id = n.item_id
           where n.user_id = %(u)s order by n.created_at desc limit %(lim)s""",
        {"u": user.id, "lim": limit},
    )
    return [{**r, "id": str(r["id"]), "created_at": r["created_at"].isoformat()} for r in rows]


def mark_read(user: Any, notification_id: str) -> None:
    row = db.fetch_one(
        "update public.notifications set status = 'read' "
        "where id = %(id)s and user_id = %(u)s returning id",
        {"id": notification_id, "u": user.id},
    )
    if not row:
        raise NotFound("Notification not found.")
