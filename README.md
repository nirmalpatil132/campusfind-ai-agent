# CampusFind AI: Campus Lost & Found Agent

An agent that helps students at Government College of Engineering, Kolhapur report lost and found items. It proposes **candidate** matches, and a human admin verifies them. No paid AI API is needed.

| Engine | When it runs | What it is |
|---|---|---|
| **Local LLM** (`mode: "llm"`) | When [Ollama](https://ollama.com) is running with a tool-calling model (default `qwen2.5:3b`) | A real LLM. It chooses tools, our code runs them, and it reads the results and decides again |
| **Rule-based fallback** (`mode: "rules"`) | When Ollama isn't available (or `LLM_PROVIDER=rules`) | A deterministic state machine plus keyword/regex extraction. **It is not an LLM.** The UI shows a "Rule-based mode" badge |

Both engines call the **same** tools, services and database. Every reply and every `agent_logs` row records which engine produced it.

More detail:
- Full plan: [PROJECT_GUIDE.md](PROJECT_GUIDE.md)
- API: [docs/api-contract.md](docs/api-contract.md)
- Test results: [docs/evaluation.md](docs/evaluation.md)

> **Source:** https://github.com/nirmalpatil132/campusfind-ai-agent
>
> **New to this project? Read [docs/BEGINNER_GUIDE.md](docs/BEGINNER_GUIDE.md).** It covers setup, a demo script and viva questions.

### Quick start (after the one-time Supabase setup in section 2)
```powershell
cd apps\api
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload       # then open http://localhost:8000
```

---

## 1. Install (Windows PowerShell)

```powershell
cd apps/api
python -m venv .venv                      # skip if .venv already exists
.venv\Scripts\Activate.ps1                # if blocked: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
pip install -r requirements.txt
Copy-Item ..\..\.env.example .env         # only if you don't have apps/api/.env yet
```

On macOS/Linux, use `python3 -m venv .venv`, then `source .venv/bin/activate` and `cp ../../.env.example .env`.

> **Already have an `apps/api/.env` from Phase 1?** Add the new lines from `.env.example`: `LLM_PROVIDER=auto`, the `OLLAMA_*` lines, `EMBEDDING_PROVIDER`, and `SUPABASE_PUBLISHABLE_KEY`. If your `.env` still says `LLM_PROVIDER=anthropic`, the agent treats it as `auto` and shows a note.

## 2. Supabase (required: all data lives here)

1. Create a free project at https://supabase.com, in the region closest to India.
2. **SQL Editor → New query →** paste the whole of `db/schema.sql` **→ Run**. It's safe to run again.
3. **Connect** (top bar) → **Session pooler** → copy the URI into `DATABASE_URL=` in `apps/api/.env`.
   - Replace `[YOUR-PASSWORD]` with your database password.
   - If the password contains `@ # / ?`, URL-encode those characters.
   - Use the pooler string: the direct connection is IPv6-only on the free plan.
4. **Project Settings → API Keys**:
   - Project URL → `SUPABASE_URL=`
   - publishable key (`sb_publishable_…`) → `SUPABASE_PUBLISHABLE_KEY=`
5. **Authentication → Users → Add user → Create new user**, with **Auto Confirm User** ticked. Create three accounts: two students and one admin.
6. Back in the SQL Editor, promote the admin:
   `update public.users set role = 'admin' where email = 'admin@yourcollege.test';`
7. If logins fail with "Invalid token": **Project Settings → JWT Keys** must use the new signing keys (ES256/RS256), not the legacy HS256 secret.
8. **Student self-registration (email confirmation ON):**
   - **Authentication → Sign In / Providers → Email**: keep **Confirm email** on.
   - **Authentication → URL Configuration**:
     - **Site URL** = where the app runs (`http://localhost:8000` locally, or your Render URL).
     - Under **Redirect URLs**, add `http://localhost:8000/**` and `https://<your-service>.onrender.com/**`. The confirmation link then opens the app, which logs the student in.
   - New accounts always get the `student` role from the database trigger. Nothing in the API or the page can make someone an admin.
   - Supabase's built-in email sender only allows a few emails per hour. If "Resend email" says too many attempts, wait, or set up custom SMTP (**Authentication → Emails → SMTP**).

Without `DATABASE_URL`, the API still starts. `/api/health` shows `database: not_configured`, and every data route returns **503 with these setup steps**. Nothing is faked.

## 3. Ollama (optional, free)

1. Install Ollama from https://ollama.com and start it.
2. Pull the models:
   ```powershell
   ollama pull qwen2.5:3b          # chat model with tool calling (~2 GB). 16 GB RAM? try qwen2.5:7b
   ollama pull nomic-embed-text    # optional: semantic similarity for matching (~270 MB)
   ```
3. Set `OLLAMA_MODEL=` in `.env` if you use a different model.

The agent checks Ollama about every 20 seconds, so starting it later is fine. New chat sessions pick it up; a session that started in rule mode stays in rule mode.

## 4. Run

```powershell
cd apps/api
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload
```

- Web app: **http://localhost:8000**
- Swagger: **http://localhost:8000/docs**
- Status: **http://localhost:8000/api/health**

Terminal chat, using the same agent: `python scripts/terminal_agent.py` (logs in with a Supabase email and password).

## 5. Demo script (about 5 minutes)

1. `/api/health` shows `database: ok` and `chat_engine: llm` or `rules`. Explain both engines.
2. **Student A** logs in and types: *"I lost my black HP laptop bag with a red keychain in CSE block yesterday around 2pm"*. The agent asks for anything missing, shows a summary, and waits for **yes**. It then saves `L-0001` and runs matching ("no candidates yet").
3. **Student B** logs in (a private/incognito window works) and types: *"I found a black laptop bag with a red keychain outside the CSE lab today"*. It asks where the bag is now ("admin office") and asks for a private detail ("ID card in the front pocket"). Then: yes → `F-0001` and a candidate.
4. **Student A** opens **Matches**, sees the candidate (*"potentially matching… please verify"*), clicks **Might be mine**, and answers *"my ID card is in the front pocket"*.
5. **Admin** opens **Admin**. The owner's answer and the finder's 🔒 private hint appear side by side. Click **Verify match**, then **Mark returned** (each button needs two clicks). Both students get notifications.
6. In Supabase, show the `agent_logs` table, `mode` column included. That's how you prove the engine chose the tools.
7. Stop Ollama, start a **New chat**, and show the "Rule-based mode" badge: the same flow still works.

## 6. Tests

```powershell
cd apps/api
python -m pytest -q                    # no DB, Ollama or keys needed
python scripts/evaluate_matching.py    # offline matching evaluation on the §22 dataset
python scripts/e2e_check.py            # full workflow on the REAL Supabase DB (44/44 passed on 17 Nov 2026)
python scripts/reset_demo.py --yes     # wipe demo reports/matches/chats (keeps accounts) before a demo
```

Verified results (17 Nov 2026):
- pytest: **149 passed, 1 skipped**, including 21 new tests: the iQube regression, rate limits, timeouts and error format.
- matching recall@3: 10/10.
- end-to-end on Supabase: 44/44, from before the UI redesign. Re-run it after a demo reset.
- read-only checks on the live Supabase after the redesign: 15/15.
- real `qwen2.5:3b` replay of the iQube chat:
  - the invented details were dropped;
  - a LOST report was saved only after "yes".

See [docs/evaluation.md](docs/evaluation.md).

## 7. Deploy (Render, free)

`render.yaml` describes the service. Hosted mode uses the **rule-based engine** (`LLM_PROVIDER=rules`), because free hosts can't run Ollama.

1. https://render.com → sign in with GitHub → **New → Blueprint** → choose the `campusfind-ai-agent` repo → **Apply**.
2. When asked, enter `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `ALLOWED_ORIGINS=https://<your-service>.onrender.com`.
3. After the build, open `https://<your-service>.onrender.com/api/health`. It should show `database: ok`.
4. In Supabase → **Authentication → URL Configuration**, add the Render URL as the Site URL.
5. The free plan sleeps after 15 minutes idle; the first request then takes about 1 minute.

## Protection: rate limits, timeouts, friendly errors

| What | Limit (configurable in `.env`) | Key |
|---|---|---|
| AI chat `POST /api/agent/chat` | 10/minute and 200/day | per user (from the verified login token) |
| Lost/found forms | 5/minute and 50/day | per user |
| Search + matching | 30/minute | per user |
| Admin verify / reject / return | 60/minute | per admin |
| Every `/api/*` route except `/api/health` | 120/minute | per client IP (flood guard) |
| Login / registration | Supabase Auth's own server-side limits, plus a page cooldown after 5 failed logins in a minute | Supabase |

How the limits behave:
- When a limit is exceeded, the server answers **HTTP 429** with a `Retry-After` header. The page then shows "Too many requests. Please try again shortly."
- The counters are **in memory, per server process**. They are not shared across several workers or servers, and they reset when the server restarts. That is fine for our single Render instance; more servers would need Redis.

Timeouts and stability:

| What | Setting |
|---|---|
| Database connect | 10 s |
| Waiting for a pooled DB connection | 15 s |
| Any one SQL statement | 15 s (`statement_timeout`) |
| Local LLM call | 120 s |
| Requests to the LLM at the same time | 1. Others wait up to 45 s, then the rule engine answers, labelled as such |
| Page waiting for an AI reply | 150 s. Other requests: 30–45 s |
| Request body | Max 64 KB, otherwise HTTP 413 |

Report saves are never retried automatically, so a retry can't create a duplicate report; the duplicate rule is a second safety net.

Every error comes back as `{"detail": "<friendly message>", "code": "<error_code>"}`. Stack traces and SQL appear only in the server log.

## How the AI agent avoids inventing details ("the LLM proposes, the server checks")

During browser testing, qwen2.5:3b invented a location and a time. It also said "report created" when nothing was saved, and then saved a FOUND report for a LOST scooter key. The fix is in `app/agent/guard.py`:
1. In LLM mode, a `create_*` tool call **never saves**. It only proposes a draft.
2. The report type comes from the student's own words ("I lost" / "I found"), not from the model's choice.
3. Every field must appear in what the student actually typed. Invented values are dropped, and the student is asked for them.
4. The deterministic form step asks one question at a time, shows the summary, and saves only after "yes". Each reply is labelled with the engine that wrote it: "Local LLM — Ollama", "Local LLM + server check" or "Rule-based engine".
5. If the model claims something was saved when the database saved nothing, that reply is replaced. Raw tool errors are never shown to students.

## What works

- Real Supabase login: JWT verified via JWKS, role read from the DB, admin routes guarded.
- Chat in both engines:
  - intent recognition, field extraction, relative dates in Asia/Kolkata;
  - one question at a time, summary + "yes" before saving (checked on the server);
  - multi-turn memory in Postgres.
- 6 agent tools:
  - `create_lost_report` and `create_found_report`
  - `search_items` and `find_matching_items`
  - `get_item_details` and `get_my_reports`
- Matching engine (§14):
  - weights, colour families, zones, time decay, brand cap, colour-aware unique-mark bonus;
  - renormalised weights when there are no embeddings;
  - duplicate detection.
- Owner confirm/decline. Admin verify / reject / mark returned, with an audit log and notifications.
- Private verification hint shown to admins only.
- Long numbers (10+ digits) masked in descriptions.
- Form endpoints that work with no LLM at all.

## Deferred (not built yet)

- Next.js frontend (Phase 6). Instead, FastAPI serves a redesigned single-page HTML/CSS/JS app: landing page, login/registration, dashboard, AI assistant, lost/found forms, search, matches, my reports, admin dashboard. It works on desktop and phones.
- Shared (multi-server) rate limiting. The current limits are in-memory, for one instance.
- Tools `notify_user`, `escalate_to_admin`, `update_item_status` (cancelling your own report).
- §17 prompt-injection test suite. Admin actions aren't tools and tool outputs exclude PII, but this isn't formally tested.
- Seeding the 40 test reports into the DB, which is blocked because every report needs a real Auth user. The offline evaluation covers that dataset.
- An Anthropic engine inside the agent. `scripts/hello_agent.py` still works if you get credits.
- Measured accuracy of the **local LLM** engine. It depends on the demo laptop; small models make more tool-calling mistakes, which the server-side guards contain.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Badge says "Rule-based mode" but Ollama is installed | Run `ollama pull qwen2.5:3b` and check `OLLAMA_MODEL`. `/api/health` → `ollama.reason` says what's wrong |
| `503 Database is not configured` | Fill `DATABASE_URL` and restart uvicorn |
| `Database unavailable` | Use the **Session pooler** string, check the password, and URL-encode special characters |
| Login works but the API says "Profile not found" | Re-run `db/schema.sql` (it backfills `public.users`) |
| "Login is not configured" on the page | Set `SUPABASE_URL` and `SUPABASE_PUBLISHABLE_KEY` |
| Chat replies are slow in LLM mode | The first reply loads the model (10–60 s). Try a smaller model, or `LLM_PROVIDER=rules` |
