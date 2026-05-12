# app/routes/review.py
# CHANGED in this push:
#   - NEW endpoint: GET /api/review/case-studies-for-student/{student_id}
#     Returns ALL published/active case studies, each with the student's
#     latest submission attached (or null if not submitted). Mirrors the
#     existing /assignments/{student_id} endpoint. Powers the AiRev hub's
#     "New Review" / "Re-analyze" / "History" tabs.
#
# CHANGED in previous push (still here):
#   - submit_and_review passes req.fileUrl + req.fileName into save_submission
#   - GET /api/review/case-study-history/{student_id}/{case_study_id}
#   - Demo /case-studies endpoint status filter is 'published' OR 'active'

import time
import json
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

    file_used = None
    parts: list[str] = []

    if cleaned:
        parts.append(cleaned)

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

    cleaned = "\n\n".join(parts).strip()
    word_count = count_words(cleaned)
    print(f"📝 Final answer: {word_count} words "
          f"(frontend={'yes' if req.answerText else 'no'}, "
          f"file={'yes' if file_used else 'no'})")

    text_overlap  = calculate_text_overlap(cleaned, case_study["description"])
    concept_check = find_mentioned_concepts(cleaned, case_study["keyConcepts"])

    submission = db_service.save_submission(
        req.caseStudyId, req.studentId, cleaned, word_count,
        file_url=req.fileUrl, file_name=req.fileName,
    )
    print(f"✅ Submission saved: id={submission['submissionId']}, "
          f"attempt={submission['attemptNumber']}")

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
        "_meta":            ai_analysis.get("_meta"),
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
@router.get("/case-study-history/{student_id}/{case_study_id}")
async def case_study_history(student_id: int, case_study_id: int):
    history = db_service.get_submission_history(case_study_id, student_id)
    return {"success": True, "history": history}


# ══════════════════════════════════════════════════════════════════════════
# NEW for the AiRev hub: list all case studies with this student's latest
# submission attached. Mirrors the assignments-list endpoint.
# ══════════════════════════════════════════════════════════════════════════

@router.get("/case-studies-for-student/{student_id}")
async def case_studies_for_student(student_id: int):
    from app.database import query
    rows = query(
        """SELECT cs.id, cs.course_id, cs.title, cs.description, cs.due_date,
                  cs.total_marks, cs.status, cs.word_limit, cs.created_at,
                  latest.id            AS submission_id,
                  latest.grade         AS submission_grade,
                  latest.status        AS submission_status,
                  latest.submitted_at  AS submitted_at,
                  latest.feedback      AS submission_feedback,
                  latest.file_name     AS submitted_file_name
           FROM case_studies cs
           LEFT JOIN (
               SELECT s1.*
               FROM case_study_submissions s1
               INNER JOIN (
                   SELECT case_study_id, student_id, MAX(submitted_at) AS max_at
                   FROM case_study_submissions
                   WHERE student_id = %s
                   GROUP BY case_study_id, student_id
               ) s2
                 ON s1.case_study_id = s2.case_study_id
                AND s1.student_id    = s2.student_id
                AND s1.submitted_at  = s2.max_at
           ) latest
             ON latest.case_study_id = cs.id
           WHERE cs.status IN ('published', 'active')
           ORDER BY cs.created_at DESC""",
        (student_id,),
    )

    out = []
    for r in rows:
        feedback = None
        if r.get("submission_feedback"):
            try:
                fb = r["submission_feedback"]
                feedback = json.loads(fb) if isinstance(fb, str) else fb
            except Exception:
                feedback = None
        out.append({
            "id":              r["id"],
            "title":           r.get("title"),
            "description":     r.get("description"),
            "courseId":        r.get("course_id"),
            "dueDate":         str(r["due_date"]) if r.get("due_date") else None,
            "totalMarks":      r.get("total_marks", 100),
            "status":          r.get("status"),
            "submissionId":    r.get("submission_id"),
            "submissionStatus": r.get("submission_status"),
            "submittedAt":     str(r["submitted_at"]) if r.get("submitted_at") else None,
            "grade":           r.get("submission_grade"),
            "submittedFile":   r.get("submitted_file_name"),
            "hasFeedback":     bool(feedback),
            "feedbackSummary": (feedback or {}).get("summary"),
        })
    return {"success": True, "caseStudies": out}


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
        "score": 0, "grade": "F", "scoreEmoji": "🤝",
        "summary": "Your submission was flagged as not a genuine attempt — score 0/100.",
        "rubricScores":   rubric_breakdown,
        "strengths":      [],
        "improvements":   ["Read the case study carefully and write your own thoughts.",
                           "Aim for at least the suggested word count.",
                           "Reach out to your mentor if you'd like guidance on how to start."],
        "missingConcepts": case_study.get("keyConcepts", []),
        "coveredConcepts": [], "suggestions": [],
        "detailedFeedback": warning,
        "wordCount": word_count,
        "wordCountMessage": f"Your answer is {word_count} words — too short to evaluate.",
        "encouragement": "Take another look at the case study and try again — you can do this. 🤝",
        "aiLikelihoodPercent": 0, "humanLikelihoodPercent": 100,
        "aiDetectionReason": "Not analysed (submission flagged as non-genuine).",
        "aiVerdict": "uncertain",
        "isGarbage": True, "garbageWarning": warning,
    }

    mentor_report = {
        "score": 0, "grade": "F",
        "gradeLabel": "Needs Significant Improvement",
        "scoreEmoji": "🤝",
        "needsAttention": True, "performanceLevel": "developing",
        "plagiarismRisk": "low", "plagiarismNote": "",
        "quickAction": "Review required: submission appears to be non-genuine — please verify.",
        "keyMissing": case_study.get("keyConcepts", []),
        "rubricBreakdown": rubric_breakdown,
        "mentorAlert": True, "mentorAlertReason": f"Non-genuine submission flagged: {reason}",
        "wordCount": word_count, "isGarbage": True,
        "aiLikelihoodPercent": 0, "humanLikelihoodPercent": 100,
    }

    internal = {
        "totalScore": 0, "grade": "F",
        "rubricScores": rubric_breakdown,
        "strengths": [], "improvements": student_feedback["improvements"],
        "missingConcepts": case_study.get("keyConcepts", []),
        "coveredConcepts": [], "suggestedModules": [],
        "detailedFeedback": warning,
        "wordCount": word_count,
        "wordCountMessage": student_feedback["wordCountMessage"],
        "plagiarismFlag": "low", "needsMentorHelp": True,
        "scoreEmoji": "🤝",
        "aiLikelihoodPercent": 0, "humanLikelihoodPercent": 100,
        "aiDetectionReason": student_feedback["aiDetectionReason"],
        "aiVerdict": "uncertain",
        "isGarbage": True, "garbageWarning": warning,
        "summary": student_feedback["summary"],
        "encouragement": student_feedback["encouragement"],
    }

    return {
        "_internal": internal,
        "response": {
            "success": True, "submission": submission,
            "feedback": student_feedback, "mentorReport": mentor_report,
            "processingTimeMs": int((time.time() - start_time) * 1000),
        },
    }

# ── GET /api/review/capstones-for-student/{student_id} ────────────────────
@router.get("/capstones-for-student/{student_id}")
async def capstones_for_student(student_id: int):
    from app.database import query
    # capstones table may use users.id instead of students.id
    # Try both: direct match + lookup via students table
    rows = query(
        """SELECT id, title, description, course_id, student_id, due_date,
                  total_marks, status, file_url, grade, feedback, submitted_at
           FROM capstones
           WHERE student_id = %s
              OR student_id = (SELECT user_id FROM students WHERE id = %s LIMIT 1)
           ORDER BY created_at DESC""",
        (student_id, student_id),
    )
    out = []
    for r in rows:
        feedback = None
        if r.get("feedback"):
            try:
                fb = r["feedback"]
                feedback = json.loads(fb) if isinstance(fb, str) else fb
            except Exception:
                feedback = None
        out.append({
            "id":              r["id"],
            "title":           r.get("title"),
            "description":     r.get("description"),
            "courseId":        r.get("course_id"),
            "dueDate":         str(r["due_date"]) if r.get("due_date") else None,
            "totalMarks":      r.get("total_marks", 100),
            "status":          r.get("status"),
            "submissionId":    r["id"] if r.get("status") in ("submitted", "graded") else None,
            "submittedAt":     str(r["submitted_at"]) if r.get("submitted_at") else None,
            "grade":           r.get("grade"),
            "fileUrl":         r.get("file_url"),
            "hasFeedback":     bool(feedback),
            "feedbackSummary": (feedback or {}).get("summary"),
        })
    return {"success": True, "capstones": out}


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

# ── POST /api/review/submit-capstone ──────────────────────────────────────
# Capstones use `users.id` as student_id (not `students.id`), so we accept
# either and resolve via the students table. Mirrors submit-assignment.
@router.post("/submit-capstone")
async def submit_capstone_review(req: dict):
    """
    Body: { capstoneId, studentId, answerText, fileUrl, fileName }
    studentId may be users.id OR students.id — we resolve both.
    """
    import time as _time
    from app.database import query, execute

    start_time = _time.time()
    capstone_id = req.get("capstoneId")
    student_id  = req.get("studentId")
    answer_text = (req.get("answerText") or "").strip()
    file_url    = req.get("fileUrl")
    file_name   = req.get("fileName")

    if not capstone_id or not student_id:
        raise HTTPException(status_code=400, detail="capstoneId and studentId are required")

    print(f"ℹ️  Capstone review: student={student_id}, capstone={capstone_id}")

    # Resolve capstone — dual lookup like the list endpoint
    rows = query(
        """SELECT id, title, description, total_marks, status, file_url,
                  student_id, course_id, due_date
           FROM capstones
           WHERE id = %s
             AND (student_id = %s
               OR student_id = (SELECT user_id FROM students WHERE id = %s LIMIT 1))
           LIMIT 1""",
        (capstone_id, student_id, student_id),
    )
    if not rows:
        # Helpful debug — list a few capstone IDs in the tenant
        sample = query("SELECT id, title, status FROM capstones LIMIT 10")
        print(f"[CAPSTONE] capstone {capstone_id} not found for student {student_id}. "
              f"Sample capstones in tenant DB: {sample}")
        raise HTTPException(status_code=404, detail="Capstone not found or not yours")

    capstone = rows[0]
    print(f"[CAPSTONE] resolved: id={capstone['id']}, title={capstone.get('title')}, "
          f"status={capstone.get('status')}")

    # Build the answer text from request OR existing capstone submission
    parts = []
    if answer_text:
        parts.append(answer_text)
    if file_url:
        from app.utils.file_extractor import extract_text_from_url
        extracted, why = extract_text_from_url(file_url, file_name or "")
        if extracted:
            print(f"📄 Extracted capstone file: {file_name} ({len(extracted.split())} words)")
            parts.append(extracted)
        elif why:
            print(f"📄 Capstone file extraction failed: {why}")

    # Fallback: read previously-saved capstone file
    if not parts and capstone.get("file_url"):
        from app.utils.file_extractor import extract_text_from_url
        extracted, why = extract_text_from_url(capstone["file_url"], "")
        if extracted:
            print(f"📄 Extracted prior capstone file: {capstone['file_url']}")
            parts.append(extracted)

    if not parts:
        total_time = int((_time.time() - start_time) * 1000)
        msg = ("We couldn't find any content for this capstone. Upload your project "
               "deliverable (PDF / DOCX / ZIP) or paste your write-up, then click Submit again.")
        return {
            "success": True,
            "submission": {"submissionId": capstone["id"], "attemptNumber": 1},
            "feedback": {
                "score": 0, "grade": "-",
                "summary": msg,
                "rubricScores": [], "strengths": [],
                "improvements": [
                    "Upload your capstone deliverable as a PDF, DOCX, or ZIP.",
                    "Or paste your write-up directly into the AiRev answer box.",
                ],
                "missingConcepts": [], "coveredConcepts": [], "suggestions": [],
                "detailedFeedback": msg,
                "wordCount": 0, "wordCountMessage": "",
                "encouragement": "Drop your deliverable in and re-run — you've got this.",
                "aiLikelihoodPercent": None, "humanLikelihoodPercent": None,
                "aiDetectionReason": "Not analysed.", "aiVerdict": "uncertain",
                "isGarbage": False, "garbageWarning": "",
            },
            "mentorReport": {},
            "processingTimeMs": total_time,
        }

    from app.utils.text_processor import clean_text, count_words
    cleaned = clean_text("\n\n".join(parts))
    word_count = count_words(cleaned)
    print(f"📝 Capstone answer: {word_count} words")

    # Capstones have lighter rubrics by default — synthesize one if the table
    # doesn't carry one, so the AI still has dimensions to score against.
    default_rubric = {
        "criteria": [
            {"name": "Problem Framing",        "maxScore": 20},
            {"name": "Methodology",            "maxScore": 25},
            {"name": "Execution Quality",      "maxScore": 25},
            {"name": "Insight & Recommendation","maxScore": 20},
            {"name": "Communication",          "maxScore": 10},
        ]
    }

    try:
        ai_analysis = ai_service.analyze_answer(
            case_study={
                "title":       capstone.get("title") or "Capstone Project",
                "description": capstone.get("description") or "",
                "questions":   [],
            },
            model_answer="",  # no canonical answer for capstones
            student_answer=cleaned,
            grading_rubric=default_rubric,
            key_concepts=[],
        )
    except Exception as e:
        print(f"⚠️  Capstone AI review unavailable: {e}")
        return {
            "success":       True,
            "partialReview": True,
            "message": "Your capstone is saved. The AI reviewer is briefly unavailable — a mentor has been notified.",
            "submission": {"submissionId": capstone["id"], "attemptNumber": 1},
        }

    scores = scoring_service.calculate_scores(
        ai_analysis, default_rubric, word_count, 0, 999999,
    )
    feedback = feedback_service.generate_feedback(
        scores, ai_analysis, word_count, 0, 999999,
    )

    total_time = int((_time.time() - start_time) * 1000)
    print(f"✅ Capstone review complete: score={scores['totalScore']}, "
          f"grade={scores['grade']}, time={total_time}ms")

    # Persist back to capstones table (grade + feedback JSON)
    try:
        execute(
            "UPDATE capstones SET grade = %s, feedback = %s, status = 'graded' WHERE id = %s",
            (scores["totalScore"], json.dumps({
                "summary":          feedback["studentFeedback"]["summary"],
                "rubricScores":     scores["rubricBreakdown"],
                "strengths":        feedback["strengths"],
                "improvements":     feedback["improvements"],
                "detailedFeedback": feedback["detailed"],
            }), capstone["id"]),
        )
    except Exception as db_err:
        print(f"⚠️  Capstone DB update failed: {db_err}")

    return {
        "success":    True,
        "submission": {"submissionId": capstone["id"], "attemptNumber": 1},
        "feedback": {
            "score":                  scores["totalScore"],
            "grade":                  scores["grade"],
            "summary":                feedback["studentFeedback"]["summary"],
            "rubricScores":           scores["rubricBreakdown"],
            "strengths":              feedback["strengths"],
            "improvements":           feedback["improvements"],
            "missingConcepts":        ai_analysis.get("conceptsMissing", []),
            "coveredConcepts":        ai_analysis.get("conceptsCovered", []),
            "suggestions":            feedback["suggestedModules"],
            "detailedFeedback":       feedback["detailed"],
            "wordCount":              word_count,
            "wordCountMessage":       feedback["wordCountMessage"],
            "encouragement":          feedback["studentFeedback"]["encouragement"],
            "aiLikelihoodPercent":    feedback["aiLikelihoodPercent"],
            "humanLikelihoodPercent": feedback["humanLikelihoodPercent"],
            "aiDetectionReason":      feedback["aiDetectionReason"],
            "aiVerdict":              feedback["aiVerdict"],
            "isGarbage":              feedback["isGarbage"],
            "garbageWarning":         feedback["garbageWarning"],
        },
        "_meta":            ai_analysis.get("_meta"),
        "mentorReport":     feedback["mentorSummary"],
        "processingTimeMs": total_time,
    }