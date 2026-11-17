"""Admin-only routes. Every route depends on require_admin (role comes from the database)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends

from app.ratelimit import limit
from app.auth import CurrentUser, require_admin
from app.schemas.api import AdminNote, RejectRequest
from app.services import matches

router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])

MatchStatus = Literal["candidate", "owner_confirmed", "verified", "rejected", "closed"]


@router.get("/matches")
def queue(status: MatchStatus = "owner_confirmed") -> dict:
    """Matches with BOTH reports side by side, the owner's answer and the private hint."""
    return {"matches": matches.admin_queue(status)}


@router.post("/matches/{match_id}/verify", dependencies=[Depends(limit("admin"))])
def verify(match_id: UUID, body: AdminNote | None = None,
           admin: CurrentUser = Depends(require_admin)) -> dict:
    return matches.verify(admin, str(match_id), body.note if body else None)


@router.post("/matches/{match_id}/reject", dependencies=[Depends(limit("admin"))])
def reject(match_id: UUID, body: RejectRequest, admin: CurrentUser = Depends(require_admin)) -> dict:
    return matches.reject(admin, str(match_id), body.reason)


@router.post("/items/{code}/returned", dependencies=[Depends(limit("admin"))])
def returned(code: str, body: AdminNote | None = None,
             admin: CurrentUser = Depends(require_admin)) -> dict:
    return matches.mark_returned(admin, code, body.note if body else None)


@router.get("/stats")
def stats() -> dict:
    return matches.admin_stats()


@router.get("/items")
def all_items(status: str | None = None) -> dict:
    return {"items": matches.admin_items(status)}
