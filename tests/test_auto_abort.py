"""A sweep that is going wrong says so in its first ten results.

Day 03 (assignment 20) ran three hours and marked ~174 learners under an
invented rubric before it was stopped by hand. Day 05 produced a wall of zeros
and ran to completion. Nothing was watching either time.
"""

import os
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bulk_review import ABORT_MIN_SAMPLE, abort_reason


@dataclass
class FakeResult:
    ok: bool = True
    skipped: str = ""
    score: float | None = 60.0
    extra: dict = field(default_factory=dict)


def good(score=60.0):
    return FakeResult(ok=True, score=score)


def failure():
    return FakeResult(ok=False, score=None)


def skip():
    return FakeResult(ok=False, skipped="unreadable_published_link", score=None)


# ── the run is not judged too early ─────────────────────────────────────────

def test_a_short_run_is_never_aborted():
    """Three failures out of three could be three bad files."""
    assert abort_reason([failure(), failure(), failure()]) == ""


def test_the_sample_size_is_the_full_ten():
    nine = [failure() for _ in range(ABORT_MIN_SAMPLE - 1)]
    assert abort_reason(nine) == ""
    assert abort_reason(nine + [failure()]) != ""


# ── failure rate ────────────────────────────────────────────────────────────

def test_half_the_run_failing_stops_it():
    why = abort_reason([failure()] * 5 + [good()] * 5)
    assert "FAILED" in why


def test_a_few_failures_among_many_successes_do_not_stop_it():
    assert abort_reason([failure()] * 2 + [good(g) for g in range(50, 68)]) == ""


# ── the wall of zeros ───────────────────────────────────────────────────────

def test_a_wall_of_zeros_stops_the_run():
    why = abort_reason([good(0.0)] * 9 + [good(55.0)])
    assert "ZERO" in why


def test_genuinely_low_but_varied_marks_are_left_alone():
    """A weak cohort is not a broken marker. This must never fire on one."""
    assert abort_reason([good(m) for m in
                         (10, 15, 22, 8, 30, 12, 25, 18, 35, 20)]) == ""


def test_one_zero_among_real_marks_is_normal():
    assert abort_reason([good(0.0)] + [good(m) for m in range(40, 58, 2)]) == ""


# ── the marker that is not discriminating ───────────────────────────────────

def test_identical_marks_across_ten_students_stop_the_run():
    why = abort_reason([good(45.0)] * 10)
    assert "identical" in why


def test_near_identical_but_distinct_marks_are_allowed():
    assert abort_reason([good(45.0 + i / 10) for i in range(10)]) == ""


# ── skips are the system working, not failing ───────────────────────────────

def test_skips_are_excluded_from_the_failure_rate():
    """Ten refusals to mark unreadable work is CORRECT behaviour. Counting
    them as failures would abort every link day."""
    assert abort_reason([skip()] * 10 + [good(55.0)] * 3) == ""


def test_a_run_that_is_all_skips_is_not_aborted():
    assert abort_reason([skip()] * 30) == ""


def test_skips_do_not_dilute_a_real_failure_rate():
    """Five real failures out of ten judged still stops, however many skips
    sit beside them."""
    why = abort_reason([skip()] * 20 + [failure()] * 5 + [good()] * 5)
    assert "FAILED" in why


# ── the wiring ──────────────────────────────────────────────────────────────

def test_the_loop_cancels_outstanding_work_when_it_aborts():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "bulk_review.py"), encoding="utf-8").read()
    block = src[src.index("stop = \"\" if args.no_abort"):
                src.index("RUN STOPPED") + 200]
    assert "cancel()" in block, "an abort that lets queued reviews run is not an abort"


def test_the_canary_flag_caps_the_run_at_the_sample_size():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "bulk_review.py"), encoding="utf-8").read()
    assert "args.limit = min(args.limit, ABORT_MIN_SAMPLE)" in src


def test_there_is_an_escape_hatch_but_it_is_off_by_default():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "bulk_review.py"), encoding="utf-8").read()
    assert '"--no-abort", action="store_true"' in src
