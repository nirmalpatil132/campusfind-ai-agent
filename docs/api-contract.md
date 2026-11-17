# API Contract (no-paid-API build)

- **Base URL:** `http://localhost:8000`.
- **Auth:** every route except `/api/health`, `/api/public-config` and `/` needs `Authorization: Bearer <Supabase access token>`.
- **Interactive docs:** `/docs` (Swagger).

Error codes:

| Code | Meaning |
|---|---|
| `401` | No token, or an invalid token |
| `403` | Not allowed (for example, a student on an admin route) |
| `404` | Not found |
| `409` | Duplicate (`{detail, duplicate_of}`) or wrong status |
| `422` | Validation failed |
| `503` | Database or auth not configured/unavailable (`{detail, setup[]}`) |

## Agent

### `POST /api/agent/chat`
```json
// request
{"session_id": "uuid (optional; omit to start a new session)", "message": "I lost my wallet (max 2000 chars)"}
// response
{
  "session_id": "uuid",
  "reply": "Where did you last have it? ...",
  "mode": "llm | rules | unavailable",
  "mode_reason": "Ollama model qwen2.5:3b  |  Local LLM unavailable, using rule-based fallback: ...",
  "tool_calls": [{"name": "create_lost_report", "input": {...}, "ok": true, "summary": "Saved report L-0001", "engine": "rules"}],
  "items_created": ["L-0001"],
  "candidates": [ /* same shape as find_matching_items candidates */ ]
}
```

### `GET /api/agent/sessions/{id}`
Returns `{session_id, messages: [{role, text, mode, tool_calls, candidates, created_at}]}`. Only the session's owner can read it; anyone else gets 404.

## Items (the form path; uses the same services as the agent)

| Method | Path | Body / query | Response |
|---|---|---|---|
| POST | `/api/items/lost` | `{category, item_name, color?, brand?, description, location, event_date, event_time?}` | 201 `{item_id, code, status, embedding_saved, matching}` |
| POST | `/api/items/found` | same, plus `current_holding?` (`finder`/`admin_office`/`security`) and `verification_hint?` | 201 (same shape) |
| GET | `/api/items/search` | `?q=&type=lost\|found&category=&location=&from=&to=` | `{count, results: [ItemCard]}` |
| GET | `/api/items/mine` | | `{reports: [ItemCard]}` |
| GET | `/api/items/{code}` | | `ItemCard`, plus `is_mine`; `verification_hint` only for the owner or an admin |

An `ItemCard` holds: `code`, `report_type`, `category`, `item_name`, `brand`, `color`, `description`, `location`, `event_date`, `event_time`, `status`, `current_holding`. It never contains the reporter's identity.

## Matches (owner)

| Method | Path | Body | Response |
|---|---|---|---|
| GET | `/api/matches/mine` | | `{matches: [{match_id, status, total_score, explanation, lost_code, found: ItemCard-like}]}` |
| POST | `/api/matches/search` | `{item_code, limit?}` | same as the `find_matching_items` tool |
| POST | `/api/matches/{id}/confirm` | `{answer}` (3–500 chars) | `{match_id, status: "owner_confirmed", message}` |
| POST | `/api/matches/{id}/decline` | | `{match_id, status: "rejected"}` |

## Admin (all `require_admin`; the role comes from `public.users`)

| Method | Path | Body | Effect |
|---|---|---|---|
| GET | `/api/admin/matches?status=owner_confirmed` | | Both reports, the owner's answer, and the **private hint** |
| POST | `/api/admin/matches/{id}/verify` | `{note?}` | owner_confirmed → verified; owner notified |
| POST | `/api/admin/matches/{id}/reject` | `{reason}` | → rejected; items reopened if nothing else is active |
| POST | `/api/admin/items/{code}/returned` | `{note?}` | Needs a verified match. Match → closed, items → returned, other candidates auto-rejected, both users notified |
| GET | `/api/admin/items?status=` | | Every report |

Every admin action is written to `admin_actions`.

## Other

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | `{status, database, auth_configured, llm_provider, chat_engine, ollama, embeddings}` |
| GET | `/api/me` | `{id, email, name, role}` |
| GET | `/api/public-config` | `{supabase_url, supabase_publishable_key, college_name}`: public values only |
| GET | `/api/notifications` | `{unread, notifications: [...]}` |
| POST | `/api/notifications/{id}/read` | Marks one notification read |

## Agent tools (shared by the LLM engine and the rule engine)

The user is injected from the verified JWT; model-supplied `user_id`/`role` are ignored. Every tool returns either a result or `{error, message}`.

| Tool | Required inputs | Returns |
|---|---|---|
| `create_lost_report` | `category, item_name, description, location, lost_date` (+ `color, brand, lost_time`) | `{item_id, code, status, embedding_saved}`, or `{duplicate_of, message}`, or `{error: "not_confirmed"}` if the user's message wasn't a plain "yes" |
| `create_found_report` | `category, item_name, description, location, found_date` (+ `found_time, current_holding, verification_hint`) | same |
| `search_items` | (all optional) `query, report_type, category, location, date_from, date_to` | `{count, results: [ItemCard], note}` |
| `find_matching_items` | `item_code` (+ `limit`) | `{item_code, candidates: [...], semantic_used, note}`; owner or admin only |
| `get_item_details` | `item_code` | `ItemCard` (+ hint for the owner/admin) |
| `get_my_reports` | (optional) `status` | `{count, reports: [{code, report_type, item_name, location, event_date, status}]}` |

Candidate shape:
```json
{"item_code": "F-0001", "report_type": "found", "item_name": "laptop bag", "category": "bag",
 "color": "black", "brand": "HP", "location": "CSE Block", "event_date": "2026-11-16",
 "current_holding": "at the admin office", "total_score": 0.93, "band": "strong_candidate",
 "breakdown": {"category": 1.0, "color": 1.0, "location": 1.0, "time": 1.0, "brand": 1.0},
 "explanation": "same category, same colour, same place, within about a day, same brand",
 "match_id": "uuid", "match_status": "candidate", "lost_code": "L-0001", "found_code": "F-0001"}
```
