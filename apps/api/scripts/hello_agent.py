"""Phase 1 learning script: the smallest possible Claude tool-calling loop.

Run from the apps/api folder (with the venv active):
    python scripts/hello_agent.py

What it shows:
  1. We send Claude a question plus the description of ONE tool.
  2. Claude replies with stop_reason == "tool_use": "please run lookup_location".
  3. OUR Python code runs the tool and sends back a tool_result with the same id.
  4. Claude reads the result and answers with stop_reason == "end_turn".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

# Let "from app.config import settings" work when this file is run directly.
# (scripts/ is one level below apps/api, which contains the "app" package.)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import anthropic  # noqa: E402

from app.config import settings  # noqa: E402

QUESTION = "Which zone is the Central Library in?"

# ---------------------------------------------------------------------------
# 1. The tool's real implementation: plain Python, no AI involved.
# ---------------------------------------------------------------------------
CAMPUS_LOCATIONS: dict[str, str] = {  # PROJECT_GUIDE.md Section 22
    "CSE Block": "academic",
    "Mech Block": "academic",
    "Civil Block": "academic",
    "E&TC Block": "academic",
    "Workshop": "academic",
    "Central Library": "academic",
    "Auditorium": "common",
    "Main Canteen": "common",
    "Admin Building": "common",
    "Bus Stop": "common",
    "Parking Lot": "common",
    "Sports Ground": "sports",
    "Boys Hostel A": "hostel",
    "Girls Hostel B": "hostel",
}


def lookup_location(name: str) -> dict[str, Any]:
    """Return the zone for a campus location (case-insensitive)."""
    wanted = name.strip().lower()
    for location, zone in CAMPUS_LOCATIONS.items():
        if location.lower() == wanted:
            return {"name": location, "zone": zone}
    return {"error": f"Unknown location: {name!r}", "known_locations": list(CAMPUS_LOCATIONS)}


# ---------------------------------------------------------------------------
# 2. The tool's description: this is ALL the model sees. It never sees the code above.
# ---------------------------------------------------------------------------
LOOKUP_LOCATION_TOOL: dict[str, Any] = {
    "name": "lookup_location",
    "description": (
        "Look up which zone (academic, common, sports, hostel) a campus location belongs to. "
        "Use this whenever the user asks about a location's zone. Returns {name, zone}, "
        "or an error with the list of known locations."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Campus location name, e.g. 'CSE Block'"},
        },
        "required": ["name"],
    },
}

# Registry: tool name (what the model asks for) -> Python function (what we run).
TOOL_FUNCS = {"lookup_location": lookup_location}


def run_tool(block: Any) -> dict[str, Any]:
    """Execute one tool_use block and build the matching tool_result block."""
    print(f"  -> tool_use: {block.name}({json.dumps(block.input)})")
    func = TOOL_FUNCS.get(block.name)
    try:
        output = func(**block.input) if func else {"error": f"Unknown tool: {block.name}"}
    except Exception as exc:  # never crash the loop; report the error to the model
        output = {"error": str(exc)}
    print(f"  <- tool_result: {json.dumps(output)}")
    return {
        "type": "tool_result",
        "tool_use_id": block.id,  # MUST match the id of the tool_use it answers
        "content": json.dumps(output),
        "is_error": "error" in output,
    }


# ---------------------------------------------------------------------------
# 3. The agent loop.
# ---------------------------------------------------------------------------
def run_agent(client: anthropic.Anthropic, question: str) -> anthropic.types.Message:
    # The API is stateless: we keep the whole conversation here and resend it every call.
    messages: list[dict[str, Any]] = [{"role": "user", "content": question}]

    for step in range(1, settings.AGENT_MAX_STEPS + 1):
        response = client.messages.create(
            model=settings.LLM_MODEL,
            max_tokens=4096,  # room for the model's built-in thinking + the answer
            tools=[LOOKUP_LOCATION_TOOL],
            messages=messages,
        )
        print(f"[step {step}] stop_reason: {response.stop_reason}")

        # Append the assistant turn EXACTLY as returned (thinking + text + tool_use blocks).
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            return response  # end_turn (done), or max_tokens / refusal (handled by caller)

        # One tool_result for EVERY tool_use, all together in the next user message.
        results = [run_tool(b) for b in response.content if b.type == "tool_use"]
        messages.append({"role": "user", "content": results})

    raise RuntimeError(f"Stopped after AGENT_MAX_STEPS={settings.AGENT_MAX_STEPS} steps.")


def main() -> int:
    if not settings.ANTHROPIC_API_KEY:
        print("ANTHROPIC_API_KEY is empty. Copy .env.example to apps/api/.env and paste your key.")
        return 1

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
    print(f"Model: {settings.LLM_MODEL}")
    print(f"User:  {QUESTION}\n")

    try:
        response = run_agent(client, QUESTION)
    except anthropic.AuthenticationError:
        print("401: the API key was rejected. Check ANTHROPIC_API_KEY in apps/api/.env.")
        return 1
    except anthropic.NotFoundError:
        print(f"404: model {settings.LLM_MODEL!r} not found. Check LLM_MODEL in apps/api/.env.")
        return 1
    except anthropic.RateLimitError:
        print("429: rate limited. Wait a minute and try again.")
        return 1
    except anthropic.APIStatusError as exc:
        print(f"API error {exc.status_code}: {exc.message}")
        print("(A 400 about 'credit balance' means you need to add API credits in the Console.)")
        return 1
    except anthropic.APIConnectionError:
        print("Could not reach the Anthropic API. Check your internet connection.")
        return 1
    except RuntimeError as exc:
        print(exc)
        return 1

    if response.stop_reason == "refusal":
        print("\nThe model declined to answer this request.")
        return 1
    if response.stop_reason == "max_tokens":
        print("\nThe reply was cut off by max_tokens. Raise max_tokens and try again.")

    answer = "".join(b.text for b in response.content if b.type == "text")
    print(f"\nAgent: {answer}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
