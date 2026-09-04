"""The audit record was written empty (04 Sep 2026).

review_payload.audit_record() reads decisions.scoringPath / gatesHit from
the result dict the assignment route hands to the database — and the route
never put `decisions` there. Every assignment mark since 23 Aug stored
scoringPath "" and gatesHit [], so "which rules made this mark" and "was
the format deduction applied" could not be answered from the row. Pinned:
the stored payload carries the pipeline's decisions and the scoring
narrative.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.routes import assignment_review as route
from app.services import assignment_db_service as dbs
from app.services import review_payload


def _pipeline_result():
    return {
        "wrongTask": {"declared": False, "whatItIs": ""},
        "scores": {"totalScore": 62, "rubricBreakdown": [
            {"name": "Gamma presentation on Data Science", "score": 82, "maxScore": 100,
             "percentage": 82, "judgment": "Judged."}],
            "gatesHit": [{"gate": "format_miss", "criterion": "TOTAL", "from": 82, "to": 62,
                          "detail": "asked for Gamma link; arrived as PPTX"}],
            "wordCountNote": "", "wordCountPenalty": 0},
        "strengths": ["s"], "improvements": ["i"], "conceptsMissing": [], "conceptsCovered": [],
        "detailedFeedback": "d", "feedbackPoints": ["p"], "hardTruth": "h",
        "isGarbage": False, "garbageWarning": "",
        "authorship": {"aiLikelihoodPercent": 30, "humanLikelihoodPercent": 70,
                       "aiDetectionReason": "r", "aiVerdict": "human"},
        "howYouScored": "Deduction of 20 points for the format — asked for Gamma link; arrived as PPTX",
        "languageReport": {}, "factualErrors": [],
        "decisions": {"packVersion": 1, "scoringPath": "haiku-single+rejudged-on-content",
                      "gatesHit": [{"gate": "format_miss"}]},
    }


def test_the_stored_payload_carries_scoring_path_and_gates(monkeypatch):
    captured = {}

    def fake_update(tenant, submission_id, result, max_marks=100, manifest="", course_id=None):
        captured["payload"] = review_payload.build(result, max_marks, 6.2)
        return True

    monkeypatch.setattr(dbs, "update_assignment_submission_with_ai_results", fake_update)
    monkeypatch.setattr(route.scoring_service, "build_summary",
                        lambda awarded, max_marks, requirements=None: "summary")
    route._pipeline_assignment_response(
        None, {"submissionId": 5378, "attemptNumber": 1}, _pipeline_result(), 525, 0.0,
        max_marks=10, task_title="Day 09 : Gamma")
    audit = captured["payload"]["audit"]
    assert audit["scoringPath"] == "haiku-single+rejudged-on-content"
    assert audit["gatesHit"] and audit["gatesHit"][0]["gate"] == "format_miss"
    assert "Deduction of 20 points" in captured["payload"]["facultyView"]["howItWasScored"]
    # The requirement rows still come through the faculty view unchanged.
    assert captured["payload"]["facultyView"]["requirements"][0]["requirement"] == \
        "Gamma presentation on Data Science"
