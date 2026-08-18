"""The batch worker grades real coursework unattended, so its logic is proven
here before it is allowed near a cohort.

The laptop-driven run failed on 17 Aug because six workers hit a cold Space and
each independently derived the same rubric and built the same knowledge pack;
all six timed out. The fix is not a bigger timeout — it is one worker, serial,
warming the cache for the ones behind it. These tests pin that, and the four
safety properties that let it run without supervision:

    it cannot be started twice
    one bad row cannot stop the queue
    a broken provider stops it instead of burning the cohort
    an interrupted job resumes instead of re-marking finished work
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import review_job_service as jobs


# ── a fake tenant + in-memory item store, so no database is needed ────────

class FakeStore:
    """Stands in for the two job tables. Records every state change."""

    def __init__(self, items):
        self.items = [dict(i) for i in items]
        self.job_state = ("running", "")

    def install(self, monkeypatch):
        monkeypatch.setattr(jobs, "job_items", lambda tenant, job_id: self.items)

        def _mark(tenant, item_id, state, detail="", score=None):
            for i in self.items:
                if i["id"] == item_id:
                    i.update(state=state, detail=detail, score=score)

        def _set(tenant, job_id, state, note=""):
            self.job_state = (state, note)

        monkeypatch.setattr(jobs, "mark_item", _mark)
        monkeypatch.setattr(jobs, "set_job_state", _set)
        return self


def pending(n, scope_id=17):
    return [{"id": i, "scope_id": scope_id, "submission_id": 1000 + i,
             "state": "pending", "detail": "", "score": None}
            for i in range(1, n + 1)]


@pytest.fixture
def store(monkeypatch):
    def _make(items):
        return FakeStore(items).install(monkeypatch)
    return _make


# ── the flag: shipping this must change nothing ───────────────────────────

def test_the_subsystem_is_inert_without_the_flag(monkeypatch):
    monkeypatch.delenv("REVIEW_JOBS_ENABLED", raising=False)
    assert jobs.jobs_enabled() is False
    for value in ("1", "true", "YES"):
        monkeypatch.setenv("REVIEW_JOBS_ENABLED", value)
        assert jobs.jobs_enabled() is True
    monkeypatch.setenv("REVIEW_JOBS_ENABLED", "0")
    assert jobs.jobs_enabled() is False


# ── it grades everything, in order, once ──────────────────────────────────

def test_every_pending_item_is_reviewed_exactly_once(store):
    s = store(pending(5))
    seen = []

    def review_one(scope_id, submission_id):
        seen.append(submission_id)
        return "done", "", 7.5

    final = jobs.drain(object(), 1, review_one, pause=0)
    assert seen == [1001, 1002, 1003, 1004, 1005], seen
    assert final.done == 5 and final.finished
    assert s.job_state[0] == "finished", s.job_state


def test_items_are_taken_in_order_so_the_cache_stays_warm(store):
    """Grouped by assignment: the rubric and knowledge pack are built by the
    first review of an item and reused by every one after it."""
    items = pending(2, scope_id=14) + pending(2, scope_id=17)
    for n, i in enumerate(items, 1):
        i["id"] = n
    store(items)
    order = []
    jobs.drain(object(), 1, lambda sid, sub: (order.append(sid), ("done", "", 5))[1],
               pause=0)
    assert order == sorted(order), f"assignments interleaved: {order}"


# ── one bad row must not stop the queue ───────────────────────────────────

def test_a_raising_review_is_recorded_and_the_queue_continues(store):
    s = store(pending(4))

    def review_one(scope_id, submission_id):
        if submission_id == 1002:
            raise RuntimeError("vision OCR blew up")
        return "done", "", 6.0

    final = jobs.drain(object(), 1, review_one, pause=0)
    assert final.done == 3 and final.failed == 1, final
    bad = next(i for i in s.items if i["submission_id"] == 1002)
    assert "RuntimeError" in bad["detail"] and "vision OCR" in bad["detail"]


def test_a_skipped_row_is_not_a_failure(store):
    """An unreadable deliverable is left alone deliberately. Counting it as a
    failure would make a clean run look broken and hide the real ones."""
    s = store(pending(3))

    def review_one(scope_id, submission_id):
        if submission_id == 1002:
            return "skipped", "unassessable_deliverable", None
        return "done", "", 8.0

    final = jobs.drain(object(), 1, review_one, pause=0)
    assert (final.done, final.skipped, final.failed) == (2, 1, 0), final
    assert final.finished
    assert "1 skipped" in s.job_state[1], s.job_state


# ── a broken provider stops the run instead of burning the cohort ─────────

def test_repeated_failure_aborts_rather_than_spending_the_whole_cohort(store):
    s = store(pending(200))
    calls = []

    def always_fails(scope_id, submission_id):
        calls.append(submission_id)
        return "failed", "HTTP 401 invalid api key", None

    jobs.drain(object(), 1, always_fails, pause=0)
    assert len(calls) == jobs.MAX_CONSECUTIVE_FAILURES, (
        f"kept going through {len(calls)} rows against a dead provider")
    assert s.job_state[0] == "aborted", s.job_state
    assert "consecutive failures" in s.job_state[1]


def test_the_failure_counter_resets_on_success(store):
    """Scattered failures are ordinary. Only an unbroken run of them means the
    system is down, and a naive total would abort a healthy batch."""
    store(pending(30))
    n = {"i": 0}

    def alternating(scope_id, submission_id):
        n["i"] += 1
        return ("failed", "one bad row", None) if n["i"] % 2 else ("done", "", 5)

    final = jobs.drain(object(), 1, alternating, pause=0)
    assert final.finished, "a batch with scattered failures must still complete"
    assert final.failed == 15 and final.done == 15, final


def test_should_abort_is_pure_and_exact():
    assert jobs.should_abort(7, limit=8) is False
    assert jobs.should_abort(8, limit=8) is True


# ── an interrupted job resumes ────────────────────────────────────────────

def test_finished_work_is_never_re_marked(store):
    """HF Spaces restart on their own. A resumed job must not re-review — that
    would spend money again AND overwrite marks with a second opinion."""
    items = pending(5)
    items[0]["state"] = "done"
    items[1]["state"] = "failed"
    items[2]["state"] = "skipped"
    store(items)
    seen = []
    jobs.drain(object(), 1, lambda sid, sub: (seen.append(sub), ("done", "", 5))[1],
               pause=0)
    assert seen == [1004, 1005], f"re-reviewed settled rows: {seen}"


def test_next_item_is_pure_and_skips_settled_rows():
    assert jobs.next_item([]) is None
    assert jobs.next_item([{"state": "done"}]) is None
    assert jobs.next_item([{"state": "done"}, {"state": "pending", "id": 9}])["id"] == 9


# ── progress arithmetic ───────────────────────────────────────────────────

def test_progress_reports_what_is_left():
    p = jobs.Progress(total=100, done=40, failed=5, skipped=5)
    assert p.pending == 50 and p.percent == 50 and p.finished is False
    assert jobs.Progress(total=10, done=10, failed=0, skipped=0).finished is True
    assert jobs.Progress(total=0, done=0, failed=0, skipped=0).percent == 100


# ── one worker, process-wide ──────────────────────────────────────────────

def test_a_second_worker_cannot_start(monkeypatch):
    """Two workers would race on the same rows and reintroduce exactly the
    contention this exists to remove."""
    monkeypatch.setattr(jobs, "_worker_running", True, raising=False)
    assert jobs.start_worker(object(), 1, lambda a, b: ("done", "", 5)) is False


def test_the_worker_releases_its_lock_when_the_job_crashes(monkeypatch):
    """A crash that leaves the flag set would block every future job until the
    Space restarted."""
    monkeypatch.setattr(jobs, "_worker_running", False, raising=False)
    monkeypatch.setattr(jobs, "drain",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(jobs, "set_job_state", lambda *a, **k: None)

    assert jobs.start_worker(object(), 1, lambda a, b: ("done", "", 5)) is True
    for _ in range(100):
        if not jobs.worker_is_running():
            break
        import time as _t
        _t.sleep(0.02)
    assert jobs.worker_is_running() is False, "the worker lock was never released"


# ── it must never write a mark itself ─────────────────────────────────────

def test_the_service_never_touches_a_submission_table():
    """Marks are written by the regrade route, which UPDATEs in place and
    refuses to overwrite a faculty grade. This service must own queue state
    and nothing else."""
    import inspect
    src = inspect.getsource(jobs).lower()
    for table in ("assignment_submissions", "case_study_submissions",
                  "industry_session_submissions"):
        assert table not in src, f"job service writes to {table} directly"


# ── the loop must be finite, whatever else is wrong ───────────────────────
#
# Found by mutation testing, not by review: break next_item and drain() spins
# forever, re-reviewing the same row and spending real money with nobody
# watching. An unattended worker needs the loop bounded by construction, not by
# the correctness of the function it calls.

def test_the_worker_cannot_loop_forever(store, monkeypatch):
    store(pending(5))
    monkeypatch.setattr(jobs, "next_item",
                        lambda items: items[0] if items else None)  # never advances
    calls = []

    def tripwire(_seconds):
        """Fail FAST rather than hang.

        The escape has to be here, not in review_one: drain() catches
        everything a review raises — correctly, so one bad row cannot stop the
        queue — which means an assertion thrown from there is swallowed and
        recorded as a failed item. The injected sleeper runs OUTSIDE that
        try/except, so it is the one place a runaway loop can be caught. A
        hanging test reports nothing; a failing one names the defect.
        """
        if len(calls) > 20:
            raise AssertionError(
                f"drain() ran away: {len(calls)} reviews on a 5-item queue")

    def review_one(scope_id, submission_id):
        calls.append(submission_id)
        return "done", "", 5

    final = jobs.drain(object(), 1, review_one, pause=0.001, sleeper=tripwire)
    assert len(calls) <= 7, f"ran away: {len(calls)} reviews on a 5-item queue"
    assert final is not None


def test_a_queue_that_will_not_drain_is_reported_not_declared_finished(store):
    """Saying 'finished' with rows still pending is the worst outcome: it looks
    like success and the learners never get marked."""
    s = store(pending(3))
    rounds = {"n": 0}

    def tripwire(_seconds):
        rounds["n"] += 1
        if rounds["n"] > 20:
            raise AssertionError("drain() never gave up on a queue going nowhere")

    jobs.drain(object(), 1, lambda sid, sub: ("pending", "went nowhere", None),
               pause=0.001, sleeper=tripwire)
    assert s.job_state[0] == "aborted", s.job_state
    assert "not draining" in s.job_state[1]
