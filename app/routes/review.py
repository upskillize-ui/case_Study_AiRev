# app/routes/review.py
# CHANGED:
#   - submit_and_review now passes req.fileUrl + req.fileName into
#     save_submission so case-study file uploads are persisted (bug #26 fix).
#   - New endpoint: GET /api/review/case-study-history/{student_id}/{case_study_id}
#     Returns all of a student's attempts on a case study, newest first.
#     Frontend uses this to show prior review on reopen with a Re-analyze button.
#   - Demo endpoint /case-studies status filter: 'published' OR 'active'.

import time
import os
from fastapi import APIRouter, HTTPException
from app.models.schemas import SubmitAnswerRequest, TestReviewRequest, MentorApproveRequest
from app.services import ai_service, scoring_service, feedback_service, db_service
from app.utils.text_processor import (
    count_words, clean_text, calculate_text_overlap, find_mentioned_concepts, is_likely_copy
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

    cleaned    = clean_text(req.answerText or "")
    word_count = count_words(cleaned)

    # ── Build best answer text. Priority order, accumulated:
    #    1. Student's frontend answerText (always taken if non-empty)
    #    2. Extracted text from THIS submission's uploaded file (req.fileUrl)
    #    3. Extracted text from any prior submission's file (DB)
    #    4. Notes from prior submission (DB)

    file_used = None
    parts: list[str] = []

    if cleaned:
        parts.append(cleaned)

    # NEW: extract text from the file URL the frontend just uploaded
    if req.fileUrl:
        extracted, why = extract_text_from_url(req.fileUrl, req.fileName or "")
        if extracted:
            file_used = req.fileName or req.fileUrl
            print(f"📄 Extracted file (this submission): {file_used} "
                  f"({count_words(extracted)} words)")
            if not cleaned or count_words(extracted) > word_count * 1.2:
                parts.append(extracted)
        elif why:
            print(f"📄 File extraction failed for {req.fileName or '?'}: {why}")

    # Fall back to a prior submission only if we still have nothing useful
    if not parts:
        prior = db_service.get_latest_submission_file(req.caseStudyId, req.studentId)
        if prior:
            if prior.get("file_url"):
                extracted, why = extract_text_from_url(prior["file_url"], prior.get("file_name", ""))
                if extracted:
                    file_used = prior.get("file_name") or prior["file_url"]
                    print(f"📄 Extracted file (prior submission): {file_used}")
                    parts.append(extracted)
                elif why:
                    print(f"📄 Prior file extraction failed: {why}")
            db_notes = clean_text(prior.get("notes") or "")
            if db_notes:
                parts.append(db_notes)

    # ── Still nothing → friendly "no submission yet" ──
    if not parts:
        total_time = int((time.time() - start_time) * 1000)
        msg = ("We couldn't find any answer text. Please write your analysis in the "
               "answer box (or upload a PDF), then click Submit again.")
        return {
            "success": True,
            "submission": {"submissionId": 0, "attemptNumber": 0},
            "feedback": {
                "score": 0, "grade": "-", "scoreEmoji": "📝",
                "summary": msg,
                "rubricScores": [], "strengths": [], "improvements": [
                    "Write your analysis directly in the answer box.",
                    "Upload a PDF with your detailed answer if you have one.",
                    "Aim for at least the suggested word count.",
                ],
                "missingConcepts": [], "coveredConcepts": [], "suggestions": [],
                "detailedFeedback": msg,
                "wordCount": 0, "wordCountMessage": "",
                "encouragement": "Type your answer and click Submit — you've got this! 📝",
                "aiLikelihoodPercent": None, "humanLikelihoodPercent": None,
                "aiDetectionReason": "Not analysed.", "aiVerdict": "uncertain",
                "isGarbage": False, "garbageWarning": "",
            },
            "mentorReport": {},
            "processingTimeMs": total_time,
        }

    # Combine and recount
    cleaned = "\n\n".join(parts).strip()
    word_count = count_words(cleaned)
    print(f"📝 Final answer: {word_count} words "
          f"(frontend={'yes' if req.answerText else 'no'}, "
          f"file={'yes' if file_used else 'no'})")

    # Plagiarism warning (informational)
    text_overlap  = calculate_text_overlap(cleaned, case_study["description"])
    concept_check = find_mentioned_concepts(cleaned, case_study["keyConcepts"])

    # NEW: persist file_url + file_name. INSERTs a new row every attempt.
    submission = db_service.save_submission(
        req.caseStudyId, req.studentId, cleaned, word_count,
        file_url=req.fileUrl, file_name=req.fileName,
    )
    print(f"✅ Submission saved: id={submission['submissionId']}, "
          f"attempt={submission['attemptNumber']}")

    # ── Pre-AI cheap garbage check ──
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

    if text_overlap >= 35:
        ai_analysis["plagiarismRisk"] = "high" if text_overlap >= 60 else "medium"
        ai_analysis["plagiarismNote"] = (
            f"Some sections closely mirror the case-study text "
            f"({text_overlap}% phrase overlap). Try rephrasing in your own words."
        )
        if text_overlap >= 60:
            ai_analysis["mentorAlert"] = True
            ai_analysis["mentorAlertReason"] = "High verbatim overlap — mentor review recommended."

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
        "scoreEmoji":             feedback["scoreEmoji"],
        "aiLikelihoodPercent":    feedback["aiLikelihoodPercent"],
        "humanLikelihoodPercent": feedback["humanLikelihoodPercent"],
        "aiDetectionReason":      feedback["aiDetectionReason"],
        "aiVerdict":              feedback["aiVerdict"],
        "isGarbage":              feedback["isGarbage"],
        "garbageWarning":         feedback["garbageWarning"],
        "summary":                feedback["studentFeedback"]["summary"],
        "encouragement":          feedback["studentFeedback"]["encouragement"],
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

    pre_garbage_reason = _pre_garbage_check(cleaned, word_count)
    if pre_garbage_reason:
        return {
            "success": True,
            "result": {
                "score": 0, "grade": "F",
                "rubricScores": [],
                "feedback": {
                    "summary": f"Submission flagged: {pre_garbage_reason}",
                    "encouragement": "Take another look at the case study and try again.",
                    "isGarbage": True,
                    "garbageWarning": pre_garbage_reason,
                    "aiLikelihoodPercent": 0,
                    "humanLikelihoodPercent": 100,
                },
                "aiMeta": None,
            },
        }

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


# ── GET /api/review/case-study-history/{student_id}/{case_study_id} ───────
# NEW: powers "show previous review on reopen" + Re-analyze flow.
@router.get("/case-study-history/{student_id}/{case_study_id}")
async def case_study_history(student_id: int, case_study_id: int):
    history = db_service.get_submission_history(case_study_id, student_id)
    return {"success": True, "history": history}


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
        if vowel_ratio < 0.08 and len(letters) > 30:
            return "Submission appears to be keyboard mashing rather than real writing."

    return ""


def _build_garbage_response(submission, case_study, text, word_count, reason, start_time):
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
        "summary":                student_feedback["summary"],
        "encouragement":          student_feedback["encouragement"],
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


# ═════════════════════════════════════════════════════════════════════════
# DEMO / TESTING ENDPOINTS
# ═════════════════════════════════════════════════════════════════════════

@router.get("/case-studies", tags=["demo"])
async def list_all_published_case_studies():
    from app.database import query
    rows = query(
        "SELECT id, course_id, title, description, company_name, industry, "
        "difficulty, total_marks, due_date, word_limit "
        "FROM case_studies WHERE status IN ('published', 'active') "
        "ORDER BY created_at DESC"
    )
    return {"success": True, "caseStudies": rows}