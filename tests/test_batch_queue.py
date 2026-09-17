"""Grade-all batches queue behind a running worker (07 Sep 2026).

Until now a batch started while another was draining answered 409, and the
admin's loop had to sleep and knock again. Pinned:
  1. A batch started while a worker is busy is CREATED and PARKED, with the
     job it waits behind named; it is not refused.
  2. Rows already waiting in a queued batch are not queued twice.
  3. The exiting worker drains every parked batch, oldest first, not a
     fixed three.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.routes import review_jobs as route
from app.services import review_job_service as jobs


class _T:
    id = "lms"


def _start(monkeypatch, busy, waiting=frozenset(), rows=None):
    created, started = [], []
    monkeypatch.setattr(route, "_require_enabled", lambda: None)
    monkeypatch.setattr(route, "_require_staff", lambda key: None)
    monkeypatch.setattr(route, "select_rows", lambda t, aid, below: rows or [
        {"id": 11, "student_id": 1, "grade": None}, {"id": 12, "student_id": 2, "grade": None}])
    monkeypatch.setattr(jobs, "reap_orphans", lambda t: None)
    monkeypatch.setattr(jobs, "pending_submission_ids", lambda t: set(waiting))
    monkeypatch.setattr(jobs, "running_jobs", lambda t, include_live=True: (
        [{"id": 900, "note": "assignment 7"}] if busy else []))
    monkeypatch.setattr(jobs, "create_job", lambda t, st, pairs, note="": (created.append(pairs), 901)[1])
    monkeypatch.setattr(jobs, "start_worker", lambda t, jid, fn, live=False, workers=None, night=False: (started.append(jid), not busy)[1])
    monkeypatch.setattr(route, "make_review_one", lambda t, key: (lambda a, s: None))
    res = route.start_job(route.JobRequest(assignmentId=7), tenant=_T(), x_admin_key="k")
    return res, created, started


def test_a_batch_behind_a_running_worker_is_parked_not_refused(monkeypatch):
    res, created, started = _start(monkeypatch, busy=True)
    assert res["jobId"] == 901 and res["queued"] == 2
    assert res["parked"] is True and res["behind"] == [900]
    assert "queued behind job 900" in res["detail"]
    assert created == [[(7, 11), (7, 12)]]


def test_an_idle_worker_starts_the_batch_at_once(monkeypatch):
    res, created, started = _start(monkeypatch, busy=False)
    assert res["parked"] is False and res["detail"] == "draining" and started == [901]


def test_rows_already_waiting_are_not_queued_twice(monkeypatch):
    res, created, started = _start(monkeypatch, busy=True, waiting={11})
    assert res["queued"] == 1 and created == [[(7, 12)]]
    res, created, started = _start(monkeypatch, busy=True, waiting={11, 12})
    assert res["queued"] == 0 and "already waiting" in res["detail"] and created == []


def test_the_exiting_worker_drains_every_parked_batch(monkeypatch):
    """Five parked; parked_jobs() hands out three at a time. All five drain."""
    parked_all = [951, 952, 953, 954, 955]
    drained = []
    monkeypatch.setattr(jobs, "parked_jobs", lambda t, exclude=0, limit=3: [
        j for j in parked_all if j not in drained and j != exclude][:limit])
    monkeypatch.setattr(jobs, "drain_parallel", lambda t, jid, fn, workers=None, night=False: (drained.append(jid), type("P", (), {"done": 0, "skipped": 0, "failed": 0})())[1])
    monkeypatch.setattr(jobs, "night_for", lambda t, jid: False)
    monkeypatch.setattr(jobs, "has_pending", lambda t, jid: False)
    monkeypatch.setattr(jobs, "open_live_job", lambda t: None)
    monkeypatch.setattr(jobs, "set_job_state", lambda *a, **k: None)
    assert jobs.start_worker(_T(), 950, lambda a, s: None)
    import time
    for _ in range(50):
        if len(drained) >= 6:
            break
        time.sleep(0.05)
    assert drained == [950] + parked_all


def test_each_picked_up_job_rides_its_own_lane(monkeypatch):
    """A live worker exiting past a parked SWEEP batch drains it at night
    rates; a parked Grade-all behind it stays live. And the job being drained
    becomes the worker's own, so a long batch wait is never read as a dead
    worker."""
    lanes, owners = [], []
    parked_all = [961, 962]
    monkeypatch.setattr(jobs, "parked_jobs", lambda t, exclude=0, limit=3: [
        j for j in parked_all if j not in [l[0] for l in lanes] and j != exclude][:limit])
    monkeypatch.setattr(jobs, "night_for", lambda t, jid: jid == 961)
    def fake_drain(t, jid, fn, workers=None, night=False):
        lanes.append((jid, night, workers)); owners.append(jobs._worker_job_id)
        return type("P", (), {"done": 0, "skipped": 0, "failed": 0})()
    monkeypatch.setattr(jobs, "drain_parallel", fake_drain)
    monkeypatch.setattr(jobs, "has_pending", lambda t, jid: False)
    monkeypatch.setattr(jobs, "open_live_job", lambda t: None)
    monkeypatch.setattr(jobs, "set_job_state", lambda *a, **k: None)
    monkeypatch.setenv("BATCH_LANE_CONCURRENCY", "4")
    assert jobs.start_worker(_T(), 960, lambda a, s: None, live=True)
    import time
    for _ in range(50):
        if len(lanes) >= 3:
            break
        time.sleep(0.05)
    assert lanes == [(960, False, None), (961, True, 4), (962, False, None)]
    assert owners == [960, 961, 962]
