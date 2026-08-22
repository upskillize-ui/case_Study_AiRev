"""The full-roster CSV: every student, graded or not, with the reason column
carrying the agent's own stamped note for problem rows. Pure parts only —
the SQL layer is submission_report's, already trusted."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from results_report import feedback_fields, reason_for, shape


def test_a_graded_row_shows_band_ai_and_coaching_text():
    fb = feedback_fields(json.dumps({
        "grade": "B+", "aiLikelihoodPercent": 45,
        "summary": "Solid research, thin sourcing.",
        "feedbackPoints": ["Name your sources.", "Add the RBI rules.", "x"],
        "hardTruth": "Research without sources is opinion."}))
    assert fb["band"] == "B+" and fb["ai_percent"] == 45
    assert "Solid research" in fb["feedback"]
    assert "Name your sources." in fb["feedback"]
    assert "Bottom line: Research without sources" in fb["feedback"]


def test_a_stamped_not_graded_note_becomes_the_reason(monkeypatch=None):
    fb = feedback_fields(json.dumps({
        "notGraded": True, "rubricScores": [],
        "message": "Not graded: your file looks like a study guide, not this "
                   "assignment's work. Please attach the correct work."}))
    row = {"grade": None, "notes_len": 200, "file_ref": "x.pdf"}
    assert "study guide" in reason_for(row, fb)


def test_an_empty_row_gets_the_resubmit_reason():
    row = {"grade": None, "notes_len": 0, "file_ref": ""}
    assert "submit your work again" in reason_for(row, feedback_fields(None))


def test_an_unprocessed_row_is_no_action_needed():
    """Ungraded, has content, no stamped note: the batch simply hasn't
    reached it — never tell this student to act."""
    row = {"grade": None, "notes_len": 500, "file_ref": ""}
    assert "No action needed" in reason_for(row, feedback_fields("{}"))


def test_graded_rows_never_get_a_reason():
    row = {"grade": 7.5, "notes_len": 0, "file_ref": ""}
    assert reason_for(row, feedback_fields("{}")) == ""


def test_malformed_feedback_never_crashes_the_roster():
    for bad in (None, "", "not json {", 42, "[1,2]"):
        fb = feedback_fields(bad)
        assert fb["feedback"] == "" and fb["band"] == ""


def test_shape_puts_top_marks_first_then_problem_rows():
    rows = [{"grade": None, "student_name": "b"},
            {"grade": 9.1, "student_name": "a"},
            {"grade": 3.0, "student_name": "c"},
            {"grade": None, "student_name": "a"}]
    out = shape(rows)
    assert [r.get("grade") for r in out] == [9.1, 3.0, None, None]
    assert out[2]["student_name"] == "a"        # ungraded alphabetical
