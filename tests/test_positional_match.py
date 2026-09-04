"""The marker answered in its own words (04 Sep 2026, job 860 live).

On the dashboard days the model returned one row per requirement, judged and
scored, but named after the "fully done when" sentence instead of the
requirement's name. Name matching found nothing, every row became unjudged,
and grade_guard refused the mark as "no judgement for any part of this task"
— dozens of real reviews thrown away. Position is the tie-breaker: same
count, same order, unmatched rows pair by index.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import review_pipeline as rp
from app.services import grade_guard as g

RUBRIC = [
    {"name": "Dashboard is built in Gemini Canvas", "maxScore": 40,
     "whatEarnsIt": "a working interactive dashboard exists and opens"},
    {"name": "At least three charts", "maxScore": 35,
     "whatEarnsIt": "three or more distinct visualisations of the data"},
    {"name": "Explanation of the data used", "maxScore": 25,
     "whatEarnsIt": "the learner says where the numbers came from"},
]


def _row(name, pct):
    return {"name": name, "evidence_quotes": ["q"], "case_specific": True,
            "judgment": f"Judged {name[:12]}.", "score_pct": pct, "confidence": "high"}


def test_paraphrased_names_pair_by_position_and_are_judged():
    criteria = [_row("a working interactive dashboard exists and opens", 80),
                _row("three or more distinct visualisations of the data", 60),
                _row("the learner says where the numbers came from", 40)]
    gated = rp.apply_gates(criteria, RUBRIC, [], [], [], gates={"generic_answer_cap": 100})
    rows = gated["breakdown"]
    assert [r["unjudged"] for r in rows] == [False, False, False]
    assert [r["percentage"] for r in rows] == [80, 60, 40]
    assert g.has_model_evidence(rows)
    assert sum(1 for h in gated["gates_hit"] if h["gate"] == "positional_match") == 3


def test_names_still_win_over_position():
    # Marker returned the rows in a different order but with matching names.
    criteria = [_row("Explanation of the data used", 10),
                _row("Dashboard is built in Gemini Canvas", 90),
                _row("At least three charts", 50)]
    rows = rp.apply_gates(criteria, RUBRIC, [], [], [], gates={"generic_answer_cap": 100})["breakdown"]
    assert [r["percentage"] for r in rows] == [90, 50, 10]
    assert not any(r["unjudged"] for r in rows)


def test_a_different_row_count_never_pairs_by_position():
    criteria = [_row("something unrelated", 70), _row("another unrelated", 20)]
    rows = rp.apply_gates(criteria, RUBRIC, [], [], [], gates={"generic_answer_cap": 100})["breakdown"]
    assert all(r["unjudged"] for r in rows)
    assert not g.has_model_evidence(rows)


def test_a_row_claimed_by_name_is_not_reused_by_position():
    # Row 0 matches rubric 1 by name; rubric 0 must not also take row 0.
    criteria = [_row("At least three charts", 55),
                _row("a working interactive dashboard exists and opens", 85),
                _row("the learner says where the numbers came from", 30)]
    rows = rp.apply_gates(criteria, RUBRIC, [], [], [], gates={"generic_answer_cap": 100})["breakdown"]
    assert rows[1]["percentage"] == 55 and not rows[1]["unjudged"]
    # rubric 0 is unmatched by name; positional row 0 is claimed, so it stays unjudged
    assert rows[0]["unjudged"]
    assert rows[2]["percentage"] == 30 and not rows[2]["unjudged"]
