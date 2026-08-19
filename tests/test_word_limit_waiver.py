"""Word limits judge what a learner WROTE. On non-written tasks, word_count
counts what we READ — including OCR text extracted from the learner's own
file — and those are not the same thing.

Live case, 19 Aug 2026, submission 4832 (student 405, Day 01): her image
carried a complete, five-year, 494-word plan. The derived caption guide for
the task was 150 words, so aggregate() charged a 5-point penalty —

    "Answer is 494 words (guide maximum 150) — 5 point penalty."

— fining the one student in the frame who did the task most thoroughly. The
inverse trap (wordMin fining a 60-word caption) was already known; this is
the same defect mirrored, and both are closed the same way: for non-written
submission kinds the limits are waived outright (0 / 999999, the idiom the
capstone route has always used), decided in ONE place shared by the submit
and regrade routes.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.routes.assignment_review import scoring_knobs
from app.services.review_pipeline import aggregate

# A gated result with a perfect breakdown, so any drop below 100 in these
# tests is the word penalty and nothing else.
_PERFECT = {"breakdown": [{"score": 100.0}], "total_cap": 100,
            "error_deduction": 0, "gates_hit": []}

IMAGE_TASK = {"submissionKind": "image", "wordMin": 20, "wordMax": 150}
WRITTEN_TASK = {"submissionKind": "written", "wordMin": 100, "wordMax": 500}


def test_an_image_tasks_ocr_text_is_never_an_overlength_essay():
    """Submission 4832 replayed: 494 words read from the image, caption guide
    150. With the knobs the routes now use, the penalty is gone."""
    _, word_min, word_max = scoring_knobs(IMAGE_TASK)
    scores = aggregate(_PERFECT, word_count=494,
                       word_limit_min=word_min, word_limit_max=word_max)
    assert scores["wordCountPenalty"] == 0, scores["wordCountNote"]
    assert scores["totalScore"] == 100


def test_the_defect_is_real_without_the_waiver():
    """The regression pin: the raw caption limits DO fine 494 words. If this
    ever stops failing-without-the-fix, aggregate() changed and the waiver
    should be re-examined rather than carried as cargo."""
    scores = aggregate(_PERFECT, word_count=494,
                       word_limit_min=IMAGE_TASK["wordMin"],
                       word_limit_max=IMAGE_TASK["wordMax"])
    assert scores["wordCountPenalty"] == 5


def test_a_short_caption_on_an_image_task_is_not_fined_either():
    """The mirror trap. A 12-word caption beside a full deliverable is a
    label, not an under-length essay."""
    _, word_min, word_max = scoring_knobs(IMAGE_TASK)
    scores = aggregate(_PERFECT, word_count=12,
                       word_limit_min=word_min, word_limit_max=word_max)
    assert scores["wordCountPenalty"] == 0


def test_written_tasks_keep_their_limits():
    """An essay may fairly be told it is too short. Waiving limits for
    written work would delete a legitimate judgement, not a bias."""
    _, word_min, word_max = scoring_knobs(WRITTEN_TASK)
    assert (word_min, word_max) == (100, 500)
    scores = aggregate(_PERFECT, word_count=30,
                       word_limit_min=word_min, word_limit_max=word_max)
    assert scores["wordCountPenalty"] > 0


def test_gate_overrides_travel_with_the_same_decision():
    """The generic-answer gate and the word limits key off the same fact
    (submission kind); scoring_knobs must keep them in lockstep so the two
    routes cannot drift apart again."""
    gates_img, *_ = scoring_knobs(IMAGE_TASK)
    gates_txt, *_ = scoring_knobs(WRITTEN_TASK)
    assert gates_img == {"generic_answer_cap": 100}
    assert gates_txt == {}


def test_mixed_kind_counts_as_written():
    """'mixed' tasks ask for real prose alongside a deliverable — the prose
    half keeps its limits."""
    knobs = scoring_knobs({"submissionKind": "mixed", "wordMin": 50, "wordMax": 300})
    assert knobs == ({}, 50, 300)
