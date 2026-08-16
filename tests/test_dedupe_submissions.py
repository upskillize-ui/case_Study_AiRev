"""Deleting a learner's submission row is irreversible in effect, even with a
backup — so the decision logic is tested before it is ever pointed at the
database.

The rows this tool exists to clean were created by a mistake of mine: the bulk
run used the student submit endpoint, which INSERTs, and assignment 14 went
from 49 rows to 95. The one outcome that would make it worse is deleting a real
mark to keep an empty one.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import dedupe_submissions as dd


def row(i, grade=None, aid=14, sid=201):
    return {"id": i, "assignment_id": aid, "student_id": sid, "grade": grade}


# ── the core decision ─────────────────────────────────────────────────────

def test_a_single_row_is_never_touched():
    d = dd.plan([row(10, grade=5)])
    assert d["keep"] == 10 and d["drop"] == []


def test_no_rows_at_all_is_survivable():
    assert dd.plan([])["drop"] == []


def test_the_newest_row_is_kept():
    """The newest holds the latest review, so keeping it preserves the mark."""
    d = dd.plan([row(10, 3), row(55, 7), row(31, 4)])
    assert d["keep"] == 55
    assert sorted(d["drop"]) == [10, 31]


def test_order_of_input_does_not_matter():
    a = dd.plan([row(10, 3), row(55, 7)])
    b = dd.plan([row(55, 7), row(10, 3)])
    assert a == b


def test_the_kept_row_is_never_in_the_drop_list():
    d = dd.plan([row(1, 2), row(2, 3), row(3, 4)])
    assert d["keep"] not in d["drop"]


def test_every_row_is_either_kept_or_dropped():
    rows = [row(i, 1) for i in (4, 9, 2, 7)]
    d = dd.plan(rows)
    assert {d["keep"], *d["drop"]} == {r["id"] for r in rows}


# ── the refusal that matters ──────────────────────────────────────────────

def test_it_refuses_to_drop_a_grade_to_keep_an_ungraded_row():
    """The one outcome nobody wants: the newest row is an ungraded duplicate
    and an older row holds the learner's real mark."""
    d = dd.plan([row(99, grade=None), row(50, grade=8)])
    assert d["drop"] == []
    assert d["skip_reason"] and "no grade" in d["skip_reason"]
    assert "50" in d["skip_reason"], "the reason must name the graded row"


def test_an_ungraded_newest_with_ungraded_older_rows_is_fine():
    """Nothing to lose — cleared-grade rows are the normal case here."""
    d = dd.plan([row(99, None), row(50, None)])
    assert sorted(d["drop"]) == [50]
    assert d["skip_reason"] is None


def test_a_graded_newest_row_supersedes_older_grades():
    d = dd.plan([row(99, grade=6), row(50, grade=8)])
    assert d["drop"] == [50]
    assert d["skip_reason"] is None


def test_zero_is_a_grade_not_an_absence_on_the_newest_row():
    d = dd.plan([row(99, grade=0), row(50, grade=8)])
    assert d["drop"] == [50], "grade 0 on the newest row must count as graded"


def test_zero_is_a_grade_not_an_absence_on_an_OLDER_row():
    """The case a mutation run caught me missing.

    The refusal inspects OLDER rows for a grade. Testing truthiness instead of
    `is not None` there means an older row marked 0.0 reads as ungraded, so the
    tool would delete a real mark to keep an empty row. 0.0 is a mark — that is
    the entire premise of this project's unfair-zero work.
    """
    d = dd.plan([row(99, grade=None), row(50, grade=0)])
    assert d["drop"] == [], "an older row graded 0 must block the delete"
    assert d["skip_reason"] and "50" in d["skip_reason"]


# ── grouping ──────────────────────────────────────────────────────────────

def test_learners_are_grouped_separately():
    g = dd.group_by_learner([row(1, sid=201), row(2, sid=201), row(3, sid=999)])
    assert len(g) == 2
    assert len(g[(14, 201)]) == 2


def test_the_same_student_on_two_assignments_is_two_groups():
    g = dd.group_by_learner([row(1, aid=14), row(2, aid=17)])
    assert len(g) == 2


# ── backup naming ─────────────────────────────────────────────────────────

def test_the_backup_name_is_derived_from_the_stamp():
    assert dd.backup_table_name("20260814") == "airev_dupe_backup_20260814"


@pytest.mark.parametrize("stamp", ["2026-08-14", "2026;DROP TABLE x", "../../etc"])
def test_the_backup_name_cannot_carry_sql(stamp):
    """The name is interpolated into DDL, so it must contain nothing else."""
    name = dd.backup_table_name(stamp)
    assert all(c.isalnum() or c == "_" for c in name), name
