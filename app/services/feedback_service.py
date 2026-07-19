# app/services/feedback_service.py
# Generates student-facing feedback and mentor summaries.
# Tone: constructive and direct — coaching wording, honest scoring.
#
# Surfaces:
#   - aiLikelihoodPercent / humanLikelihoodPercent   (authorship indicator — advisory only)
#   - aiDetectionReason / aiVerdict
#   - garbageWarning                                  (for nonsense/empty submissions)
#
# Brand rule: NO emojis anywhere in feedback text. `scoreEmoji` is kept as an
# API key for frontend compatibility but is always empty — the UI renders
# Lucide SVG icons, never emojis.

from app.services.scoring_service import get_grade_label


def ai_verdict(ai_pct: int) -> str:
    """Map an AI-likelihood percentage to a verdict slug.

    Pure function, shared by every review flow (case studies, assignments,
    industry sessions) so thresholds live in exactly one place.
    Advisory only — the verdict never influences any score.
    """
    if ai_pct >= 80:
        return "very-likely-ai"
    if ai_pct >= 60:
        return "likely-ai"
    if ai_pct >= 40:
        return "uncertain"
    if ai_pct >= 20:
        return "likely-human"
    return "very-likely-human"


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
            f"Expand your analysis — more depth will strengthen your score."
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
            f"Your answer is {word_count} words — within the expected range."
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
    verdict      = ai_verdict(ai_pct)

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
        "aiVerdict":              verdict,

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
        "aiVerdict":              verdict,
        "isGarbage":              is_garbage,
        "garbageWarning":         garbage_warning,
    }


def _emoji_for(score: int) -> str:
    """Deprecated. Brand rule: never emojis in feedback — the UI renders
    Lucide SVG icons keyed off the score/band. Key retained for API
    compatibility; always empty."""
    return ""


def _get_summary_note(score: int) -> str:
    if score >= 85: return "Fantastic work — you're really mastering this material!"
    if score >= 70: return "Solid performance! A little more depth and you can push even higher."
    if score >= 50: return "Good effort! Focus on the improvement suggestions and you'll see great progress."
    if score >= 30: return "You've made a start — reviewing the suggested topics will help a lot."
    return "Everyone starts somewhere — your mentor will help you build from here."


def _get_encouragement(score: int) -> str:
    if score >= 90:
        return ("Outstanding work. You have shown exceptional understanding of the subject — "
                "keep this momentum going.")
    if score >= 70:
        return ("An answer that holds up. You have a solid grasp of the key concepts. "
                "A bit more depth in your analysis and you could be scoring even higher next time.")
    if score >= 50:
        return ("You're on the right track. "
                "Focus on the improvement areas, review the suggested modules, and re-attempt — "
                "the gap between this attempt and a strong one is closable.")
    if score >= 30:
        return ("Every attempt is a step forward. "
                "Review the suggested study topics and reach out to your mentor — "
                "with targeted guidance, you'll improve quickly.")
    return ("This attempt needs rebuilding from the ground up — and that's doable. "
            "Review the course modules and schedule a session with your mentor; "
            "they are there to help you close the gap.")


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