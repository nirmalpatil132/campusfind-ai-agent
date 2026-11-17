"""Owner side of the verification workflow."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

from app.auth import CurrentUser, get_current_user
from app.schemas.api import ConfirmRequest, MatchSearchRequest
from app.services import matches, matching

router = APIRouter(prefix="/api/matches", tags=["matches"])


@router.get("/mine")
def mine(user: CurrentUser = Depends(get_current_user)) -> dict:
    """Candidates for the caller's LOST items (found-report public fields only, never the hint)."""
    return {"matches": matches.list_for_owner(user)}


@router.post("/search")
def search(body: MatchSearchRequest, user: CurrentUser = Depends(get_current_user)) -> dict:
    return matching.find_matches(user, body.item_code, body.limit)


@router.post("/{match_id}/confirm")
def confirm(match_id: UUID, body: ConfirmRequest, user: CurrentUser = Depends(get_current_user)) -> dict:
    return matches.confirm(user, str(match_id), body.answer)


@router.post("/{match_id}/decline")
def decline(match_id: UUID, user: CurrentUser = Depends(get_current_user)) -> dict:
    return matches.decline(user, str(match_id))
