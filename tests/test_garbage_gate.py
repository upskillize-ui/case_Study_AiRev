"""A garbage verdict must not zero a genuine attempt.

Pinned to the real batch of 13 Aug 2026, where every one of these was flagged
garbage and scored a hard 0 despite being a real submission:

    student 1360 — 125 words (OCR'd from their image)
    students 479, 492, 937, 394, 312, 1052, 116 — 37 to 135 words

Only objectively negligible content may bypass the rubric now.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services.review_pipeline import should_hard_zero, GARBAGE_HARD_ZERO_MAX_WORDS

GARBAGE = {"is_garbage": True}
GENUINE = {"is_garbage": False}

# word counts straight out of the 13 Aug run
REAL_SUBMISSIONS_ZEROED = [125, 37, 41, 50, 76, 95, 135, 152, 164, 169, 193, 199]


def test_real_submissions_from_13aug_are_no_longer_zeroed():
    for words in REAL_SUBMISSIONS_ZEROED:
        if words <= GARBAGE_HARD_ZERO_MAX_WORDS:
            continue                      # genuinely tiny; the rubric has nothing
        assert should_hard_zero(GARBAGE, words) is False, (
            f"{words}-word submission would still be hard-zeroed")


def test_the_125_word_case_specifically():
    """Student 1360: 125 words OCR'd from an image, flagged garbage, scored 0."""
    assert should_hard_zero(GARBAGE, 125) is False


def test_negligible_content_still_zeroes():
    for words in (0, 1, 5, GARBAGE_HARD_ZERO_MAX_WORDS):
        assert should_hard_zero(GARBAGE, words) is True, words


def test_boundary_is_inclusive_then_releases():
    assert should_hard_zero(GARBAGE, GARBAGE_HARD_ZERO_MAX_WORDS) is True
    assert should_hard_zero(GARBAGE, GARBAGE_HARD_ZERO_MAX_WORDS + 1) is False


def test_a_genuine_verdict_never_zeroes_whatever_the_length():
    for words in (0, 10, 500, 5000):
        assert should_hard_zero(GENUINE, words) is False, words


def test_unknown_length_trusts_the_verdict():
    """Missing word count must fail closed, not silently pass a real zero."""
    assert should_hard_zero(GARBAGE, None) is True
    assert should_hard_zero(GARBAGE, "not-a-number") is True
    assert should_hard_zero(GENUINE, None) is False


def test_threshold_is_configurable():
    os.environ["GARBAGE_HARD_ZERO_MAX_WORDS"] = "10"
    sys.modules.pop("app.services.review_pipeline", None)
    rp = importlib.import_module("app.services.review_pipeline")
    try:
        assert rp.GARBAGE_HARD_ZERO_MAX_WORDS == 10
        assert rp.should_hard_zero(GARBAGE, 11) is False
        assert rp.should_hard_zero(GARBAGE, 10) is True
    finally:
        os.environ.pop("GARBAGE_HARD_ZERO_MAX_WORDS", None)
        sys.modules.pop("app.services.review_pipeline", None)


def test_schema_defines_what_garbage_means():
    """The field had no description, which is why the model over-triggered."""
    sys.modules.pop("app.services.review_pipeline", None)
    rp = importlib.import_module("app.services.review_pipeline")
    desc = rp.REVIEW_SCHEMA["properties"]["is_garbage"].get("description", "")
    assert desc, "is_garbage must tell the model what garbage is"
    assert "not an attempt" in desc.lower() or "not a genuine" in desc.lower()
    assert "however short" in desc.lower()


# ── tidy_review must never reach into scoring ─────────────────────────────

def test_trimming_concepts_does_not_move_the_coverage_ratio():
    """concepts_missing / concepts_covered feed apply_gates, which divides one
    by their sum and caps the TOTAL at 69 below half. Trimming both to four for
    the card pulled every ratio towards 0.5 and changed marks.

    Pinned because the change that did it was committed as 'wording only'.
    """
    import importlib
    rp = importlib.import_module("app.services.review_pipeline")

    covered = [f"concept {i}" for i in range(12)]
    missing = [f"gap {i}" for i in range(3)]
    real_ratio = len(covered) / (len(covered) + len(missing))          # 0.80

    review = {"concepts_covered": list(covered), "concepts_missing": list(missing),
              "strengths": [], "improvements": [], "feedback_points": [],
              "hard_truth": "", "criteria": []}
    tidied = rp.tidy_review(review)
    shown = len(tidied["concepts_covered"]) / (len(tidied["concepts_covered"])
                                               + len(tidied["concepts_missing"]))

    assert shown != real_ratio, "guard is meaningless if trimming changes nothing"
    assert real_ratio >= rp.GATES["concept_min_ratio"]
    # The display copy may differ; what must NOT happen is gating on it.
    # review_with_knowledge calls apply_gates BEFORE tidy_review for this reason.
    import inspect
    src = inspect.getsource(rp.run_review)      # where both calls actually live
    assert "apply_gates(" in src and "tidy_review(review)" in src
    assert src.index("apply_gates(") < src.index("tidy_review(review)"), \
        "tidy_review must run AFTER apply_gates, or trimming changes the score"
