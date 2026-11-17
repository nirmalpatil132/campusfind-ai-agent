"""Terminal chat with the CampusFind agent (same engine as the web page).

Run from apps/api (venv active):   python scripts/terminal_agent.py

You log in with a REAL Supabase account (email + password). The token is verified exactly like
the API does, so there is no fake user. Type 'exit' to quit, 'new' for a new session.
"""

from __future__ import annotations

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402

from app import db  # noqa: E402
from app.agent.loop import run_turn  # noqa: E402
from app.auth import AuthError, user_from_token  # noqa: E402
from app.config import settings  # noqa: E402


def login() -> str:
    if not (settings.SUPABASE_URL and settings.SUPABASE_PUBLISHABLE_KEY):
        sys.exit("Set SUPABASE_URL and SUPABASE_PUBLISHABLE_KEY in apps/api/.env first (see README).")
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")
    resp = httpx.post(f"{settings.SUPABASE_URL.rstrip('/')}/auth/v1/token",
                      params={"grant_type": "password"},
                      headers={"apikey": settings.SUPABASE_PUBLISHABLE_KEY},
                      json={"email": email, "password": password}, timeout=20)
    if resp.status_code != 200:
        sys.exit(f"Login failed: {resp.json().get('error_description') or resp.text}")
    return resp.json()["access_token"]


def main() -> None:
    if not db.is_configured():
        sys.exit("DATABASE_URL is empty in apps/api/.env. Steps:\n  - " + "\n  - ".join(db.SETUP_STEPS))
    try:
        user = user_from_token(login())
    except AuthError as exc:
        sys.exit(f"Auth error: {exc}")
    print(f"\nLogged in as {user.name} ({user.role}). Type 'exit' to quit, 'new' for a new session.\n")

    session_id = None
    while True:
        try:
            text = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text.lower() in ("exit", "quit"):
            break
        if text.lower() == "new":
            session_id = None
            print("(new session)\n")
            continue
        result = run_turn(user, session_id, text)
        session_id = result.session_id
        for call in result.tool_calls:
            mark = "ok " if call["ok"] else "ERR"
            print(f"  [{call['engine']}] {mark} {call['name']}({call['input']}) -> {call['summary']}")
        print(f"\nAgent [{result.mode}]: {result.reply}\n")
    db.close_pool()


if __name__ == "__main__":
    main()
