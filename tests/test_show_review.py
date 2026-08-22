"""show_review: what is actually stored, before anything is patched.

Day 04 left eleven students with a bare "You scored 0 out of 10." — a mark
and no reasons, which the scoring rule forbids. The detector below is what
finds them; getting it wrong either hides the defect or flags every healthy
review as broken.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from show_review import as_dict, is_unsubstantiated, outline


def test_stored_feedback_parses_from_json_or_dict_and_never_raises():
    assert as_dict('{"grade": "B"}') == {"grade": "B"}
    assert as_dict({"grade": "B"}) == {"grade": "B"}
    for bad in (None, "", "not json {", 42, "[1,2]"):
        assert as_dict(bad) == {}


def test_the_eleven_bare_score_rows_are_detected():
    """Exactly their shape: a score, a summary line, nothing behind it."""
    assert is_unsubstantiated({"scoreMarks": 0, "grade": "F",
                               "summary": "You scored 0 out of 10."}) is True


def test_a_healthy_review_is_not_flagged():
    healthy = {"scoreMarks": 6.9, "grade": "B",
               "summary": "You scored 6.9 out of 10.",
               "rubricScores": [{"name": "Evidence use", "score": 3}],
               "feedbackPoints": ["Name your sources."]}
    assert is_unsubstantiated(healthy) is False


def test_any_one_kind_of_substance_is_enough():
    for key, value in (("feedbackPoints", ["do this"]),
                       ("hardTruth", "the argument does not hold"),
                       ("rubricBreakdown", [{"score": 2}]),
                       ("detailedFeedback", "a paragraph")):
        assert is_unsubstantiated({"score": 0, key: value}) is False, key


def test_empty_lists_do_not_count_as_substance():
    assert is_unsubstantiated({"score": 0, "rubricScores": [],
                               "feedbackPoints": []}) is True


def test_a_not_graded_row_is_not_an_unsubstantiated_mark():
    """No mark was given, so there is nothing to substantiate."""
    assert is_unsubstantiated({"notGraded": True,
                               "message": "publish the page"}) is False
    assert is_unsubstantiated({}) is False


def test_the_outline_shows_empty_lists_loudly():
    lines = "\n".join(outline({"score": 0, "feedbackPoints": []}))
    assert "EMPTY" in lines
