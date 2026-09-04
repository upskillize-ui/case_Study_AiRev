"""Format is not substance (04 Sep 2026, owner's ruling).

Day 09 asked for a Gamma share link; students exported the same deck as PDF
or PPTX. The marker declared "not a Gamma presentation" and the rows went to
0/10 or to "different task's work". Owner: "if format change then cut 1 or
2 marks, not complete zero". Pinned: a format miss is on-task, never
wrong_task, scored on content, minus a fixed 20 points.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import review_pipeline as rp

RUBRIC = {"criteria": [{"name": "Gamma presentation on Data Science", "maxScore": 100}]}
EMPTY_LANG = {"grammar_issues": [], "spelling_examples": [], "redundancy_note": "", "clarity_note": ""}


def _review(pct, declare_wrong=False, fm=None):
    return {"is_garbage": False, "garbage_reason": "",
            "criteria": [{"name": "Gamma presentation on Data Science", "evidence_quotes": ["Slide 1"],
                          "case_specific": True, "judgment": "Judged.", "score_pct": pct, "confidence": "high"}],
            "concepts_covered": [], "concepts_missing": [], "factual_errors": [],
            "strengths": ["s"], "improvements": ["i"], "feedback_points": ["p"], "hard_truth": "h",
            "language_report": EMPTY_LANG, "authorship": {"ai_likelihood_percent": 40, "reason": "r"},
            "wrong_task": {"is_wrong_task": declare_wrong,
                           "what_it_is": "a static PDF slide deck, not a Gamma presentation" if declare_wrong else ""},
            "format_miss": fm or {"is_format_miss": False, "asked": "", "arrived": ""}}


PPTX = {"is_format_miss": True, "asked": "Gamma link", "arrived": "PPTX"}


def _run(monkeypatch, *answers):
    """Each call to the model returns the next answer; the last one repeats."""
    calls = []

    def fake(blocks, schema, **kw):
        calls.append(blocks)
        return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(rp.ai_service, "call_structured", fake)
    monkeypatch.setattr(rp.ai_service, "set_student_context", lambda *a, **k: None, raising=False)
    out = rp.run_review(scope_type="assignment", scope_id=33, pack={"summary": "Day 09 Gamma"},
                        pack_version=1, rubric=RUBRIC, student_answer="Slide 1 Data Science " * 60,
                        word_count=180, word_limit_min=0, word_limit_max=99999, student_id=1,
                        task_text="Day 09 : Gamma — Data Science presentation, submit a Gamma share link")
    return out, calls


def test_a_pdf_of_the_deck_is_scored_on_content_minus_a_fixed_deduction(monkeypatch):
    out, calls = _run(monkeypatch, _review(70, fm={"is_format_miss": True, "asked": "Gamma share link",
                                                   "arrived": "PDF export of the deck"}))
    assert len(calls) == 1
    assert out["wrongTask"]["declared"] is False
    assert out["scores"]["totalScore"] == 70 - rp.FORMAT_MISS_PENALTY
    gates = [g for g in out["scores"]["gatesHit"] if g["gate"] == "format_miss"]
    assert gates and "PDF export" in gates[0]["detail"]
    assert "format" in out["howYouScored"].lower()


def test_a_format_miss_can_never_be_ruled_wrong_task(monkeypatch):
    """Even when the marker also declared wrong_task, a format miss overrides it."""
    out, _ = _run(monkeypatch, _review(45, declare_wrong=True, fm=PPTX))
    assert out["wrongTask"]["declared"] is False
    assert out["scores"]["totalScore"] == 45 - rp.FORMAT_MISS_PENALTY


# ─── job 868: the marker declared the miss, then docked the wrapper anyway ──

def test_a_format_miss_scored_under_the_base_is_rejudged_on_content(monkeypatch):
    """5378's shape: 'asked Gamma link, arrived PPTX' — and the one criterion
    'Gamma presentation on Data Science' at 0 for not being a link. 0 - 20
    floored at 0 and a 525-word deck read 0.0/10. One more pass, wrapper off
    the table; the second score carries the deduction."""
    out, calls = _run(monkeypatch, _review(0, declare_wrong=True, fm=PPTX), _review(62))
    assert len(calls) == 2
    note = calls[1][-1]["text"]
    assert "Gamma link" in note and "PPTX" in note and "0/100" in note
    assert "MUST be false" in note
    assert out["scores"]["totalScore"] == 62 - rp.FORMAT_MISS_PENALTY
    assert out["wrongTask"]["declared"] is False
    assert "rejudged-on-content" in out["decisions"]["scoringPath"]
    # The declaration survives the second pass even when the model drops it.
    assert [g for g in out["scores"]["gatesHit"] if g["gate"] == "format_miss"]


def test_the_second_pass_is_the_only_extra_call_and_its_verdict_stands(monkeypatch):
    """A second pass that still scores low is accepted: one call, not a loop."""
    out, calls = _run(monkeypatch, _review(10, fm=PPTX), _review(25))
    assert len(calls) == 2
    assert out["scores"]["totalScore"] == 25 - rp.FORMAT_MISS_PENALTY


def test_a_format_miss_at_or_above_the_base_costs_no_second_call(monkeypatch):
    out, calls = _run(monkeypatch, _review(rp.FORMAT_MISS_REJUDGE_BELOW, fm=PPTX))
    assert len(calls) == 1
    assert out["scores"]["totalScore"] == rp.FORMAT_MISS_REJUDGE_BELOW - rp.FORMAT_MISS_PENALTY


def test_a_low_score_without_a_format_miss_costs_no_second_call(monkeypatch):
    out, calls = _run(monkeypatch, _review(10))
    assert len(calls) == 1
    assert out["scores"]["totalScore"] == 10


def test_format_miss_contradiction_is_pure():
    assert rp.format_miss_contradiction(_review(0), 0) == ""
    assert rp.format_miss_contradiction(_review(0, fm=PPTX), 40) == ""
    why = rp.format_miss_contradiction(_review(0, fm=PPTX), 12.5)
    assert "Gamma link -> PPTX" in why and "12.5/100" in why


def test_no_format_miss_changes_nothing(monkeypatch):
    out, _ = _run(monkeypatch, _review(70))
    assert out["scores"]["totalScore"] == 70
    assert not [g for g in out["scores"]["gatesHit"] if g["gate"] == "format_miss"]


def test_a_missing_or_malformed_field_is_harmless(monkeypatch):
    answer = _review(60)
    answer["format_miss"] = "pdf"
    out, _ = _run(monkeypatch, answer)
    assert out["scores"]["totalScore"] == 60
