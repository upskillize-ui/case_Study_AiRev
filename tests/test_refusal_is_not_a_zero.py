"""A refusal must never be reported as a zero.

23 Aug, the Day 07 canary printed:

    [7/10] OK   item 24 student 872 -> 0.0/10 (0%) F
    [10/10] OK  item 24 student 525 -> 0.0/10 (0%) F

Both rows were in fact NOT GRADED — grade=None, status='submitted', carrying
the guard's own message. The database was right. The report was wrong, and it
was the report Ranjana was reading to decide whether to release the run to
183 more students.

The cause: update_assignment_submission_with_ai_results returns False when the
guard refuses, and the route ignored that return value — answering success
with result['totalScore'] = 0 still in the payload. A guard that works and
then lies about having worked is worse than no guard, because it teaches you
to distrust the one signal that was telling the truth.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bulk_review
from app.routes import assignment_review as ar


# ── the route ───────────────────────────────────────────────────────────────

def test_the_route_reads_the_writers_answer():
    src = inspect.getsource(ar._pipeline_assignment_response)
    assert "written = assignment_db_service.update_assignment_submission" in src, \
        "ignoring the return value is the whole bug"
    assert "if written is False:" in src


def test_a_refused_write_answers_not_graded_not_a_score():
    src = inspect.getsource(ar._pipeline_assignment_response)
    block = src[src.index("if written is False:"):]
    assert '"status": "not_graded"' in block
    assert '"notGraded": True' in block
    assert '"totalScore"' not in block, "a refusal must carry no score at all"


def test_the_refusal_check_precedes_the_normal_response():
    src = inspect.getsource(ar._pipeline_assignment_response)
    assert src.index("if written is False:") < src.index("_build_response(")


def test_the_learner_is_told_it_is_not_their_fault():
    src = inspect.getsource(ar._pipeline_assignment_response)
    block = src[src.index("if written is False:"):src.index("_build_response(")]
    assert "no \n                \"marks have been recorded" in block \
        or "marks have been recorded" in block
    assert "Nothing you submitted is lost" in block


# ── the reporting tool ──────────────────────────────────────────────────────

def test_the_tool_treats_a_refusal_as_a_skip():
    src = inspect.getsource(bulk_review.review_one)
    assert 'data.get("notGraded") or data.get("status") == "not_graded"' in src
    block = src[src.index('data.get("notGraded")'):]
    block = block[:block.index('if data.get("status") == "wrong_task"')]
    assert 'skipped="not_graded"' in block


def test_the_refusal_branch_runs_before_the_success_branch():
    """Ordering is the bug. success=True with feedback was reached first, and
    fb['score'] of 0 printed as a mark."""
    src = inspect.getsource(bulk_review.review_one)
    assert src.index('data.get("notGraded")') < src.index('if data.get("success") and fb:')


def test_a_refusal_is_not_counted_as_a_review():
    """`Result(ok=False, skipped=...)` keeps it out of the reviewed tally AND
    out of the auto-abort's failure ratio — a refusal is the system working."""
    from bulk_review import abort_reason

    class R:
        def __init__(self):
            self.ok, self.skipped, self.score = False, "not_graded", None
    assert abort_reason([R() for _ in range(20)]) == "", \
        "twenty correct refusals must not look like a broken run"


# ── and a genuine zero still reports as a zero ─────────────────────────────

def test_a_real_zero_is_still_reported():
    """Student 220 scored 0 with quoted evidence and three improvements. That
    is a judgement and must still print as a mark."""
    src = inspect.getsource(bulk_review.review_one)
    assert 'return Result(p, True, score=fb.get("score")' in src
