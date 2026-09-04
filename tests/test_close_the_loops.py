"""Four loops closed in one restart (04 Sep 2026, afternoon).

1. Too little of the task paired to a verdict → one re-judge with the exact
   requirement names and count, instead of a refusal that returns next sweep.
2. A guard refusal in the job ledger is "our outage — not graded", never an
   attempt spent, never a failure that trips the brake; a wrong-task ruling
   is a policy skip.
3. A batch parked while a live worker was busy is picked up by whichever
   worker exits next.
(4. Startup always schedules the resume sweep — main.py, read at boot.)
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import review_pipeline as rp
from app.services import review_job_service as jobs
from app.routes import review_jobs as route

RUBRIC = {"criteria": [
    {"name": "Dashboard is built in Gemini Canvas", "maxScore": 40},
    {"name": "At least three charts",                "maxScore": 35},
    {"name": "Explanation of the data used",         "maxScore": 25},
]}
EMPTY_LANG = {"grammar_issues": [], "spelling_examples": [],
              "redundancy_note": "", "clarity_note": ""}


def _review(names, pct=60):
    return {"is_garbage": False, "garbage_reason": "",
            "criteria": [{"name": n, "evidence_quotes": ["q"], "case_specific": True,
                          "judgment": "Judged.", "score_pct": pct, "confidence": "high"}
                         for n in names],
            "concepts_covered": [], "concepts_missing": [], "factual_errors": [],
            "strengths": ["s"], "improvements": ["i"], "feedback_points": ["p"],
            "hard_truth": "h", "language_report": EMPTY_LANG,
            "authorship": {"ai_likelihood_percent": 40, "reason": "r"},
            "wrong_task": {"is_wrong_task": False, "what_it_is": ""}}


def _run(monkeypatch, answers):
    calls = []

    def fake(blocks, schema, **kw):
        calls.append(blocks)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(rp.ai_service, "call_structured", fake)
    monkeypatch.setattr(rp.ai_service, "set_student_context", lambda *a, **k: None, raising=False)
    out = rp.run_review(scope_type="assignment", scope_id=33, pack={"summary": "Day 07"},
                        pack_version=1, rubric=RUBRIC, student_answer="dashboard " * 200,
                        word_count=200, word_limit_min=0, word_limit_max=99999, student_id=1,
                        task_text="Day 07 : Gemini Canvas")
    return out, calls


def test_a_row_count_mismatch_is_rejudged_with_the_names_spelled_out(monkeypatch):
    """Two rows in the marker's own words for a three-requirement rubric:
    nothing pairs, positional pairing cannot apply (counts differ)."""
    first = _review(["the dashboard opens and works", "charts are present"])
    second = _review([c["name"] for c in RUBRIC["criteria"]], pct=55)
    out, calls = _run(monkeypatch, [first, second])
    assert len(calls) == 2
    note = calls[1][-1]["text"]
    assert "EXACTLY 3" in note and "At least three charts" in note
    assert "rejudged-with-names" in out["decisions"]["scoringPath"]
    assert out["scores"]["totalScore"] > 0
    assert not out["decisions"]["unjudgedRequirements"]


def test_a_fully_paired_review_costs_one_call(monkeypatch):
    out, calls = _run(monkeypatch, [_review([c["name"] for c in RUBRIC["criteria"]])])
    assert len(calls) == 1


def test_guard_refusal_is_our_outage_in_the_ledger_and_wrong_task_is_a_skip(monkeypatch):
    class T:  # a tenant stand-in; set_current_tenant only stores it
        id = "lms"
    monkeypatch.setattr(route, "set_current_tenant", lambda t: None)
    answers = {}
    monkeypatch.setattr(route, "re_review_assignment",
                        lambda sid, **kw: answers[sid])
    review_one = route.make_review_one(T(), "admin")

    answers[1] = {"success": True, "notGraded": True,
                  "feedback": {"summary": "We could not finish reviewing this attempt"}}
    state, detail, score = review_one(17, 1)
    assert state == "skipped" and detail.startswith("our outage — not graded") and score is None

    answers[2] = {"success": True, "notGraded": True,
                  "wrongTask": {"declared": True, "whatItIs": "an investment deck"},
                  "feedback": {"summary": "Not graded: what reached us looks like…"}}
    state, detail, _ = review_one(17, 2)
    assert state == "skipped" and detail.startswith("wrong_task")

    answers[3] = {"success": True, "previousGrade": None, "feedback": {"scoreMarks": 6.1}}
    assert review_one(17, 3) == ("done", "None -> 6.1", 6.1)


def test_parked_jobs_are_the_running_batches_with_pending_rows(monkeypatch):
    monkeypatch.setattr(jobs, "running_jobs", lambda t, include_live=True: [
        {"id": 855, "note": "sweep"}, {"id": 860, "note": "sweep"}, {"id": 861, "note": "sweep"}])
    monkeypatch.setattr(jobs, "has_pending", lambda t, j: j in (855, 861))
    assert jobs.parked_jobs("lms", exclude=860) == [855, 861]
    assert jobs.parked_jobs("lms", exclude=855) == [861]


def test_startup_always_schedules_the_resume_sweep():
    src = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()
    assert "if reaped and jobs_enabled():" not in src
    assert 'id="resume_sweep"' in src


def test_priority_and_skip_titles_order_the_run(monkeypatch):
    from app.services import sweeper_service as sw
    rows = [{"id": 1, "title": "Day 06: Suno : Create a song"},
            {"id": 2, "title": "Day 02 - Claude:  Artfiact Creation"},
            {"id": 3, "title": "Day 09 : Gamma"},
            {"id": 4, "title": "Day 08: Lovable"},
            {"id": 5, "title": "Day 10: Nano Banana"},
            {"id": 6, "title": "Day 09 : Gamma"}]
    monkeypatch.setenv("SWEEP_PRIORITY_TITLES", "Gamma, Lovable ,claude")
    monkeypatch.setenv("SWEEP_SKIP_TITLES", "Suno")
    assert [r["id"] for r in sw.order_for_run(rows)] == [3, 6, 4, 2, 5]
    monkeypatch.delenv("SWEEP_PRIORITY_TITLES"); monkeypatch.delenv("SWEEP_SKIP_TITLES")
    assert [r["id"] for r in sw.order_for_run(rows)] == [1, 2, 3, 4, 5, 6]


def test_hard_stop_counts_every_attempt_whatever_the_label(monkeypatch):
    from app.services import sweeper_service as sw
    monkeypatch.setattr(sw, "tquery", lambda t, sql, params: [
        {"submission_id": 1, "n": 1, "total": 3},   # one real try, two outages: still offered
        {"submission_id": 2, "n": 0, "total": 6},   # six outages: hard stop
        {"submission_id": 3, "n": 2, "total": 2}])  # ceiling by real tries
    spent = sw.attempts_spent("lms", [1, 2, 3])
    assert spent[1] < sw.MAX_SWEEP_ATTEMPTS
    assert spent[2] >= sw.MAX_SWEEP_ATTEMPTS
    assert spent[3] >= sw.MAX_SWEEP_ATTEMPTS
