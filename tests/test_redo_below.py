"""--redo-below: the targeted final pass.

After the 19-20 Aug rule fixes (count-literalism, prompt-provenance,
wrong-task overfire), the rows needing a re-score are the LOW and UNGRADED
ones — Dhruv's 4.3 earned under a judge that punished 8 steps for not being
5, Anushka's 0.0 from an unread image. The 7-9s were scored under the fixed
rules and re-buying ~200 of those reviews would cost real money to change
nothing. keep_below() is that filter, pure and provable.
"""
import os
import sys
from types import SimpleNamespace as Row

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from bulk_review import keep_below


def _ids(rows):
    return [r.student_id for r in rows]


def test_low_and_ungraded_rows_are_kept_high_ones_spared():
    pending = [
        Row(student_id=200, current_grade=4.3),    # Dhruv — suspect
        Row(student_id=405, current_grade=8.7),    # fair, spare it
        Row(student_id=1301, current_grade=0.0),   # Anushka — suspect
        Row(student_id=1224, current_grade=9.2),   # fair, spare it
        Row(student_id=641, current_grade=None),   # never graded — suspect
    ]
    assert _ids(keep_below(pending, 7.0)) == [200, 1301, 641]


def test_the_threshold_boundary_spares_exactly_at_the_bar():
    """'Below 7' must not re-buy a 7.0 — at the bar is at the bar."""
    pending = [Row(student_id=1, current_grade=7.0),
               Row(student_id=2, current_grade=6.9)]
    assert _ids(keep_below(pending, 7.0)) == [2]


def test_unparseable_grades_count_as_suspect_never_as_safe():
    """A DECIMAL that arrives as a string, or junk, must not be silently
    treated as 'fair mark, skip it' — when in doubt, re-score."""
    pending = [Row(student_id=1, current_grade="6.5"),
               Row(student_id=2, current_grade="8.5"),
               Row(student_id=3, current_grade="not-a-number")]
    assert _ids(keep_below(pending, 7.0)) == [1, 3]


def test_an_empty_cohort_is_fine():
    assert keep_below([], 7.0) == []
