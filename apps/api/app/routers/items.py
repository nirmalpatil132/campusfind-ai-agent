"""Form path for reports. Uses the SAME services as the agent, so forms work even with no LLM."""

from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, Query

from app.ratelimit import limit
from app.auth import CurrentUser, get_current_user
from app.schemas.items import Category, FoundReportIn, LostReportIn
from app.services import items, matching

router = APIRouter(prefix="/api/items", tags=["items"])


@router.post("/lost", status_code=201, dependencies=[Depends(limit("reports"))])
def create_lost(body: LostReportIn, user: CurrentUser = Depends(get_current_user)) -> dict:
    created = items.create_report(user, "lost", body)  # 409 {duplicate_of} on a duplicate
    return {**created, "matching": matching.find_matches(user, created["code"])}


@router.post("/found", status_code=201, dependencies=[Depends(limit("reports"))])
def create_found(body: FoundReportIn, user: CurrentUser = Depends(get_current_user)) -> dict:
    created = items.create_report(user, "found", body)
    return {**created, "matching": matching.find_matches(user, created["code"])}


@router.get("/search", dependencies=[Depends(limit("search"))])
def search(q: str = Query("", max_length=200), type: Literal["lost", "found"] | None = None,
           category: Category | None = None, location: str | None = Query(None, max_length=80),
           from_: date | None = Query(None, alias="from"), to: date | None = None,
           user: CurrentUser = Depends(get_current_user)) -> dict:
    results = items.search(user, q, type, category, location, from_, to)
    return {"count": len(results), "results": results}


@router.get("/mine")
def mine(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"reports": items.list_mine(user)}


@router.get("/{code}")
def detail(code: str, user: CurrentUser = Depends(get_current_user)) -> dict:
    return items.get_by_code(user, code)
