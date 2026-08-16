"""explain_review must survive the rows people actually ask about.

The rows an operator brings to this tool are the broken ones: unparseable
feedback, no rubric, no gates, a missing grade. Crashing on those is the one
behaviour that makes the tool useless exactly when it is needed.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import explain_review as ex


# ── feedback parsing ──────────────────────────────────────────────────────

def test_a_json_string_parses():
    assert ex.parse_feedback('{"grade": "B"}') == {"grade": "B"}


def test_a_dict_passes_through():
    assert ex.parse_feedback({"grade": "B"}) == {"grade": "B"}


@pytest.mark.parametrize("blob", [None, "", "not json", "[1,2,3]", '"a string"', b""])
def test_unreadable_feedback_returns_empty_not_a_crash(blob):
    """A row whose payload will not parse is exactly the kind of row someone
    is asking about."""
    assert ex.parse_feedback(blob) == {}


# ── gate descriptions ─────────────────────────────────────────────────────

def test_a_gate_is_described_with_what_it_cut():
    lines = ex.describe_gates([
        {"gate": "no_evidence", "criterion": "Evidence use", "from": 70, "to": 20}])
    assert len(lines) == 1
    assert "no_evidence" in lines[0] and "70% -> 20%" in lines[0]


def test_a_total_cap_carries_its_detail():
    lines = ex.describe_gates([
        {"gate": "concept_coverage", "criterion": "TOTAL", "from": 100, "to": 69,
         "detail": "1/10 concepts covered"}])
    assert "1/10 concepts covered" in lines[0]


@pytest.mark.parametrize("gates", [None, [], ["not a dict"], [{}]])
def test_malformed_gates_do_not_crash(gates):
    ex.describe_gates(gates)


# ── the verdict line ──────────────────────────────────────────────────────

def test_no_evidence_gate_is_named_as_the_reason():
    v = ex.verdict(2, 10, [{"gate": "no_evidence", "criterion": "X"}], 1200)
    assert "no quotable evidence" in v


def test_coverage_cap_is_named_as_the_reason():
    v = ex.verdict(6.9, 10, [{"gate": "concept_coverage", "criterion": "TOTAL"}], 1200)
    assert "must-cover concepts" in v


def test_a_low_mark_with_no_gates_points_at_the_answer_not_the_code():
    """The 14 Aug question. If no gate fired, the marker simply judged the
    answer as off-task — and learners do submit the wrong assignment."""
    v = ex.verdict(0.0, 10, [], 1264)
    assert "No gate fired" in v
    assert "wrong assignment" in v


def test_an_empty_submission_says_so():
    v = ex.verdict(0, 10, [], 0)
    assert "Nothing was stored" in v


@pytest.mark.parametrize("marks,out_of", [
    (None, None),
    ("abc", 10),
    (None, 10),
    (5, None),
    (5, 0),        # ZeroDivisionError is NOT a ValueError — it escapes the
                   # except clause and crashes the tool. A mutation run found
                   # this: removing the explicit guard broke nothing in the
                   # suite, because no test covered a zero total.
    (5, "x"),
])
def test_an_unreadable_mark_does_not_crash(marks, out_of):
    assert "Could not read" in ex.verdict(marks, out_of, [], 100)


# ── the report renders whatever it is given ───────────────────────────────

def _row(**over):
    row = {"id": 1, "assignment_id": 14, "student_id": 201, "grade": 0,
           "status": "graded", "submitted_at": "2026-08-14", "notes": "My answer.",
           "file_name": None, "feedback": '{"outOf": 10, "scoreMarks": 0}',
           "title": "Day 01", "description": "Build a custom GPT.", "total_marks": 10}
    row.update(over)
    return row


@pytest.mark.parametrize("over", [
    {},
    {"feedback": None},
    {"feedback": "corrupt{"},
    {"notes": ""},
    {"notes": None},
    {"grade": None},
    {"description": None},
    {"file_name": "x.png", "notes": ""},
    {"feedback": '{"rubricScores": [{"criteria": "A", "score": 0, "maxScore": 40, "percentage": 0}]}'},
    {"feedback": '{"_meta": {"pipeline": {"gatesHit": [{"gate": "no_evidence", "criterion": "A", "from": 60, "to": 20}]}}}'},
])
def test_the_report_never_crashes(capsys, over):
    ex.report(_row(**over))
    assert capsys.readouterr().out


def test_full_prints_the_whole_answer(capsys):
    long_answer = "word " * 800
    ex.report(_row(notes=long_answer), full=True)
    assert "more chars" not in capsys.readouterr().out


def test_the_preview_is_truncated_and_says_so(capsys):
    ex.report(_row(notes="word " * 800), full=False)
    assert "more chars" in capsys.readouterr().out
