"""Auto-review on submit, without a spinner and without an error message.

Ranjana, 23 Aug: "tomorrow onwards students submit and get instant feedback",
and "so students not get busy of error msg if same time using multiple
students".

Reviewing inside the submit request cannot do that. A review takes 25-65
seconds and the browser is serial, so fifty submissions in one evening either
queue behind each other invisibly or meet the capacity governor and read
"AiRev is at full capacity" — an error message for having done the work on
time.

So a submission ENQUEUES: one INSERT, an answer in milliseconds, and the same
worker that drains a cohort batch drains this.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services import review_job_service as jobs


class FakeDB:
    """The two job tables, in memory. Enough to exercise the queue's logic
    without a database — which is the point of keeping that logic pure."""

    def __init__(self):
        self.jobs, self.items, self.next_job, self.next_item = {}, [], 1, 1

    # -- the three functions the queue actually calls ---------------------
    def tquery(self, tenant, sql, params=()):
        s = " ".join(sql.split())
        if "SELECT MAX(id) AS id FROM review_jobs" in s:
            return [{"id": self.next_job - 1}]
        if "FROM review_jobs WHERE note" in s:
            note, = params
            live = [j for j in self.jobs.values()
                    if j["note"] == note and j["state"] == "running"]
            return [{"id": live[-1]["id"]}] if live else []
        if "FROM review_job_items WHERE job_id = %s AND submission_id" in s:
            job_id, sub = params
            return [{"id": i["id"]} for i in self.items
                    if i["job_id"] == job_id and i["submission_id"] == sub
                    and i["state"] == "pending"][:1]
        if "FROM review_job_items WHERE job_id = %s AND state = 'pending'" in s:
            job_id, = params
            return [{"id": i["id"]} for i in self.items
                    if i["job_id"] == job_id and i["state"] == "pending"][:1]
        return []

    def texecute(self, tenant, sql, params=()):
        s = " ".join(sql.split())
        if "INSERT INTO review_jobs" in s:
            scope_type, note = params
            self.jobs[self.next_job] = {"id": self.next_job, "note": note,
                                        "state": "queued"}
            self.next_job += 1
        elif "INSERT INTO review_job_items" in s:
            job_id, scope_id, submission_id = params
            self.items.append({"id": self.next_item, "job_id": job_id,
                               "scope_id": scope_id,
                               "submission_id": submission_id,
                               "state": "pending"})
            self.next_item += 1
        elif "UPDATE review_jobs SET state" in s:
            state, note, job_id = params
            self.jobs[job_id].update(state=state, note=note)


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(jobs, "tquery", fake.tquery)
    monkeypatch.setattr(jobs, "texecute", fake.texecute)
    monkeypatch.setattr(jobs, "ensure_tables", lambda tenant: None)
    return fake


TENANT = object()


# ── enqueueing ──────────────────────────────────────────────────────────────

def test_a_submission_is_queued_and_a_live_job_is_created(db):
    job_id, queued = jobs.enqueue_live(TENANT, 24, 7327)
    assert queued is True
    assert db.jobs[job_id]["note"] == jobs.LIVE_NOTE
    assert db.jobs[job_id]["state"] == "running"
    assert len(db.items) == 1


def test_the_second_learner_joins_the_SAME_live_job(db):
    """A job per learner would mean a hundred jobs each wanting the one
    worker that may run."""
    first, _ = jobs.enqueue_live(TENANT, 24, 7327)
    second, _ = jobs.enqueue_live(TENANT, 24, 7328)
    assert first == second
    assert len(db.items) == 2


def test_a_double_click_is_not_queued_twice(db):
    jobs.enqueue_live(TENANT, 24, 7327)
    job_id, queued = jobs.enqueue_live(TENANT, 24, 7327)
    assert queued is False
    assert len(db.items) == 1, "the learner would have been reviewed twice"


def test_the_same_submission_may_be_queued_again_once_it_has_been_reviewed(db):
    """A resubmission after a fix must reach the marker."""
    jobs.enqueue_live(TENANT, 24, 7327)
    db.items[0]["state"] = "done"
    _job, queued = jobs.enqueue_live(TENANT, 24, 7327)
    assert queued is True


def test_fifty_submissions_all_land(db):
    """The evening-rush case. Nobody is turned away, nobody waits on a spinner."""
    for n in range(50):
        jobs.enqueue_live(TENANT, 24, 9000 + n)
    assert len(db.items) == 50
    assert len(db.jobs) == 1


def test_has_pending_sees_work_that_arrived_after_the_drain_started(db):
    job_id, _ = jobs.enqueue_live(TENANT, 24, 7327)
    db.items[0]["state"] = "done"
    assert jobs.has_pending(TENANT, job_id) is False
    jobs.enqueue_live(TENANT, 24, 7328)
    assert jobs.has_pending(TENANT, job_id) is True


# ── the worker keeps going while work keeps arriving ────────────────────────

def test_the_worker_looks_again_for_work_that_arrived_mid_drain():
    """drain_parallel snapshots its items once. Without a second look, the
    last learner to submit waits for the NEXT submission to wake the worker."""
    src = open("app/services/review_job_service.py", encoding="utf-8").read()
    body = src[src.index("    def _run():"):src.index("    threading.Thread(")]
    assert "for _round in range(LIVE_MAX_ROUNDS)" in body
    assert "has_pending(tenant, job_id)" in body


def test_a_batch_job_still_finishes_after_one_pass():
    """Only the live queue loops. A cohort batch must not spin after it is
    done."""
    src = open("app/services/review_job_service.py", encoding="utf-8").read()
    body = src[src.index("    def _run():"):src.index("    threading.Thread(")]
    assert "if not live or not has_pending" in body


def test_the_relook_is_bounded():
    """An unbounded loop on a daemon thread is how a Space stops answering
    /health."""
    assert 0 < jobs.LIVE_MAX_ROUNDS <= 500


# ── the route ───────────────────────────────────────────────────────────────

def test_the_enqueue_route_is_staff_only_and_flagged():
    import inspect
    from app.routes import review_jobs
    src = inspect.getsource(review_jobs.enqueue_one)
    assert "_require_enabled()" in src and "_require_staff(x_admin_key)" in src


def test_the_route_answers_without_reviewing_anything():
    """One INSERT and a start_worker call — no review inside the request."""
    import inspect
    from app.routes import review_jobs
    src = inspect.getsource(review_jobs.enqueue_one)
    assert "jobs.enqueue_live(" in src
    assert "re_review_assignment(" not in src, \
        "reviewing here would put the learner back on a 60-second wait"
