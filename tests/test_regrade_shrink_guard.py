"""Batch 1 of the 19 Aug re-run, student 1233: a 7.1/10 earned on a full read
of her image was overwritten with 2.7/10 by a regrade that evidently read far
less of the same file (intermittent fetch/OCR). The route already refused to
score rows where NOTHING was readable — but a partial read walked straight
past that check and replaced an honest mark with a verdict on a fragment.

The guard: every stored review carries wordCount, a receipt of how much
content it was based on. A regrade that reads less than half of that receipt
(on rows of 60+ words, below which OCR variance is noise) refuses to write,
keeps the existing grade, and says why. force=true remains the deliberate
override, same as for human-graded rows.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import json

from app.routes import assignment_review as ar
from app.routes.assignment_review import content_shrunk, _prior_word_count
from app.utils import submission_intake as intake


# ── the decision itself, pure ─────────────────────────────────────────────

def test_reading_a_fragment_of_a_previously_full_read_is_shrinkage():
    assert content_shrunk(prior_words=300, current_words=40) is True


def test_reading_about_the_same_amount_is_not():
    assert content_shrunk(300, 280) is False
    assert content_shrunk(300, 151) is False, "half is the line, not the trap"


def test_reading_more_than_before_is_never_shrinkage():
    """Intake improvements legitimately read MORE on a re-run — that is the
    point of re-running."""
    assert content_shrunk(100, 400) is False


def test_caption_sized_rows_are_exempt():
    """A 30-word caption re-reading as 12 words is OCR jitter, not a failed
    fetch — refusing there would freeze every small row forever."""
    assert content_shrunk(30, 12) is False


def test_ungraded_rows_have_no_receipt_and_no_guard():
    assert content_shrunk(0, 5) is False
    assert _prior_word_count({"feedback": None}) == 0
    assert _prior_word_count({"feedback": "not json {"}) == 0
    assert _prior_word_count({"feedback": json.dumps({"wordCount": 288})}) == 288


# ── the route honours it ──────────────────────────────────────────────────

def _regrade(monkeypatch, ocr_text, prior_wc, force=False):
    """Drive the real route with dryRun=True (no AI call). The row holds a
    stored review whose receipt says prior_wc words; the file now extracts to
    ocr_text."""
    monkeypatch.setattr(ar.intake, "from_stored_file",
                        lambda url, name="": intake.Artefact(
                            kind="image", label=name or url, text=ocr_text))
    monkeypatch.setattr(ar.ai_service, "begin_run_billing", lambda k="": True)
    monkeypatch.setattr(
        ar.assignment_db_service, "get_submission_for_regrade",
        lambda tenant, sid: {
            "id": sid, "student_id": 1233, "assignment_id": 17,
            "grade": 7.1,
            "feedback": json.dumps({"wordCount": prior_wc,
                                    "rubricScores": [], "detailedFeedback": "x"}),
            "notes": "", "file_path": "/uploads/plan.jpg",
            "file_name": "plan.jpg", "attempt_number": 1})
    monkeypatch.setattr(ar.assignment_db_service, "get_assignment_by_id",
                        lambda tenant, aid: {"id": aid, "title": "Day 01",
                                             "maxScore": 10})
    return ar.re_review_assignment(submission_id=1, dryRun=True, force=force,
                                   tenant=object(), x_admin_key="k")


def test_a_shrunken_read_leaves_the_row_untouched(monkeypatch):
    """Student 1233's overwrite, replayed and refused: receipt says ~300
    words, this fetch produced ~40."""
    result = _regrade(monkeypatch, "five steps " * 20, prior_wc=300)
    assert result["success"] is False
    assert result["skipped"] == "content_shrunk"
    assert result["previousGrade"] == 7.1


def test_a_full_read_regrades_normally(monkeypatch):
    result = _regrade(monkeypatch, "step " * 290, prior_wc=300)
    assert result["success"] is True and result["dryRun"] is True


def test_force_overrides_the_guard(monkeypatch):
    """Staff who KNOW the file changed (learner replaced their upload with a
    smaller one) keep a deliberate, logged way through."""
    result = _regrade(monkeypatch, "five steps " * 20, prior_wc=300, force=True)
    assert result["success"] is True and result["dryRun"] is True
