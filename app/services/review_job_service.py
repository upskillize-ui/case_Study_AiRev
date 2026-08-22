# app/services/review_job_service.py
# ---------------------------------------------------------------------------
# UNATTENDED BATCH REVIEW — start it once, walk away, come back to marks.
#
# WHY THIS EXISTS. Grading a cohort has been driven from a laptop: a script
# firing hundreds of HTTP requests, six at a time, each one holding a
# connection open for the 25-65 seconds a review with vision OCR takes. On
# 17 Aug the first six requests of a 446-row batch all timed out together —
# every worker had hit a cold Space and was independently deriving the same
# rubric and building the same knowledge pack. Nothing was wrong with the
# students' work or with the marker. The BATCH was the wrong shape.
#
# A review is slow, stateful and cache-warming. That is a queue's job, not a
# request's. So: the caller enqueues and gets an id back in milliseconds, one
# worker drains the queue inside the Space, and progress is a cheap GET.
#
# SAFETY, in the order it matters:
#
#   1. FEATURE FLAGGED. Without REVIEW_JOBS_ENABLED the routes refuse and no
#      thread starts. Shipping this cannot change anything that works today.
#   2. ADDITIVE ONLY. Two new tables, no change to any existing one. Nothing
#      here writes to a submission — it calls the SAME regrade path the
#      correction runs already use, which UPDATEs in place, never INSERTs, and
#      refuses to overwrite a faculty grade.
#   3. ONE WORKER. Serial by design. That is not a limitation to fix later —
#      it is what keeps the rubric and knowledge-pack caches warm and stops the
#      thundering herd that broke the laptop-driven run.
#   4. SURVIVES RESTART. HF Spaces restart on their own. Item state lives in
#      the database, so a job resumes where it stopped instead of re-marking
#      work that was already done.
#   5. CANNOT TAKE THE APP DOWN. The worker is a daemon thread whose loop
#      catches everything; a failing item is recorded and the queue moves on.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from app.database import tquery, texecute

JOBS_TABLE = "review_jobs"
ITEMS_TABLE = "review_job_items"

# Consecutive failures that mean something systemic is wrong — a dead provider,
# an expired key — rather than one bad submission. Stop and say so rather than
# burning the remaining cohort against a wall.
MAX_CONSECUTIVE_FAILURES = int(os.getenv("REVIEW_JOB_MAX_FAILS", "8"))

# Breathing room between reviews. Small, but it keeps the Space responsive to
# /health and to the learners using it while a cohort is grading.
PAUSE_SECONDS = float(os.getenv("REVIEW_JOB_PAUSE", "0.5"))

# Reviews in flight at once AFTER the warm-up item. 2 is the ceiling the
# 21 Aug sweeps proved on cpu-basic hardware — 3 concurrent OCR-heavy reviews
# restarted the Space mid-cohort. Raise this env only after upgrading the
# Space hardware, never speculatively.
WORKER_CONCURRENCY = max(1, int(os.getenv("REVIEW_JOB_CONCURRENCY", "2")))


def jobs_enabled() -> bool:
    """The flag. Absent means this whole subsystem is inert."""
    return os.getenv("REVIEW_JOBS_ENABLED", "").strip().lower() in ("1", "true", "yes")


# ─── pure logic (no I/O, so it is testable without a database) ───────────────

@dataclass(frozen=True)
class Progress:
    total: int
    done: int
    failed: int
    skipped: int

    @property
    def pending(self) -> int:
        return max(0, self.total - self.done - self.failed - self.skipped)

    @property
    def finished(self) -> bool:
        return self.pending == 0

    @property
    def percent(self) -> int:
        return 100 if not self.total else round(
            100 * (self.done + self.failed + self.skipped) / self.total)


def should_abort(consecutive_failures: int,
                 limit: int = MAX_CONSECUTIVE_FAILURES) -> bool:
    """Is this a broken system rather than a bad submission? Pure.

    One review failing is ordinary. Eight in a row is a dead provider or an
    expired key, and continuing would spend the rest of the cohort's budget
    discovering that repeatedly.
    """
    return consecutive_failures >= limit


def next_item(items: list) -> Optional[dict]:
    """The next row to review: first pending, in order. Pure.

    Order matters for cost. Items are enqueued grouped by assignment so the
    rubric and knowledge pack — identical for every learner on one item — stay
    in the prompt cache. Interleaving assignments misses that cache every time.
    """
    for row in items or []:
        if (row or {}).get("state") == "pending":
            return row
    return None


# ─── schema ─────────────────────────────────────────────────────────────────

_tables_ready: set = set()


def ensure_tables(tenant) -> None:
    """Create the two job tables. Additive: touches nothing that exists."""
    key = getattr(tenant, "id", str(tenant))
    if key in _tables_ready:
        return
    texecute(tenant, f"""
        CREATE TABLE IF NOT EXISTS {JOBS_TABLE} (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            scope_type    VARCHAR(32)  NOT NULL,
            state         VARCHAR(16)  NOT NULL DEFAULT 'running',
            note          VARCHAR(255) NOT NULL DEFAULT '',
            created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
            updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP
                                       ON UPDATE CURRENT_TIMESTAMP
        )
    """)
    texecute(tenant, f"""
        CREATE TABLE IF NOT EXISTS {ITEMS_TABLE} (
            id            INT AUTO_INCREMENT PRIMARY KEY,
            job_id        INT          NOT NULL,
            scope_id      INT          NOT NULL,
            submission_id INT          NOT NULL,
            state         VARCHAR(16)  NOT NULL DEFAULT 'pending',
            detail        VARCHAR(255) NOT NULL DEFAULT '',
            score         FLOAT        NULL,
            updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP
                                       ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_job_state (job_id, state)
        )
    """)
    _tables_ready.add(key)


# ─── job lifecycle ──────────────────────────────────────────────────────────

def create_job(tenant, scope_type: str, submission_ids: list, note: str = "") -> int:
    """Enqueue a batch. Returns the job id. Writes no marks."""
    ensure_tables(tenant)
    texecute(tenant, f"INSERT INTO {JOBS_TABLE} (scope_type, note) VALUES (%s, %s)",
             (scope_type, note[:255]))
    rows = tquery(tenant, f"SELECT MAX(id) AS id FROM {JOBS_TABLE}")
    job_id = int(rows[0]["id"])
    for scope_id, submission_id in submission_ids:
        texecute(tenant,
                 f"INSERT INTO {ITEMS_TABLE} (job_id, scope_id, submission_id) "
                 f"VALUES (%s, %s, %s)", (job_id, int(scope_id), int(submission_id)))
    return job_id


def job_items(tenant, job_id: int) -> list:
    ensure_tables(tenant)
    return list(tquery(
        tenant,
        f"SELECT id, scope_id, submission_id, state, detail, score "
        f"FROM {ITEMS_TABLE} WHERE job_id = %s ORDER BY scope_id, id",
        (job_id,)) or [])


def progress(tenant, job_id: int) -> Progress:
    items = job_items(tenant, job_id)
    return Progress(
        total=len(items),
        done=sum(1 for i in items if i["state"] == "done"),
        failed=sum(1 for i in items if i["state"] == "failed"),
        skipped=sum(1 for i in items if i["state"] == "skipped"),
    )


def mark_item(tenant, item_id: int, state: str, detail: str = "",
              score=None) -> None:
    texecute(tenant,
             f"UPDATE {ITEMS_TABLE} SET state = %s, detail = %s, score = %s "
             f"WHERE id = %s", (state, str(detail)[:255], score, item_id))


def set_job_state(tenant, job_id: int, state: str, note: str = "") -> None:
    texecute(tenant, f"UPDATE {JOBS_TABLE} SET state = %s, note = %s WHERE id = %s",
             (state, note[:255], job_id))


def running_jobs(tenant) -> list:
    ensure_tables(tenant)
    return list(tquery(
        tenant, f"SELECT id, scope_type, state, note FROM {JOBS_TABLE} "
                f"WHERE state = 'running' ORDER BY id") or [])


# ─── the worker ─────────────────────────────────────────────────────────────

_worker_lock = threading.Lock()
_worker_running = False


def worker_is_running() -> bool:
    return _worker_running


def _run_one(tenant, item: dict, review_one: Callable) -> str:
    """Review one item, record the outcome, return the state. Never raises —
    a failing row is data for the abort counter, not a queue-stopper."""
    try:
        state, detail, score = review_one(item["scope_id"], item["submission_id"])
    except Exception as e:
        state, detail, score = "failed", f"{type(e).__name__}: {e}"[:255], None
    mark_item(tenant, item["id"], state, detail, score)
    return state


def _finalize(tenant, job_id: int) -> Progress:
    """Write the job's closing state from what the items actually say."""
    final = progress(tenant, job_id)
    if not final.finished:
        set_job_state(tenant, job_id, "aborted",
                      f"stopped with {final.pending} still pending — the queue "
                      f"is not draining, look at the Space log")
    else:
        set_job_state(tenant, job_id, "finished",
                      f"{final.done} reviewed, {final.skipped} skipped, "
                      f"{final.failed} failed")
    return final


def drain(tenant, job_id: int, review_one: Callable, pause: float = PAUSE_SECONDS,
          sleeper: Callable = time.sleep) -> Progress:
    """Review every pending item in this job, one at a time.

    review_one(scope_id, submission_id) -> (state, detail, score)

    Serial on purpose: the rubric and knowledge pack for an item are built by
    the first review and reused by every one after it. Parallel workers on a
    cold cache each build their own, which is what timed out the laptop-driven
    run. `sleeper` is injected so tests do not actually wait.
    """
    consecutive = 0

    # HARD BOUND on the loop. Found by mutation testing: break next_item and
    # drain() spins forever, re-reviewing the same row and spending real money
    # unattended with nobody watching. The queue is finite by construction, so
    # the loop must be too — one pass per item, plus a small margin for a row
    # legitimately re-queued. Belt and braces on the `is None` exit, because
    # this thread runs for hours with no human near it.
    budget = len(job_items(tenant, job_id)) + 2

    while budget > 0:
        budget -= 1
        item = next_item(job_items(tenant, job_id))
        if item is None:
            break

        state = _run_one(tenant, item, review_one)
        consecutive = consecutive + 1 if state == "failed" else 0

        if should_abort(consecutive):
            set_job_state(tenant, job_id, "aborted",
                          f"stopped after {consecutive} consecutive failures — "
                          f"check the provider key and the Space log")
            return progress(tenant, job_id)

        if pause:
            sleeper(pause)

    return _finalize(tenant, job_id)


def drain_parallel(tenant, job_id: int, review_one: Callable,
                   workers: Optional[int] = None, pause: float = PAUSE_SECONDS,
                   sleeper: Callable = time.sleep) -> Progress:
    """drain() with a small pool — the shape the 21 Aug sweeps proved by hand.

    The FIRST item still runs alone: it derives the rubric and builds the
    knowledge pack that every later review of the same assignment reuses, so
    starting parallel on a cold cache would rebuild them N times (the exact
    thundering herd drain()'s docstring records). After that warm-up, up to
    `workers` reviews run at once from a shared cursor. workers<=1 is drain()
    exactly.

    The consecutive-failure abort survives the pool: a shared counter behind a
    lock, an Event the threads check before taking the next row. Eight dead
    provider calls stop every thread, not just the one that saw them.
    """
    workers = WORKER_CONCURRENCY if workers is None else workers
    if workers <= 1:
        return drain(tenant, job_id, review_one, pause, sleeper)

    pending = [i for i in job_items(tenant, job_id) if i["state"] == "pending"]
    if not pending:
        return _finalize(tenant, job_id)

    first_state = _run_one(tenant, pending[0], review_one)   # cache warm-up
    rest = pending[1:]

    lock = threading.Lock()
    shared = {"next": 0, "consecutive": 1 if first_state == "failed" else 0}
    stop = threading.Event()

    def take() -> Optional[dict]:
        with lock:
            if stop.is_set() or shared["next"] >= len(rest):
                return None
            item = rest[shared["next"]]
            shared["next"] += 1
            return item

    def work() -> None:
        while True:
            item = take()
            if item is None:
                return
            state = _run_one(tenant, item, review_one)
            with lock:
                shared["consecutive"] = (shared["consecutive"] + 1
                                         if state == "failed" else 0)
                if should_abort(shared["consecutive"]):
                    stop.set()
            if pause:
                sleeper(pause)

    threads = [threading.Thread(target=work, name=f"review-job-w{n}", daemon=True)
               for n in range(min(workers, max(1, len(rest))))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    if stop.is_set():
        set_job_state(tenant, job_id, "aborted",
                      f"stopped after {shared['consecutive']} consecutive "
                      f"failures — check the provider key and the Space log")
        return progress(tenant, job_id)
    return _finalize(tenant, job_id)


def start_worker(tenant, job_id: int, review_one: Callable) -> bool:
    """Run drain() on a daemon thread. Returns False if one is already going.

    ONE at a time, process-wide. Two workers would race on the same rows and
    reintroduce exactly the contention this exists to remove.
    """
    global _worker_running
    with _worker_lock:
        if _worker_running:
            return False
        _worker_running = True

    def _run():
        global _worker_running
        try:
            print(f"[JOB {job_id}] worker started "
                  f"(concurrency {WORKER_CONCURRENCY})")
            final = drain_parallel(tenant, job_id, review_one)
            print(f"[JOB {job_id}] finished — {final.done} reviewed, "
                  f"{final.skipped} skipped, {final.failed} failed")
        except Exception as e:
            print(f"[JOB {job_id}] worker crashed: {type(e).__name__}: {e}")
            try:
                set_job_state(tenant, job_id, "aborted", f"worker crashed: {e}")
            except Exception:
                pass
        finally:
            with _worker_lock:
                _worker_running = False

    threading.Thread(target=_run, name=f"review-job-{job_id}", daemon=True).start()
    return True
