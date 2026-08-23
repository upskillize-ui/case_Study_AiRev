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

import os
from datetime import datetime, timezone
from typing import Optional


def audit_record(result: dict, manifest: str = "", word_count=None) -> dict:
    """What this mark was made from — enough to reproduce or defend it.

    Why (23 Aug 2026): a learner disputes a 2/10 and there is no way to show
    what was judged. The submission text is already stored, but not WHICH task
    wording, WHICH scoring rules, or WHICH model produced the number — so a
    mark from before a rules change is indistinguishable from one after it,
    and "we re-ran it and got something else" is the best answer available.

    Cheap to store, and the only thing that makes an appeal answerable.
    """
    from app.services.rubric_service import RUBRIC_VERSION as REQUIREMENTS_VERSION
    decisions = result.get("decisions") or {}
    return {
        "reviewedAt":     datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model":          os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        # The number that says WHICH RULES made this mark. Named for what
        # it now tracks: how the task's requirements are read, not a rubric.
        "rulesVersion":   REQUIREMENTS_VERSION,
        "scoringPath":    decisions.get("scoringPath", ""),
        "packVersion":    decisions.get("packVersion"),
        "gatesHit":       decisions.get("gatesHit", []),
        # What actually reached the marker, in the intake's own words. This is
        # the line that answers "but my page WAS published".
        "manifest":       (manifest or "")[:2000],
        "wordsRead":      word_count if word_count is not None
                          else result.get("wordCount"),
    }


def _faculty_view(result: dict) -> dict:
    """What the task asked for, and how each requirement fared.

    Reads whichever shape the writer used — the pipeline files its rows under
    facultyView, the legacy path leaves them at the top level — and re-emits
    them in one vocabulary. Nothing is invented here and nothing is dropped;
    only the naming changes, so a mentor reading this sees the BRIEF, not a
    scoring instrument that was never given to the learner.
    """
    faculty = dict(result.get("facultyView") or {})
    rows = faculty.pop("rubricScores", None) or result.get("rubricScores") or []
    requirements = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        requirements.append({
            "requirement": r.get("criteria") or r.get("name") or "",
            "scored":      r.get("scoreMarks", r.get("score")),
            "outOf":       r.get("maxScoreMarks", r.get("maxScore")),
            "percent":     r.get("percentage"),
            "note":        r.get("judgment") or r.get("note") or "",
            "evidence":    r.get("evidence") or [],
        })
    out = {k: v for k, v in faculty.items() if k != "howYouScored"}
    if requirements:
        out["requirements"] = requirements
    narrative = (result.get("facultyView") or {}).get("howYouScored")
    if narrative:
        out["howItWasScored"] = narrative
    return out


def build(result: dict, max_marks: int = 100, score_marks: Optional[float] = None,
          reviewed_by: str = "ai", manifest: str = "") -> dict:
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
        # THE RUBRIC IS GONE (23 Aug 2026). Ranjana: "remove completely."
        #
        # What faculty see is no longer a rubric table — it is the list of
        # things THIS TASK ASKED FOR, in the task's own words, with how each
        # one fared. There is no framework behind it, invented or otherwise:
        # requirements come from the brief, weight equally, and are scored
        # one by one. The word "rubric" survives nowhere a person reads.
        "facultyView":      _faculty_view(result),
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

        # ── Provenance: what this mark was made from ──
        "audit":            audit_record(result, manifest),

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
