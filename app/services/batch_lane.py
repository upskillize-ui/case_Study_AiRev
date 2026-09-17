"""The night lane — Claude calls at half price when nobody is waiting.

Anthropic's Message Batches API bills every request in a batch at 50 % of
the live price and answers within minutes to a few hours. The 3-hourly
sweep is exactly that shape: a queue drained after the fact, with no
student waiting on the other end. A Grade-all pressed by an admin, or a
student's own re-attempt, stays on the live path — those are the calls
where minutes matter.

HOW IT PLUGS IN. `create_message` (ai_service) asks `active()` before it
touches the provider chain. When the sweep's worker is inside
`night_lane()`, the request is handed to the collector below instead: the
collector gathers requests from all the worker's threads for a few
seconds, submits them as ONE batch to the official API, polls until the
batch ends, and hands each result back to its caller. The caller blocks
meanwhile, exactly as it would on a live call — nothing above this module
changes shape. Anything the batch cannot deliver (a failed request, a
timeout, no direct key) raises BatchLaneError, and create_message falls
through to the live chain: a review never fails BECAUSE of the night lane.

The gateway carries no batch endpoint, so the night lane always goes to
api.anthropic.com directly — with the 50 % off, that is still the cheaper
seat by a distance.

Flags (Space variables):
  REVIEW_BATCH_LANE=1          turn the lane on (default off)
  BATCH_LANE_CONCURRENCY=4     sweep worker threads while the lane is on
  BATCH_FLUSH_SECONDS=10       how long the collector waits to fill a batch
  BATCH_MAX_REQUESTS=50        submit when this many are waiting
  BATCH_POLL_SECONDS=15        how often an open batch is checked
  BATCH_MAX_WAIT_SECONDS=1800  give up on a batch after this (then live)
"""
from __future__ import annotations

import contextvars
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Optional

_OFF = ("", "0", "false", "off", "no")

_lane: contextvars.ContextVar = contextvars.ContextVar("airev_night_lane", default=False)


class BatchLaneError(RuntimeError):
    """The night lane could not deliver this request; go live."""


def enabled() -> bool:
    return os.getenv("REVIEW_BATCH_LANE", "").strip().lower() not in _OFF


def concurrency() -> int:
    """Worker threads for a night-lane job. Waiting on a batch costs no CPU,
    so the lane can hold more rows open than the live worker's two — but the
    intake BEFORE the call (OCR, a rendered link, video frames) still runs
    on the Space's own CPU, and 3 of those at once restarted cpu-basic on
    21 Aug. Four is the cautious default; raise it with the hardware."""
    return max(1, int(os.getenv("BATCH_LANE_CONCURRENCY", "4")))


def active() -> bool:
    """Is the CURRENT thread inside night_lane() with the lane switched on?"""
    return enabled() and bool(_lane.get())


@contextmanager
def night_lane(on: bool = True):
    """Mark every Claude call made inside this block as night-lane work."""
    token = _lane.set(bool(on))
    try:
        yield
    finally:
        _lane.reset(token)


# ---------------------------------------------------------------------------
# The collector: many callers, one batch.
# ---------------------------------------------------------------------------

class _Pending:
    __slots__ = ("kwargs", "done", "result", "error", "born")

    def __init__(self, kwargs: dict):
        self.kwargs = kwargs
        self.done = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.born = time.monotonic()


class Collector:
    """Gathers requests for a few seconds and submits them as one batch.

    `client_factory` returns an Anthropic SDK client for the official API;
    `sleeper` is injectable for tests. One collector per process.
    """

    def __init__(self, client_factory: Callable[[], Any],
                 sleeper: Callable[[float], None] = time.sleep):
        self._client_factory = client_factory
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._queue: list = []
        self._wake = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.batches_submitted = 0
        self.requests_submitted = 0

    # -- settings read per call so a Space variable change needs no restart
    @staticmethod
    def _flush_seconds() -> float:
        return float(os.getenv("BATCH_FLUSH_SECONDS", "10"))

    @staticmethod
    def _max_requests() -> int:
        return max(1, int(os.getenv("BATCH_MAX_REQUESTS", "50")))

    @staticmethod
    def _poll_seconds() -> float:
        return float(os.getenv("BATCH_POLL_SECONDS", "15"))

    @staticmethod
    def _max_wait() -> float:
        return float(os.getenv("BATCH_MAX_WAIT_SECONDS", "1800"))

    def submit(self, kwargs: dict) -> Any:
        """Queue one request and block until its result is back. Raises
        BatchLaneError when the batch could not deliver it."""
        item = _Pending(kwargs)
        with self._lock:
            self._queue.append(item)
            self._ensure_thread()
        self._wake.set()
        # A request can wait through one whole batch (the collector submits
        # serially) and then its own: two wait limits, not one. Giving up
        # early would go live while the batch still delivers — paying twice.
        if not item.done.wait(2 * self._max_wait() + 2 * self._flush_seconds() + 5):
            raise BatchLaneError("night lane: no result within the wait limit")
        if item.error is not None:
            raise BatchLaneError(f"night lane: {item.error}") from item.error
        return item.result

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name="night-lane", daemon=True)
            self._thread.start()

    def _take_ready(self) -> list:
        """The requests due for submission: a full batch, or an oldest one
        that has waited the flush window."""
        with self._lock:
            if not self._queue:
                return []
            oldest = min(p.born for p in self._queue)
            if (len(self._queue) < self._max_requests()
                    and time.monotonic() - oldest < self._flush_seconds()):
                return []
            ready, self._queue = self._queue[:self._max_requests()], self._queue[self._max_requests():]
            return ready

    def _loop(self) -> None:
        while True:
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            ready = self._take_ready()
            if ready:
                self._run_batch(ready)
            with self._lock:
                idle = not self._queue
            if idle:
                # Nothing waiting: sleep until a submit wakes us. The thread
                # is kept so a late-night trickle does not respawn it per row.
                self._wake.wait(timeout=30.0)

    def _run_batch(self, items: list) -> None:
        """Submit, poll, distribute. Every item ends with a result or an error."""
        by_id = {f"r{i}": p for i, p in enumerate(items)}
        try:
            client = self._client_factory()
            batch = client.messages.batches.create(requests=[
                {"custom_id": cid, "params": p.kwargs} for cid, p in by_id.items()])
            self.batches_submitted += 1
            self.requests_submitted += len(items)
            print(f"[BATCH] submitted {len(items)} request(s) as {batch.id} — "
                  f"half price, polling every {self._poll_seconds():.0f}s")
            started = time.monotonic()
            while getattr(batch, "processing_status", "") != "ended":
                if time.monotonic() - started > self._max_wait():
                    raise BatchLaneError(f"batch {batch.id} still open after "
                                         f"{self._max_wait():.0f}s")
                self._sleep(self._poll_seconds())
                batch = client.messages.batches.retrieve(batch.id)
            delivered = 0
            for entry in client.messages.batches.results(batch.id):
                item = by_id.get(getattr(entry, "custom_id", ""))
                if item is None:
                    continue
                result = getattr(entry, "result", None)
                if getattr(result, "type", "") == "succeeded":
                    item.result = result.message
                    delivered += 1
                else:
                    detail = getattr(result, "error", None) or getattr(result, "type", "no result")
                    item.error = BatchLaneError(f"request {entry.custom_id}: {detail}")
                item.done.set()
            print(f"[BATCH] {batch.id} ended in {time.monotonic() - started:.0f}s — "
                  f"{delivered}/{len(items)} delivered")
        except BaseException as exc:  # noqa: BLE001 — every waiter must be released
            for item in items:
                if not item.done.is_set():
                    item.error = exc
                    item.done.set()
        finally:
            for item in items:            # belt and braces: nobody waits for ever
                item.done.set()


_collector: Optional[Collector] = None
_collector_lock = threading.Lock()


def _direct_client():
    """An SDK client for api.anthropic.com — the only host with batches."""
    from app.services import ai_service
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise BatchLaneError("no ANTHROPIC_API_KEY for the night lane")
    return ai_service._client_for(ai_service.Provider(
        "anthropic", key, os.getenv("ANTHROPIC_BASE_URL", "").strip(),
        bearer_auth=False, strict=True))


def collector() -> Collector:
    global _collector
    with _collector_lock:
        if _collector is None:
            _collector = Collector(_direct_client)
        return _collector


def submit(kwargs: dict) -> Any:
    """One Claude request through the night lane. Raises BatchLaneError."""
    return collector().submit(kwargs)


def snapshot() -> dict:
    """For /health: what the lane has sent since boot."""
    c = _collector
    return {"enabled": enabled(),
            "batches": c.batches_submitted if c else 0,
            "requests": c.requests_submitted if c else 0}
