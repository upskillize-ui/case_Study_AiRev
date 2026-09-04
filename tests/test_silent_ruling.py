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

def test_route_writes_the_wrong_task_notice_not_a_guard_apology(monkeypatch):
    from app.routes import assignment_review as route
    from app.services import assignment_db_service as dbs

    written = {}

    def fake_mark(tenant, submission_id, message, card=None, stamp=True):
        written.update(id=submission_id, message=message, stamp=stamp)

    monkeypatch.setattr(dbs, "mark_not_graded", fake_mark)
    monkeypatch.setattr(
        dbs, "update_assignment_submission_with_ai_results",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not grade")))

    r = {"wrongTask": {"declared": True,
                       "whatItIs": "an investment analysis slide deck"},
         "scores": {"totalScore": 4, "rubricBreakdown": []}}
    res = route._pipeline_assignment_response(
        None, {"submissionId": 1151, "attemptNumber": 1}, r, 400, 0.0,
        max_marks=10, task_title="Day 01: Yourself in 5 years")

    assert res["notGraded"] is True and res["success"] is True
    assert written["id"] == 1151 and written["stamp"] is True
    # The LMS classifier's "Different work from the brief" test, verbatim.
    assert re.match(r"^Not graded: what reached us looks like", written["message"])
    assert "investment analysis slide deck" in written["message"]
    assert "Yourself in 5 years" in written["message"]
    assert "our side" not in written["message"].lower()
