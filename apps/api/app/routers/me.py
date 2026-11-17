"""Who am I + the public config the web page needs to log in."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.auth import CurrentUser, get_current_user
from app.campus import CATEGORIES, LOCATIONS
from app.config import settings

router = APIRouter(prefix="/api", tags=["me"])


@router.get("/me")
def me(user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"id": user.id, "email": user.email, "name": user.name, "role": user.role}


@router.get("/locations")
def locations() -> dict:
    """Campus places + categories for the report forms (public, static data)."""
    return {"locations": [{"name": n, "zone": z} for n, z in LOCATIONS.items()],
            "categories": list(CATEGORIES)}


@router.get("/public-config")
def public_config() -> dict:
    """Only PUBLIC values (safe in a browser). Never secret keys or the database URL."""
    return {"supabase_url": settings.SUPABASE_URL,
            "supabase_publishable_key": settings.SUPABASE_PUBLISHABLE_KEY,
            "college_name": settings.COLLEGE_NAME}
