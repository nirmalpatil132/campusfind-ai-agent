"""Supabase authentication (PROJECT_GUIDE.md §16).

The browser (or terminal runner) logs in with Supabase Auth and sends the access token as
`Authorization: Bearer <token>`. We verify the signature with the project's public JWKS keys,
then load the user's role from public.users. The role is NEVER taken from the request body
or from the LLM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import jwt
from fastapi import Depends, Header, HTTPException
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError

from app import db
from app.config import settings


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str
    name: str
    role: str  # student | staff | admin (from the database)

    @property
    def first_name(self) -> str:
        return (self.name or "there").split()[0]

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class AuthError(Exception):
    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


_jwks_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwks_client
    if not settings.SUPABASE_URL:
        raise AuthError("SUPABASE_URL is not set in apps/api/.env, so logins can't be verified.", 503)
    if _jwks_client is None:
        url = settings.SUPABASE_URL.rstrip("/") + "/auth/v1/.well-known/jwks.json"
        _jwks_client = PyJWKClient(url, cache_keys=True, lifespan=3600)
    return _jwks_client


def verify_token(token: str) -> dict[str, Any]:
    """Return the verified JWT claims or raise AuthError."""
    try:
        key = _jwks().get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["ES256", "RS256"], audience="authenticated")
    except AuthError:
        raise
    except PyJWKClientConnectionError as exc:
        raise AuthError(f"Could not reach Supabase to fetch signing keys: {exc}", 503) from exc
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("Your login has expired. Please log in again.") from exc
    except Exception as exc:  # bad signature, wrong audience, legacy HS256 project, garbage...
        raise AuthError(
            "Invalid token. (If your Supabase project still uses the legacy HS256 JWT secret, "
            "switch to JWT Signing Keys in Project Settings → JWT Keys.)") from exc


def load_user_profile(user_id: str) -> CurrentUser | None:
    row = db.fetch_one("select id, email, name, role from public.users where id = %(id)s", {"id": user_id})
    if not row:
        return None
    return CurrentUser(id=str(row["id"]), email=row["email"], name=row["name"], role=row["role"])


def user_from_token(token: str) -> CurrentUser:
    claims = verify_token(token)
    user = load_user_profile(claims["sub"])
    if not user:
        raise AuthError("Profile not found. Re-run db/schema.sql (it backfills public.users).")
    return user


def get_current_user(authorization: str | None = Header(default=None)) -> CurrentUser:
    """FastAPI dependency: every protected route uses this."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token. Log in first.")
    try:
        return user_from_token(authorization.removeprefix("Bearer ").strip())
    except AuthError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    if not user.is_admin:
        raise HTTPException(403, "Admins only.")
    return user
