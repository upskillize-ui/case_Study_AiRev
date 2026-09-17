"""The sweep that always lost the coin toss (04 Sep 2026, report day).

Students submitting all afternoon kept a one-row live job open at the exact
second each manual sweep landed; twelve sweeps in a row answered "a worker
is busy" while 25 re-offered rows sat ready. A live worker is gone in a
minute and drains parked batches on its way out, so the sweep must queue
behind it. Since 07 Sep a busy BATCH worker no longer means "busy" either:
the rows it already holds are left out and the rest is parked behind it.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import review_job_service as jobs
from app.services import sweeper_service as sw

ROWS = [{"id": 5378, "assignment_id": 33, "student_id": 7}]


def _sweep(monkeypatch, running: bool, live: bool, parked=(), waiting=frozenset()):
    created, started = [], []
    monkeypatch.setattr(jobs, "parked_jobs", lambda tenant, exclude=0, limit=3: list(parked))
    monkeypatch.setattr(jobs, "worker_is_running", lambda: running)
    monkeypatch.setattr(jobs, "pending_submission_ids", lambda tenant: set(waiting))
    monkeypatch.setattr(jobs, "night_for", lambda tenant, job_id: False)
    monkeypatch.setattr(sw, "find_unreviewed", lambda tenant, course_ids, limit: ROWS)
    monkeypatch.setattr(jobs, "create_job", lambda tenant, st, ids, note="": (created.append(ids), 901)[1])
    monkeypatch.setattr(jobs, "start_worker", lambda tenant, job_id, fn, workers=None, night=False: (started.append(job_id), not running)[1])
    return sw.sweep("lms", lambda a, b: ("done", "", 5.0)), created, started


def test_a_live_worker_does_not_stop_the_sweep_from_queuing(monkeypatch):
    res, created, started = _sweep(monkeypatch, running=True, live=True)
    assert res["jobId"] == 901 and res["queued"] == 1
    assert res["workerStarted"] is False and "parked" in res["detail"]
    assert created == [[(33, 5378)]]


def test_a_batch_worker_no_longer_means_busy_the_rest_is_parked(monkeypatch):
    """THE QUEUE (07 Sep 2026): rows the running batch already holds are left
    out; whatever is left parks behind it instead of waiting three hours."""
    res, created, started = _sweep(monkeypatch, running=True, live=False, waiting={5378})
    assert res["queued"] == 0 and "running worker" in res["detail"]
    assert created == [] and started == []
    res, created, started = _sweep(monkeypatch, running=True, live=False)
    assert res["queued"] == 1 and res["workerStarted"] is False and "parked" in res["detail"]
    assert created == [[(33, 5378)]]


def test_an_idle_worker_drains_at_once(monkeypatch):
    res, created, started = _sweep(monkeypatch, running=False, live=False)
    assert res["workerStarted"] is True and res["detail"] == "draining"


def test_a_second_sweep_never_parks_the_same_rows_twice(monkeypatch):
    res, created, started = _sweep(monkeypatch, running=True, live=True, parked=[901])
    assert res["jobId"] == 901 and res["queued"] == 0 and "already parked" in res["detail"]
    assert created == [] and started == []


def test_an_idle_sweep_resumes_a_parked_batch_instead_of_duplicating_it(monkeypatch):
    res, created, started = _sweep(monkeypatch, running=False, live=False, parked=[901])
    assert res["jobId"] == 901 and res["workerStarted"] is True
    assert "resumed" in res["detail"] and created == [] and started == [901]
