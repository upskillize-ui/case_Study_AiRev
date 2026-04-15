# app/services/feedback_service.py
# Generates student-facing feedback and mentor summaries.
# Tone: warm, encouraging, constructive — no harsh language.
#
# This version surfaces three new fields:
#   - aiLikelihoodPercent / humanLikelihoodPercent   (Human vs AI detector)
#   - aiDetectionReason
#   - garbageWarning                                  (for nonsense/empty submissions)
# It also returns `scoreEmoji` so the UI doesn't have to guess.

from app.services.scoring_service import get_grade_label


def generate_feedback(
    scores: dict,
    ai_analysis: dict,
    word_count: int,
    word_limit_min: int,
    word_limit_max: int,
) -> dict:

    # ── Word count message ──
    if word_count < word_limit_min:
        word_count_status  = "too_short"
        word_count_message = (
            f"Your answer is {word_count} words. The suggested minimum is {word_limit_min} words. "
            f"Try expanding your analysis a little — more depth will help your score! 📝"
        )
    elif word_count > word_limit_max:
        word_count_status  = "too_long"
        word_count_message = (
            f"Your answer is {word_count} words, which is a bit above the {word_limit_max}-word guide. "
            f"Consider trimming slightly to keep your points crisp and clear."
        )
    else:
        word_count_status  = "ok"
        word_count_message = (
            f"Your answer is {word_count} words — nicely within the expected range. ✅"
        )

    # ── Concept coverage ──
    covered  = ai_analysis.get("conceptsCovered", [])
    missing  = ai_analysis.get("conceptsMissing",  [])
    total    = len(covered) + len(missing)
    coverage_pct = round((len(covered) / total) * 100) if total > 0 else 0

    total_score = scores["totalScore"]
    grade       = scores["grade"]
    grade_label = get_grade_label(total_score)
    score_emoji = _emoji_for(total_score)

    # ── Garbage / AI-detection ──
    is_garbage   = bool(ai_analysis.get("isGarbage"))
    ai_pct       = int(ai_analysis.get("aiLikelihoodPercent", 50) or 50)
    human_pct    = max(0, min(100, 100 - ai_pct))
    ai_reason    = ai_analysis.get("aiDetectionReason", "")
    ai_verdict   = (
        "very-likely-ai"   if ai_pct >= 80
        else "likely-ai"   if ai_pct >= 60
        else "uncertain"   if ai_pct >= 40
        else "likely-human"if ai_pct >= 20
        else "very-likely-human"
    )

    garbage_warning = ""
    if is_garbage:
        reason = ai_analysis.get("garbageReason", "").strip()
        garbage_warning = (
            "Your submission did not appear to be a genuine attempt at the case study. "
            + (f"Reason: {reason}. " if reason else "")
            + "Please re-read the case study and submit a thoughtful response."
        )

    # ── Student-facing feedback ──
    student_feedback = {
        "summary": (
            f"You scored {total_score}/100 ({grade} — {grade_label}). "
            f"You covered {len(covered)} out of {total} key concepts ({coverage_pct}% coverage). "
            f"{_get_summary_note(total_score)}"
        ),
        "scoreEmoji":             score_emoji,
        "strengths":              ai_analysis.get("strengths",     []),
        "improvements":           ai_analysis.get("improvements",  []),
        "detailedFeedback":       ai_analysis.get("detailedFeedback", ""),
        "conceptsCovered":        covered,
        "conceptsMissing":        missing,
        "conceptCoveragePercent": coverage_pct,
        "wordCountStatus":        word_count_status,
        "wordCountMessage":       word_count_message,
        "suggestedModules":       ai_analysis.get("suggestedTopics", []),
        "encouragement":          _get_encouragement(total_score),

        # NEW — Human vs AI detection
        "aiLikelihoodPercent":    ai_pct,
        "humanLikelihoodPercent": human_pct,
        "aiDetectionReason":      ai_reason,
        "aiVerdict":              ai_verdict,

        # NEW — Garbage / nonsense flag
        "isGarbage":              is_garbage,
        "garbageWarning":         garbage_warning,
    }

    # ── Mentor summary ──
    mentor_summary = {
        "score":             total_score,
        "grade":             grade,
        "gradeLabel":        grade_label,
        "scoreEmoji":        score_emoji,
        "needsAttention":    total_score < 40 or is_garbage,
        "performanceLevel":  ("strong"   if total_score >= 70
                              else "moderate"  if total_score >= 40
                              else "developing"),
        "plagiarismRisk":    ai_analysis.get("plagiarismRisk", "low"),
        "plagiarismNote":    ai_analysis.get("plagiarismNote", ""),
        "quickAction":       _get_quick_action(total_score, ai_analysis),
        "keyMissing":        missing,
        "rubricBreakdown":   scores["rubricBreakdown"],
        "mentorAlert":       ai_analysis.get("mentorAlert", False) or is_garbage,
        "mentorAlertReason": ai_analysis.get("mentorAlertReason", "")
                             or ("Possible non-genuine submission." if is_garbage else ""),
        "conceptCoverage":   f"{len(covered)}/{total} ({coverage_pct}%)",
        "wordCount":         word_count,
        "aiLikelihoodPercent": ai_pct,
        "humanLikelihoodPercent": human_pct,
        "aiDetectionReason": ai_reason,
        "isGarbage":         is_garbage,
    }

    return {
        "strengths":        ai_analysis.get("strengths",    []),
        "improvements":     ai_analysis.get("improvements", []),
        "detailed":         ai_analysis.get("detailedFeedback", ""),
        "wordCountStatus":  word_count_status,
        "wordCountMessage": word_count_message,
        "suggestedModules": ai_analysis.get("suggestedTopics", []),
        "studentFeedback":  student_feedback,
        "mentorSummary":    mentor_summary,

        # Top-level convenience fields the route also surfaces:
        "scoreEmoji":             score_emoji,
        "aiLikelihoodPercent":    ai_pct,
        "humanLikelihoodPercent": human_pct,
        "aiDetectionReason":      ai_reason,
        "aiVerdict":              ai_verdict,
        "isGarbage":              is_garbage,
        "garbageWarning":         garbage_warning,
    }


def _emoji_for(score: int) -> str:
    if score >= 85: return "🌟"
    if score >= 70: return "👏"
    if score >= 50: return "💪"
    if score >= 30: return "📚"
    return "🤝"


def _get_summary_note(score: int) -> str:
    if score >= 85: return "Fantastic work — you're really mastering this material!"
    if score >= 70: return "Solid performance! A little more depth and you can push even higher."
    if score >= 50: return "Good effort! Focus on the improvement suggestions and you'll see great progress."
    if score >= 30: return "You've made a start — reviewing the suggested topics will help a lot."
    return "Everyone starts somewhere — your mentor will help you build from here."


def _get_encouragement(score: int) -> str:
    if score >= 90:
        return ("Outstanding work! 🌟 You have shown exceptional understanding of the subject. "
                "Keep this momentum going — you're a star student!")
    if score >= 70:
        return ("Great job! 👏 You have a solid grasp of the key concepts. "
                "A bit more depth in your analysis and you could be scoring even higher next time.")
    if score >= 50:
        return ("Good effort! 💪 You're on the right track. "
                "Focus on the improvement areas, review the suggested modules, and try re-attempting — "
                "you'll be surprised how much better you can do.")
    if score >= 30:
        return ("Thank you for submitting! 🙂 Every attempt is a step forward. "
                "Review the suggested study topics and reach out to your mentor — "
                "with a little guidance, you'll improve quickly.")
    return ("Thank you for giving this a go! 🤝 It looks like you might benefit from some extra support. "
            "Please review the course modules and don't hesitate to schedule a session with your mentor — "
            "they are here to help you succeed. Remember: every expert was once a beginner.")


def _get_quick_action(score: int, ai_analysis: dict) -> str:
    if ai_analysis.get("isGarbage"):
        return "Review required: submission appears to be non-genuine — please verify."
    if ai_analysis.get("plagiarismRisk") == "high":
        return "Review required: High text similarity detected — please check for original analysis."
    if score >= 70:
        return "Student is performing well — approve feedback."
    if score >= 40:
        return "Student needs guidance on some concepts — consider a quick check-in."
    return "Priority: Student may benefit from 1-on-1 mentoring — please reach out."