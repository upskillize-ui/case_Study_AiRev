# app/routes/review.py
import time
import os
from fastapi import APIRouter, HTTPException
from app.models.schemas import SubmitAnswerRequest, TestReviewRequest, MentorApproveRequest
from app.services import ai_service, scoring_service, feedback_service, db_service
from app.utils.text_processor import (
    count_words, clean_text, calculate_text_overlap, find_mentioned_concepts
)
from app.utils.file_extractor import extract_text_from_url

router = APIRouter(prefix="/api/review", tags=["review"])


# ── POST /api/review/submit ────────────────────────────────────────────────
@router.post("/submit")
async def submit_and_review(req: SubmitAnswerRequest):
    start_time = time.time()
    print(f"ℹ️  New submission: student={req.studentId}, caseStudy={req.caseStudyId}")

    case_study = db_service.get_case_study_by_id(req.caseStudyId)
    if not case_study:
        raise HTTPException(status_code=404, detail="Case study not found or not published")

    cleaned    = clean_text(req.answerText)
    word_count = count_words(cleaned)

    # ── If the inline answer is short, see if there's a previously-uploaded
    #    file (PDF/DOCX) for this student+case study and use its contents. ──
    file_used = None
    if word_count < 50:
        prior = db_service.get_latest_submission_file(req.caseStudyId, req.studentId)
        if prior and prior.get("file_url"):
            extracted, why = extract_text_from_url(prior["file_url"], prior.get("file_name", ""))
            if extracted and len(extracted.split()) >= max(word_count, 30):
                # Combine: use file as primary, append the inline notes if any
                cleaned = (cleaned + "\n\n" + extracted).strip() if cleaned else extracted
                word_count = count_words(cleaned)
                file_used = prior.get("file_name") or prior["file_url"]
                print(f"📄 Using uploaded file content: {file_used} "
                      f"({word_count} words extracted)")
            else:
                print(f"📄 File extraction skipped/failed for "
                      f"{prior.get('file_name', '?')}: {why or 'too little text'}")

    text_overlap  = calculate_text_overlap(cleaned, case_study["description"])
    concept_check = find_mentioned_concepts(cleaned, case_study["keyConcepts"])

    submission = db_service.save_submission(
        req.caseStudyId, req.studentId, cleaned, word_count
    )
    print(f"✅ Submission saved: id={submission['submissionId']}, attempt={submission['attemptNumber']}")

    # ── Pre-AI cheap garbage check (saves an AI call for obvious junk) ──
    pre_garbage_reason = _pre_garbage_check(cleaned, word_count)
    if pre_garbage_reason:
        print(f"⚠️  Pre-AI garbage check tripped: {pre_garbage_reason}")
        garbage_payload = _build_garbage_response(
            submission, case_study, cleaned, word_count, pre_garbage_reason, start_time,
        )
        try:
            db_service.update_submission_with_ai_results(submission["submissionId"], garbage_payload["_internal"])
            db_service.update_performance_tracker(req.studentId, req.caseStudyId, 0)
        except Exception as db_err:
            print(f"⚠️  DB update (garbage path) failed: {db_err}")
        return garbage_payload["response"]

    # ── Real AI review ──
    try:
        ai_analysis = ai_service.analyze_answer(
            case_study={
                "title":       case_study["title"],
                "description": case_study["description"],
                "questions":   case_study["questions"],
            },
            model_answer=case_study["modelAnswers"],
            student_answer=cleaned,
            grading_rubric=case_study["gradingRubric"],
            key_concepts=case_study["keyConcepts"],
        )
    except Exception as e:
        print(f"⚠️  AI review unavailable: {e}")
        db_service.log_ai_review(submission["submissionId"], None, None, str(e))
        return {
            "success":       True,
            "partialReview": True,
            "message": (
                "Your answer has been saved successfully! ✅ "
                "Our AI reviewer is temporarily unavailable, but a mentor has been notified "
                "and will review your submission personally. You'll hear back soon!"
            ),
            "submission": submission,
        }

    if text_overlap > 60:
        ai_analysis["plagiarismRisk"]    = "medium"
        ai_analysis["plagiarismNote"]    = (
            f"Some sections closely mirror the case study text ({text_overlap}% similarity). "
            "Try rephrasing ideas in your own words to strengthen your analysis."
        )
        ai_analysis["mentorAlert"]       = True
        ai_analysis["mentorAlertReason"] = "High text similarity — mentor review recommended."

    ai_analysis["conceptsCovered"] = list(set(
        (ai_analysis.get("conceptsCovered") or []) + concept_check["mentioned"]
    ))
    ai_analysis["conceptsMissing"] = [
        c for c in concept_check["missing"]
        if c not in (ai_analysis.get("conceptsCovered") or [])
    ]

    scores   = scoring_service.calculate_scores(
        ai_analysis, case_study["gradingRubric"], word_count,
        case_study["wordLimitMin"], case_study["wordLimitMax"],
    )
    feedback = feedback_service.generate_feedback(
        scores, ai_analysis, word_count,
        case_study["wordLimitMin"], case_study["wordLimitMax"],
    )

    result = {
        "totalScore":             scores["totalScore"],
        "grade":                  scores["grade"],
        "rubricScores":           scores["rubricBreakdown"],
        "strengths":              feedback["strengths"],
        "improvements":           feedback["improvements"],
        "missingConcepts":        ai_analysis.get("conceptsMissing", []),
        "coveredConcepts":        ai_analysis.get("conceptsCovered", []),
        "suggestedModules":       feedback["suggestedModules"],
        "detailedFeedback":       feedback["detailed"],
        "wordCount":              word_count,
        "wordCountMessage":       feedback["wordCountMessage"],
        "plagiarismFlag":         ai_analysis.get("plagiarismRisk", "low"),
        "needsMentorHelp":        scores["totalScore"] < 40 or ai_analysis.get("mentorAlert", False),
        # NEW — surfaced for storage + frontend display
        "scoreEmoji":             feedback["scoreEmoji"],
        "aiLikelihoodPercent":    feedback["aiLikelihoodPercent"],
        "humanLikelihoodPercent": feedback["humanLikelihoodPercent"],
        "aiDetectionReason":      feedback["aiDetectionReason"],
        "aiVerdict":              feedback["aiVerdict"],
        "isGarbage":              feedback["isGarbage"],
        "garbageWarning":         feedback["garbageWarning"],
    }

    try:
        db_service.update_submission_with_ai_results(submission["submissionId"], result)
        db_service.update_performance_tracker(req.studentId, req.caseStudyId, scores["totalScore"])
        db_service.log_ai_review(submission["submissionId"], ai_analysis.get("_meta"), ai_analysis)
    except Exception as db_err:
        print(f"⚠️  DB update failed after AI review: {db_err} — returning result anyway")

    total_time = int((time.time() - start_time) * 1000)
    print(f"✅ Review complete: score={scores['totalScore']}, grade={scores['grade']}, "
          f"ai={feedback['aiLikelihoodPercent']}%, time={total_time}ms")

    return {
        "success":    True,
        "submission": submission,
        "feedback": {
            "score":                  result["totalScore"],
            "grade":                  result["grade"],
            "scoreEmoji":             result["scoreEmoji"],
            "summary":                feedback["studentFeedback"]["summary"],
            "rubricScores":           result["rubricScores"],
            "strengths":              result["strengths"],
            "improvements":           result["improvements"],
            "missingConcepts":        result["missingConcepts"],
            "coveredConcepts":        result["coveredConcepts"],
            "suggestions":            result["suggestedModules"],
            "detailedFeedback":       result["detailedFeedback"],
            "wordCount":              result["wordCount"],
            "wordCountMessage":       result["wordCountMessage"],
            "encouragement":          feedback["studentFeedback"]["encouragement"],
            # NEW —
            "aiLikelihoodPercent":    result["aiLikelihoodPercent"],
            "humanLikelihoodPercent": result["humanLikelihoodPercent"],
            "aiDetectionReason":      result["aiDetectionReason"],
            "aiVerdict":              result["aiVerdict"],
            "isGarbage":              result["isGarbage"],
            "garbageWarning":         result["garbageWarning"],
        },
        "mentorReport":     feedback["mentorSummary"],
        "processingTimeMs": total_time,
    }


# ── POST /api/review/test ──────────────────────────────────────────────────
@router.post("/test")
async def test_review(req: TestReviewRequest):
    cleaned    = clean_text(req.studentAnswer)
    word_count = count_words(cleaned)

    ai_analysis = ai_service.analyze_answer(
        case_study=req.caseStudy,
        model_answer=req.modelAnswer,
        student_answer=cleaned,
        grading_rubric=req.gradingRubric,
        key_concepts=req.keyConcepts,
    )
    scores   = scoring_service.calculate_scores(
        ai_analysis, req.gradingRubric, word_count, req.wordLimitMin, req.wordLimitMax
    )
    feedback = feedback_service.generate_feedback(
        scores, ai_analysis, word_count, req.wordLimitMin, req.wordLimitMax
    )
    return {
        "success": True,
        "result": {
            "score":         scores["totalScore"],
            "grade":         scores["grade"],
            "rubricScores":  scores["rubricBreakdown"],
            "feedback":      feedback["studentFeedback"],
            "mentorSummary": feedback["mentorSummary"],
            "aiMeta":        ai_analysis.get("_meta"),
        },
    }


# ── GET /api/review/student-progress/{student_id} ─────────────────────────
@router.get("/student-progress/{student_id}")
async def student_progress(student_id: int):
    progress = db_service.get_student_progress(student_id)
    return {"success": True, "student": progress}


# ── GET /api/review/mentor-dashboard/{case_study_id} ──────────────────────
@router.get("/mentor-dashboard/{case_study_id}")
async def mentor_dashboard(case_study_id: int):
    dashboard = db_service.get_mentor_dashboard(case_study_id)
    return {"success": True, "dashboard": dashboard}


# ── POST /api/review/mentor-approve/{submission_id} ───────────────────────
@router.post("/mentor-approve/{submission_id}")
async def mentor_approve(submission_id: int, req: MentorApproveRequest):
    db_service.mentor_approve_submission(
        submission_id, req.mentorId, req.mentorScore, req.mentorFeedback
    )
    return {"success": True, "message": "Submission reviewed by mentor."}


# ── GET /api/review/case-studies/{course_id} ──────────────────────────────
@router.get("/case-studies/{course_id}")
async def list_case_studies(course_id: int):
    case_studies = db_service.get_all_case_studies(course_id)
    return {"success": True, "caseStudies": case_studies}


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _pre_garbage_check(text: str, word_count: int) -> str:
    """Cheap heuristics that catch obvious junk before paying for an AI call.
    Returns a short reason string if junk, or '' if the text deserves a real review."""
    if not text or not text.strip():
        return "Submission is empty."
    if word_count < 10:
        return f"Submission is only {word_count} words — too short to evaluate meaningfully."

    stripped = text.strip()
    unique_chars = len(set(stripped.lower().replace(" ", "")))
    if unique_chars <= 3:
        return "Submission consists of repeated characters."

    letters = [c for c in stripped.lower() if c.isalpha()]
    if letters:
        vowel_ratio = sum(1 for c in letters if c in "aeiou") / len(letters)
        if vowel_ratio < 0.10 and len(letters) > 20:
            return "Submission appears to be keyboard mashing rather than real writing."

    return ""


def _build_garbage_response(submission, case_study, text, word_count, reason, start_time):
    """Same response shape as a normal review but score=0 and a clear garbageWarning."""
    rubric_breakdown = [
        {"criteria": c["name"], "maxScore": c["maxScore"], "score": 0,
         "percentage": 0, "status": "needs_improvement"}
        for c in case_study["gradingRubric"].get("criteria", [])
    ]

    warning = (
        "Your submission did not appear to be a genuine attempt at the case study. "
        f"Reason: {reason} Please re-read the case study and submit a thoughtful response."
    )

    student_feedback = {
        "score": 0,
        "grade": "F",
        "scoreEmoji": "🤝",
        "summary": "Your submission was flagged as not a genuine attempt — score 0/100.",
        "rubricScores":           rubric_breakdown,
        "strengths":              [],
        "improvements":           ["Read the case study carefully and write your own thoughts.",
                                   "Aim for at least the suggested word count.",
                                   "Reach out to your mentor if you'd like guidance on how to start."],
        "missingConcepts":        case_study.get("keyConcepts", []),
        "coveredConcepts":        [],
        "suggestions":            [],
        "detailedFeedback":       warning,
        "wordCount":              word_count,
        "wordCountMessage":       f"Your answer is {word_count} words — too short to evaluate.",
        "encouragement":          "Take another look at the case study and try again — you can do this. 🤝",
        "aiLikelihoodPercent":    0,
        "humanLikelihoodPercent": 100,
        "aiDetectionReason":      "Not analysed (submission flagged as non-genuine).",
        "aiVerdict":              "uncertain",
        "isGarbage":              True,
        "garbageWarning":         warning,
    }

    mentor_report = {
        "score":             0,
        "grade":             "F",
        "gradeLabel":        "Needs Significant Improvement",
        "scoreEmoji":        "🤝",
        "needsAttention":    True,
        "performanceLevel":  "developing",
        "plagiarismRisk":    "low",
        "plagiarismNote":    "",
        "quickAction":       "Review required: submission appears to be non-genuine — please verify.",
        "keyMissing":        case_study.get("keyConcepts", []),
        "rubricBreakdown":   rubric_breakdown,
        "mentorAlert":       True,
        "mentorAlertReason": f"Non-genuine submission flagged: {reason}",
        "wordCount":         word_count,
        "isGarbage":         True,
        "aiLikelihoodPercent":    0,
        "humanLikelihoodPercent": 100,
    }

    internal = {
        "totalScore":             0,
        "grade":                  "F",
        "rubricScores":           rubric_breakdown,
        "strengths":              [],
        "improvements":           student_feedback["improvements"],
        "missingConcepts":        case_study.get("keyConcepts", []),
        "coveredConcepts":        [],
        "suggestedModules":       [],
        "detailedFeedback":       warning,
        "wordCount":              word_count,
        "wordCountMessage":       student_feedback["wordCountMessage"],
        "plagiarismFlag":         "low",
        "needsMentorHelp":        True,
        "scoreEmoji":             "🤝",
        "aiLikelihoodPercent":    0,
        "humanLikelihoodPercent": 100,
        "aiDetectionReason":      student_feedback["aiDetectionReason"],
        "aiVerdict":              "uncertain",
        "isGarbage":              True,
        "garbageWarning":         warning,
    }

    return {
        "_internal": internal,
        "response": {
            "success":    True,
            "submission": submission,
            "feedback":   student_feedback,
            "mentorReport": mentor_report,
            "processingTimeMs": int((time.time() - start_time) * 1000),
        },
    }