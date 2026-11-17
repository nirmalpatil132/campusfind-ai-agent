"""Reset the database to a clean demo state: deletes ALL reports, matches, notifications,
chats and logs, and restarts codes at L-0001 / F-0001. User accounts and locations are kept.

Run from apps/api:   python scripts/reset_demo.py --yes
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db  # noqa: E402

TABLES = ["admin_actions", "notifications", "matches", "lost_reports", "found_reports", "items",
          "chat_messages", "agent_sessions", "agent_logs"]


def main() -> None:
    if "--yes" not in sys.argv:
        sys.exit("This deletes all reports, matches, chats and logs (accounts are kept).\n"
                 "Run again with --yes to confirm:  python scripts/reset_demo.py --yes")
    if not db.is_configured():
        sys.exit("DATABASE_URL is empty in apps/api/.env.")
    with db.transaction() as conn:
        for t in TABLES:  # fixed list of table names, not user input
            conn.execute(f"delete from public.{t}")
        conn.execute("alter sequence public.lost_code_seq restart with 1")
        conn.execute("alter sequence public.found_code_seq restart with 1")
    users = db.fetch_one("select count(*)::int as n from public.users")["n"]
    print(f"Demo data cleared. {users} user account(s) kept. Next codes: L-0001 / F-0001.")
    db.close_pool()


if __name__ == "__main__":
    main()
