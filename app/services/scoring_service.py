# app/services/scoring_service.py
# Converts AI analysis into final scores and grades.
#
# This version uses the AI's per-criterion scores (from `criterionScores` in the
# AI response) instead of brittle keyword matching. The keyword logic is kept
# only as a safety fallback when the AI doesn't return a score for a criterion.


def calculate_scores(ai_analysis: dict, grading_rubric: dict, word_count: int,
                     word_limit_min: int, word_limit_max: int) -> dict:
    rubric_breakdown = []
    criterion_scores = ai_analysis.get("criterionScores") or {}
    is_garbage = bool(ai_analysis.get("isGarbage"))

    for criterion in grading_rubric.get("criteria", []):
        name      = criterion["name"]
        max_score = criterion["maxScore"]

        if is_garbage:
            ai_score = 0
        elif name in criterion_scores:
            # Preferred path: AI scored this exact criterion by name
            ai_score = criterion_scores[name]
        else:
            # Fallback: legacy keyword matching against generic dimensions
            ai_score = _legacy_keyword_score(name, ai_analysis)

        ai_score = max(0, min(100, int(round(float(ai_score)))))
        weighted_score = round((ai_score / 100) * max_score, 2)
        status = ("good"               if ai_score >= 70
                  else "average"       if ai_score >= 40
                  else "needs_improvement")

        rubric_breakdown.append({
            "criteria":   name,
            "maxScore":   max_score,
            "score":      weighted_score,
            "percentage": ai_score,
            "status":     status,
        })

    # Word-count penalty
    word_count_penalty = 0
    word_count_note = ""
    if word_count < word_limit_min:
        shortfall = 1 - (word_count / max(word_limit_min, 1))
        word_count_penalty = min(20, round(shortfall * 30))
        word_count_note = (
            f"Answer is {word_count} words (minimum {word_limit_min}). "
            f"{word_count_penalty} point penalty."
        )
    elif word_count > word_limit_max * 1.5:
        word_count_penalty = 5
        word_count_note = (
            f"Answer is {word_count} words (maximum {word_limit_max}). "
            f"5 point penalty for exceeding limit."
        )

    raw_total   = sum(r["score"] for r in rubric_breakdown)
    total_score = max(0, min(100, round(raw_total - word_count_penalty)))

    return {
        "totalScore":       total_score,
        "grade":            _get_grade(total_score),
        "rubricBreakdown":  rubric_breakdown,
        "wordCountPenalty": word_count_penalty,
        "wordCountNote":    word_count_note,
        "rawTotal":         round(raw_total),
    }


def _legacy_keyword_score(name: str, ai_analysis: dict) -> int:
    """Fallback only — used when AI didn't return a score for this criterion."""
    n = (name or "").lower()
    if "understanding" in n or "concept" in n or "relevance" in n:
        return ai_analysis.get("relevanceScore", 50)
    if "application" in n or "analysis" in n or "critical" in n or "recommend" in n:
        return ai_analysis.get("applicationScore", 50)
    if "depth" in n or "detail" in n or "research" in n:
        return ai_analysis.get("depthScore", 50)
    if "accuracy" in n or "correct" in n or "fact" in n:
        return ai_analysis.get("accuracyScore", 50)
    if "structure" in n or "clarity" in n or "writing" in n or "presentation" in n or "format" in n:
        return ai_analysis.get("structureScore", 50)
    if "example" in n or "real-world" in n or "practical" in n:
        return ai_analysis.get("applicationScore", 50)
    return 50


def _get_grade(score: int) -> str:
    if score >= 90: return "A+"
    if score >= 80: return "A"
    if score >= 70: return "B+"
    if score >= 60: return "B"
    if score >= 50: return "C+"
    if score >= 40: return "C"
    if score >= 30: return "D"
    return "F"


def get_grade_label(score: int) -> str:
    if score >= 90: return "Excellent"
    if score >= 80: return "Very Good"
    if score >= 70: return "Good"
    if score >= 60: return "Above Average"
    if score >= 50: return "Average"
    if score >= 40: return "Below Average"
    if score >= 30: return "Poor"
    return "Needs Significant Improvement"