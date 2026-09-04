"""The marker went silent behind its own ruling (04 Sep 2026, job 849 live).

Rule 12 told the model "do not score" wrong-task work. So every declaration
came back with EMPTY criteria and EMPTY feedback — and when Python voided the
declaration ("identification names this task's own deliverable", or under
120 words read) it "scored normally" from nothing. grade_guard refused
("the reviewer produced no feedback at all"), the learner read "our side,
not yours", and the sweep re-offered the row to the same silence.

And when the ruling DID stand, no route acted on it: the corroborated
declaration fell through to the same guard refusal. student_notices.wrong_task
had no caller since 18 Aug.

Pinned here:
  1. A rejected ruling with nothing scored behind it is asked again, ONCE,
     with the ruling off the table — and the second answer is what scores.
  2. A rejected ruling WITH scores behind it costs no second call.
  3. A ruling that stands costs no second call either.
  4. The route turns a standing ruling into the wrong-task notice — the exact
     sentence the LMS "Different work from the brief" bucket matches.
"""
import os
import json
import re
import sys

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
EMPTY_LANG = {"grammar_issues": [], "spelling_examples": [],
              "redundancy_note": "", "clarity_note": ""}


def _silent_declaration(what_it_is: str) -> dict:
    """What the model actually returned on job 849: a ruling and nothing else."""
    return {"is_garbage": False, "garbage_reason": "",
            "criteria": [], "concepts_covered": [], "concepts_missing": [],
            "factual_errors": [], "strengths": [], "improvements": [],
            "feedback_points": [], "hard_truth": "",
            "language_report": EMPTY_LANG,
            "authorship": {"ai_likelihood_percent": 40, "reason": "r"},
            "wrong_task": {"is_wrong_task": True, "what_it_is": what_it_is}}


def _scored(pct: int, declare: bool = False, what: str = "") -> dict:
    return {"is_garbage": False, "garbage_reason": "",
            "criteria": [{"name": c["name"], "evidence_quotes": ["I will"],
                          "case_specific": True, "judgment": "Judged.",
                          "score_pct": pct, "confidence": "high"}
                         for c in RUBRIC["criteria"]],
            "concepts_covered": ["a"], "concepts_missing": [],
            "factual_errors": [], "strengths": ["s"], "improvements": ["i"],
            "feedback_points": ["p"], "hard_truth": "h",
            "language_report": EMPTY_LANG,
            "authorship": {"ai_likelihood_percent": 40, "reason": "r"},
            "wrong_task": {"is_wrong_task": declare, "what_it_is": what}}


def _run(monkeypatch, answers: list, word_count: int = 400):
    calls = []

    def fake(blocks, schema, **kw):
        calls.append(blocks)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(rp.ai_service, "call_structured", fake)
    monkeypatch.setattr(rp.ai_service, "set_student_context",
                        lambda *a, **k: None, raising=False)
    out = rp.run_review(
        scope_type="assignment", scope_id=17, pack=PACK, pack_version=1,
        rubric=RUBRIC, student_answer="In five years I will be a lawyer. " * 40,
        word_count=word_count, word_limit_min=0, word_limit_max=999999,
        student_id=229,
        task_text="Day 01: ChatGPT Assignment - Yourself in 5 years")
    return out, calls


def test_a_voided_silent_ruling_is_rejudged_once_and_scored(monkeypatch):
    """Submission 229's shape: the model called a personal 5-year plan
    'a personal 5-year career plan' (voided — names this task's own
    deliverable) and scored nothing. One more pass, ruling off the table."""
    out, calls = _run(monkeypatch, [
        _silent_declaration("a personal 5-year career plan as a lawyer"),
        _scored(65)])
    assert len(calls) == 2
    assert "MUST be false" in calls[1][-1]["text"]
    assert out["wrongTask"]["declared"] is False
    assert out["scores"]["totalScore"] > 0
    assert out["strengths"] == ["s"]
    assert "rejudged-without-ruling" in out["decisions"]["scoringPath"]


def test_a_thin_silent_ruling_is_rejudged_too(monkeypatch):
    """Under WRONG_TASK_MIN_WORDS the ruling cannot stand either — same
    silence, same repair."""
    out, calls = _run(monkeypatch, [
        _silent_declaration("an investment analysis slide deck"), _scored(20)],
        word_count=40)
    assert len(calls) == 2
    assert out["wrongTask"]["declared"] is False
    assert out["scores"]["totalScore"] > 0


def test_a_voided_ruling_with_scores_behind_it_costs_no_second_call(monkeypatch):
    out, calls = _run(monkeypatch, [
        _scored(55, declare=True, what="a personal 5-year career plan")])
    assert len(calls) == 1
    assert out["wrongTask"]["declared"] is False
    assert out["scores"]["totalScore"] > 0


def test_a_ruling_that_stands_costs_no_second_call(monkeypatch):
    """Student 1151's deck: identified, substantial, low — declared."""
    out, calls = _run(monkeypatch, [
        _scored(5, declare=True, what="an investment analysis slide deck")])
    assert len(calls) == 1
    assert out["wrongTask"]["declared"] is True


def test_a_second_answer_cannot_redeclare(monkeypatch):
    """The ruling was rejected on pass one; a second declaration would be the
    same empty shape again. It is stripped, and the scores stand."""
    out, calls = _run(monkeypatch, [
        _silent_declaration("a personal 5-year career plan"),
        _scored(30, declare=True, what="a personal plan")])
    assert len(calls) == 2
    assert out["wrongTask"] == {"declared": False, "whatItIs": ""}


def test_ruling_blocked_reason_names_each_gate():
    task = "Day 01: ChatGPT Assignment - Yourself in 5 years"
    assert rp.ruling_blocked_reason(_scored(10), 400, task) == ""
    assert "no identification" in rp.ruling_blocked_reason(
        _scored(10, declare=True, what=""), 400, task)
    assert "words read" in rp.ruling_blocked_reason(
        _scored(10, declare=True, what="a slide deck"), 40, task)
    assert "own deliverable" in rp.ruling_blocked_reason(
        _scored(10, declare=True, what="a personal 5-year career plan"), 400, task)
    assert rp.ruling_blocked_reason(
        _scored(10, declare=True, what="an investment deck"), 400, task) == ""


# ─── the route acts on a standing ruling ──────────────────────────────────

def test_route_writes_a_zero_for_a_wrong_task_said_plainly(monkeypatch):
    """04 Sep evening (Ranjana): a wrong submission is a 0, not "Not graded",
    and the learner reads a statement of fact — never "looks like", never
    "read the brief again", never our own reading of the file."""
    from app.routes import assignment_review as route
    from app.services import assignment_db_service as dbs

    written = {}

    def fake_execute(tenant, sql, params):
        written["sql"] = " ".join(sql.split())
        written["params"] = params

    monkeypatch.setattr(dbs, "texecute", fake_execute)
    monkeypatch.setattr(
        dbs, "update_assignment_submission_with_ai_results",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not grade")))
    monkeypatch.setattr(
        dbs, "mark_not_graded",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("a wrong task is a mark")))

    r = {"wrongTask": {"declared": True,
                       "whatItIs": "An investment analysis slide deck"},
         "scores": {"totalScore": 4, "rubricBreakdown": []}}
    res = route._pipeline_assignment_response(
        None, {"submissionId": 1151, "attemptNumber": 1}, r, 400, 0.0,
        max_marks=10, task_title="Day 01: Yourself in 5 years")

    assert res["success"] is True and res["zeroed"] is True
    assert "notGraded" not in res
    assert "grade = 0" in written["sql"] and "status = 'graded'" in written["sql"]
    assert written["params"][1] == 1151
    stored = json.loads(written["params"][0])
    # The LMS /reopen endpoint reads exactly these two keys.
    assert stored["zeroed"] is True and stored["blocker"] == "wrong_task"
    assert stored["reviewedBy"] == "ai" and stored["scoreMarks"] == 0
    assert stored["outOf"] == 10 and stored["rulesVersion"]
    msg = stored["message"]
    assert msg.startswith("This submission is for a different assignment")
    assert "an investment analysis slide deck" in msg
    assert "Yourself in 5 years" in msg and "0 out of 10" in msg
    for banned in ("looks like", "not graded", "our side", "read the", "brief"):
        assert banned not in msg.lower(), banned
    assert res["feedback"]["scoreMarks"] == 0 and res["feedback"]["summary"] == stored["summary"]
    assert res["feedback"]["encouragement"] == ""


# ─── the second silence: judgments present, lists empty (job 852) ─────────

def test_per_criterion_judgments_become_feedback_points_when_lists_are_empty(monkeypatch):
    """3050 / 1786 / 1515 / 8611 / 1710: six honest judgments, empty
    strengths / improvements / feedback_points / hard_truth — refused as
    'no feedback at all' because the fallback read a key (`note`) the schema
    never had (`judgment`)."""
    answer = _scored(45)
    answer.update(strengths=[], improvements=[], feedback_points=[], hard_truth="")
    out, calls = _run(monkeypatch, [answer])
    assert len(calls) == 1
    assert out["feedbackPoints"], "judgments must surface as feedback points"
    assert all("Judged." in p for p in out["feedbackPoints"])
    assert out["detailedFeedback"]


def test_an_advisory_garbage_flag_with_nothing_scored_is_rejudged(monkeypatch):
    """The model says 'not genuine', scores nothing; at 400 words the flag is
    advisory, so the work must actually be scored — second pass."""
    silent = _silent_declaration("")
    silent["wrong_task"] = {"is_wrong_task": False, "what_it_is": ""}
    silent["is_garbage"] = True
    silent["garbage_reason"] = "pasted tool output"
    # A garbage flag first escalates to the strong tier (call 2, same answer),
    # and only then is the unheld flag re-judged (call 3).
    out, calls = _run(monkeypatch, [silent, silent, _scored(30)])
    assert len(calls) == 3
    assert "advisory" in calls[2][-1]["text"]
    assert out["isGarbage"] is False
    assert out["scores"]["totalScore"] > 0


def test_a_short_garbage_flag_still_hard_zeros_without_a_second_call(monkeypatch):
    silent = _silent_declaration("")
    silent["wrong_task"] = {"is_wrong_task": False, "what_it_is": ""}
    silent["is_garbage"] = True
    silent["garbage_reason"] = "keyboard mash"
    out, calls = _run(monkeypatch, [silent, silent], word_count=12)
    assert len(calls) == 2          # judge + escalation, no third pass
    assert out["isGarbage"] is True and out["scores"]["totalScore"] == 0


# ─── the third silence: the ruling was voided, the zero it justified stayed ──
# (job 868, 04 Sep 2026, submission 6929: "a guide to prompt engineering
# methodology" on the prompt-engineering day — voided as naming this task's
# own deliverable — every criterion 0 → 0.0/10 for 576 words.)

def test_a_voided_own_deliverable_ruling_with_a_zero_behind_it_is_rejudged(monkeypatch):
    out, calls = _run(monkeypatch, [
        _scored(0, declare=True, what="a personal 5-year career plan as a lawyer"),
        _scored(58)])
    assert len(calls) == 2
    assert "own deliverable" in calls[1][-1]["text"]
    assert "0/100" in calls[1][-1]["text"]
    assert out["wrongTask"]["declared"] is False
    assert out["scores"]["totalScore"] == 58
    assert "rejudged-without-ruling" in out["decisions"]["scoringPath"]


def test_a_voided_ruling_with_real_marks_behind_it_costs_no_second_call(monkeypatch):
    out, calls = _run(monkeypatch, [
        _scored(45, declare=True, what="a personal 5-year career plan")])
    assert len(calls) == 1
    assert out["scores"]["totalScore"] == 45


def test_a_thin_read_void_keeps_its_low_score_without_a_second_call(monkeypatch):
    """'only 40 words read' does not assert the work is this task's — a low
    score on thin content is honest, no extra call."""
    out, calls = _run(monkeypatch, [
        _scored(5, declare=True, what="an investment analysis slide deck")],
        word_count=40)
    assert len(calls) == 1
    assert out["wrongTask"]["declared"] is False
    assert out["scores"]["totalScore"] == 5


def test_the_second_pass_is_never_asked_twice(monkeypatch):
    """A silent ruling already re-judged once, still under 40: one extra
    call, not two."""
    out, calls = _run(monkeypatch, [
        _silent_declaration("a personal 5-year career plan"), _scored(12)])
    assert len(calls) == 2
    assert out["scores"]["totalScore"] == 12


def test_voided_ruling_contradiction_is_pure():
    assert rp.voided_ruling_contradiction("", 0) == ""
    assert rp.voided_ruling_contradiction("only 40 words read (need 120)", 0) == ""
    assert rp.voided_ruling_contradiction("no identification of what the work is", 0) == ""
    assert rp.voided_ruling_contradiction(
        "identification names this task's own deliverable", 40) == ""
    why = rp.voided_ruling_contradiction(
        "identification names this task's own deliverable", 6)
    assert "own deliverable" in why and "6/100" in why
    assert rp.voided_ruling_contradiction(
        "identified as a PERSONAL plan — career choice is never grounds", 0)


def test_the_wrong_task_notice_is_a_noun_phrase_without_the_markers_contrast():
    from app.services import student_notices as sn
    assert sn._what_arrived("A study plan infographic, not a personal 5-year plan") == "a study plan infographic"
    assert sn._what_arrived("PDF document on the scientific method.") == "PDF document on the scientific method"
    assert sn._what_arrived("A photograph of a workshop, unrelated to the deck") == "a photograph of a workshop"
    assert sn._what_arrived("") == "work for a different assignment"
    lines = sn.wrong_task_points("A Day 17 avatar poster", "Day 18: Landing page", 10)
    assert lines[0] == ("This submission is for a different assignment: what reached us is "
                        "a Day 17 avatar poster, and this assignment is \"Day 18: Landing page\".")
    assert lines[1] == "Marks for this attempt: 0 out of 10."
    assert "reopen" in lines[2]
