"""Tool registry: confirmation guard, validation, injected identity, safe failures."""

from __future__ import annotations

from app.agent.tools import CreateLostArgs, ToolContext, _category, execute_tool
from tests.conftest import TODAY

VALID = {"category": "wallet", "item_name": "wallet", "description": "brown leather wallet",
         "location": "canteen", "lost_date": "yesterday"}


def ctx(user, message: str) -> ToolContext:
    return ToolContext(user=user, session_id="00000000-0000-0000-0000-000000000000",
                       last_user_message=message, today=TODAY)


def test_create_refused_without_user_confirmation(student) -> None:
    out, err = execute_tool("create_lost_report", VALID, ctx(student, "I lost my wallet in the canteen"))
    assert out["error"] == "not_confirmed" and err


def test_create_after_yes_reaches_the_database_layer(student) -> None:
    # The test settings have no DATABASE_URL: it must fail safely, never pretend it saved.
    out, err = execute_tool("create_lost_report", VALID, ctx(student, "yes"))
    assert out["error"] == "database_not_configured"
    assert "code" not in out


def test_invalid_input_returns_error_result(student) -> None:
    out, _ = execute_tool("create_lost_report", {"category": "wallet"}, ctx(student, "yes"))
    assert out["error"] == "invalid_input"


def test_unparseable_date_is_an_error(student) -> None:
    out, _ = execute_tool("create_lost_report", {**VALID, "lost_date": "sometime"}, ctx(student, "yes"))
    assert "Could not understand the date" in out["message"]


def test_unknown_location_is_an_error(student) -> None:
    out, _ = execute_tool("create_lost_report", {**VALID, "location": "Mars"}, ctx(student, "yes"))
    assert "Unknown campus location" in out["message"]


def test_unknown_tool(student) -> None:
    out, err = execute_tool("delete_all_reports", {}, ctx(student, "yes"))
    assert out["error"] == "unknown_tool" and err


def test_model_supplied_identity_is_ignored() -> None:
    args = CreateLostArgs(**VALID, user_id="someone-else", role="admin")
    dumped = args.model_dump()
    assert "user_id" not in dumped and "role" not in dumped


def test_category_normalisation() -> None:
    assert _category("Electronics") == "electronics"
    assert _category("jewelry") == "jewellery"
    assert _category("gadget", "laptop bag") == "bag"
    assert _category(None, "mystery thing") == "other"
