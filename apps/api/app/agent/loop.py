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
  * after a report is created, matching ALWAYS runs, even if the small model forgot to call it
  * any reply that shows candidates contains the "potentially matching" sentence
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

from app.agent import memory, rule_agent
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
                 message: str, execute: Executor, max_steps: int | None = None) -> LLMTurn:
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

    return LLMTurn(reply="Sorry, I couldn't finish that. Please try again, or rephrase your request.",
                   messages=turn_messages, tool_calls=calls)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def _summary(name: str, out: dict[str, Any]) -> str:
    if "error" in out:
        return out.get("message", out["error"])
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


def run_turn(user: Any, session_id: str | None, message: str, *, client: OllamaClient | None = None,
             today: date | None = None) -> TurnResult:
    session_id = str(uuid.UUID(session_id)) if session_id else str(uuid.uuid4())
    message = message.strip()[:2000]
    today = today or today_ist()

    session = memory.get_session(session_id, user)  # raises NotFound for someone else's session
    mode, reason = choose_mode(session["mode"] if session else None, client)
    records = memory.load_records(session_id, user, settings.CHAT_HISTORY_LIMIT) if mode == "llm" else []
    memory.append_message(session_id, user, "user", {"text": message})

    tool_calls: list[dict[str, Any]] = []
    items_created: list[str] = []
    candidates: list[dict[str, Any]] = []
    current_mode = [mode]  # which engine is running right now (for the logs)
    ctx = ToolContext(user=user, session_id=session_id, last_user_message=message, today=today)

    def execute(name: str, args: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        out, error = execute_tool(name, args, ctx)
        log_tool_call(session_id, current_mode[0], name, args, out, error,
                      int((time.perf_counter() - started) * 1000))
        tool_calls.append({"name": name, "input": args, "ok": error is None, "summary": _summary(name, out),
                           "engine": current_mode[0]})
        if name.startswith("create_") and out.get("code"):
            items_created.append(out["code"])
        if name == "find_matching_items" and out.get("candidates"):
            candidates.extend(out["candidates"])
        return out

    llm_messages: list[dict[str, Any]] | None = None
    state = (session or {}).get("state") or {}
    reply = ""

    if mode == "unavailable":
        reply = (f"The local AI model is unavailable: {reason} "
                 "(Set LLM_PROVIDER=auto in apps/api/.env to use the rule-based fallback instead.)")
    elif mode == "llm":
        try:
            turn = run_llm_turn(client or OllamaClient(), build_system_prompt(user, today),
                                memory.to_llm_history(records), message, execute)
            reply, llm_messages = turn.reply, turn.messages
        except OllamaError as exc:
            if provider() == "ollama":
                mode, reason = "unavailable", str(exc)
                reply = f"The local AI model failed: {exc}"
            else:
                log.warning("Ollama failed mid-session, switching to rules: %s", exc)
                mode, reason = "rules", f"Local LLM failed ({exc}); switched to rule-based fallback"
                current_mode[0] = "rules"
                state = {}  # the rule engine starts fresh; it answers this same message below
        if mode == "llm":
            # Safety net: matching must always run after a create, even if the model forgot.
            matched = {c["input"].get("item_code", "").upper() for c in tool_calls
                       if c["name"] == "find_matching_items"}
            for code in items_created:
                if code not in matched:
                    out = execute("find_matching_items", {"item_code": code})
                    reply += "\n\n" + rule_agent.format_candidates(out, "lost" if code.startswith("L") else "found", code)
            if candidates and "potentially matching" not in (reply or "").lower():
                reply = POTENTIAL_MATCH_SENTENCE + "\n" + reply
    if mode == "rules":
        turn_r = rule_agent.handle_message(message, state, execute, today, user.first_name)
        reply, state = turn_r.reply, turn_r.state

    if mode in ("llm", "rules"):
        memory.save_session(session_id, user, mode, state if mode == "rules" else {})
    memory.append_message(session_id, user, "assistant", {
        "text": reply, "mode": mode, "mode_reason": reason, "tool_calls": tool_calls,
        "candidates": candidates, **({"llm_messages": llm_messages} if llm_messages else {}),
    })
    return TurnResult(session_id=session_id, reply=reply, mode=mode, mode_reason=reason,
                      tool_calls=tool_calls, items_created=items_created, candidates=candidates)
