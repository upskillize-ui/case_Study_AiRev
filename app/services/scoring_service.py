# app/services/scoring_service.py
# Converts AI analysis into scores and grades


def calculate_scores(ai_analysis: dict, grading_rubric: dict, word_count: int, word_limit_min: int, word_limit_max: int) -> dict:
    rubric_breakdown = []

    for criterion in grading_rubric.get("criteria", []):
        name = criterion["name"].lower()
        ai_score = 50  # default

        if "understanding" in name or "concept" in name or "relevance" in name:
            ai_score = ai_analysis.get("relevanceScore", 50)
        elif "application" in name or "analysis" in name or "critical" in name:
            ai_score = ai_analysis.get("applicationScore", 50)
        elif "depth" in name or "detail" in name:
            ai_score = ai_analysis.get("depthScore", 50)
        elif "accuracy" in name or "correct" in name:
            ai_score = ai_analysis.get("accuracyScore", 50)
        elif "structure" in name or "clarity" in name or "writing" in name:
            ai_score = ai_analysis.get("structureScore", 50)
        elif "example" in name or "real-world" in name or "practical" in name:
            ai_score = ai_analysis.get("applicationScore", 50)

        ai_score = max(0, min(100, ai_score))
        weighted_score = round((ai_score / 100) * criterion["maxScore"], 2)

        status = "good" if ai_score >= 70 else ("average" if ai_score >= 40 else "needs_improvement")

        rubric_breakdown.append({
            "criteria": criterion["name"],
            "maxScore": criterion["maxScore"],
            "score": weighted_score,
            "percentage": ai_score,
            "status": status,
        })

    # Word count penalty
    word_count_penalty = 0
    word_count_note = ""

    if word_count < word_limit_min:
        shortfall = 1 - (word_count / word_limit_min)
        word_count_penalty = min(20, round(shortfall * 30))
        word_count_note = f"Answer is {word_count} words (minimum {word_limit_min}). {word_count_penalty} point penalty."
    elif word_count > word_limit_max * 1.5:
        word_count_penalty = 5
        word_count_note = f"Answer is {word_count} words (maximum {word_limit_max}). 5 point penalty for exceeding limit."

    raw_total = sum(r["score"] for r in rubric_breakdown)
    total_score = max(0, min(100, round(raw_total - word_count_penalty)))

    return {
        "totalScore": total_score,
        "grade": _get_grade(total_score),
        "rubricBreakdown": rubric_breakdown,
        "wordCountPenalty": word_count_penalty,
        "wordCountNote": word_count_note,
        "rawTotal": round(raw_total),
    }


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
