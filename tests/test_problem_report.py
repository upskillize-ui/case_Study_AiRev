"""problem_report answers 'why was this student not reviewed, and whose move
is it?' — the question Ranjana asked mid-sweep on 19 Aug when the batch log
showed ~150 SKIPs and FAILs scrolling past with no per-student list anywhere.

The split that must never blur: a STUDENT-side reason (unreadable file,
nothing submitted, wrong work) goes on the messaging list; a row the agent
simply hasn't finished (server busy, sweep not reached) is OUR move, and
messaging that student would tell them to fix something that is not broken.

Classification is pure, so it is proven here without a database.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import json

import problem_report as pr


def test_a_stamped_not_graded_note_is_the_reason_verbatim():
    """A review attempt actually READ this row and wrote why it could not be
    graded, in the simple words the student sees. The report must repeat
    those words, not invent its own."""
    note = ("We could not open your file, and there was no written answer. "
            "No marks given yet. Please upload your work again.")
    who, reason, action = pr.classify({
        "feedback": json.dumps({"notGraded": True, "message": note}),
        "file_name": "gone.png", "notes": ""})
    assert who == pr.ACT_STUDENT
    assert reason == note and action == note


def test_an_empty_submission_is_the_students_move():
    who, reason, action = pr.classify(
        {"feedback": None, "file_name": "", "file_path": "", "notes": "  "})
    assert who == pr.ACT_STUDENT
    assert "no file" in reason.lower()
    assert "Submit" in action


def test_an_unstamped_row_with_a_file_is_OUR_move_not_the_students():
    """The boundary that keeps the messaging list honest: a file exists, no
    review ever stamped a verdict — the agent hasn't finished (busy server /
    sweep not reached). Telling this student to resubmit would be wrong."""
    who, reason, action = pr.classify(
        {"feedback": None, "file_name": "plan.png", "notes": ""})
    assert who == pr.ACT_US
    assert "re-run" in reason.lower()


def test_a_graded_review_payload_without_notgraded_does_not_count_as_a_note():
    """An old review payload (has feedback, no notGraded flag) on a grade-NULL
    row — e.g. wrong-task rows whose mark was cleared before notes existed —
    must not be read as a student explanation."""
    who, _, _ = pr.classify({
        "feedback": json.dumps({"detailedFeedback": "old review text"}),
        "file_name": "deck.pptx", "notes": ""})
    assert who == pr.ACT_US


def test_unparseable_feedback_never_crashes_the_report():
    who, _, _ = pr.classify(
        {"feedback": "not json {", "file_name": "", "notes": ""})
    assert who == pr.ACT_STUDENT   # no file, no text — nothing to read


def test_report_rows_put_student_side_first_ordering_contract():
    """to_report_row carries the fields the CSV promises."""
    row = pr.to_report_row({
        "student_name": "Asha", "student_id": 1212, "assignment_id": 17,
        "assignment_title": "Day 01", "file_name": "", "file_path": "",
        "notes": "", "feedback": None, "submitted_at": "2026-08-05"})
    assert row["who_fixes"] == pr.ACT_STUDENT
    assert set(pr.FIELDS) == set(row.keys())


def test_the_probe_reclassifies_an_existing_file_off_the_messaging_list():
    """Ranjana's exact question (19 Aug): 'is it agent side issue or not?'
    A row stamped 'could not open your file' whose file probes 200-with-bytes
    is OUR reading gap — messaging that student would tell them to fix
    something that is not broken."""
    row = pr.to_report_row({
        "student_name": "Asha", "student_id": 501, "assignment_id": 17,
        "assignment_title": "Day 01", "file_name": "plan.png",
        "file_path": "/uploads/plan.png", "notes": "",
        "feedback": json.dumps({"notGraded": True,
                                "message": "We could not open your file."}),
        "submitted_at": ""})
    assert row["who_fixes"] == pr.ACT_STUDENT          # before evidence
    row = pr.apply_probe(row, 200, "FILE EXISTS — agent-side reading gap. ...")
    assert row["who_fixes"].startswith("US")           # after evidence
    assert "Nothing" in row["what_student_should_do"]


def test_a_404_keeps_the_student_on_the_messaging_list():
    row = pr.to_report_row({
        "student_name": "", "student_id": 641, "assignment_id": 17,
        "assignment_title": "", "file_name": "inbound.png",
        "file_path": "/uploads/inbound.png", "notes": "",
        "feedback": json.dumps({"notGraded": True,
                                "message": "We could not open your file."}),
        "submitted_at": ""})
    row = pr.apply_probe(row, 404, "file missing on server — student must re-upload")
    assert row["who_fixes"] == pr.ACT_STUDENT
    assert row["probe_status"] == 404


def test_file_references_resolve_the_way_the_agent_resolves_them():
    assert pr.resolve_file_url("https://x.com/a.png") == "https://x.com/a.png"
    assert pr.resolve_file_url("/uploads/a b.png").startswith("http")
    assert "/uploads/a%20b.png" in pr.resolve_file_url("/uploads/a b.png")
    assert pr.resolve_file_url("") == ""
    assert pr.resolve_file_url("not-a-path") == ""


def test_the_report_never_writes():
    import inspect
    src = inspect.getsource(pr).lower()
    for verb in ("update ", "delete ", "insert ", "drop ", "replace into"):
        assert verb not in src, f"problem_report contains a write: {verb!r}"
