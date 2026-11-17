"""In-app notifications."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from app.auth import CurrentUser, get_current_user
from app.services import notifications

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("")
def list_mine(user: CurrentUser = Depends(get_current_user)) -> dict:
    items = notifications.list_for(user)
    return {"unread": sum(1 for n in items if n["status"] == "unread"), "notifications": items}


@router.post("/{notification_id}/read")
def mark_read(notification_id: UUID, user: CurrentUser = Depends(get_current_user)) -> dict:
    notifications.mark_read(user, str(notification_id))
    return {"ok": True}
