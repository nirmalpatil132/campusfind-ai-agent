"""agent_logs: every tool call with input, output, latency, error and which engine chose it."""

from __future__ import annotations

import logging
from typing import Any

from app import db

log = logging.getLogger(__name__)


def log_tool_call(session_id: Any, mode: str, tool_name: str, tool_input: dict[str, Any],
                  tool_output: Any, error: str | None, latency_ms: int) -> None:
    """Never raises: a logging failure must not break the user's chat."""
    log.info("[%s] tool=%s error=%s latency=%dms", mode, tool_name, error, latency_ms)
    if not db.is_configured():
        return
    try:
        db.execute(
            "insert into public.agent_logs "
            "(session_id, mode, tool_name, tool_input, tool_output, latency_ms, error) "
            "values (%(s)s, %(m)s, %(t)s, %(i)s, %(o)s, %(l)s, %(e)s)",
            {"s": session_id, "m": mode, "t": tool_name, "i": db.jsonb(tool_input),
             "o": db.jsonb(tool_output), "l": latency_ms, "e": error},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not write agent_logs: %s", exc)
