"""Policy (Ranjana, 19 Aug, verbatim): "if it student side fault show them
what is the issue so they can re-submit or next time don't repeat same issue."

The 19 Aug sweep skipped ~150 rows as no_readable_content /
unassessable_deliverable — correctly refusing to grade invisible work — but
the reason lived only in a staff CSV. On the student's own card the item just
sat ungraded with no explanation, so nothing was learned and nothing was
fixed. Now every student-side blocker on a NEVER-graded row is written onto
the student's review card as a plain instruction: what went wrong, what to do
(re-attach / type the answer), and that no score was recorded.

The boundary that matters: a row holding a REAL grade is never touched. A
failed read on a graded row is our fetch problem, not the student's fault,
and an explanation card must not replace an earned review.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import json

import pytest

from app.routes import assignment_review as ar
from app.utils import submission_intake as intake


def _regrade(monkeypatch, grade, notes="", file_path="/uploads/gone.png",
             feedback=None):
    """Drive the real route against a row whose file yields NOTHING readable.
    Records every mark_not_graded call."""
    notes_written = []
    monkeypatch.setattr(ar.intake, "from_stored_file",
                        lambda url, name="": intake.Artefact(
                            kind="image", label=name or url, text="",
                            note="could not be fetched"))
    monkeypatch.setattr(ar.ai_service, "begin_run_billing", lambda k="": True)
    monkeypatch.setattr(ar.assignment_db_service, "mark_not_graded",
                        lambda tenant, sid, message, card=None:
                        notes_written.append({"sid": sid, "message": message,
                                              "card": card}))
    monkeypatch.setattr(
        ar.assignment_db_service, "get_submission_for_regrade",
        lambda tenant, sid: {
            "id": sid, "student_id": 1212, "assignment_id": 17,
            "grade": grade, "feedback": feedback, "notes": notes,
            "file_path": file_path, "file_name": "gone.png",
            "attempt_number": 1})
    monkeypatch.setattr(ar.assignment_db_service, "get_assignment_by_id",
                        lambda tenant, aid: {"id": aid, "title": "Day 01",
                                             "maxScore": 10})
    result = ar.re_review_assignment(submission_id=9, dryRun=True, force=False,
                                     tenant=object(), x_admin_key="k")
    return result, notes_written


def test_an_ungraded_unreadable_row_tells_the_student_why(monkeypatch):
    """Student 1212's case: never graded, nothing readable. The skip stands
    AND the student's card now carries the reason and the fix."""
    result, notes = _regrade(monkeypatch, grade=None)
    assert result["skipped"] == "no_readable_content"
    assert len(notes) == 1
    msg = notes[0]["message"]
    # Policy: simple English — short words, one problem, one fix.
    assert "could not open" in msg
    assert "Submit" in msg
    assert "No marks given" in msg


def test_the_note_is_a_renderable_card_not_a_bare_string(monkeypatch):
    """The LMS review components render summary/detailedFeedback — a payload
    without that shape would store the explanation where no student ever
    sees it, recreating the exact problem this fixes."""
    _, notes = _regrade(monkeypatch, grade=None)
    card = notes[0]["card"]
    assert card and card["summary"] == notes[0]["message"]
    assert card["detailedFeedback"] == notes[0]["message"]


def test_a_graded_row_is_never_overwritten_with_an_explanation(monkeypatch):
    """The boundary. A 7.1 earned on a full read stays a 7.1 even when a
    later read finds nothing — that failure is ours, not the student's."""
    result, notes = _regrade(monkeypatch, grade=7.1, feedback=json.dumps(
        {"detailedFeedback": "earned on a full read", "wordCount": 40,
         "aiLikelihoodPercent": 30}))
    assert result["skipped"] == "no_readable_content"
    assert result["previousGrade"] == 7.1
    assert notes == [], "an earned review was replaced by an excuse card"


def test_an_unassessable_deliverable_note_names_both_ways_out(monkeypatch):
    """Work EXISTS but can't be opened: the student must hear both remedies —
    re-attach the file, or describe the work in the answer box."""
    result, notes = _regrade(monkeypatch, grade=None,
                             notes="my submission link here " * 2)
    if result.get("skipped") == "unassessable_deliverable":
        msg = notes[0]["message"]
        assert "upload the file again" in msg and "answer box" in msg
    else:
        # Intake judged the typed words reviewable, so the route proceeded
        # (dryRun success) — legitimate. What must NEVER happen is a skip
        # for this student with no note written.
        assert result.get("success") or notes, result


def test_a_note_failure_never_breaks_the_skip_response(monkeypatch):
    """Telling the student is best-effort; the skip (mark unchanged) is the
    contract. A DB hiccup writing the note must not turn a safe skip into a
    500."""
    monkeypatch.setattr(ar.assignment_db_service, "mark_not_graded",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(ar.intake, "from_stored_file",
                        lambda url, name="": intake.Artefact(
                            kind="image", label="x", text="", note="gone"))
    monkeypatch.setattr(ar.ai_service, "begin_run_billing", lambda k="": True)
    monkeypatch.setattr(
        ar.assignment_db_service, "get_submission_for_regrade",
        lambda tenant, sid: {"id": sid, "student_id": 1, "assignment_id": 17,
                             "grade": None, "feedback": None, "notes": "",
                             "file_path": "/uploads/x.png", "file_name": "x.png",
                             "attempt_number": 1})
    monkeypatch.setattr(ar.assignment_db_service, "get_assignment_by_id",
                        lambda tenant, aid: {"id": aid, "title": "Day 01",
                                             "maxScore": 10})
    result = ar.re_review_assignment(submission_id=9, dryRun=True, force=False,
                                     tenant=object(), x_admin_key="k")
    assert result["skipped"] == "no_readable_content"
