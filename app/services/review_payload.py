# app/services/review_payload.py
# ---------------------------------------------------------------------------
# ONE definition of "a stored review".
#
# Why this exists (12 Aug 2026): the assignment and case-study writers each
# built their own feedback JSON. Both omitted feedbackPoints and hardTruth, so
# "Personalised Feedback" and "The Bottom Line" existed only in the live
# response and vanished the moment the student navigated away — View Details
# and the Graded tab could never show them, however the frontend was written.
#
# The two writers still own their table-specific columns; they share the shape
# of the payload itself.
# ---------------------------------------------------------------------------

from typing import Optional


def build(result: dict, max_marks: int = 100, score_marks: Optional[float] = None,
          reviewed_by: str = "ai") -> dict:
    """The review as it is stored and later re-rendered.

    Every field the review card can display must be here. If the UI shows it,
    this dict carries it — otherwise the review is lossy on reload.
    """
    percent = result.get("totalScore")
    return {
        # ── Identity ──
        "reviewedBy":       reviewed_by,

        # ── Score, in both units so no consumer has to guess ──
        "grade":            result.get("grade"),
        "totalScore":       percent,
        "scorePercent":     percent,
        "scoreMarks":       score_marks,
        "outOf":            max_marks,

        # ── The review body ──
        "summary":          result.get("summary", ""),
        "rubricScores":     result.get("rubricScores", []),
        "strengths":        result.get("strengths", []),
        "improvements":     result.get("improvements", []),
        "feedbackPoints":   result.get("feedbackPoints", []),
        "detailedFeedback": result.get("detailedFeedback", ""),
        "hardTruth":        result.get("hardTruth", ""),
        "missingConcepts":  result.get("missingConcepts", []),
        "coveredConcepts":  result.get("coveredConcepts", []),
        "suggestedModules": result.get("suggestedModules", []),
        "encouragement":    result.get("encouragement", ""),
        "nextAction":       result.get("nextAction", "") or result.get("next_action", ""),

        # ── Diagnostics ──
        "wordCount":        result.get("wordCount"),
        "wordCountMessage": result.get("wordCountMessage", ""),
        # Rubric rows and the headline are now BOTH in marks, so a hidden
        # deduction makes the card fail its own arithmetic in public. Carry it.
        "penaltyPercent":   result.get("penaltyPercent", 0),
        "penaltyMarks":     round(float(result.get("penaltyPercent", 0) or 0)
                                  * max(1, int(max_marks or 100)) / 100, 1),
        "isGarbage":        bool(result.get("isGarbage")),
        "garbageWarning":   result.get("garbageWarning", ""),

        # ── Authorship: advisory only, never part of the score ──
        "aiLikelihoodPercent":    result.get("aiLikelihoodPercent"),
        "humanLikelihoodPercent": result.get("humanLikelihoodPercent"),
        "aiDetectionReason":      result.get("aiDetectionReason", ""),
        "aiVerdict":              result.get("aiVerdict", ""),
    }
