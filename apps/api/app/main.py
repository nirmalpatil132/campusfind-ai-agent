"""FastAPI app: routes, CORS, error mapping, health check, and the web page at /.

Run from apps/api:   uvicorn app.main:app --reload
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.exceptions import HTTPException

from app import db, ratelimit
from app.agent.llm import cached_status
from app.agent.loop import provider
from app.config import settings
from app.routers import admin, agent, items, matches, me, notifications
from app.services.errors import ServiceError

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("campusfind")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    db.close_pool()  # on shutdown


app = FastAPI(title="CampusFind AI", version="0.2.0", lifespan=lifespan,
              description="Campus Lost & Found agent: local LLM (Ollama) or rule-based fallback.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH"],
    allow_headers=["Authorization", "Content-Type"],
)



# ---- request protection: runs before any route ----
def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:  # behind Render's proxy the first entry is the visitor
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def protect(request: Request, call_next):  # noqa: ANN001, ANN201
    path = request.url.path
    if path.startswith("/api/") and path != "/api/health":
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.MAX_REQUEST_BYTES:
            return JSONResponse(status_code=413, content={
                "detail": "This request is too large.", "code": "request_too_large"})
        if settings.RATE_LIMIT_ENABLED:  # flood guard per IP (per-user limits are on the routes)
            retry = ratelimit.limiter.hit("ip:" + _client_ip(request), settings.RATE_LIMIT_GLOBAL_IP)
            if retry is not None:
                return JSONResponse(status_code=429, headers={"Retry-After": str(int(retry) + 1)},
                                    content={"detail": ratelimit.TOO_MANY, "code": "rate_limited"})
    response: Response = await call_next(request)
    return response


for r in (agent.router, items.router, matches.router, admin.router, notifications.router, me.router):
    app.include_router(r)


# ---- error mapping: services raise plain Python errors, we turn them into HTTP codes ----
@app.exception_handler(ServiceError)
def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message, **exc.extra})


@app.exception_handler(db.DatabaseNotConfigured)
def _db_not_configured(_: Request, exc: db.DatabaseNotConfigured) -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": str(exc), "setup": db.SETUP_STEPS})


@app.exception_handler(db.DatabaseUnavailable)
def _db_unavailable(_: Request, exc: db.DatabaseUnavailable) -> JSONResponse:
    return JSONResponse(status_code=503, content={
        "detail": "The database is temporarily unavailable. Please try again shortly.",
        "code": "database_unavailable"})


@app.exception_handler(Exception)
def _unexpected(request: Request, exc: Exception) -> JSONResponse:
    # Full details go to the server log only; the user never sees stack traces or SQL.
    log.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={
        "detail": "Something went wrong on our side. Please try again.", "code": "internal_error"})


@app.exception_handler(HTTPException)
def _http_error(_: Request, exc: HTTPException) -> JSONResponse:
    code = {401: "unauthorized", 403: "forbidden", 404: "not_found", 429: "rate_limited"}.get(
        exc.status_code, "error")
    return JSONResponse(status_code=exc.status_code, headers=exc.headers,
                        content={"detail": exc.detail, "code": code})


@app.get("/api/health", tags=["health"])
def health() -> dict:
    """Never requires auth or a database. Shows which engine chat will use."""
    p = provider()
    ollama = cached_status() if p != "rules" else {"available": False, "reason": "LLM_PROVIDER=rules"}
    if p == "rules" or not ollama["available"]:
        engine = "rules" if p != "ollama" else "unavailable"
    else:
        engine = "llm"
    return {
        "status": "ok",
        "database": db.db_status(),
        "auth_configured": bool(settings.SUPABASE_URL and settings.SUPABASE_PUBLISHABLE_KEY),
        "llm_provider": p,
        "chat_engine": engine,
        "ollama": {"available": ollama["available"], "model": settings.OLLAMA_MODEL, "reason": ollama["reason"]},
        "embeddings": settings.EMBEDDING_PROVIDER,
    }


_INDEX = Path(__file__).parent / "static" / "index.html"


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(_INDEX)
