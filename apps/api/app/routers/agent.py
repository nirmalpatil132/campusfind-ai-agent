"""Chat with the agent."""

from __future__ import annotations

from dataclasses import asdict
from uuid import UUID

from fastapi import APIRouter, Depends

from app.agent import memory
from app.agent.loop import run_turn
from app.ratelimit import limit
from app.auth import CurrentUser, get_current_user
from app.schemas.api import ChatRequest

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/chat", dependencies=[Depends(limit("chat"))])
def chat(body: ChatRequest, user: CurrentUser = Depends(get_current_user)) -> dict:
    """{session_id?, message} → {session_id, reply, mode, mode_reason, tool_calls[], items_created[], candidates[]}.

    `mode` says which engine answered: "llm" (local Ollama model) or "rules" (deterministic fallback).
    """
    result = run_turn(user, str(body.session_id) if body.session_id else None, body.message)
    return asdict(result)


@router.get("/sessions/{session_id}")
def get_session(session_id: UUID, user: CurrentUser = Depends(get_current_user)) -> dict:
    return {"session_id": str(session_id), "messages": memory.transcript(str(session_id), user)}
