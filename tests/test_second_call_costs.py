"""The second call (07 Sep 2026).

Of 482 marks since the audit record was fixed, 202 paid for a second
full-price model call: a format miss scored 0 for its wrapper (102), a
wrong-task declaration with nothing scored behind it (59), too few or
misnamed criteria rows (27), and a strong-tier escalation to the same model
(26). Each repair below turns a second call into a first call that complies,
or into no call at all. The re-judges stay as fallbacks.
"""
import json
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import review_pipeline as rp
from app.services import ai_service
from app.services import assignment_db_service as dbs

RUBRIC = {"criteria": [{"name": "Gamma presentation on Data Science", "maxScore": 60},
                       {"name": "Speaker notes", "maxScore": 40}]}
EMPTY_LANG = {"grammar_issues": [], "spelling_examples": [], "redundancy_note": "", "clarity_note": ""}


def _row(name, pct):
    return {"name": name, "evidence_quotes": ["q"], "case_specific": True,
            "judgment": "Judged.", "score_pct": pct, "confidence": "high"}


def _review(pcts, fm=None):
    names = [c["name"] for c in RUBRIC["criteria"]]
    return {"is_garbage": False, "garbage_reason": "",
            "criteria": [_row(n, p) for n, p in zip(names, pcts)],
            "concepts_covered": [], "concepts_missing": [], "factual_errors": [],
            "strengths": ["s"], "improvements": ["i"], "feedback_points": ["p"], "hard_truth": "h",
            "language_report": EMPTY_LANG, "authorship": {"ai_likelihood_percent": 40, "reason": "r"},
            "wrong_task": {"is_wrong_task": False, "what_it_is": ""},
            "format_miss": fm or {"is_format_miss": False, "asked": "", "arrived": ""}}


PPTX = {"is_format_miss": True, "asked": "Gamma link", "arrived": "PPTX"}


# ─── 1. the schema is pinned to the rubric ──────────────────────────────────

def test_the_schema_pins_exactly_one_row_per_requirement_by_name():
    schema = rp.review_schema_for(RUBRIC["criteria"])
    crit = schema["properties"]["criteria"]
    assert crit["minItems"] == crit["maxItems"] == 2
    assert crit["items"]["properties"]["name"]["enum"] == [
        "Gamma presentation on Data Science", "Speaker notes"]
    assert "Speaker notes" in crit["description"]
    # The shared schema is untouched — a per-call copy, never a mutation.
    assert "minItems" not in rp.REVIEW_SCHEMA["properties"]["criteria"]
    assert "enum" not in rp.REVIEW_SCHEMA["properties"]["criteria"]["items"]["properties"]["name"]


def test_an_empty_rubric_falls_back_to_the_shared_schema():
    assert rp.review_schema_for([]) is rp.REVIEW_SCHEMA


def test_declarations_come_before_the_criteria():
    keys = list(rp.REVIEW_SCHEMA["properties"])
    assert keys.index("wrong_task") < keys.index("criteria")
    assert keys.index("format_miss") < keys.index("criteria")
    assert keys[0] == "is_garbage"
    fm = rp.REVIEW_SCHEMA["properties"]["format_miss"]
    assert fm["properties"]["content_scored_as_asked_format"]["enum"] == [True]
    assert "content_scored_as_asked_format" in fm["required"]


def test_output_is_bounded_so_a_review_cannot_overrun_max_tokens():
    c = rp.REVIEW_SCHEMA["properties"]["criteria"]["items"]["properties"]
    assert c["evidence_quotes"]["maxItems"] == 3
    assert c["judgment"]["maxLength"] == 240 and c["judgment"]["minLength"] == 1
    assert rp.REVIEW_SCHEMA["properties"]["factual_errors"]["maxItems"] == 3
    assert rp.REVIEW_SCHEMA["properties"]["language_report"]["properties"]["grammar_issues"]["maxItems"] == 3


# ─── 2. a re-judge is for a contradiction, not for a low mark ───────────────

def test_ruling_shaped_means_every_criterion_at_or_near_zero():
    assert rp.ruling_shaped([_row("a", 0), _row("b", 5)])
    assert rp.ruling_shaped([])
    assert not rp.ruling_shaped([_row("a", 0), _row("b", 30)])


def test_an_honest_low_mark_beside_a_format_miss_is_not_rejudged():
    """A thin deck delivered as PPTX scoring 30: the mark stands, no second
    call — and no nudge upwards."""
    assert rp.format_miss_contradiction(_review([30, 30], fm=PPTX), 30) == ""
    assert rp.format_miss_contradiction(_review([0, 5], fm=PPTX), 3) != ""


def test_a_voided_ruling_beside_real_scores_is_not_rejudged():
    blocked = "identification names this task's own deliverable"
    assert rp.voided_ruling_contradiction(blocked, 25, [_row("a", 25), _row("b", 25)]) == ""
    assert rp.voided_ruling_contradiction(blocked, 0, [_row("a", 0), _row("b", 0)]) != ""
    # Callers that pass no criteria keep the old behaviour.
    assert rp.voided_ruling_contradiction(blocked, 25) != ""


def _run(monkeypatch, *answers, words=180):
    calls = []

    def fake(blocks, schema, **kw):
        calls.append((blocks, schema))
        return answers[min(len(calls) - 1, len(answers) - 1)]

    monkeypatch.setattr(rp.ai_service, "call_structured", fake)
    monkeypatch.setattr(rp.ai_service, "set_student_context", lambda *a, **k: None, raising=False)
    out = rp.run_review(scope_type="assignment", scope_id=33, pack={"summary": "Day 09 Gamma"},
                        pack_version=1, rubric=RUBRIC, student_answer="Slide 1 Data Science " * 60,
                        word_count=words, word_limit_min=0, word_limit_max=99999, student_id=1,
                        task_text="Day 09 : Gamma — Data Science presentation")
    return out, calls


def test_every_call_in_a_review_uses_the_pinned_schema(monkeypatch):
    out, calls = _run(monkeypatch, _review([0, 0], fm=PPTX), _review([60, 50]))
    assert len(calls) == 2                       # ruling-shaped: the fallback still fires
    for _blocks, schema in calls:
        assert schema["properties"]["criteria"]["maxItems"] == 2
    assert out["scores"]["totalScore"] == 56 - rp.FORMAT_MISS_PENALTY


def test_a_low_but_real_format_miss_mark_costs_one_call(monkeypatch):
    out, calls = _run(monkeypatch, _review([30, 30], fm=PPTX))
    assert len(calls) == 1
    assert out["scores"]["totalScore"] == 30 - rp.FORMAT_MISS_PENALTY


# ─── 3. escalation only when it can change something ────────────────────────

def test_low_confidence_does_not_escalate_to_the_same_model():
    r = _review([50, 50])
    for c in r["criteria"]:
        c["confidence"] = "low"
    assert rp.needs_escalation(r, 200, strong_is_default=True) is False
    assert rp.needs_escalation(r, 200, strong_is_default=False) is True


def test_garbage_suspicion_escalates_only_when_a_hard_zero_is_at_stake():
    r = _review([0, 0]); r["is_garbage"] = True
    assert rp.needs_escalation(r, rp.GARBAGE_HARD_ZERO_MAX_WORDS) is True
    assert rp.needs_escalation(r, rp.GARBAGE_HARD_ZERO_MAX_WORDS + 1) is False


def test_strong_is_default_reads_the_env(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL_STRONG", raising=False)
    assert ai_service.strong_is_default() is True
    monkeypatch.setenv("ANTHROPIC_MODEL_STRONG", "claude-sonnet-4-5")
    assert ai_service.strong_is_default() is False


# ─── 4. the blank-review loop has a floor ───────────────────────────────────

def _capture(monkeypatch, prior_feedback):
    writes = []
    monkeypatch.setattr(dbs, "tquery", lambda t, sql, params=(): [{"feedback": prior_feedback}])
    monkeypatch.setattr(dbs, "texecute", lambda t, sql, params=(): writes.append(params))
    from app.services import grade_guard
    monkeypatch.setattr(grade_guard, "may_write_grade",
                        lambda **kw: (False, "the reviewer produced no feedback at all"))
    return writes


def test_the_first_empty_review_is_retryable_and_says_nothing_about_our_side(monkeypatch):
    writes = _capture(monkeypatch, None)
    assert dbs.update_assignment_submission_with_ai_results("t", 1, {"totalScore": 0}) is False
    payload = json.loads(writes[-1][0])
    assert payload["ourRefusal"] is True and payload["emptyReviews"] == 1
    assert "rulesVersion" not in payload and "manualCheck" not in payload
    assert "our side" not in payload["message"] and "could not" not in payload["message"]
    assert payload["message"] == dbs.REFUSAL_NOTICE


def test_the_second_empty_review_is_parked_for_a_person(monkeypatch):
    writes = _capture(monkeypatch, json.dumps({"notGraded": True, "emptyReviews": 1}))
    dbs.update_assignment_submission_with_ai_results("t", 1, {"totalScore": 0})
    payload = json.loads(writes[-1][0])
    assert payload["emptyReviews"] == 2 and payload["manualCheck"] is True
    assert "rulesVersion" in payload                  # stamped: the sweep will not re-buy it
    assert payload["message"] == dbs.REFUSAL_NOTICE_MANUAL


def test_the_sweeper_reads_the_machine_marker_not_the_wording():
    from app.services import sweeper_service as sw
    stored = json.dumps({"notGraded": True, "ourRefusal": True, "message": dbs.REFUSAL_NOTICE})
    assert any(p in stored for p in sw.OUR_REFUSAL_PHRASES)
    assert not any(p in dbs.REFUSAL_NOTICE for p in sw.OUR_REFUSAL_PHRASES)
