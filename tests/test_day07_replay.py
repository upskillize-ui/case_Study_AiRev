"""REPLAY OF REAL ROWS — not invented inputs.

Every fixture below is taken verbatim from what the Day 07 canary actually
stored on 23 Aug 2026, read back with:

    python tools\\show_review.py --assignment-id 24 --students 220,880,188 --full

The unit tests elsewhere prove each rule in isolation. This file proves the
rules FIRE, IN ROUTE ORDER, on the three rows that were really marked 0.0/10 —
which is the thing unit tests could not tell us this morning.
"""

from app.services import grade_guard as gg
from app.utils import submission_intake as intake
from app.utils.submission_intake import Artefact

# The real task text for assignment 24.
DAY07_TASK = ("Day 07 : Gemini Canvas Create a Dashboard from a data set "
              "using Gemini Canvas")


def _artefacts(link_url, link_readable, typed_chars):
    """A submission in the shape intake really produces."""
    return [
        Artefact(kind="typed text", label="answer box", text="x " * (typed_chars // 2)),
        Artefact(kind="link", label=link_url, confirmed=True,
                 text=("page content " * 40) if link_readable else "",
                 note="" if link_readable else "opened Gemini's own page"),
    ]


def route_outcome(artefacts, task_text, is_garbage: bool):
    """The submit route's gates, in the order the route consults them.

    Returns the name of the gate that fired, or "graded". Kept in one place so
    a reordering in the route shows up here as a changed answer.
    """
    if (intake.link_is_the_deliverable(task_text)
            and intake.link_deliverable_unseen(artefacts)):
        return "no_mark_publish_your_link"
    if is_garbage and intake.link_is_the_deliverable(task_text) \
            and intake.deliverable_is_only_links(artefacts):
        return "no_mark_link_is_not_the_work"
    return "graded"


# ── student 220: Gemini link that opened Google's own page + 6,292 chars ────
#
# Stored: grade 0.00, judgment "No assessment available.", evidence [], every
# feedback field empty, wordCount 1057.

S220_ARTEFACTS = _artefacts("https://share.gemini.google/0u7BSEGjswtk",
                            link_readable=False, typed_chars=6292)
S220_STORED = {
    "totalScore": 0, "wordCount": 1057, "isGarbage": False,
    "strengths": [], "improvements": [], "feedbackPoints": [],
    "detailedFeedback": "", "hardTruth": "", "missingConcepts": [],
    "coveredConcepts": [], "encouragement": "", "nextAction": "",
}
S220_CRITERIA = [{
    "criteria": "Dashboard created from a data set", "maxScore": 100,
    "percentage": 0, "score": 0.0, "status": "needs_improvement",
    "evidence": [], "judgment": "No assessment available.",
    "maxScoreMarks": 10.0, "scoreMarks": 0.0,
}]


def test_220_never_reaches_the_marker_at_all():
    """Caught before any model spend: the page was the deliverable and we
    never saw it. The 6,292-character caption does not rescue it."""
    assert route_outcome(S220_ARTEFACTS, DAY07_TASK,
                         is_garbage=False) == "no_mark_publish_your_link"


def test_220s_stored_row_would_also_be_refused_at_the_writer():
    """Belt and braces: even if the gate above were bypassed, the mark that
    was really stored cannot be written now."""
    ok, why = gg.may_write_grade(criteria=S220_CRITERIA, proposed_score=0,
                                 words_read=1057, review=S220_STORED)
    assert not ok and "no feedback at all" in why


# ── student 880: a Suno song link + 1,119 chars ─────────────────────────────
#
# Stored: grade 0.00, isGarbage true, "Submission is a music song link
# unrelated to creating a financial dashboard using Gemini Canvas."

S880_ARTEFACTS = _artefacts("https://suno.com/s/sjNn14OjfobXpnwW",
                            link_readable=True, typed_chars=1119)


def test_880_is_told_what_arrived_instead_of_being_zeroed():
    assert route_outcome(S880_ARTEFACTS, DAY07_TASK,
                         is_garbage=True) == "no_mark_link_is_not_the_work"


def test_880s_link_being_readable_is_what_makes_this_the_second_gate():
    """Their link opened fine (2,977 words). The publish rule correctly does
    NOT fire — this is a wrong link, not an unseen one."""
    assert intake.link_deliverable_unseen(S880_ARTEFACTS) is False


# ── student 188: Gemini's advertisement page + 1,828 chars ──────────────────
#
# Stored: grade 0.00, isGarbage true, "Student submitted only a generic Gemini
# landing page link with no actual dashboard artifact".

S188_ARTEFACTS = _artefacts(
    "https://gemini.google.com/canvas?utm_source=sem&utm_medium=paid-media",
    link_readable=True, typed_chars=1828)


def test_188_is_told_what_arrived_instead_of_being_zeroed():
    assert route_outcome(S188_ARTEFACTS, DAY07_TASK,
                         is_garbage=True) == "no_mark_link_is_not_the_work"


# ── the students who were marked CORRECTLY must still be marked ─────────────
#
# 2564 -> 4.5, 1355 -> 6.8, 1024 -> 7.2, 356 -> 6.2. If the guards swallow
# these too, the run produces nothing and the fix is worse than the fault.

def test_a_text_only_submission_is_still_graded():
    """1355 and 2564: 1,162 chars typed, no link at all."""
    only_typed = [Artefact(kind="typed text", label="answer box",
                           text="x " * 580)]
    assert route_outcome(only_typed, DAY07_TASK, is_garbage=False) == "graded"


def test_a_pdf_submission_is_still_graded():
    """1024 and 356: Gemini_Canvas_Assignment_Day07.pdf."""
    with_pdf = [
        Artefact(kind="typed text", label="answer box", text="x " * 1350),
        Artefact(kind="document", label="Gemini_Canvas_Assignment_Day07.pdf",
                 confirmed=True, text="dashboard export " * 50),
    ]
    assert route_outcome(with_pdf, DAY07_TASK, is_garbage=False) == "graded"


def test_a_screenshot_beside_a_dead_link_is_still_graded():
    """872 and 525 submitted .png files. A screenshot IS the work."""
    with_shot = [
        Artefact(kind="typed text", label="answer box", text="x " * 12),
        Artefact(kind="link", label="https://share.gemini.google/x",
                 confirmed=True, text=""),
        Artefact(kind="image", label="inbound6662052139867739710.png",
                 confirmed=True, text="a dashboard screenshot with three charts"),
    ]
    assert route_outcome(with_shot, DAY07_TASK, is_garbage=False) == "graded"


def test_a_genuinely_weak_answer_still_earns_its_low_mark():
    """The guards refuse marks with nothing behind them, never harsh ones."""
    ok, _ = gg.may_write_grade(
        criteria=[{"criteria": "Dashboard created from a data set",
                   "percentage": 45,
                   "judgment": "One chart is present; no data set is named."}],
        proposed_score=45, words_read=581,
        review={"improvements": ["Name the data set you used."],
                "strengths": ["You built a chart."], "feedbackPoints": []})
    assert ok


# ── the gate order in this file must match the route's ──────────────────────

def test_this_replay_matches_the_order_the_route_really_uses():
    src = open("app/routes/assignment_review.py", encoding="utf-8").read()
    publish = src.index("intake.link_deliverable_unseen(artefacts)")
    garbage = src.index("intake.deliverable_is_only_links(artefacts)")
    assert publish < garbage, \
        "the route consults the publish rule first; this replay assumes that"
