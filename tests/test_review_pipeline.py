# tests/test_review_pipeline.py
# Pure-function tests for the evidence-gated scoring engine — no DB, no AI.
# Run: python -m pytest tests/test_review_pipeline.py -q   (or plain python)

from app.services.review_pipeline import (
    apply_gates, aggregate, needs_escalation, build_how_you_scored, GATES,
)

RUBRIC = [
    {"name": "Problem framing",   "maxScore": 25},
    {"name": "Evidence use",      "maxScore": 25},
    {"name": "Recommendation",    "maxScore": 25},
    {"name": "Structure & clarity", "maxScore": 25},
]


def crit(name, pct, evidence=None, specific=True, conf="high"):
    return {"name": name, "evidence_quotes": evidence or [], "case_specific": specific,
            "judgment": "j", "score_pct": pct, "confidence": conf}


def test_no_evidence_caps_criterion():
    criteria = [crit("Problem framing", 85, evidence=[]),          # generous but unpaid
                crit("Evidence use", 80, evidence=["quoted line"]),
                crit("Recommendation", 70, evidence=["another quote"]),
                crit("Structure & clarity", 75, evidence=["q"])]
    g = apply_gates(criteria, RUBRIC, [], ["c1", "c2"], [])
    framing = next(r for r in g["breakdown"] if r["criteria"] == "Problem framing")
    assert framing["percentage"] == GATES["no_evidence_cap"], "85 with no evidence must cap at 20"
    assert any(h["gate"] == "no_evidence" for h in g["gates_hit"])


def test_generic_answer_caps_specificity_bound_criteria():
    criteria = [crit("Problem framing", 80, ["q"], specific=False),   # not specificity-bound → untouched
                crit("Evidence use", 85, ["generic talk"], specific=False)]  # bound → capped 40
    g = apply_gates(criteria, RUBRIC[:2], [], ["c"], [])
    ev = next(r for r in g["breakdown"] if r["criteria"] == "Evidence use")
    pf = next(r for r in g["breakdown"] if r["criteria"] == "Problem framing")
    assert ev["percentage"] == GATES["generic_answer_cap"]
    assert pf["percentage"] == 80


def test_concept_coverage_caps_total():
    criteria = [crit(r["name"], 90, ["q"]) for r in RUBRIC]
    g = apply_gates(criteria, RUBRIC, ["m1", "m2", "m3"], ["c1"], [])  # 1/4 covered
    s = aggregate(g, word_count=500, word_limit_min=300, word_limit_max=800)
    assert s["totalScore"] <= GATES["concept_total_cap"], "90s across the board can't beat the concept gate"


def test_factual_errors_deduct():
    criteria = [crit(r["name"], 80, ["q"]) for r in RUBRIC]
    errors = [{"quote": "x", "issue": "wrong", "severity": "major"},
              {"quote": "y", "issue": "imprecise", "severity": "minor"}]
    g = apply_gates(criteria, RUBRIC, [], ["c1", "c2"], errors)
    assert g["error_deduction"] == GATES["major_error_deduction"] + GATES["minor_error_deduction"]
    s = aggregate(g, 500, 300, 800)
    assert s["totalScore"] == 80 - g["error_deduction"]


def test_word_count_penalty_and_clamp():
    criteria = [crit(r["name"], 100, ["q"]) for r in RUBRIC]
    g = apply_gates(criteria, RUBRIC, [], ["c1"], [])
    s = aggregate(g, word_count=100, word_limit_min=400, word_limit_max=800)
    assert s["wordCountPenalty"] > 0
    assert s["totalScore"] == 100 - s["wordCountPenalty"]
    assert 0 <= s["totalScore"] <= 100


def test_model_never_computes_total():
    """Total must equal weighted sum minus deductions — pure arithmetic."""
    criteria = [crit("Problem framing", 60, ["q"]), crit("Evidence use", 40, ["q"]),
                crit("Recommendation", 80, ["q"]), crit("Structure & clarity", 20, ["q"])]
    g = apply_gates(criteria, RUBRIC, [], ["c1"], [])
    s = aggregate(g, 500, 300, 800)
    expected = round(sum(r["score"] for r in g["breakdown"]))
    assert s["totalScore"] == expected == 50


def test_escalation_triggers():
    assert needs_escalation({"is_garbage": True, "criteria": []})
    assert needs_escalation({"is_garbage": False, "criteria": [
        crit("a", 50, conf="low"), crit("b", 50, conf="low")]})
    assert not needs_escalation({"is_garbage": False, "criteria": [
        crit("a", 50, conf="low"), crit("b", 50, conf="high")]})


def test_how_you_scored_explains_gates():
    criteria = [crit("Evidence use", 85, evidence=[])]
    g = apply_gates(criteria, RUBRIC[1:2], ["m1", "m2"], [], [])
    s = aggregate(g, 500, 300, 800)
    pack = {"band_anchors": {"outstanding": "every claim tied to a case figure"}}
    text = build_how_you_scored(s, pack, ["m1", "m2"])
    assert "Your score:" in text
    assert "cannot score above" in text          # gate explained in student language
    assert "top answer" in text                   # target shown, not just the slap
    assert "m1" in text                           # missing concepts named


if __name__ == "__main__":
    import sys, inspect
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
