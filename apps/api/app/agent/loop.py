"""Agent orchestrator: one chat turn, in either engine.

    run_turn(user, session_id, message)
      1. load session + history from Postgres
      2. choose the engine  (LLM_PROVIDER: auto | ollama | rules)
      3a. LLM engine:  ask Ollama → it picks tools → we run them → send results back → repeat
                       (max AGENT_MAX_STEPS) until it answers in text
      3b. rule engine: deterministic state machine (rule_agent.py), same tools
      4. log every tool call to agent_logs, save the turn, return the reply + which engine was used

Server-side safety nets (apply to both engines):
  * create_* tools refuse to run unless the user's message was a plain "yes" (tools.py)
  * in LLM mode a create_* call never saves: guard.py turns it into a DRAFT holding only the
    details the student actually said; the deterministic form step then asks for anything
    missing, shows the summary and saves only after "yes" (fixes invented details / wrong type)
  * a model reply that claims "saved" when the database saved nothing is replaced
  * after a report is created, matching ALWAYS runs
  * any reply that shows candidates contains the "potentially matching" sentence
  * students never see raw tool errors; tool chips show a short friendly status
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from app.agent import guard, memory, rule_agent
from app.agent.llm import OllamaClient, OllamaError, cached_status
from app.agent.prompts import POTENTIAL_MATCH_SENTENCE, build_system_prompt
from app.agent.tools import TOOLS, ToolContext, execute_tool
from app.config import settings
from app.services.logs import log_tool_call
from app.timeutil import today_ist

log = logging.getLogger(__name__)

Executor = Callable[[str, dict[str, Any]], dict[str, Any]]
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


@dataclass
class TurnResult:
    session_id: str
    reply: str
    mode: str                 # "llm" | "rules" | "unavailable"
    mode_reason: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    items_created: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    # who wrote the reply text: "llm" (the model), "rules" (rule engine / deterministic form step),
    # "server_check" (the model chose the tool; the server checked the details and wrote the text)
    reply_source: str = "llm"


@dataclass
class LLMTurn:
    reply: str
    messages: list[dict[str, Any]]  # this turn's assistant + tool messages (saved for history)
    tool_calls: list[tuple[str, dict[str, Any], dict[str, Any]]]


# ---------------------------------------------------------------------------
# Engine selection
# ---------------------------------------------------------------------------
def provider() -> str:
    p = settings.LLM_PROVIDER.strip().lower()
    return p if p in ("auto", "ollama", "rules") else "auto"


def choose_mode(session_mode: str | None, client: OllamaClient | None = None) -> tuple[str, str]:
    """Return (mode, reason). A session keeps the engine it started with."""
    p = provider()
    if settings.LLM_PROVIDER.strip().lower() not in ("auto", "ollama", "rules"):
        note = f" (LLM_PROVIDER='{settings.LLM_PROVIDER}' is not supported here; using auto)"
    else:
        note = ""
    if session_mode == "rules" and p != "ollama":
        return "rules", "This chat session started in rule mode" + note
    if p == "rules":
        return "rules", "LLM_PROVIDER=rules (rule-based engine only)"
    status = cached_status(client)
    if status["available"]:
        return "llm", status["reason"] + note
    if p == "ollama":
        return "unavailable", status["reason"]
    return "rules", "Local LLM unavailable, using rule-based fallback: " + status["reason"] + note


# ---------------------------------------------------------------------------
# LLM engine (pure: no database, so it can be unit-tested with a mocked Ollama)
# ---------------------------------------------------------------------------
def run_llm_turn(client: OllamaClient, system_prompt: str, history: list[dict[str, Any]],
                 message: str, execute: Executor, max_steps: int | None = None,
                 should_stop: Callable[[], bool] | None = None) -> LLMTurn:
    messages = [{"role": "system", "content": system_prompt}, *history, {"role": "user", "content": message}]
    turn_messages: list[dict[str, Any]] = []
    calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    for _ in range(max_steps or settings.AGENT_MAX_STEPS):
        msg = client.chat(messages, TOOLS)  # may raise OllamaError → caller falls back
        content = _THINK_RE.sub("", msg.get("content") or "").strip()
        tool_calls = msg.get("tool_calls") or []
        assistant: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            assistant["tool_calls"] = tool_calls
        messages.append(assistant)
        turn_messages.append(assistant)

        if not tool_calls:
            return LLMTurn(reply=content or "Done.", messages=turn_messages, tool_calls=calls)

        for call in tool_calls:  # one tool message per call, in order
            fn = call.get("function", {})
            name, args = fn.get("name", ""), fn.get("arguments") or {}
            output = execute(name, args if isinstance(args, dict) else {})
            calls.append((name, args, output))
            tool_msg = {"role": "tool", "tool_name": name, "content": json.dumps(output, default=str)}
            messages.append(tool_msg)
            turn_messages.append(tool_msg)
        if should_stop and should_stop():  # the server takes over (e.g. a report draft); saves a model call
            return LLMTurn(reply="", messages=turn_messages, tool_calls=calls)

    return LLMTurn(reply="Sorry, I couldn't finish that. Please try again, or rephrase your request.",
                   messages=turn_messages, tool_calls=calls)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
_FRIENDLY_ERRORS = {
    "not_confirmed": "waiting for your 'yes'",
    "invalid_input": "needs more details",
    "notfound": "not found",
    "forbidden": "not allowed",
    "conflict": "already exists",
    "database_not_configured": "database not set up",
    "database_unavailable": "database temporarily unavailable",
}


def _summary(name: str, out: dict[str, Any]) -> str:
    """Short, student-safe status for the tool chip (raw tool errors are never shown)."""
    if "error" in out:
        return _FRIENDLY_ERRORS.get(out["error"], "could not complete")
    if out.get("status") == "draft_prepared":
        return "draft prepared; details checked by the server"
    if out.get("duplicate_of"):
        return f"Already reported as {out['duplicate_of']}"
    if name.startswith("create_"):
        return f"Saved report {out.get('code')}"
    if name == "find_matching_items":
        return f"{len(out.get('candidates', []))} candidate(s) for {out.get('item_code')}"
    if name == "search_items":
        return f"{out.get('count', 0)} result(s)"
    if name == "get_my_reports":
        return f"{out.get('count', 0)} report(s)"
    return "OK"



def _draft_active(state: dict[str, Any]) -> bool:
    return state.get("stage") in ("collecting", "confirming")


def run_turn(user: Any, session_id: str | None, message: str, *, client: OllamaClient | None = None,
             today: date | None = None) -> TurnResult:
    session_id = str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())
    message = message.strip()[:2000]
    today = today or today_ist()

    session = memory.get_session(session_id, user)  # raises NotFound for someone else's session
    session_mode, reason = choose_mode(session["mode"] if session else None, client)
    mode = session_mode  # the engine that answers THIS turn (may differ from the session's engine)
    state: dict[str, Any] = (session or {}).get("state") or {}
    records = memory.load_records(session_id, user, settings.CHAT_HISTORY_LIMIT) if mode == "llm" else []
    memory.append_message(session_id, user, "user", {"text": message})

    tool_calls: list[dict[str, Any]] = []
    items_created: list[str] = []
    candidates: list[dict[str, Any]] = []
    current_mode = [mode]  # which engine is running right now (for the logs)
    ctx = ToolContext(user=user, session_id=session_id, last_user_message=message, today=today)
    draft: dict[str, Any] = {}  # set when the LLM proposes a report (see guard.py)
    user_texts = [(r["content"] or {}).get("text", "") for r in records if r["role"] == "user"] + [message]

    def record(name: str, args: dict[str, Any], out: dict[str, Any], error: str | None, ms: int) -> None:
        log_tool_call(session_id, current_mode[0], name, args, out, error, ms)
        tool_calls.append({"name": name, "input": args, "ok": error is None, "summary": _summary(name, out),
                           "engine": current_mode[0]})

    def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        if current_mode[0] == "llm" and name in guard.CREATE_TOOLS:
            # The model never saves directly: build a draft from the student's own words.
            st, dropped = guard.draft_from_llm_call(name, args if isinstance(args, dict) else {},
                                                    user_texts, today, state.get("last_code"))
            draft.update(state=st, dropped=dropped)
            out = {"status": "draft_prepared", "report_type": st["report_type"],
                   "message": "The server will ask the user for any missing details and for confirmation. "
                              "Nothing is saved yet."}
            record(name, args, out, None, int((time.perf_counter() - started) * 1000))
            return out
        out, error = execute_tool(name, args, ctx)
        record(name, args, out, error, int((time.perf_counter() - started) * 1000))
        if name.startswith("create_") and out.get("code"):
            items_created.append(out["code"])
        if name == "find_matching_items" and out.get("candidates"):
            candidates.extend(out["candidates"])
        return out

    llm_messages: list[dict[str, Any]] | None = None
    reply, reply_source = "", "llm"

    if mode == "llm" and _draft_active(state):
        # A report draft is in progress: the deterministic form step owns it until saved/cancelled.
        mode, current_mode[0] = "rules", "rules"
        reason = "Report form step (deterministic): checks the details and saves only after 'yes'"
    if mode == "unavailable":
        reply, reply_source = ("The AI model is temporarily unavailable. Please try again shortly "
                               "(or ask the admin to enable the rule-based engine)."), "rules"
    elif mode == "llm":
        try:
            turn = run_llm_turn(client or OllamaClient(), build_system_prompt(user, today),
                                memory.to_llm_history(records), message, execute,
                                should_stop=lambda: bool(draft))
            reply, llm_messages = turn.reply, turn.messages
        except OllamaError as exc:
            log.warning("Ollama failed: %s", exc)
            if provider() == "ollama":
                mode, reason = "unavailable", str(exc)
                reply = "The AI model is temporarily unavailable. Please try again shortly."
                reply_source = "rules"
            else:
                mode, reason = "rules", f"Local LLM failed ({exc}); switched to rule-based fallback"
                session_mode = current_mode[0] = "rules"
                state = {}  # the rule engine starts fresh; it answers this same message below
        if mode == "llm":
            if draft:
                state = draft["state"]
                reply, reply_source = guard.start_reply(state), "server_check"
                if draft["dropped"]:
                    log.info("LLM draft: dropped ungrounded fields %s", draft["dropped"])
            else:
                reply = guard.remove_false_save_claim(reply, items_created)
            if candidates and "potentially matching" not in (reply or "").lower():
                reply = POTENTIAL_MATCH_SENTENCE + "\n" + reply
            if llm_messages is not None and reply_source != "llm":
                llm_messages.append({"role": "assistant", "content": reply})
    if mode == "rules":
        fallback_note = ""
        if session_mode == "rules" and reason.startswith("Local LLM failed"):
            fallback_note = "The AI model is temporarily unavailable. Switching to rule-based assistance.\n\n"
        turn_r = rule_agent.handle_message(message, state, execute, today, user.first_name)
        reply, state, reply_source = fallback_note + turn_r.reply, turn_r.state, "rules"

    if session_mode in ("llm", "rules"):
        memory.save_session(session_id, user, session_mode, state)
    memory.append_message(session_id, user, "assistant", {
        "text": reply, "mode": mode, "mode_reason": reason, "reply_source": reply_source,
        "tool_calls": tool_calls, "candidates": candidates,
        **({"llm_messages": llm_messages} if llm_messages else {}),
    })
    return TurnResult(session_id=session_id, reply=reply, mode=mode, mode_reason=reason,
                      tool_calls=tool_calls, items_created=items_created, candidates=candidates,
                      reply_source=reply_source)
