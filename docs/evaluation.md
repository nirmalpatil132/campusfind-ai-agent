# Evaluation

Targets come from PROJECT_GUIDE.md §20. Every number below came from an actual run. Nothing is estimated.

## How the matcher was tested

`apps/api/scripts/evaluate_matching.py` scores the §22 dataset offline:

- **Data:** 20 lost reports, 20 found reports and 5 duplicates.
- **Code:** the live scoring functions in `app/services/matching.py`, plus a Python copy of the Stage-1 SQL filter and the duplicate rule.
- **Database:** not needed.

### Semantic-signal fallback (no paid embeddings)

Semantic similarity normally has a weight of 0.30, and it needs embeddings. When no embedding is available, that signal is dropped. This happens when Ollama isn't running or `nomic-embed-text` isn't pulled.

The other five weights are then divided by their sum (0.70), so they still add up to 1.0:

| Signal | Normal weight | Fallback weight |
|---|---|---|
| Semantic | 0.30 | (not used) |
| Category | 0.20 | 0.286 |
| Colour | 0.15 | 0.214 |
| Location | 0.15 | 0.214 |
| Time | 0.10 | 0.143 |
| Brand | 0.10 | 0.143 |

Every candidate's breakdown records `semantic_used`, so the API and logs show which formula produced it.

## Results v1: structured signals only (run 17 Nov 2026)

| Metric | Result | Target | Status |
|---|---|---|---|
| Recall@3 on 10 true pairs | **10/10** | ≥ 8/10 | ✅ |
| Trap pairs scoring ≥ 0.55 | **2/5** (L13–F13, L14–F14) | ≤ 1/5 | ❌ |
| Duplicates flagged | **5/5** | ≥ 4/5 | ✅ |
| L01 ranks F01 above F11 | **tie at 1.0** | F01 first | ❌ |

### Observations

1. **L01 tie.** F01 and F11 are both black HP laptop bags from the CSE Block on the same day, so every structured signal is 1.0. F01 also shares the "red keychain", but the +0.05 bonus is lost under the 1.0 cap. Both items end up with exactly 1.0.
2. **L14–F14 (umbrellas) = 0.692.** The pair has the same category and colour, but a different zone and is 8 days apart. Without embeddings, nothing penalises "wooden handle" vs "plastic handle".
3. **L13–F13 = 0.979.** The guide says this pair may pass. Owner verification catches it, because the years and departments on the two ID cards differ.

### Changes applied after v1 (approved by the team on 17 Nov 2026)

1. **Tie-break:** candidates are now sorted by `(total_score, rank_score)`, where `rank_score` is the score *before* the 1.0 cap. F01 (1.05, shared red keychain) now ranks above F11 (1.00). No weights changed.
2. **Stricter bar without embeddings:** with `semantic_used = false`, a pair needs ≥ 0.70 to be shown (it's still 0.55 with embeddings). Renormalising five signals by ÷0.70 inflates scores, so a stricter bar compensates. The lowest true pair is L10–F10 at 0.871, so recall is unaffected.

## Results v2: after the changes (run 17 Nov 2026)

| Metric | Result | Target | Status |
|---|---|---|---|
| Recall@3 on 10 true pairs | **10/10** | ≥ 8/10 | ✅ |
| Trap pairs shown as candidates | **1/5** (L13–F13 only, which the guide allows) | ≤ 1/5 | ✅ |
| Duplicates flagged | **5/5** | ≥ 4/5 | ✅ |
| L01 ranks F01 above F11 | **yes** | F01 first | ✅ |

L14–F14 (the umbrellas) still scores 0.692 but is now `hidden`. L13–F13 (0.979) is still shown; the owner's verification answer and the admin check catch it, as the guide intends.

## Agent tests (pytest)

`python -m pytest -q` in `apps/api`, run on 17 Nov 2026 after the v2 changes: **128 passed, 1 skipped**. The skipped test is the real-database test, which needs `RUN_DB_TESTS=1`.

| Area | File | What it proves |
|---|---|---|
| Intent | `test_intent.py` | 19 phrasings, including Hinglish, resolve to the right intent. Only a plain "yes" counts as confirmation |
| Extraction | `test_extractor.py` | Relative dates ("yesterday", "last Monday", "28 Sept"), times, locations (preferring the place after "in/at/near"), colours, digit masking |
| Rule engine | `test_rule_agent.py` | Asks one question at a time. Shows a summary and waits for "yes". "no" or "cancel" saves nothing. Gives up after 3 misses. Duplicates are not saved twice. The private hint never goes into the public description. Replies say "potentially matching" |
| LLM engine | `test_llm_agent.py` | Mocked Ollama: tool call → execution → tool result sent back → final answer. Max-steps cap. Missing model and unreachable server fall back to rules |
| Tools | `test_tools.py` | The confirmation guard blocks unconfirmed saves. A model-supplied `user_id`/`role` is ignored. Bad input gives an error result. No DB means a safe error, never a fake save |
| Matching | `test_matching.py` | Hand-calculated values: the §14 worked example (0.86), renormalisation (0.936), brand cap, time decay, colour-aware mark bonus |
| API | `test_api.py` | 401 without/with a bad token. 403 for students on admin routes. 503 with setup steps when there's no DB. 422 for a 2,001-character message |

## End-to-end test on the real Supabase database (17 Nov 2026)

`python scripts/e2e_check.py` in `apps/api` drives the real agent (rule engine, so the results repeat) and the real services against the live Supabase project, using the three real Auth accounts. Result: **44/44 checks passed.**

| Step | What was checked | Result |
|---|---|---|
| Database | Connection, all 11 tables, pgvector, 14 locations | ✅ |
| Auth | JWKS signing key reachable (ES256), and the student1/student2/admin profiles have the right roles | ✅ |
| Authorization (HTTP) | Student → 403 on `/api/admin/matches`, admin → 200 on `/api/admin/stats`, no token → 401 | ✅ |
| Lost report | Summary shown before saving; "yes" → `L-0001` saved with the right owner; matching ran | ✅ |
| Missing details | "I lost something" → asks a question, saves nothing | ✅ |
| Duplicate | The same bag reported again → not saved, the reply points to `L-0001` | ✅ |
| Found report | Asks where the item is now and for a private detail → `F-0001` | ✅ |
| Candidate | L-0001 ranked at **0.979** (`strong_candidate`); the reply says "potentially matching" | ✅ |
| Privacy | Private hint stored, but absent from the agent reply, the candidates and the owner's view | ✅ |
| Wrong user | The finder can't confirm the owner's match (Forbidden) | ✅ |
| Admin | Queue shows the owner's answer next to the private hint → verify → mark returned | ✅ |
| Final state | Both items `returned`, match `closed`, `admin_actions` has verify + return, both students notified, `agent_logs` rows carry `mode = rules` | ✅ |

The real-DB pytest (`RUN_DB_TESTS=1 python -m pytest tests/test_db_integration.py`) also passed (1 passed).

## Real local-LLM test (17 Nov 2026)

`qwen2.5:3b` on Ollama, on the demo laptop (i3-1115G4, 8 GB RAM, no GPU). The question "What reports have I made?" made the model choose the `get_my_reports` tool by itself and summarise the result correctly. It took **58 s**, including loading the model. Conclusion: LLM mode works but is slow on this CPU, so the timed demo uses rule mode and shows LLM mode once.

Not measured yet: §20 extraction accuracy and tool-selection scores over many prompts with the real LLM.
