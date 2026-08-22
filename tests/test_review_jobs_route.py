"""POST /api/review/jobs — the whole-assignment run as one HTTP call.

Day 01 was graded from a cmd window: 19 windowed batches, hours of
babysitting, a CSV lock, and every deploy killing the run. These tests pin
the replacement's contract: staff-key gated, flag gated, one job at a time,
sweep17's below-N sparing at selection so re-runs cost only what they must,
and outcome classification that keeps policy skips from tripping the
dead-provider abort.
"""
import os
import sys

import pytest
from fastapi import HTTPException

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.routes import review_jobs as rj


def _rows(monkeypatch, rows):
    monkeypatch.setattr(rj, "tquery", lambda tenant, sql, params=(): rows)


ROWS = [
    # newest-first, as the ORDER BY returns them
    {"id": 910, "student_id": 7, "grade": None, "notes_len": 0, "file_ref": "x.jpg"},
    {"id": 905, "student_id": 7, "grade": 3.0, "notes_len": 50, "file_ref": ""},   # older attempt
    {"id": 903, "student_id": 8, "grade": 8.4, "notes_len": 40, "file_ref": ""},
    {"id": 901, "student_id": 9, "grade": 4.2, "notes_len": 90, "file_ref": ""},
    {"id": 899, "student_id": 4, "grade": None, "notes_len": 0, "file_ref": ""},   # empty
]


# ── selection: latest attempt, content required, below-N spares marks ─────

def test_default_run_selects_only_ungraded_latest_attempts(monkeypatch):
    _rows(monkeypatch, ROWS)
    picked = rj.select_rows(object(), 17, below=None)
    assert [r["id"] for r in picked] == [910]


def test_below_spares_rows_at_or_above_the_threshold(monkeypatch):
    """sweep17 semantics: 8.4 keeps its mark and costs nothing; 4.2 and the
    ungraded row are re-bought."""
    _rows(monkeypatch, ROWS)
    picked = rj.select_rows(object(), 17, below=7.0)
    assert [r["id"] for r in picked] == [901, 910]


def test_an_older_attempt_never_rides_along(monkeypatch):
    """Student 7 has two rows; only the newest (910) may be reviewed — the
    review-latest-only policy, enforced at selection."""
    _rows(monkeypatch, ROWS)
    picked = rj.select_rows(object(), 17, below=10.0)
    assert 905 not in [r["id"] for r in picked]


def test_a_row_with_nothing_to_read_is_never_queued(monkeypatch):
    _rows(monkeypatch, ROWS)
    for below in (None, 10.0):
        assert 899 not in [r["id"] for r in rj.select_rows(object(), 17, below)]


# ── gates ─────────────────────────────────────────────────────────────────

def test_the_flag_gates_every_route(monkeypatch):
    monkeypatch.delenv("REVIEW_JOBS_ENABLED", raising=False)
    with pytest.raises(HTTPException) as e:
        rj.start_job(rj.JobRequest(assignmentId=18), tenant=object())
    assert e.value.status_code == 403 and "REVIEW_JOBS_ENABLED" in e.value.detail


def test_a_learner_key_cannot_start_a_cohort_run(monkeypatch):
    monkeypatch.setenv("REVIEW_JOBS_ENABLED", "1")
    monkeypatch.setattr(rj.ai_service, "begin_run_billing", lambda k="": False)
    with pytest.raises(HTTPException) as e:
        rj.start_job(rj.JobRequest(assignmentId=18), tenant=object(),
                     x_admin_key="wrong")
    assert e.value.status_code == 403


def test_a_second_job_is_refused_with_the_running_id(monkeypatch):
    monkeypatch.setenv("REVIEW_JOBS_ENABLED", "1")
    monkeypatch.setattr(rj.ai_service, "begin_run_billing", lambda k="": True)
    monkeypatch.setattr(rj, "select_rows",
                        lambda tenant, aid, below: [{"id": 1, "student_id": 2}])
    monkeypatch.setattr(rj.jobs, "worker_is_running", lambda: True)
    monkeypatch.setattr(rj.jobs, "running_jobs",
                        lambda tenant: [{"id": 41, "state": "running"}])
    with pytest.raises(HTTPException) as e:
        rj.start_job(rj.JobRequest(assignmentId=18), tenant=object(),
                     x_admin_key="k")
    assert e.value.status_code == 409 and "41" in e.value.detail


def test_an_empty_selection_starts_nothing_and_says_so(monkeypatch):
    monkeypatch.setenv("REVIEW_JOBS_ENABLED", "1")
    monkeypatch.setattr(rj.ai_service, "begin_run_billing", lambda k="": True)
    monkeypatch.setattr(rj, "select_rows", lambda tenant, aid, below: [])
    monkeypatch.setattr(rj.jobs, "start_worker",
                        lambda *a: pytest.fail("started a worker for nothing"))
    out = rj.start_job(rj.JobRequest(assignmentId=18, below=7.0),
                       tenant=object(), x_admin_key="k")
    assert out["queued"] == 0


def test_a_good_request_queues_and_starts_the_worker(monkeypatch):
    monkeypatch.setenv("REVIEW_JOBS_ENABLED", "1")
    monkeypatch.setattr(rj.ai_service, "begin_run_billing", lambda k="": True)
    monkeypatch.setattr(rj, "select_rows",
                        lambda tenant, aid, below: [{"id": 10, "student_id": 1},
                                                    {"id": 11, "student_id": 2}])
    monkeypatch.setattr(rj.jobs, "worker_is_running", lambda: False)
    monkeypatch.setattr(rj.jobs, "running_jobs", lambda tenant: [])
    created = {}

    def fake_create(tenant, scope_type, ids, note=""):
        created["ids"], created["note"] = ids, note
        return 7

    started = {}
    monkeypatch.setattr(rj.jobs, "create_job", fake_create)
    monkeypatch.setattr(rj.jobs, "start_worker",
                        lambda tenant, job_id, fn: started.setdefault("job", job_id))
    out = rj.start_job(rj.JobRequest(assignmentId=18, below=7.0),
                       tenant=object(), x_admin_key="k")
    assert out == {"success": True, "jobId": 7, "queued": 2,
                   "note": "assignment 18, redo below 7.0",
                   "poll": "/api/review/jobs/7"}
    assert created["ids"] == [(18, 10), (18, 11)] and started["job"] == 7


# ── outcome classification: policy skips must not read as breakage ────────

def _one(monkeypatch, result=None, raises=None):
    def fake_regrade(submission_id, dryRun=False, force=False,
                     tenant=None, x_admin_key=""):
        if raises:
            raise raises
        return result

    monkeypatch.setattr(rj, "re_review_assignment", fake_regrade)
    monkeypatch.setattr(rj, "set_current_tenant", lambda t: None)
    return rj.make_review_one(object(), "k")(18, 501)


def test_a_reviewed_row_reports_done_with_its_score(monkeypatch):
    state, detail, score = _one(monkeypatch, {
        "success": True, "previousGrade": 3.2,
        "feedback": {"scoreMarks": 8.6}})
    assert (state, score) == ("done", 8.6) and "3.2 -> 8.6" in detail


def test_policy_refusals_are_skips_never_failures(monkeypatch):
    for reason in sorted(rj._SKIP_STATES):
        state, detail, _ = _one(monkeypatch, {"success": False,
                                              "skipped": reason, "detail": "d"})
        assert state == "skipped", f"{reason} counted as failure"


def test_a_503_is_a_failure_that_feeds_the_abort_counter(monkeypatch):
    state, detail, _ = _one(monkeypatch,
                            raises=HTTPException(503, "reviewer unavailable"))
    assert state == "failed" and "503" in detail


def test_a_vanished_row_is_a_skip_not_a_provider_failure(monkeypatch):
    state, detail, _ = _one(monkeypatch,
                            raises=HTTPException(404, "submission not found"))
    assert state == "skipped"


# ── restart resume stays inert unless deliberately configured ─────────────

def test_resume_does_nothing_without_the_flag(monkeypatch):
    monkeypatch.delenv("REVIEW_JOBS_ENABLED", raising=False)
    monkeypatch.setattr(rj.jobs, "running_jobs",
                        lambda tenant: pytest.fail("touched the DB while off"))
    rj.resume_after_restart()


def test_resume_does_nothing_without_the_admin_key(monkeypatch):
    monkeypatch.setenv("REVIEW_JOBS_ENABLED", "1")
    monkeypatch.delenv("ADMIN_JOB_KEY", raising=False)
    monkeypatch.setattr(rj.jobs, "running_jobs",
                        lambda tenant: pytest.fail("tried to resume keyless"))
    rj.resume_after_restart()
