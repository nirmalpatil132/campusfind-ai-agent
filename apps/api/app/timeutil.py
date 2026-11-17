"""Campus-local time helpers. The college is in Asia/Kolkata, whatever the server's timezone."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

CAMPUS_TZ = ZoneInfo("Asia/Kolkata")  # needs the `tzdata` package on Windows


def now_ist() -> datetime:
    return datetime.now(CAMPUS_TZ)


def today_ist() -> date:
    return now_ist().date()
