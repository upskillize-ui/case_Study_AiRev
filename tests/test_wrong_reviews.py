"""Which students hold a mark that cannot be defended.

Ranjana, 23 Aug: "if we are do re-review do only those students who gets
wrong reviews not for all for previous review like day-1, 3, 4, 5 like that."

Re-running a whole day costs money, costs hours, and disturbs marks that were
already fair. Every rule here is a fault that can be PROVEN from the stored
row — never a guess, because a guess would drag correct marks into the re-run
and the whole point is to keep it narrow.

The fixtures are the real shapes: the 21 Day-04 rows marked while their own
feedback said the page could not be read, the 11 bare zeros, student 220's
placeholder, and everything graded before requirements replaced the rubric.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from wrong_reviews import (MARKED_BLIND, NO_SUBSTANCE, OLD_RULES, PLACEHOLDER,
                           graded_under_old_rules, has_mark,
                           has_placeholder_judgement, marked_blind,
                           no_substance, verdicts_for)


def good_review(**over) -> dict:
    """A mark that IS defensible: current rules, real evidence, real advice."""
    base = {
        "scoreMarks": 4.5, "outOf": 10,
        "facultyView": {"requirements": [
            {"requirement": "A dashboard is present", "scored": 4.5,
             "outOf": 10, "note": "One chart, no data set named.",
             "evidence": ["the submitted page shows a single bar chart"]}]},
        "improvements": ["Name the data set you used."],
        "feedbackPoints": ["Your chart has no axis labels."],
        "audit": {"rulesVersion": 6},
    }
    base.update(over)
    return base


# ── a sound mark is left alone ──────────────────────────────────────────────

def test_a_defensible_mark_is_not_in_the_list():
    assert verdicts_for(good_review(), 4.5) == []


def test_a_low_mark_with_real_reasons_is_still_defensible():
    """Harshness is not a fault. Only unsubstantiated marks are."""
    assert verdicts_for(good_review(scoreMarks=0.0), 0.0) == []


# ── a row with no mark is never re-reviewed ─────────────────────────────────

def test_a_row_the_guard_refused_is_not_in_the_list():
    """grade NULL + notGraded is the system working. Nothing to re-review."""
    refused = {"notGraded": True, "reviewedBy": "airev",
               "message": "We could not complete a fair review"}
    assert has_mark(refused, None) is False
    assert verdicts_for(refused, None) == []


def test_a_null_grade_alone_is_enough_to_skip_it():
    assert verdicts_for(good_review(), None) == []


# ── MARKED BLIND: the 21 Day-04 rows ────────────────────────────────────────

def test_a_mark_beside_its_own_could_not_be_read_is_caught():
    row = good_review(detailedFeedback=(
        "The Notion page you submitted is not readable through automated "
        "access, so this was judged on your description alone."))
    assert marked_blind(row) is True
    assert MARKED_BLIND in verdicts_for(row, 2.5)


def test_the_manifest_is_read_too():
    row = good_review(audit={"rulesVersion": 6,
                             "manifest": "1. LINK — could not be read (private)"})
    assert MARKED_BLIND in verdicts_for(row, 3.0)


def test_ordinary_feedback_is_not_mistaken_for_a_failed_read():
    assert marked_blind(good_review()) is False


# ── NO SUBSTANCE: the 11 bare zeros ─────────────────────────────────────────

def test_a_score_with_nothing_behind_it_is_caught():
    bare = {"scoreMarks": 0.0, "outOf": 10,
            "summary": "You scored 0 out of 10.",
            "audit": {"rulesVersion": 6}}
    assert no_substance(bare) is True
    assert NO_SUBSTANCE in verdicts_for(bare, 0.0)


def test_one_improvement_is_enough_substance():
    row = {"scoreMarks": 0.0, "improvements": ["Add a data set."],
           "audit": {"rulesVersion": 6}}
    assert no_substance(row) is False


def test_requirement_rows_alone_are_substance():
    row = {"scoreMarks": 3.0, "audit": {"rulesVersion": 6},
           "facultyView": {"requirements": [{"requirement": "X", "scored": 3}]}}
    assert no_substance(row) is False


# ── PLACEHOLDER: student 220's shape ────────────────────────────────────────

def test_a_number_beside_no_assessment_available_is_caught():
    row = {"scoreMarks": 0.0, "audit": {"rulesVersion": 6},
           "facultyView": {"requirements": [
               {"requirement": "Dashboard created from a data set",
                "scored": 0.0, "note": "No assessment available."}]}}
    assert has_placeholder_judgement(row) is True
    assert PLACEHOLDER in verdicts_for(row, 0.0)


def test_the_legacy_judgment_key_is_read_too():
    row = {"scoreMarks": 0.0, "audit": {"rulesVersion": 6},
           "facultyView": {"rubricScores": [
               {"criteria": "X", "judgment": "No assessment available."}]}}
    assert has_placeholder_judgement(row) is True


def test_a_real_note_is_not_a_placeholder():
    assert has_placeholder_judgement(good_review()) is False


# ── OLD RULES: Days 03, 04, 05 ──────────────────────────────────────────────

def test_a_review_with_no_audit_record_predates_the_fix():
    """Everything from that era was judged against an invented rubric, so the
    absence of a version IS the evidence."""
    old = {"scoreMarks": 6.0, "improvements": ["x"]}
    assert graded_under_old_rules(old) is True
    assert OLD_RULES in verdicts_for(old, 6.0)


def test_an_older_rules_version_is_caught():
    assert graded_under_old_rules({"audit": {"rulesVersion": 5}}) is True


def test_the_current_rules_version_passes():
    assert graded_under_old_rules({"audit": {"rulesVersion": 6}}) is False


def test_a_future_version_passes():
    assert graded_under_old_rules({"audit": {"rulesVersion": 9}}) is False


def test_the_old_field_name_is_still_honoured():
    """Reviews written between the audit record landing and the rename."""
    assert graded_under_old_rules({"audit": {"rubricVersion": 6}}) is False


def test_an_unparseable_version_is_treated_as_old():
    assert graded_under_old_rules({"audit": {"rulesVersion": "six"}}) is True


# ── several faults at once ──────────────────────────────────────────────────

def test_every_provable_fault_is_reported_worst_first():
    row = {"scoreMarks": 0.0,
           "detailedFeedback": "the page could not be read",
           "facultyView": {"requirements": [
               {"requirement": "X", "note": "No assessment available."}]}}
    found = verdicts_for(row, 0.0)
    assert found[0] == MARKED_BLIND
    assert set(found) == {MARKED_BLIND, PLACEHOLDER, OLD_RULES}


# ── the tool writes nothing ─────────────────────────────────────────────────

def test_the_tool_only_reads():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "wrong_reviews.py"), encoding="utf-8").read()
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "REPLACE INTO"):
        assert forbidden not in src.upper(), f"a read-only tool must not {forbidden}"
    assert "SELECT" in src.upper()
