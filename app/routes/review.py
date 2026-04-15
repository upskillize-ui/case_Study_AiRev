# app/routes/review.py
import time
import os
from fastapi import APIRouter, HTTPException
from app.models.schemas import SubmitAnswerRequest, TestReviewRequest, MentorApproveRequest
from app.services import ai_service, scoring_service, feedback_service, db_service
from app.utils.text_processor import (
    count_words, clean_text, calculate_text_overlap, find_mentioned_concepts
)

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

    text_overlap  = calculate_text_overlap(cleaned, case_study["description"])
    concept_check = find_mentioned_concepts(cleaned, case_study["keyConcepts"])

    submission = db_service.save_submission(
        req.caseStudyId, req.studentId, cleaned, word_count
    )
    print(f"✅ Submission saved: id={submission['submissionId']}, attempt={submission['attemptNumber']}")

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
        "totalScore":       scores["totalScore"],
        "grade":            scores["grade"],
        "rubricScores":     scores["rubricBreakdown"],
        "strengths":        feedback["strengths"],
        "improvements":     feedback["improvements"],
        "missingConcepts":  ai_analysis.get("conceptsMissing", []),
        "coveredConcepts":  ai_analysis.get("conceptsCovered", []),
        "suggestedModules": feedback["suggestedModules"],
        "detailedFeedback": feedback["detailed"],
        "wordCount":        word_count,
        "wordCountMessage": feedback["wordCountMessage"],
        "plagiarismFlag":   ai_analysis.get("plagiarismRisk", "low"),
        "needsMentorHelp":  scores["totalScore"] < 40 or ai_analysis.get("mentorAlert", False),
    }

    try:
        db_service.update_submission_with_ai_results(submission["submissionId"], result)
        db_service.update_performance_tracker(req.studentId, req.caseStudyId, scores["totalScore"])
        db_service.log_ai_review(submission["submissionId"], ai_analysis.get("_meta"), ai_analysis)
    except Exception as db_err:
        print(f"⚠️  DB update failed after AI review: {db_err} — returning result anyway")

    total_time = int((time.time() - start_time) * 1000)
    print(f"✅ Review complete: score={scores['totalScore']}, grade={scores['grade']}, time={total_time}ms")

    return {
        "success":    True,
        "submission": submission,
        "feedback": {
            "score":            result["totalScore"],
            "grade":            result["grade"],
            "summary":          feedback["studentFeedback"]["summary"],
            "rubricScores":     result["rubricScores"],
            "strengths":        result["strengths"],
            "improvements":     result["improvements"],
            "missingConcepts":  result["missingConcepts"],
            "coveredConcepts":  result["coveredConcepts"],
            "suggestions":      result["suggestedModules"],
            "detailedFeedback": result["detailedFeedback"],
            "wordCount":        result["wordCount"],
            "wordCountMessage": result["wordCountMessage"],
            "encouragement":    feedback["studentFeedback"]["encouragement"],
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


# ── GET /api/review/debug/case-study/{cid} ────────────────────────────────
# TEMPORARY DEBUG ENDPOINT — remove after debugging.
@router.get("/debug/case-study/{cid}")
async def debug_case_study(cid: int):
    from app.database import query
    rows = query("SELECT id, title, status FROM case_studies WHERE id = %s", (cid,))
    all_rows = query("SELECT id, title, status FROM case_studies LIMIT 10")
    return {"found_with_id": rows, "all_case_studies": all_rows}


# ── GET /api/review/debug/publish/{cid} ───────────────────────────────────
# TEMPORARY: sets a case study's status to 'published' so it becomes
# usable by the /submit endpoint. Remove after debugging.
@router.get("/debug/publish/{cid}")
async def debug_publish_case_study(cid: int):
    from app.database import query, execute
    execute("UPDATE case_studies SET status = 'published' WHERE id = %s", (cid,))
    rows = query("SELECT id, title, status FROM case_studies WHERE id = %s", (cid,))
    return {"updated": rows}


# ── GET /api/review/debug/columns ─────────────────────────────────────────
# TEMPORARY: shows the actual columns of the case_studies table and a
# full row dump so we can see real column names. Remove after debugging.
@router.get("/debug/columns")
async def debug_columns():
    from app.database import query
    cols = query("SHOW COLUMNS FROM case_studies")
    sample = query("SELECT * FROM case_studies WHERE id = 1")
    return {"columns": cols, "sample_row": sample}