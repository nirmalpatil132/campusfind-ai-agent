"""Database access: one psycopg connection pool to Supabase Postgres.

Rules:
  * Parameterised SQL only: pass values as %(name)s params, NEVER f-strings.
  * If DATABASE_URL is empty we raise DatabaseNotConfigured. We never pretend a save worked.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from app.config import settings

log = logging.getLogger(__name__)

SETUP_STEPS = [
    "Create a free project at https://supabase.com (region closest to India).",
    "Dashboard → SQL Editor → paste db/schema.sql → Run.",
    "Dashboard → Connect → copy the Session pooler connection string into DATABASE_URL in apps/api/.env.",
    "Dashboard → Settings → API Keys → put the Project URL in SUPABASE_URL and the publishable key "
    "in SUPABASE_PUBLISHABLE_KEY.",
    "Restart the API server.",
]


class DatabaseNotConfigured(RuntimeError):
    """DATABASE_URL is empty: database features are switched off."""

    def __init__(self) -> None:
        super().__init__("Database is not configured (DATABASE_URL is empty in apps/api/.env).")


class DatabaseUnavailable(RuntimeError):
    """DATABASE_URL is set but Postgres can't be reached."""


_pool: ConnectionPool | None = None


def is_configured() -> bool:
    return bool(settings.DATABASE_URL.strip())


def get_pool() -> ConnectionPool:
    global _pool
    if not is_configured():
        raise DatabaseNotConfigured()
    if _pool is None:
        pool = ConnectionPool(
            settings.DATABASE_URL,
            min_size=1,
            max_size=5,
            open=False,
            kwargs={
                "row_factory": dict_row,
                "prepare_threshold": None,  # required for Supabase's pgbouncer pooler
                "connect_timeout": 10,
            },
        )
        try:
            pool.open(wait=True, timeout=15)
        except Exception as exc:  # PoolTimeout / OperationalError
            pool.close()
            raise DatabaseUnavailable(f"Could not connect to the database: {exc}") from exc
        _pool = pool
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def transaction() -> Iterator[psycopg.Connection]:
    """`with transaction() as conn:` commits on success, rolls back on any exception."""
    pool = get_pool()
    try:
        with pool.connection() as conn:
            yield conn
    except psycopg.OperationalError as exc:
        raise DatabaseUnavailable(f"Database error: {exc}") from exc


def fetch_one(sql: str, params: dict[str, Any] | None = None,
              conn: psycopg.Connection | None = None) -> dict[str, Any] | None:
    if conn is not None:
        return conn.execute(sql, params or {}).fetchone()
    with transaction() as c:
        return c.execute(sql, params or {}).fetchone()


def fetch_all(sql: str, params: dict[str, Any] | None = None,
              conn: psycopg.Connection | None = None) -> list[dict[str, Any]]:
    if conn is not None:
        return conn.execute(sql, params or {}).fetchall()
    with transaction() as c:
        return c.execute(sql, params or {}).fetchall()


def execute(sql: str, params: dict[str, Any] | None = None,
            conn: psycopg.Connection | None = None) -> None:
    if conn is not None:
        conn.execute(sql, params or {})
        return
    with transaction() as c:
        c.execute(sql, params or {})


def jsonb(obj: Any) -> Jsonb:
    """Wrap a Python object for a jsonb column (dates/UUIDs become strings)."""
    return Jsonb(obj, dumps=lambda o: json.dumps(o, default=str))


def db_status() -> str:
    """'ok', 'not_configured' or 'unavailable: …' (for /api/health; never raises)."""
    if not is_configured():
        return "not_configured"
    try:
        fetch_one("select 1 as ok")
        return "ok"
    except Exception as exc:
        return f"unavailable: {type(exc).__name__}"
