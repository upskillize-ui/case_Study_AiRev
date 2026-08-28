"""The safety net: nothing a learner submits is ever silently lost.

Three independent leaks meant a submission could be accepted and then never
reviewed, with no error anywhere — no retry on the LMS hook, a live enqueue
dropped whenever another worker was busy, and rows stranded in a job that
_finalize() had already closed as 'aborted'. The sweeper fixes all three by
not trusting the queue at all: it reads assignment_submissions and asks one
question — is there work here with no mark?

The tests that matter most are the ones about what it must NOT pick up. A
sweeper that re-buys the same rows every three hours is not a safety net, it
is a standing order at the till.
"""
import pytest

from app.services import sweeper_service as sw


class FakeTenant:
    id = "lms"


@pytest.fixture
def captured(monkeypatch):
    seen = {}

    def fake_tquery(tenant, sql, params=()):
        seen["sql"] = " ".join(sql.split())
        seen["params"] = params
        return seen.get("rows", [])

    monkeypatch.setattr(sw, "tquery", fake_tquery)
    return seen


def row(sub_id, assignment_id=27, student_id=1, submitted="2026-08-27 10:00:00"):
    return {"id": sub_id, "assignment_id": assignment_id,
            "student_id": student_id, "submitted_at": submitted}


# ── what it must not touch ─────────────────────────────────────────────────

def test_drafts_are_excluded(captured):
    sw.find_unreviewed(FakeTenant())
    assert "COALESCE(s.status, '') <> 'draft'" in captured["sql"]


def test_rows_already_marked_not_graded_are_excluded(captured):
    """THE COST TRAP. mark_not_graded leaves grade=NULL and
    status='submitted' on purpose, so a sweeper that only asked "no grade?"
    would re-buy every unreadable-file and wrong-task row every three hours,
    for ever, each run reaching the identical conclusion."""
    sw.find_unreviewed(FakeTenant())
    assert "NOT LIKE" in captured["sql"] and "notGraded" in captured["sql"]


def test_a_never_reviewed_row_is_not_lost_to_null_feedback(captured):
    """THE SQL TRAP behind the exclusion above.

    `feedback NOT LIKE '%notGraded%'` evaluates to NULL — not TRUE — when
    feedback IS NULL, and a NULL predicate excludes the row. Every submission
    that has never been reviewed has NULL feedback, so the unguarded form
    would have made the sweeper skip precisely the rows it exists to catch,
    while still passing every other test here.

    It also carries the recovery path: the LMS upsert sets feedback = NULL on
    resubmit, so a learner who replaces an unreadable file clears their own
    notGraded stamp and becomes sweepable again. The discriminator is the
    student's action, not a timer — which only works if NULL means "sweep me".
    """
    sw.find_unreviewed(FakeTenant())
    assert "COALESCE(s.feedback, '') NOT LIKE" in captured["sql"], (
        "feedback must be COALESCE'd before NOT LIKE, or every unreviewed "
        "row is silently skipped")


def test_empty_rows_are_excluded(captured):
    """A row with no notes and no file has nothing to review."""
    sw.find_unreviewed(FakeTenant())
    sql = captured["sql"]
    assert "CHAR_LENGTH(COALESCE(s.notes, '')) > 0" in sql
    assert "COALESCE(s.file_path, '') <> ''" in sql


def test_only_active_assignments(captured):
    sw.find_unreviewed(FakeTenant())
    assert "a.status = 'active'" in captured["sql"]


def test_already_graded_rows_are_excluded(captured):
    sw.find_unreviewed(FakeTenant())
    assert "s.grade IS NULL" in captured["sql"]


# ── selection behaviour ────────────────────────────────────────────────────

def test_one_row_per_student_per_assignment_newest_first(captured):
    """A student with three attempts costs one review, not three."""
    captured["rows"] = [row(90, student_id=7), row(50, student_id=7),
                        row(20, student_id=7)]
    got = sw.find_unreviewed(FakeTenant())
    assert [r["id"] for r in got] == [90]


def test_different_students_all_come_through(captured):
    captured["rows"] = [row(90, student_id=7), row(91, student_id=8)]
    assert len(sw.find_unreviewed(FakeTenant())) == 2


def test_the_longest_wait_is_served_first(captured):
    """A learner waiting since Day 02 goes before one who submitted at noon."""
    captured["rows"] = [row(900, student_id=1), row(12, student_id=2)]
    assert [r["id"] for r in sw.find_unreviewed(FakeTenant())] == [12, 900]


def test_a_runaway_sweep_cannot_spend_the_night(captured):
    captured["rows"] = [row(i, student_id=i) for i in range(1, 60)]
    assert len(sw.find_unreviewed(FakeTenant(), limit=10)) == 10


def test_the_course_filter_is_parameterised(captured):
    """String-built IN lists are how a report becomes an injection."""
    sw.find_unreviewed(FakeTenant(), course_ids=[55, 46])
    assert "a.course_id IN (%s, %s)" in captured["sql"]
    assert captured["params"] == (55, 46)


def test_no_course_filter_means_every_course(captured):
    sw.find_unreviewed(FakeTenant())
    assert "course_id IN" not in captured["sql"]
    assert captured["params"] == ()


# ── orchestration ──────────────────────────────────────────────────────────

def test_nothing_to_do_costs_nothing(captured, monkeypatch):
    captured["rows"] = []
    called = {"created": False}
    monkeypatch.setattr("app.services.review_job_service.create_job",
                        lambda *a, **k: called.__setitem__("created", True))
    out = sw.sweep(FakeTenant(), lambda *a: None)
    assert out == {"queued": 0, "detail": "nothing unreviewed"}
    assert called["created"] is False, "a quiet sweep must not create a job"


def test_a_busy_worker_leaves_the_rows_queued(captured, monkeypatch):
    """start_worker returns False when one is already running — the rows stay
    in the job and the NEXT sweep picks them up. That is the whole point."""
    captured["rows"] = [row(11)]
    monkeypatch.setattr("app.services.review_job_service.create_job",
                        lambda *a, **k: 777)
    monkeypatch.setattr("app.services.review_job_service.start_worker",
                        lambda *a, **k: False)
    out = sw.sweep(FakeTenant(), lambda *a: None)
    assert out["jobId"] == 777 and out["queued"] == 1
    assert out["workerStarted"] is False
    assert "next sweep" in out["detail"]
