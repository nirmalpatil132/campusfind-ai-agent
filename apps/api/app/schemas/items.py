"""Pydantic models for item reports. Enums + max lengths are part of the security rules (§17)."""

from __future__ import annotations

from datetime import date, time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal["bag", "bottle", "electronics", "id_card", "wallet", "keys",
                   "book", "clothing", "jewellery", "stationery", "other"]
Holding = Literal["finder", "admin_office", "security"]
ReportType = Literal["lost", "found"]


class ReportBase(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    category: Category
    item_name: str = Field(min_length=1, max_length=80)
    color: str | None = Field(default=None, max_length=40)
    brand: str | None = Field(default=None, max_length=40)
    description: str = Field(min_length=1, max_length=1000)
    location: str = Field(min_length=1, max_length=80, description="A known campus location")
    event_date: date = Field(description="Date lost/found (YYYY-MM-DD)")
    event_time: time | None = Field(default=None, description="Approximate time (HH:MM)")


class LostReportIn(ReportBase):
    pass


class FoundReportIn(ReportBase):
    current_holding: Holding | None = Field(default=None, description="Where the item is now")
    verification_hint: str | None = Field(
        default=None, max_length=300,
        description="PRIVATE detail only the real owner would know. Shown to admins only.")
