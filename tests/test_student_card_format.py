"""The student card format, per Ranjana (18 Aug, verbatim): "From review
remove rubric framework it will address by faculty if human review happen.
just review give the score of total marks and provide personalized feedback
... make it pointwise and it should be hard review with soft tone."

What changes: presentation. The student-facing payload carries the total in
the assignment's own marks plus pointwise feedback (feedbackPoints, hardTruth,
strengths, improvements, language report). The per-criterion rubric table and
the scoring narrative move under facultyView, where the mentor dashboard and
any human re-review can still read them.

What does NOT change: judgement. The rubric engine still scores every
criterion against evidence, gates still cap, aggregate() still does the
arithmetic. Both LMS review surfaces guard the rubric section with
`fb?.rubricScores?.length > 0 && (...)` (checked in AiRevPanel.jsx and
AssignmentReview.jsx on 19 Aug), so a payload without the key simply renders
no table — no crash, no blank card.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import json

from app.services import review_payload
from app.routes import assignment_review as ar

RUBRIC_ROWS = [{"criteria": "Five steps are listed", "maxScore": 35,
                "score": 33.2, "percentage": 95, "status": "good"}]

RESULT = {
    "totalScore": 80, "grade": "A",
    "facultyView": {"rubricScores": RUBRIC_ROWS, "howYouScored": "narrative"},
    "strengths": ["s"], "improvements": ["i"],
    "feedbackPoints": ["Your steps name actions but not reasons."],
    "hardTruth": "Link each year to the goal.",
    "detailedFeedback": "d", "missingConcepts": [], "coveredConcepts": [],
    "suggestedModules": [], "wordCount": 200, "wordCountMessage": "",
    "aiLikelihoodPercent": 35, "humanLikelihoodPercent": 65,
    "aiDetectionReason": "r", "aiVerdict": "likely-human",
}


def test_the_stored_review_has_no_student_facing_rubric_table():
    payload = review_payload.build(RESULT, max_marks=10, score_marks=8.0)
    assert "rubricScores" not in payload
    rows = payload["facultyView"]["requirements"]
    assert [r["requirement"] for r in rows] == [c["criteria"] for c in RUBRIC_ROWS]
    assert [r["outOf"] for r in rows] == [c["maxScore"] for c in RUBRIC_ROWS]
    assert payload["facultyView"]["howItWasScored"] == "narrative"
    # The invariant Ranjana asked for, 23 Aug: the word "rubric" appears
    # nowhere in a stored review. Not a key, not a label, not a version field.
    import json
    assert "rubric" not in json.dumps(payload).lower()


def test_the_student_card_still_carries_everything_actionable():
    """Removing the table must not thin the feedback — the pointwise review
    IS the deliverable now."""
    payload = review_payload.build(RESULT, max_marks=10, score_marks=8.0)
    assert payload["scoreMarks"] == 8.0 and payload["outOf"] == 10
    assert payload["feedbackPoints"] == RESULT["feedbackPoints"]
    assert payload["hardTruth"] == RESULT["hardTruth"]
    assert payload["strengths"] and payload["improvements"]
    assert payload["aiLikelihoodPercent"] == 35, "authorship stays advisory-visible"


def test_a_legacy_result_with_top_level_rubric_is_rehomed_not_dropped():
    """The fallback (non-pipeline) writer still produces rubricScores at the
    top level. That data is kept — moved under facultyView — never lost."""
    legacy = {**RESULT}
    del legacy["facultyView"]
    legacy["rubricScores"] = RUBRIC_ROWS
    payload = review_payload.build(legacy, max_marks=10)
    assert "rubricScores" not in payload
    assert [r["requirement"] for r in payload["facultyView"]["requirements"]] \
        == [c["criteria"] for c in RUBRIC_ROWS]


def test_the_live_response_matches_the_stored_shape():
    resp = ar._build_response({"submissionId": 1, "attemptNumber": 1},
                              RESULT, "summary", start_time=0, max_marks=10)
    fb = resp["feedback"]
    assert "rubricScores" not in fb
    assert fb["facultyView"]["rubricScores"] == RUBRIC_ROWS
    assert fb["scoreMarks"] == 8.0 and fb["outOf"] == 10
    assert fb["feedbackPoints"] == RESULT["feedbackPoints"]


def test_new_format_payloads_are_still_recognized_as_agent_graded():
    """THE regression that would matter most: _graded_by_human treats a blob
    with no agent markers as a human grade and REFUSES regrades. A new-format
    payload (facultyView instead of rubricScores) must still read as ours,
    or every review written after this change becomes un-regradeable."""
    payload = review_payload.build(RESULT, max_marks=10, score_marks=8.0)
    row = {"grade": 8.0, "feedback": json.dumps(payload)}
    assert ar._graded_by_human(row) is False


def test_a_true_faculty_grade_is_still_protected():
    """The other side of the marker check must keep holding: a bare grade
    with hand-typed feedback is a person's work and is refused by default."""
    row = {"grade": 9.0, "feedback": json.dumps(
        {"comment": "Excellent effort, well structured."})}
    assert ar._graded_by_human(row) is True
