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


# Live 22 Aug, assignment 19 "India's Fintech Market": Day-03 work sent to
# the wrong day. The identification is plainly foreign, but it shares two
# GENERIC words with the task — and that used to void the ruling and score
# the row 0/F instead of leaving it un-graded for resubmission.
FINTECH_TASK = ("Day 04: India's Fintech Market - research the Indian fintech "
                "market, UPI adoption and the RBI regulations shaping it.")


def test_generic_topic_words_alone_no_longer_void_a_ruling():
    for foreign in (
        "An infographic on AI's labor market impact in India "
        "(2022-2026 hiring/job losses)",
        "A career plan for the Indian job market",
        "A report on India's population growth",
    ):
        assert not rp.names_this_task(foreign, FINTECH_TASK), foreign


def test_a_distinctive_word_still_voids_the_ruling():
    assert rp.names_this_task("A research note on UPI adoption", FINTECH_TASK)
    assert rp.names_this_task("A deck about the fintech sector", FINTECH_TASK)


def test_a_task_with_no_distinctive_words_keeps_the_two_word_rule():
    """Day 01 owns nothing but generic words; there the original rule is the
    only protection a real 5-year plan has."""
    assert rp.names_this_task("A personal 5-year career plan", TASK)
    assert not rp.names_this_task("A poster about the Indian market", TASK)


def test_apostrophe_debris_never_counts_as_overlap():
    assert "s" not in rp._significant_words("AI's labor market")
    assert "5" in rp._significant_words("5 years")       # digits are real


def test_the_judge_carries_the_contradiction_check():
    rules = rp._JUDGE_INSTRUCTIONS.lower()
    assert "contradiction check" in rules
    assert "is_wrong_task must be false: you have just identified the work" \
        in rules


# ── 22 Aug PRODUCTION defect: the guard compared against an EMPTY string ──
#
# The first contradiction guard read pack["title"]/["summary"] — keys the
# real knowledge pack does not have (concepts, question_demands, band_anchors
# ...). task_text was "" in production, the guard never fired, and 143 rows
# were re-run through it unprotected while the tests stayed green on a toy
# pack that DID have "summary". These tests replay the real pack shape and
# the live rulings from that run.

REAL_PACK = {   # the shape knowledge_service actually builds — no title/summary
    "concepts": [{"name": "Goal decomposition", "why_it_matters": "x",
                  "must_cover": True}],
    "question_demands": ["Generate an AI image of your 5-year future self",
                         "List the 5 steps you will take to achieve it"],
    "ideal_answer_skeleton": ["A personal vision", "Five sequenced steps"],
    "band_anchors": {"outstanding": "o", "proficient": "p", "emerging": "e"},
    "common_misconceptions": [], "specificity_markers": [],
}

TASK_EXPLICIT = ("Day 01:ChatGpt Assignment - Yourself in 5 years "
                 "Generate an AI image of your 5-year future self, along "
                 "with the 5 steps you will take to achieve it.")


def _run_real_pack(monkeypatch, answer, task_text=""):
    monkeypatch.setattr(rp.ai_service, "call_structured",
                        lambda blocks, schema, **kw: answer)
    monkeypatch.setattr(rp.ai_service, "set_student_context",
                        lambda *a, **k: None, raising=False)
    return rp.run_review(
        scope_type="assignment", scope_id=17, pack=REAL_PACK, pack_version=1,
        rubric=RUBRIC, student_answer="My plan. " * 100, word_count=300,
        word_limit_min=0, word_limit_max=999999, student_id=713,
        task_text=task_text)


def test_student_713_the_upsc_plan_is_scored_not_ungraded(monkeypatch):
    """Live 22 Aug ruling, verbatim: the judge NAMES the work a personal
    5-year career plan and still declares — with the real pack shape."""
    answer = _review(pct=15, declare_wrong=True)
    answer["wrong_task"]["what_it_is"] = (
        "This is a personal 5-year career plan focused on preparing for the "
        "UPSC examination to become an IAS Officer, which falls entirely "
        "outside the FinTech, Banking, and AI domain")
    out = _run_real_pack(monkeypatch, answer, task_text=TASK_EXPLICIT)
    assert out["wrongTask"]["declared"] is False


def test_the_pack_fallback_protects_even_without_explicit_task_text(monkeypatch):
    """No route-supplied task text: question_demands/skeleton carry the
    task's words, so the guard still fires — never an empty comparison."""
    answer = _review(pct=15, declare_wrong=True)
    answer["wrong_task"]["what_it_is"] = "A personal 5-year career plan"
    out = _run_real_pack(monkeypatch, answer, task_text="")
    assert out["wrongTask"]["declared"] is False


def test_student_685_the_lawyer_poster_is_scored(monkeypatch):
    """'A personal career aspiration poster for becoming a lawyer in India,
    which lies entirely outside the FinTech...' — personal + domain
    exclusion, both forbidden grounds."""
    answer = _review(pct=20, declare_wrong=True)
    answer["wrong_task"]["what_it_is"] = (
        "A personal career aspiration poster for becoming a lawyer in India, "
        "which lies entirely outside the FinTech, Banking, and AI domains")
    out = _run_real_pack(monkeypatch, answer, task_text=TASK_EXPLICIT)
    assert out["wrongTask"]["declared"] is False


def test_student_121_missing_image_is_incomplete_not_wrong(monkeypatch):
    """'...career planning document outlining a Finance Manager role and
    five career steps, with no AI-generated image submitted' — an attempt
    missing ONE element is a low score on that element, never wrong_task."""
    answer = _review(pct=25, declare_wrong=True)
    answer["wrong_task"]["what_it_is"] = (
        "A text-only career planning document outlining a Finance Manager "
        "role and five career steps, with no AI-generated image submitted")
    out = _run_real_pack(monkeypatch, answer, task_text=TASK_EXPLICIT)
    assert out["wrongTask"]["declared"] is False


def test_verbose_negations_do_not_void_legitimate_rulings(monkeypatch):
    """The judge restates the task in the negation clause ('...not a
    personal 5-year future-self plan'). Overlap runs on the pre-negation
    part only, so the Asian Paints deck and the Van Gogh stay declared."""
    for legit in (
        "A comparative investment analysis slide deck evaluating Asian "
        "Paints versus Berger Paints as financial instruments — a financial "
        "research assignment, not a personal 5-year future-self career plan "
        "with AI image and sequential steps.",
        "A Van Gogh-style landscape painting (Starry Night derivative) with "
        "no connection to a personal 5-year career plan, future professional "
        "self, or FinTech/Banking/AI domain.",
        "A multi-level marketing (MLM) recruitment and sales roadmap for "
        "Herbalife, not a personal 5-year career vision or AI-generated "
        "image of the student's future self.",
        "This is the HTML boilerplate and JavaScript runtime of the "
        "Claude.ai web application interface itself, not a student "
        "submission to the 5-year future self assignment.",
    ):
        answer = _review(pct=10, declare_wrong=True)
        answer["wrong_task"]["what_it_is"] = legit
        out = _run_real_pack(monkeypatch, answer, task_text=TASK_EXPLICIT)
        assert out["wrongTask"]["declared"] is True, legit[:60]


def test_generic_qualification_infographics_stay_declared(monkeypatch):
    """Her ruling's OTHER half: generic reference material with no personal
    plan in it IS wrong task — CA/CS pathway posters keep their ruling."""
    answer = _review(pct=12, declare_wrong=True)
    answer["wrong_task"]["what_it_is"] = (
        "This is a generic career-progression roadmap for pursuing the "
        "Company Secretary (CS) qualification in India, describing the "
        "statutory pathway set by ICSI")
    out = _run_real_pack(monkeypatch, answer, task_text=TASK_EXPLICIT)
    assert out["wrongTask"]["declared"] is True


def test_void_reason_is_pure_and_names_its_grounds():
    t = TASK_EXPLICIT
    assert "names this task" in rp.wrong_task_void_reason(
        "A personal 5-year career plan", t)
    assert "PERSONAL" in rp.wrong_task_void_reason(
        "A personal career aspiration poster for becoming a lawyer", t)
    assert "any career counts" in rp.wrong_task_void_reason(
        "A study roadmap, which falls entirely outside the FinTech domain", t)
    assert rp.wrong_task_void_reason(
        "an investment analysis slide deck", t) == ""
    assert rp.wrong_task_void_reason("", t) == ""


# ── our own handouts, submitted back to us (28 Aug 2026) ───────────────────
# Live on Day 15: a learner uploaded a screenshot of the assignment sheet. The
# judge identified it precisely — "a teaching aid or assignment instruction
# sheet for Day 15, not the learner's work" — and the name-overlap voider
# killed the ruling, because an instruction sheet for Day 15 necessarily
# shares its words with Day 15's brief. The row scored 0.0/10, which tells the
# learner their work was bad when the truth is they attached the wrong file.

DAY15 = ("Day 15: ElevenLabs Assignment. Create a 60-90 second video for a "
         "business using ChatGPT and ElevenLabs. Choose a real business, write "
         "a script, generate two voices and select one.")


@pytest.mark.parametrize("ident", [
    "This is a teaching aid or assignment instruction sheet for Day 15, "
    "not the learner's own work",
    "A screenshot of the assignment brief for the ElevenLabs task",
    "The task description handout, not a submission",
    "A promotional poster advertising Day 14 of the 30 Days 30 AI Tools course",
    "Course material for the ElevenLabs session",
])
def test_course_material_is_never_voided_by_word_overlap(ident):
    assert rp.wrong_task_void_reason(ident, DAY15) == "", ident


def test_a_real_attempt_is_still_protected_by_the_overlap_voider():
    """The rule this narrowing must not break: work that IS this task's
    deliverable can never be un-graded, however weak it is."""
    ident = "A 70-second promotional video for a gold loan business with a "\
            "generated voice-over"
    assert rp.wrong_task_void_reason(ident, DAY15) != ""


def test_the_other_two_voiders_are_untouched():
    plan = "A personal career plan poster for becoming a lawyer"
    assert "PERSONAL plan" in rp.wrong_task_void_reason(plan, DAY15)
    domain = "An infographic that falls entirely outside the FinTech domain"
    assert "domain-exclusion" in rp.wrong_task_void_reason(domain, DAY15)


# ── The void read the model's restatement of the BRIEF (28 Aug 2026) ───────
# wrong_task_void_reason tests the identification for word overlap with the
# task text. But the model often writes two sentences: what the learner SENT,
# then what the task ASKED. Testing the second concludes "they did the task"
# from the brief quoted back at us.
#
# Live, Day 15: "A photograph or AI-generated image of a jewelry shop
# storefront. The task requires a 60-90 second video" — voided on "video" and
# "60-90", both from the second sentence, and a storefront photo was scored
# 0.3/10. Policy for wrong work is NO grade with a reason, never a low grade;
# a 0.3 tells the student their work was poor when it was simply not this task.

from app.services.review_pipeline import wrong_task_void_reason

DAY15 = ("Day 15: ElevenLabs Assignment Create a 60-90 second video for a "
         "business using ChatGPT and ElevenLabs")


def test_the_task_restatement_cannot_void_the_ruling():
    reason = wrong_task_void_reason(
        "A photograph or AI-generated image of a jewelry shop storefront. "
        "The task requires a 60-90 second video", DAY15)
    assert reason == "", f"voided on the brief quoted back at us: {reason}"


@pytest.mark.parametrize("tail", [
    "But the task asks for a 60-90 second video.",
    "However the assignment requires a video with a voiceover.",
    "The assignment brief asks for a 60-90 second business video.",
    "While the task expects a video, this is a still image.",
])
def test_every_way_the_model_pivots_to_the_brief(tail):
    reason = wrong_task_void_reason(
        f"A photograph of a jewelry shop storefront. {tail}", DAY15)
    assert reason == "", f"voided by the pivot {tail!r}: {reason}"


def test_course_material_is_still_not_the_learners_work():
    assert wrong_task_void_reason(
        "This is a teaching aid or assignment instruction sheet for Day 15, "
        "not the learner's work", DAY15) == ""


# ── the protections that must survive ──────────────────────────────────────
# Un-grading a REAL attempt is far worse than grading a wrong one, so the
# voiders that stop over-eager wrong_task rulings must keep firing.

def test_a_genuine_submission_is_never_un_graded():
    reason = wrong_task_void_reason(
        "A 60-90 second promotional video for a gold loan business with an "
        "ElevenLabs voiceover", DAY15)
    assert reason, "a real attempt at this task would have been un-graded"


def test_a_career_choice_is_never_grounds():
    assert wrong_task_void_reason(
        "A personal 5-year career plan as a lawyer",
        "Day 01 Create an image of yourself in 5 years and list 5 steps")
