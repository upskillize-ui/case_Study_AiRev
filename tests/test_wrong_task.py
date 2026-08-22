"""B8 — the wrong-task rule. Ranjana, 18 Aug, verbatim policy: "if someone
submit different link or claude artifacts do not grade or give score."

Live case: student 1151 attached Asian_Paints_vs_Berger_Paints_Investment_
Analysis.pptx to Day 01 ("make an image of your 5-year career plan"). Real
work — someone's work — but not THIS task's work. The old pipeline stretched
the rubric over it and wrote a low mark; policy says wrong work carries NO
mark at all: the learner is told what arrived and asked to attach the right
deliverable.

The declaration follows the house pattern — the model DECLARES, the
arithmetic CORROBORATES, Python DECIDES. A declaration alone cannot un-grade
an answer: if the rubric total is ≥ 40, the submission earned real marks
against this task's own criteria, so the declaration is ignored.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import review_pipeline as rp

RUBRIC = {"criteria": [
    {"name": "AI image of the 5-year future self is present", "maxScore": 40},
    {"name": "Five steps are listed",                          "maxScore": 35},
    {"name": "Steps are specific to the learner",              "maxScore": 25},
]}
PACK = {"summary": "Day 01: generate an AI image of yourself in 5 years."}


def _review(pct, declare_wrong, quotes=("slide 3: Berger Paints margin",)):
    return {
        "is_garbage": False, "garbage_reason": "",
        "criteria": [
            {"name": c["name"], "evidence_quotes": list(quotes),
             "case_specific": False,
             "judgment": "Judged from the quoted evidence.",
             "score_pct": pct, "confidence": "high"}
            for c in RUBRIC["criteria"]
        ],
        "concepts_covered": ["a"], "concepts_missing": [],
        "factual_errors": [], "strengths": ["s"], "improvements": ["i"],
        "feedback_points": ["p"], "hard_truth": "h",
        "language_report": {"grammar_issues": [], "spelling_examples": [],
                            "redundancy_note": "", "clarity_note": ""},
        "authorship": {"ai_likelihood_percent": 30, "reason": "r"},
        "wrong_task": {"is_wrong_task": declare_wrong,
                       "what_it_is": "an investment analysis slide deck"
                                     if declare_wrong else ""},
    }


def _run(monkeypatch, answer, word_count=400):
    monkeypatch.setattr(rp.ai_service, "call_structured",
                        lambda blocks, schema, **kw: answer)
    monkeypatch.setattr(rp.ai_service, "set_student_context",
                        lambda *a, **k: None, raising=False)
    return rp.run_review(
        scope_type="assignment", scope_id=17, pack=PACK, pack_version=1,
        rubric=RUBRIC, student_answer="Deck about paints. " * 40,
        word_count=word_count, word_limit_min=0, word_limit_max=999999,
        student_id=1151)


def test_the_investment_deck_is_declared_not_scored(monkeypatch):
    """Student 1151's pptx, replayed: model declares wrong task, rubric total
    is honestly low — the route is told NOT to write a mark."""
    out = _run(monkeypatch, _review(pct=10, declare_wrong=True))
    assert out["wrongTask"]["declared"] is True
    assert "slide deck" in out["wrongTask"]["whatItIs"]


def test_a_declaration_cannot_ungrade_a_scoring_answer(monkeypatch):
    """The corroboration rule. If the answer earned 70% against THIS task's
    criteria, the wrong-task declaration is a model error and is ignored —
    a stray declaration must never delete a real review."""
    out = _run(monkeypatch, _review(pct=70, declare_wrong=True))
    assert out["wrongTask"]["declared"] is False


def test_a_weak_on_task_attempt_is_a_low_score_not_a_wrong_task(monkeypatch):
    """The other boundary: bad work AT the task keeps its honest low mark."""
    out = _run(monkeypatch, _review(pct=15, declare_wrong=False))
    assert out["wrongTask"]["declared"] is False
    assert out["scores"]["totalScore"] < 40


def test_wrong_task_returned_as_plain_text_never_crashes(monkeypatch):
    """Live 21 Aug: the model answered wrong_task as a STRING instead of the
    schema's object, and `.get()` on it crashed the whole review to a bare
    500 mid-sweep. A malformed declaration carries no valid ruling — the
    review must complete normally with wrongTask undeclared."""
    answer = _review(pct=60, declare_wrong=False)
    answer["wrong_task"] = "This is not the right task"
    out = _run(monkeypatch, answer)
    assert out["wrongTask"] == {"declared": False, "whatItIs": ""}
    assert out["scores"]["totalScore"] > 0


def test_a_model_that_omits_the_field_changes_nothing(monkeypatch):
    """Older responses / degraded outputs: absence of wrong_task must degrade
    to normal scoring, never crash, never un-grade."""
    answer = _review(pct=60, declare_wrong=False)
    del answer["wrong_task"]
    out = _run(monkeypatch, answer)
    assert out["wrongTask"] == {"declared": False, "whatItIs": ""}


def test_a_thin_or_unreadable_row_can_never_be_ruled_wrong_task(monkeypatch):
    """THE 19 Aug false-positive storm, pinned. The sweep marked ~95 rows
    wrong_task — most were rows whose files could not be read, including
    'My_Future_Self_in_5_Years.pdf' (obviously on-task), and five students'
    real grades were cleared. To the judge, invisible work 'isn't this
    task's work' — so a declaration on thin content is suspicion, not
    recognition, and must be ignored."""
    out = _run(monkeypatch, _review(pct=5, declare_wrong=True), word_count=40)
    assert out["wrongTask"]["declared"] is False


def test_a_declaration_that_names_nothing_is_ignored(monkeypatch):
    """'Not this task' without saying WHAT it is instead — no ruling."""
    answer = _review(pct=10, declare_wrong=True)
    answer["wrong_task"]["what_it_is"] = "  "
    out = _run(monkeypatch, answer)
    assert out["wrongTask"]["declared"] is False


def test_career_choice_is_never_grounds_for_wrong_task():
    """Ranjana's ruling, 21 Aug (verbatim option: 'Any career counts'). The
    21 Aug sweep un-graded ~20 real personal plans — lawyer (772, was 5.5),
    government officer via MPSC/SSC (1104, 729), CA/CS roadmaps, a professor
    plan — because the career sat outside FinTech/Banking/AI. A personal
    5-year vision in ANY field IS the task; wrong_task is reserved for
    content that is not a personal plan at all."""
    rules = rp._JUDGE_INSTRUCTIONS.lower()
    assert "career choice is never grounds" in rules
    assert "any career counts" in rules
    assert "not a personal future-self plan at all" in rules


def test_the_judge_is_told_wrong_work_is_not_low_quality_work():
    rules = rp._JUDGE_INSTRUCTIONS.lower()
    assert "wrong work is not low-quality work" in rules
    assert "no grade, not a low grade" in rules
    assert "never wrong_task" in rules
    assert "unreadable content is never wrong_task" in rules \
        or "unreadable" in rules


def test_the_schema_requires_the_declaration():
    """Forced tool use validates against the schema — requiring the field is
    what makes 'the model omitted it' a non-event instead of a silent gap."""
    assert "wrong_task" in rp.REVIEW_SCHEMA["required"]
    props = rp.REVIEW_SCHEMA["properties"]["wrong_task"]["properties"]
    assert set(props) == {"is_wrong_task", "what_it_is"}


def test_mark_not_graded_clears_the_grade_and_reopens_the_row(monkeypatch):
    """The regrade route's action for old wrongly-scored rows (1151 held
    1.2/10): grade NULL, learner-readable message, status back to
    'submitted' so the corrected resubmission flows through the upsert."""
    from app.services import assignment_db_service as svc
    captured = {}

    def fake_texecute(tenant, sql, params=()):
        captured["sql"], captured["params"] = sql, params
        return 1

    monkeypatch.setattr(svc, "texecute", fake_texecute)
    svc.mark_not_graded(object(), 4321, "Not graded: wrong work attached.")
    import re
    assert re.search(r"grade\s*=\s*NULL", captured["sql"])
    assert re.search(r"status\s*=\s*'submitted'", captured["sql"])
    assert captured["params"][-1] == 4321
    assert "notGraded" in captured["params"][0]


# ── the 22 Aug contradiction: a declaration that NAMES this task ──────────
#
# The 22 Aug sweep un-graded rows whose own identification read "A personal
# 5-year career plan" (students 171, 314, 583) — on the 5-year-plan
# assignment. The judge recognized the task and declared against it anyway.
# Python now voids any declaration whose identification describes this
# task's own deliverable.

TASK = "Day 01: generate an AI image of yourself in 5 years, with 5 steps."


def test_a_declaration_naming_this_task_is_voided(monkeypatch):
    answer = _review(pct=15, declare_wrong=True)
    answer["wrong_task"]["what_it_is"] = "A personal 5-year career plan"
    out = _run(monkeypatch, answer)
    assert out["wrongTask"]["declared"] is False, \
        "the judge identified the work as THIS task and still un-graded it"


def test_genuinely_foreign_work_still_declares(monkeypatch):
    for foreign in ("an investment analysis slide deck",
                    "a photo of a historical monument",
                    "a generic banking study guide and course material",
                    "a Van Gogh-style painting of a landscape"):
        answer = _review(pct=10, declare_wrong=True)
        answer["wrong_task"]["what_it_is"] = foreign
        out = _run(monkeypatch, answer)
        assert out["wrongTask"]["declared"] is True, foreign


def test_names_this_task_is_pure_and_medium_blind():
    assert rp.names_this_task("A personal 5-year career plan", TASK)
    assert rp.names_this_task("A typed career vision and five year roadmap",
                              TASK)
    # medium words (image, AI, deck) never count toward the overlap
    assert not rp.names_this_task("an AI-generated image of a monument", TASK)
    assert not rp.names_this_task("an investment analysis slide deck", TASK)
    assert not rp.names_this_task("", TASK)


def test_the_judge_carries_the_contradiction_check():
    rules = rp._JUDGE_INSTRUCTIONS.lower()
    assert "contradiction check" in rules
    assert "is_wrong_task must be false: you have just identified the work" \
        in rules
