"""The three zeros the Day 07 canary produced, 23 Aug 2026.

Eight students were marked. Three got 0.0/10, and all three were wrong in a
different way. This file holds each one shut.

  student 220 — 1,057 words read, one criterion whose judgment was "No
                assessment available.", evidence [], and EVERY feedback field
                empty. A stored 0.00 with no review behind it. This is the
                bare-zero shape from Day 04, caught in the act.

  student 880 — pasted their Day-06 Suno song. The marker diagnosed it
                correctly ("a music song link unrelated to creating a
                financial dashboard") and then awarded 0.00/10.

  student 188 — pasted Gemini's own advertisement page
                (gemini.google.com/canvas?utm_source=sem&utm_medium=paid-media).
                Diagnosed correctly, awarded 0.00/10.

Policy (Ranjana, 22 Aug): work we cannot judge gets NO grade and an
explanation, so the learner can send the right link the same evening. A zero
teaches them nothing and they cannot undo it from their side.
"""

from app.services import grade_guard as gg
from app.services import student_notices as sn
from app.utils import submission_intake as intake
from app.utils.submission_intake import Artefact


# ── student 220: a mark with no review behind it ────────────────────────────

STUDENT_220_CRITERIA = [{
    "criteria": "Dashboard created from a data set",
    "maxScore": 100, "percentage": 0, "score": 0.0,
    "status": "needs_improvement", "evidence": [],
    "judgment": "No assessment available.",
}]
STUDENT_220_REVIEW = {
    "totalScore": 0, "wordCount": 1057, "isGarbage": False,
    "strengths": [], "improvements": [], "feedbackPoints": [],
    "detailedFeedback": "", "hardTruth": "", "missingConcepts": [],
    "coveredConcepts": [], "encouragement": "",
}


def test_a_placeholder_judgement_is_not_evidence():
    assert gg.has_model_evidence(STUDENT_220_CRITERIA) is False


def test_a_real_judgement_still_counts():
    real = [dict(STUDENT_220_CRITERIA[0],
                 judgment="The dashboard shows three charts but no data source.")]
    assert gg.has_model_evidence(real) is True


def test_a_score_with_no_prose_at_all_still_counts():
    """Some writers store no per-row judgement. The score alone is enough —
    only the PLACEHOLDER shapes are rejected."""
    assert gg.has_model_evidence([{"criteria": "X", "percentage": 40}]) is True


def test_a_review_with_no_feedback_anywhere_did_not_happen():
    assert gg.review_is_empty(STUDENT_220_REVIEW) is True


def test_a_review_with_one_improvement_did_happen():
    assert gg.review_is_empty(dict(STUDENT_220_REVIEW,
                                   improvements=["Add a data source."])) is False


def test_a_review_carried_only_by_its_prose_did_happen():
    assert gg.review_is_empty(dict(STUDENT_220_REVIEW,
                                   detailedFeedback="Your charts lack labels.")) is False


def test_student_220s_exact_row_is_refused():
    ok, why = gg.may_write_grade(
        criteria=STUDENT_220_CRITERIA, proposed_score=0, words_read=1057,
        review=STUDENT_220_REVIEW)
    assert not ok and "no feedback at all" in why


def test_a_genuine_low_mark_with_real_feedback_still_writes():
    """The guard must refuse empty reviews, never harsh ones."""
    ok, _ = gg.may_write_grade(
        criteria=[{"criteria": "Dashboard", "percentage": 20,
                   "judgment": "One chart, no data set named."}],
        proposed_score=20, words_read=400,
        review={"improvements": ["Name your data set."], "strengths": []})
    assert ok


# ── students 880 and 188: the wrong link is not a zero ──────────────────────

def _link(readable=True):
    return Artefact(kind="link", label="https://suno.com/s/abc", confirmed=True,
                    text="a song page" if readable else "")


def _typed():
    return Artefact(kind="typed text", label="answer box",
                    text="Here is my submission for day 7.")


def _shot():
    return Artefact(kind="image", label="dashboard.png", confirmed=True,
                    text="a screenshot of a revenue dashboard")


DAY07 = ("Day 07 : Gemini Canvas. Create a Dashboard from a data set using "
         "Gemini Canvas, then share your published link.")


def test_day07_is_a_link_deliverable_task():
    assert intake.link_is_the_deliverable(DAY07) is True


def test_a_link_only_submission_is_recognised():
    assert intake.deliverable_is_only_links([_link(), _typed()]) is True


def test_a_screenshot_means_it_is_not_link_only():
    """That learner showed us the work; judge it, do not refuse it."""
    assert intake.deliverable_is_only_links([_link(), _shot()]) is False


def test_typed_text_alone_is_not_a_produced_deliverable():
    assert intake.deliverable_is_only_links([_typed()]) is False


def test_no_artefacts_at_all_is_not_link_only():
    assert intake.deliverable_is_only_links([]) is False


def test_the_route_refuses_a_garbage_zero_on_a_link_only_task():
    src = open("app/routes/assignment_review.py", encoding="utf-8").read()
    assert 'r.get("isGarbage")' in src
    branch = src[src.index('r.get("isGarbage")'):]
    branch = branch[:branch.index("processingTimeMs")]
    assert "deliverable_is_only_links" in branch
    assert "link_is_not_the_work" in branch
    assert '"needsInput": True' in branch


def test_the_refusal_runs_before_the_mark_is_written():
    src = open("app/routes/assignment_review.py", encoding="utf-8").read()
    refusal = src.index('r.get("isGarbage")')
    write = src.index("_pipeline_assignment_response(")
    assert refusal < write


# ── what those two learners are told ────────────────────────────────────────

def test_student_880_is_told_what_actually_arrived():
    msg = sn.link_is_not_the_work(
        "Submission is a music song link unrelated to creating a financial "
        "dashboard using Gemini Canvas", "Day 07 : Gemini Canvas")
    assert "music song link" in msg
    assert "Day 07 : Gemini Canvas" in msg
    assert sn.NO_MARK in msg


def test_the_notice_asks_for_the_right_link_or_a_screenshot():
    msg = sn.link_is_not_the_work("a landing page", "Day 07")
    assert "screenshot" in msg and "submit again" in msg


def test_the_notice_survives_an_empty_diagnosis():
    msg = sn.link_is_not_the_work("", "Day 07")
    assert "Day 07" in msg and sn.NO_MARK in msg
    assert "  " not in msg          # no gap where the diagnosis would go


def test_the_notice_is_registered():
    assert sn.NOTICES["link_not_the_work"] is sn.link_is_not_the_work
