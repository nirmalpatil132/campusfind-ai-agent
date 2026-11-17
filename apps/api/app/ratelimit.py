"""Lightweight in-memory rate limiting (no Redis, no paid service).

How it works (a "sliding window log"): for every key (e.g. "chat:<user id>") we keep the times of
recent requests. A new request is refused with HTTP 429 + a Retry-After header if the window
already holds `count` requests.

Limits are strings from settings, e.g. "10/minute;200/day" (both must pass).

LIMITATION (documented on purpose): the counters live in this server process's memory.
They are NOT shared between several worker processes or several server instances, and they reset
when the server restarts. That is fine for our single Render instance; a multi-server deployment
would need a shared store such as Redis.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from fastapi import Depends, HTTPException

from app.auth import CurrentUser, get_current_user
from app.config import settings

_PERIODS = {"second": 1.0, "minute": 60.0, "hour": 3600.0, "day": 86400.0}
TOO_MANY = "Too many requests. Please try again shortly."


def parse_limits(spec: str) -> list[tuple[int, float]]:
    """'10/minute;200/day' -> [(10, 60.0), (200, 86400.0)]. Bad parts are ignored."""
    limits = []
    for part in (spec or "").split(";"):
        count, _, period = part.strip().partition("/")
        period = period.strip().lower().rstrip("s")
        if count.strip().isdigit() and period in _PERIODS and int(count) > 0:
            limits.append((int(count), _PERIODS[period]))
    return limits


class RateLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._last_sweep = clock()

    def hit(self, key: str, spec: str) -> float | None:
        """Record one request. Returns None if allowed, else seconds until the next one is allowed."""
        limits = parse_limits(spec)
        if not limits:
            return None
        longest = max(window for _, window in limits)
        now = self._clock()
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and now - q[0] >= longest:
                q.popleft()
            for count, window in limits:
                recent = [t for t in q if now - t < window]
                if len(recent) >= count:
                    return max(1.0, window - (now - recent[-count]))
            q.append(now)
            if now - self._last_sweep > 600:  # forget idle keys so memory can't grow forever
                for k in [k for k, v in self._hits.items() if not v or now - v[-1] >= longest]:
                    del self._hits[k]
                self._last_sweep = now
        return None

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def check(key: str, spec: str) -> None:
    """Raise HTTP 429 (with Retry-After) if `key` is over its limit."""
    if not settings.RATE_LIMIT_ENABLED:
        return
    retry_after = limiter.hit(key, spec)
    if retry_after is not None:
        raise HTTPException(status_code=429, detail=TOO_MANY,
                            headers={"Retry-After": str(math.ceil(retry_after))})


def limit(bucket: str) -> Callable[..., None]:
    """FastAPI dependency: per-USER limit for one bucket (chat / reports / search / admin).

    The user comes from the verified login token, so it can't be faked by changing a request field.
    """
    setting = f"RATE_LIMIT_{bucket.upper()}"

    def dependency(user: CurrentUser = Depends(get_current_user)) -> None:
        check(f"{bucket}:{user.id}", getattr(settings, setting))

    dependency.__name__ = f"rate_limit_{bucket}"
    return dependency
