"""The 1126 nesting rule, on the SUBMIT path (28 Aug 2026).

Every review path writes `manifest + content` back into notes. A later call
that reads that row and ALSO re-extracts the attachment hands the marker the
OCR twice — the second copy wrapped in the previous manifest and presented as
the learner's own typing, with our instructions to the judge inside it.

The regrade route has refused this since student 1126 fell from 6.8/10 to
1.2/10 on identical input. The submit route did not, and `bulk_review.py`
without --redo posts THERE — which on 28 Aug produced six 0.0s out of nine on
Day 11, including a Speaker Report Card carrying every element the brief asked
for: name, session type, six metrics, strengths, weaknesses, three action
points and the Yoodli share link.

What makes this defect expensive is that it is invisible in the output: the
mark is a number like any other, and only reading the assembled prompt shows
why it is wrong.
"""
from app.utils import submission_intake as intake

ASSEMBLED = """=== SUBMISSION MANIFEST ===
The learner submitted 1 item(s):
  1. IMAGE — report.png — read (416 words of content, item 1 below)
This list is the record of WHAT was submitted, not of its quality. Judge
quality, depth and correctness only from the content below.
=== ITEM 1: IMAGE (report.png) ===
TEXT:
SPEAKER REPORT CARD
FILLER WORDS 14  PACING 138 WPM  CLARITY 8.5
=== ITEM 2: TYPED TEXT (answer box) ===
I practised a three minute talk and built the report card.
"""

RAW = "I practised a three minute talk and built the report card."


def test_an_assembly_is_recognised_as_one():
    assert intake.from_stored_submission(ASSEMBLED) is not None


def test_raw_learner_text_is_not_mistaken_for_an_assembly():
    """A learner who happens to write plainly must not have their answer
    filtered — from_typed has to keep passing raw text straight through."""
    assert intake.from_stored_submission(RAW) is None


def test_only_the_learners_own_words_survive_an_assembly():
    out = intake.typed_text_from(ASSEMBLED)
    assert "I practised a three minute talk" in out
    assert "SUBMISSION MANIFEST" not in out
    assert "=== ITEM" not in out
    assert "SPEAKER REPORT CARD" not in out, (
        "the OCR came through as the learner's typing — that is the second "
        "copy the marker must never see")


def test_the_judges_own_instructions_never_reach_the_answer():
    """The manifest tells the model how to weigh the submission. Inside
    <student_submission> it becomes text the model is told to ignore, and the
    learner's real answer is buried under it."""
    out = intake.typed_text_from(ASSEMBLED)
    for instruction in ("record of WHAT was submitted",
                        "Judge quality, depth and correctness"):
        assert instruction not in out


def test_the_submit_route_applies_the_guard():
    """Pins the call, not just the helper: the helper existed all along and
    the submit route simply never used it."""
    import inspect
    from app.routes import assignment_review
    src = inspect.getsource(assignment_review.submit_and_review_assignment)
    assert "from_stored_submission(db_notes)" in src, (
        "submit path no longer checks whether stored notes are an assembly")
    assert "typed_text_from(db_notes)" in src
