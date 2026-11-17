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
```

## 7. Deploy (Render, free)

`render.yaml` describes the service. Hosted mode uses the **rule-based engine** (`LLM_PROVIDER=rules`), because free hosts can't run Ollama.

1. https://render.com → sign in with GitHub → **New → Blueprint** → choose the `campusfind-ai-agent` repo → **Apply**.
2. When asked, enter `DATABASE_URL`, `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`, and `ALLOWED_ORIGINS=https://<your-service>.onrender.com`.
3. After the build, open `https://<your-service>.onrender.com/api/health`. It should show `database: ok`.
4. In Supabase → **Authentication → URL Configuration**, add the Render URL as the Site URL.
5. The free plan sleeps after 15 minutes idle; the first request then takes about 1 minute.

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

- Next.js frontend (Phase 6). A single-page HTML app is served from FastAPI instead, with these screens: landing, login/register, AI chat, lost/found forms, search, matches, my reports, admin dashboard.
- Rate limiting (slowapi).
- Tools `notify_user`, `escalate_to_admin`, `update_item_status` (cancelling your own report).
- §17 prompt-injection test suite. Admin actions aren't tools and tool outputs exclude PII, but this isn't formally tested.
- Deployment (Render/Vercel).
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
