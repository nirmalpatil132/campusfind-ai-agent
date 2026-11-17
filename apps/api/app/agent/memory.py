"""Conversation memory (§13), stored in Postgres.

chat_messages   one row per user message and one per assistant reply.
                For LLM turns the assistant row also stores the turn's raw tool-call messages
                ("llm_messages"), so the next call can resend them. Because a whole turn lives in
                ONE row, trimming history by rows can never split a tool call from its result.
agent_sessions  which engine runs the session ('llm' or 'rules') + the rule engine's state machine.
"""

from __future__ import annotations

from typing import Any

from app import db
from app.services.errors import NotFound


def get_session(session_id: str, user: Any) -> dict[str, Any] | None:
    row = db.fetch_one("select session_id, user_id, mode, state from public.agent_sessions "
                       "where session_id = %(s)s", {"s": session_id})
    if row and str(row["user_id"]) != str(user.id):
        raise NotFound("Chat session not found.")  # someone else's session: pretend it doesn't exist
    return row


def save_session(session_id: str, user: Any, mode: str, state: dict[str, Any]) -> None:
    db.execute(
        """insert into public.agent_sessions (session_id, user_id, mode, state)
           values (%(s)s, %(u)s, %(m)s, %(st)s)
           on conflict (session_id) do update
              set mode = excluded.mode, state = excluded.state, updated_at = now()
              where public.agent_sessions.user_id = excluded.user_id""",
        {"s": session_id, "u": user.id, "m": mode, "st": db.jsonb(state)},
    )


def append_message(session_id: str, user: Any, role: str, content: dict[str, Any]) -> None:
    db.execute(
        "insert into public.chat_messages (session_id, user_id, role, content) "
        "values (%(s)s, %(u)s, %(r)s, %(c)s)",
        {"s": session_id, "u": user.id, "r": role, "c": db.jsonb(content)},
    )


def load_records(session_id: str, user: Any, limit: int) -> list[dict[str, Any]]:
    """The last `limit` rows of this session, oldest first, starting at a USER message."""
    rows = db.fetch_all(
        """select role, content, created_at from (
               select role, content, created_at, id from public.chat_messages
               where session_id = %(s)s and user_id = %(u)s
               order by id desc limit %(lim)s) t
           order by id""",
        {"s": session_id, "u": user.id, "lim": max(2, limit)},
    )
    while rows and rows[0]["role"] != "user":
        rows.pop(0)
    return rows


def to_llm_history(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rebuild the message list the LLM saw, including earlier tool calls/results."""
    history: list[dict[str, Any]] = []
    for r in records:
        content = r["content"] or {}
        if r["role"] == "user":
            history.append({"role": "user", "content": content.get("text", "")})
        elif content.get("llm_messages"):
            history.extend(content["llm_messages"])
        else:  # a turn answered by the rule engine
            history.append({"role": "assistant", "content": content.get("text", "")})
    return history


def transcript(session_id: str, user: Any) -> list[dict[str, Any]]:
    """For GET /api/agent/sessions/{id}: what the UI shows (no raw tool messages)."""
    rows = db.fetch_all(
        "select role, content, created_at from public.chat_messages "
        "where session_id = %(s)s and user_id = %(u)s order by id",
        {"s": session_id, "u": user.id})
    if not rows:
        raise NotFound("Chat session not found.")
    out = []
    for r in rows:
        c = r["content"] or {}
        out.append({"role": r["role"], "text": c.get("text", ""), "mode": c.get("mode"),
                    "tool_calls": c.get("tool_calls", []), "candidates": c.get("candidates", []),
                    "created_at": r["created_at"].isoformat()})
    return out
