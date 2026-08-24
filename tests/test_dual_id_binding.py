"""DUAL_ID_MATCH binds the learner's id THREE times.

24 Aug 2026: the new enqueue route bound it twice and answered HTTP 500 on
its first real use — the very call every student's Submit makes tomorrow.

The fragment expands to three placeholders (students.id, users.id, and the
mapping between them). A caller passing two is a parameter-count mismatch:
MySQL errors, FastAPI returns 500.

A NOTE ON WHAT IS **NOT** HERE. I first wrote a sweep over every caller,
counting id bindings in the code that follows each use. It failed twice — on
industry_session_review (`params += [student_id] * 6`, two fragments in one
query) and on review.py (`(student_id,) * 6`). Both were CORRECT; the test
was not. Teaching a regex every legitimate way to build an argument list is a
losing game, and a guard that accuses working code gets switched off and then
deleted, which leaves nothing.

So this file checks the two things it can check honestly: the fragment's own
shape, and the one caller that actually broke.
"""

from pathlib import Path

from app.database import DUAL_ID_MATCH


APP = Path(__file__).resolve().parent.parent / "app"


def test_the_fragment_really_needs_three_placeholders():
    """If this ever changes, every caller in the codebase needs revisiting —
    and this test failing is the notice to do it."""
    assert DUAL_ID_MATCH.count("%s") == 3


def test_the_enqueue_route_binds_it_three_times():
    """The route that 500'd. Tomorrow every submission goes through it."""
    src = (APP / "routes" / "review_jobs.py").read_text(encoding="utf-8")
    window = src[src.index("DUAL_ID_MATCH}"):]
    window = window[:window.index("if not rows")]
    assert window.count("req.studentId") == 3, (
        "the enqueue route must bind the learner id three times; found "
        f"{window.count('req.studentId')}")


def test_the_route_looks_up_a_submission_rather_than_creating_one():
    """A 500 was one failure mode; the worse one would be inventing a row."""
    src = (APP / "routes" / "review_jobs.py").read_text(encoding="utf-8")
    window = src[src.index("DUAL_ID_MATCH}"):]
    window = window[:window.index("if not rows")]
    assert "SELECT id FROM assignment_submissions" in \
        src[src.index("def enqueue_one"):src.index("DUAL_ID_MATCH}") + 200]
    assert "INSERT" not in window.upper()
