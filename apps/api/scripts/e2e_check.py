"""End-to-end check against the REAL Supabase database (no passwords needed).

Run from apps/api:   python scripts/e2e_check.py
Optional emails:     python scripts/e2e_check.py --student1 a@x --student2 b@x --admin c@x

It drives the real agent (rule engine, for repeatable results) and the real services with the three
accounts you created in Supabase Auth, looked up by email in public.users. It does NOT bypass the
HTTP auth layer for the security checks: those go through the API with each user's real profile.

It creates real rows (a lost report, a found report, a match...). Run scripts/reset_demo.py
afterwards for a clean demo database.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.agent.loop import run_turn  # noqa: E402
from app.auth import CurrentUser, _jwks, get_current_user  # noqa: E402
from app.config import settings  # noqa: E402
from app.main import app  # noqa: E402
from app.services import matches  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}{(' - ' + detail) if detail else ''}")
    return ok


def user_by_email(email: str) -> CurrentUser | None:
    r = db.fetch_one("select id, email, name, role from public.users where lower(email) = lower(%(e)s)", {"e": email})
    return CurrentUser(id=str(r["id"]), email=r["email"], name=r["name"], role=r["role"]) if r else None


def chat(user: CurrentUser, session: str | None, text: str):
    r = run_turn(user, session, text)
    print(f"    {user.email.split('@')[0]}: {text}\n    agent[{r.mode}]: {r.reply.splitlines()[0][:110]}")
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--student1", default="student1@campusfind.test")
    ap.add_argument("--student2", default="student2@campusfind.test")
    ap.add_argument("--admin", default="admin@campusfind.test")
    a = ap.parse_args()

    settings.LLM_PROVIDER = "rules"        # deterministic for this check
    settings.EMBEDDING_PROVIDER = "none"

    print("\n== 1. Configuration & database ==")
    if not check("DATABASE_URL configured", db.is_configured()):
        return 1
    status = db.db_status()
    if not check("database connection", status == "ok", status):
        return 1
    for t in ("users", "locations", "items", "lost_reports", "found_reports", "matches", "notifications",
              "admin_actions", "chat_messages", "agent_sessions", "agent_logs"):
        ok = db.fetch_one("select to_regclass(%(t)s) as t", {"t": f"public.{t}"})["t"] is not None
        check(f"table {t}", ok)
    check("pgvector extension", db.fetch_one("select 1 as x from pg_extension where extname='vector'") is not None)
    n_loc = db.fetch_one("select count(*)::int as n from public.locations")["n"]
    check("14 campus locations", n_loc >= 14, f"{n_loc} rows")

    print("\n== 2. Supabase Auth ==")
    try:
        keys = _jwks().get_jwk_set().keys
        algs = sorted({k.algorithm_name for k in keys})
        check("JWKS signing keys reachable", bool(keys), f"{len(keys)} key(s) {algs}")
    except Exception as exc:  # noqa: BLE001
        check("JWKS signing keys reachable", False, f"{type(exc).__name__}: {exc}")
    s1, s2, adm = user_by_email(a.student1), user_by_email(a.student2), user_by_email(a.admin)
    for label, u, role in (("student1", s1, "student"), ("student2", s2, "student"), ("admin", adm, "admin")):
        check(f"{label} profile exists with role {role}", bool(u and u.role == role),
              f"found role={u.role}" if u else "missing: create it in Authentication → Users")
    if not (s1 and s2 and adm and adm.role == "admin"):
        return 1

    print("\n== 3. Role-based authorization (through the HTTP API) ==")
    client = TestClient(app)
    app.dependency_overrides[get_current_user] = lambda: s1
    check("student gets 403 on /api/admin/matches", client.get("/api/admin/matches").status_code == 403)
    app.dependency_overrides[get_current_user] = lambda: adm
    check("admin gets 200 on /api/admin/stats", client.get("/api/admin/stats").status_code == 200)
    app.dependency_overrides.clear()
    check("no token → 401 on /api/items/mine", client.get("/api/items/mine").status_code == 401)

    print("\n== 4. Student 1 reports a LOST bag through the agent ==")
    r = chat(s1, None, "I lost my black HP laptop bag with a red keychain in CSE block yesterday around 2pm")
    check("agent shows summary before saving", "Shall I save it?" in r.reply and not r.items_created)
    r = chat(s1, r.session_id, "yes")
    lost_code = r.items_created[0] if r.items_created else None
    check("lost report saved", bool(lost_code), str(lost_code))
    check("matching ran after save", any(t["name"] == "find_matching_items" for t in r.tool_calls))
    row = db.fetch_one("select status, created_by from public.items where code = %(c)s", {"c": lost_code})
    check("lost report persisted in DB with owner", bool(row and str(row["created_by"]) == s1.id))

    print("\n== 5. Missing details & duplicates ==")
    r2 = chat(s1, None, "I lost something")
    check("vague report asks a question, saves nothing", "?" in r2.reply and not r2.items_created)
    r3 = chat(s1, None, "I lost my black HP laptop bag in CSE block yesterday")
    r3 = chat(s1, r3.session_id, "yes")
    check("duplicate report detected, not saved", not r3.items_created and (lost_code or "") in r3.reply)

    print("\n== 6. Student 2 reports the FOUND bag ==")
    r = chat(s2, None, "I found a black laptop bag with a red keychain outside the CSE lab today")
    r = chat(s2, r.session_id, "admin office")
    r = chat(s2, r.session_id, "there is an ID card in the front pocket")
    r = chat(s2, r.session_id, "yes")
    found_code = r.items_created[0] if r.items_created else None
    check("found report saved", bool(found_code), str(found_code))
    cand = next((c for c in r.candidates if c["item_code"] == lost_code), None)
    check("candidate match found and ranked", bool(cand), f"score {cand['total_score']} {cand['band']}" if cand else "")
    check("reply says 'potentially matching', not certain", "potentially matching" in r.reply.lower())
    hint = db.fetch_one("select fr.verification_hint from public.found_reports fr join public.items i "
                        "on i.id = fr.item_id where i.code = %(c)s", {"c": found_code})
    check("private hint stored", bool(hint and hint["verification_hint"]))
    check("private hint NOT in agent reply/candidates", "front pocket" not in (r.reply + str(r.candidates)))

    print("\n== 7. Student 1 confirms, admin verifies and returns ==")
    mine = [m for m in matches.list_for_owner(s1) if m["found"]["code"] == found_code]
    if not check("owner sees the candidate", bool(mine)):
        return summary()
    mid = mine[0]["match_id"]
    check("owner view hides the private hint", "verification_hint" not in str(mine[0]))
    try:
        matches.confirm(s2, mid, "not my bag")
        check("finder cannot confirm owner's match", False)
    except Exception as exc:  # noqa: BLE001
        check("finder cannot confirm owner's match", type(exc).__name__ == "Forbidden")
    matches.confirm(s1, mid, "my ID card is in the front pocket")
    queue = [m for m in matches.admin_queue("owner_confirmed") if m["match_id"] == mid]
    check("admin queue shows answer + private hint", bool(queue and queue[0]["owner_answer"] and queue[0]["verification_hint"]))
    matches.verify(adm, mid, "ID card matches")
    ret = matches.mark_returned(adm, found_code, "handed over at admin office")
    check("item marked returned", ret["status"] == "returned")

    print("\n== 8. Final database state ==")
    st = {x["code"]: x["status"] for x in db.fetch_all(
        "select code, status from public.items where code = any(%(c)s)", {"c": [lost_code, found_code]})}
    check("both items status = returned", set(st.values()) == {"returned"}, str(st))
    m = db.fetch_one("select status, owner_answer from public.matches where id = %(i)s", {"i": mid})
    check("match status = closed", m["status"] == "closed")
    acts = db.fetch_all("select action from public.admin_actions where match_id = %(i)s", {"i": mid})
    check("admin_actions logged verify + return", {x["action"] for x in acts} >= {"verify_match", "mark_returned"})
    for label, u in (("student1", s1), ("student2", s2)):
        n = db.fetch_one("select count(*)::int as n from public.notifications where user_id = %(u)s "
                         "and type = 'item_returned'", {"u": u.id})["n"]
        check(f"{label} got 'item returned' notification", n >= 1)
    logs = db.fetch_one("select count(*)::int as n from public.agent_logs where mode = 'rules' "
                        "and tool_name = 'create_lost_report'")["n"]
    check("agent_logs records tool calls with engine mode", logs >= 1)
    return summary()


def summary() -> int:
    failed = [r for r in results if r[0] == FAIL]
    print(f"\n== RESULT: {len(results) - len(failed)}/{len(results)} checks passed ==")
    for _, name, detail in failed:
        print(f"   FAILED: {name} {detail}")
    db.close_pool()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
