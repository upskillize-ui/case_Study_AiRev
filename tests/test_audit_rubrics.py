"""The rubric audit answers the question every investigation so far asked too
late: of the 100 marks on this assignment, how many can a learner actually earn
from what they submit?

Day 06 answered 65. Thirty-five marks sat behind "ChatGPT lyrics with music
style line" and "Suno Custom mode used" — facts a finished song cannot carry.
That was readable in the rubric row for free, before any review ran.

The logic is pure, so it is proven here without a database and without an AI
call.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import audit_rubrics as ar
from app.services.rubric_service import FALLBACK_CRITERIA


# The real Day 06 rubric, read off the live agent on 16 Aug.
DAY_06 = [
    {"name": "Song file or link submitted", "maxScore": 35},
    {"name": "Patriotic India theme", "maxScore": 30},
    {"name": "ChatGPT lyrics with music style line", "maxScore": 20},
    {"name": "Suno Custom mode used", "maxScore": 15},
]

# What Day 01 should look like: every mark readable from what arrives.
DAY_01_GOOD = [
    {"name": "AI image of the 5-year future self is present", "maxScore": 30},
    {"name": "A specific career is named", "maxScore": 25},
    {"name": "Qualifications needed are identified", "maxScore": 25},
    {"name": "Five steps are listed and concrete", "maxScore": 20},
]


def test_it_finds_the_thirty_five_unreachable_marks_on_day_06():
    assert ar.reachable_marks(DAY_06) == 65, [
        (c["name"], ar.classify(c)) for c in DAY_06]


def test_a_fair_rubric_reports_a_full_ceiling():
    assert ar.reachable_marks(DAY_01_GOOD) == 100, [
        (c["name"], ar.classify(c)) for c in DAY_01_GOOD]


def test_the_two_kinds_of_problem_are_reported_differently():
    """A shape strip_offplatform matches is removed automatically. A shape it
    cannot match safely is reported for a human — never silently stripped,
    because an over-broad pattern once dropped nine legitimate criteria."""
    assert ar.classify({"name": "Suno Custom mode used"})[0] == "stripped"
    assert ar.classify({"name": "ChatGPT lyrics with music style line"})[0] == "suspect"
    assert ar.classify({"name": "A specific career is named"})[0] == "ok"


def test_real_criteria_are_not_called_suspect():
    """False positives here would send someone rewriting a rubric that was
    fine. These are all legitimate and must come back clean."""
    for name in ("Song file or link submitted", "Patriotic India theme",
                 "Five steps are listed and concrete", "Structure and clarity",
                 "Creative use of the prompt", "Qualifications needed are identified",
                 "Recommendation is actionable", "Evidence supports the argument"):
        assert ar.classify({"name": name})[0] == "ok", name


def test_the_length_trap_is_caught_on_an_image_task():
    """An image task's text is a caption. wordMin=100 costs a 60-word
    submission real marks for doing exactly what was asked."""
    trap = ar.length_trap(100, "image")
    assert trap and "penalty" in trap
    assert ar.length_trap(20, "image") == ""
    assert ar.length_trap(300, "written") == "", "essays may demand length"


def test_a_fallback_rubric_is_named_as_one():
    """The generic rubric means derivation failed — the assignment is being
    marked against criteria it was never written for."""
    assert ar.is_fallback([dict(c) for c in FALLBACK_CRITERIA]) is True
    assert ar.is_fallback(DAY_01_GOOD) is False


def test_the_audit_never_writes():
    """It reads live coursework rows. Read-only is the whole safety story."""
    import inspect
    src = inspect.getsource(ar).lower()
    for verb in ("update ", "delete ", "insert ", "drop ", "commit()", "replace into"):
        assert verb not in src, f"audit_rubrics contains a write: {verb!r}"
