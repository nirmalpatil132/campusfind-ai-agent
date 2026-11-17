"""Request bodies for the HTTP API (max lengths per §17)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _Body(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")


class ChatRequest(_Body):
    session_id: UUID | None = Field(default=None, description="Omit to start a new chat session")
    message: str = Field(min_length=1, max_length=2000)


class MatchSearchRequest(_Body):
    item_code: str = Field(min_length=2, max_length=12)
    limit: int = Field(default=5, ge=1, le=10)


class ConfirmRequest(_Body):
    answer: str = Field(min_length=3, max_length=500,
                        description="A detail only the real owner would know")


class AdminNote(_Body):
    note: str | None = Field(default=None, max_length=300)


class RejectRequest(_Body):
    reason: str = Field(min_length=3, max_length=300)
