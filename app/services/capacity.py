# app/services/capacity.py
# Live-session capacity governor.
#
# A review is a heavy, multi-second LLM operation. To protect response quality
# and the upstream API, AiRev admits a bounded number of CONCURRENT reviews
# (LIVE_CAPACITY, default 100). Beyond that ceiling, additional students are
# turned away immediately with a courteous, reassuring notice rather than being
# left to time out — the agent stays responsive under load instead of degrading
# for everyone.
#
# Design: an async counter guarded by a lock (asyncio.Semaphore lacks a clean
# non-blocking acquire). Applied ONLY to the four heavy submit endpoints via a
# FastAPI dependency — light GET/list calls are never gated. On saturation the
# dependency raises CapacityFull, which the app-level handler renders as a
# structured, frontend-recognized "busy" response (blocked="capacity") plus a
# proper HTTP 503 + Retry-After for any non-UI caller.

import asyncio
import os

MAX_CONCURRENT = int(os.getenv("LIVE_CAPACITY", "100"))
RETRY_AFTER_SECONDS = int(os.getenv("LIVE_RETRY_AFTER", "60"))

_active = 0
_peak = 0
_rejected = 0
_lock = asyncio.Lock()

# Formal, reassuring copy shown to a student when all sessions are engaged.
BUSY_MESSAGE = (
    "AiRev is at full capacity right now — every review session is currently "
    "engaged with another student. To keep every review thorough and fair, we "
    "run a fixed number of sessions at once rather than rushing them.\n\n"
    "Please try again in a minute or two. Your work is saved and nothing is "
    "lost — the moment a session frees up, your review will run in full. "
    "Thank you for your patience."
)


class CapacityFull(Exception):
    """Raised when the concurrent-review ceiling is reached."""


async def capacity_guard():
    """FastAPI dependency for heavy review endpoints. Admits the request if a
    slot is free (incrementing the live count) and releases it after the
    response; otherwise raises CapacityFull before the handler runs, so a
    rejected request spends zero AI tokens."""
    global _active, _peak, _rejected
    async with _lock:
        if _active >= MAX_CONCURRENT:
            _rejected += 1
            raise CapacityFull()
        _active += 1
        if _active > _peak:
            _peak = _active
    try:
        yield
    finally:
        async with _lock:
            _active -= 1


def snapshot() -> dict:
    """Observability for /health and the ops view."""
    return {
        "activeReviews": _active,
        "capacity": MAX_CONCURRENT,
        "peakReviews": _peak,
        "rejectedWhenFull": _rejected,
    }
