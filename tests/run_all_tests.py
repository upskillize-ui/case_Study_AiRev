"""One command, four hours, unattended. It must resume, never lose evidence,
and never use the row-duplicating path.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import run_all


def test_it_always_uses_the_in_place_path():
    """--redo rewrites the row. Without it, every review INSERTs a duplicate —
    assignment 14 went from 49 rows to 95 that way."""
    cmd = run_all.build_command(14, "log.csv", 6, 1000)
    assert "--redo" in cmd


def test_each_assignment_runs_in_its_own_invocation():
    """One assignment per call keeps the rubric and knowledge pack in the
    prompt cache. Interleaving misses it on every single call."""
    cmd = run_all.build_command(14, "log.csv", 6, 1000)
    assert cmd.count("--assignment-id") == 1
    assert "14" in cmd


def test_logs_are_timestamped_per_assignment():
    """bulk_review.py opens its log with 'w'. Shared names destroyed the 13 Aug
    batch record."""
    a = run_all.log_name(14, "20260814_1930")
    b = run_all.log_name(17, "20260814_1930")
    c = run_all.log_name(14, "20260814_2100")
    assert a != b, "two assignments must not share a log"
    assert a != c, "two runs must not share a log"
    assert a.endswith(".csv")


def test_smallest_assignment_runs_first():
    """A fault should surface after 86 reviews, not 426."""
    assert run_all.COURSE_ASSIGNMENTS[0] == 23
    assert run_all.COURSE_ASSIGNMENTS[-1] == 17


def test_the_whole_course_is_covered():
    assert set(run_all.COURSE_ASSIGNMENTS) == {14, 17, 18, 19, 20, 21, 22, 23}


# ── resume ────────────────────────────────────────────────────────────────

def test_a_fresh_run_does_everything():
    assert run_all.plan([1, 2, 3], []) == [1, 2, 3]


def test_finished_assignments_are_skipped_on_resume():
    assert run_all.plan([1, 2, 3], [1, 2]) == [3]


def test_a_fully_finished_run_has_nothing_left():
    assert run_all.plan([1, 2], [1, 2]) == []


def test_resume_preserves_order():
    assert run_all.plan([23, 22, 21, 17], [22]) == [23, 21, 17]


def test_unknown_ids_in_the_state_file_are_harmless():
    """A stale resume file from a different scope must not break the run."""
    assert run_all.plan([1, 2], [99]) == [1, 2]


# ── credentials ───────────────────────────────────────────────────────────

def test_all_three_credentials_are_required():
    """A missing admin key would silently bill 1,700 students' credits."""
    assert set(run_all.REQUIRED_ENV) == {
        "AIREV_DB_URL", "AIREV_API_KEY", "AIREV_ADMIN_KEY"}
