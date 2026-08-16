"""The operator tools must at least IMPORT.

tools/ is not touched by any other test, so nothing caught a dataclass field
ordering fault in bulk_review.py — the module raised TypeError at import and the
first anyone knew was a traceback in front of a live correction run.

These are cheap smoke tests. They do not need a database or a network.
"""
import importlib
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

TOOLS = ["bulk_review", "audit_reviews", "clear_unfair_zeros"]


@pytest.mark.parametrize("name", TOOLS)
def test_the_tool_imports(name):
    if not os.path.exists(os.path.join(ROOT, "tools", name + ".py")):
        pytest.skip(f"{name}.py not present")
    importlib.import_module(name)


def test_pending_constructs_without_a_grade():
    """Defaulted fields must stay LAST — this is the exact fault that shipped."""
    b = importlib.import_module("bulk_review")
    p = b.Pending(review_type="assignment", submission_id=1, item_id=17,
                  student_id=9, title="Day 01", notes_len=0,
                  file_name="x.png", submitted_at="2026-08-13")
    assert p.current_grade is None
    assert p.has_content is True          # a filename alone is content


def test_pending_carries_a_grade_for_redo_runs():
    b = importlib.import_module("bulk_review")
    p = b.Pending("assignment", 2, 17, 9, "Day 01", 120, "", "2026-08-13", 6.0)
    assert p.current_grade == 6.0


def test_redo_only_targets_types_with_an_in_place_endpoint():
    """--redo must never fall back to the student submit endpoint, which
    INSERTs a duplicate submission for every learner."""
    b = importlib.import_module("bulk_review")
    assert "regrade_endpoint" in b.TYPES["assignment"]
    assert "regrade_endpoint" not in b.TYPES["casestudy"]


# ── --redo must cover the whole population, not just graded rows ──────────
# Assignment 14 went from 49 submission rows to 95 in a single run, because
# --redo only selected already-graded rows and everything else fell through to
# the student submit endpoint, which INSERTs.

def _sql(redo):
    import importlib
    b = importlib.import_module("bulk_review")
    captured = {}

    class FakeCur:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, sql, params): captured["sql"] = sql
        def fetchall(self): return []

    class FakeConn:
        def cursor(self): return FakeCur()

    b.fetch_pending(FakeConn(), "assignment", 14, redo=redo)
    return captured["sql"]


def test_redo_does_not_filter_on_grade():
    """Re-scoring in place is valid whether or not a grade is present."""
    sql = _sql(redo=True)
    assert "s.grade IS NOT NULL" not in sql
    assert "s.grade IS NULL" not in sql


def test_the_normal_path_still_targets_unreviewed_rows_only():
    assert "s.grade IS NULL" in _sql(redo=False)


def test_both_modes_scope_to_the_requested_assignment():
    for redo in (True, False):
        assert "assignment_id = %s" in _sql(redo)
