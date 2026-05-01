# app/routes/assignment_review.py
# ---------------------------------------------------------------------------
# Multi-tenant assignment review endpoints.
#
# Mirrors app/routes/review.py but for the `assignments` + `assignment_submissions`
# tables. Same AI pipeline (file extraction → AI analysis → scoring → feedback),
# different DB tables and submission shape.
#
# Routes:
#   GET  /api/review/assignments/{student_id}          List with statuses
#   POST /api/review/submit-assignment                 Submit + review
#   GET  /api/review/assignment-submission/{id}        Re-fetch a past review
# ---------------------------------------------------------------------------

import time
import json
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import (
    ai_service,
    scoring_service,
    feedback_service,
    assignment_db_service,
)
from app.utils.text_processor import (
    count_words, clean_text, find_mentioned_concepts
)
from app.utils.file_extractor import extract_text_from_url

router = APIRouter(prefix="/api/review", tags=["assignment-review"])


# ---------- Request schema (assignment-specific) ---------------------------

class SubmitAssignmentRequest(BaseModel):
    assignmentId: int
    studentId: int
    answerText: Optional[str] = ""        # optional — file is primary
    fileUrl: Optional[str] = None          # Cloudinary URL
    fileName: Optional[str] = None


# ---------- GET /api/review/assignments/{student_id} -----------------------

@router.get("/assignments/{student_id}")
async def list_student_assignments(student_id: int):
    """Return all assignments visible to this student in the current tenant."""
    rows = assignment_db_service.get_student_assignments(student_id)

    out = []
    for r in rows:
        feedback_blob = None
        if r.get("submission_feedback"):
            try:
                fb = r["submission_feedback"]
                feedback_blob = json.loads(fb) if isinstance(fb, str) else fb
            except Exception:
                feedback_blob = None

        out.append({
            "id":             r["id"],
            "title":          r["title"],
            "description":    r.get("description"),
            "dueDate":        str(r["due_date"]) if r.get("due_date") else None,
            "totalMarks":     r.get("total_marks", 100),
            "status":         r.get("status"),
            "submissionId":   r.get("submission_id"),
            "submissionStatus": r.get("submission_status"),
            "submittedAt":    str(r["submitted_at"]) if r.get("submitted_at") else None,
            "grade":          r.get("submission_grade"),
            "submittedFile":  r.get("submitted_file_name"),
            "hasFeedback":    bool(feedback_blob),
            "reviewedBy":     (feedback_blob or {}).get("reviewedBy"),
        })

    return {"success": True, "assignments": out}


# ---------- POST /api/review/submit-assignment -----------------------------

@router.post("/submit-assignment")
async def submit_and_review_assignment(req: SubmitAssignmentRequest):
    start_time = time.time()
    print(f"ℹ️  Assignment submission: student={req.studentId}, assignment={req.assignmentId}")

    assignment = assignment_db_service.get_assignment_by_id(req.assignmentId)
    if not assignment:
        raise HTTPException(status_code=404, detail="Assignment not found or inactive")

    # ── Build the answer text. For assignments, file is PRIMARY (most students
    #    upload PDFs of their work) and typed notes are secondary.
    cleaned_typed = clean_text(req.answerText or "")
    parts: list[str] = []
    file_used = None

    # 1. If a file URL was sent in this request, extract it now
    if req.fileUrl:
        extracted, why = extract_text_from_url(req.fileUrl, req.fileName or "")
        if extracted:
            file_used = req.fileName or req.fileUrl
            parts.append(extracted)
            print(f"📄 Extracted uploaded file: {file_used} ({count_words(extracted)} words)")
        elif why:
            print(f"📄 Upload extraction failed: {why}")

    # 2. Frontend typed answer (notes)
    if cleaned_typed:
        parts.append(cleaned_typed)

    # 3. If neither, look for the student's prior submission (re-review case)
    if not parts:
        prior = assignment_db_service.get_latest_assignment_submission(
            req.assignmentId, req.studentId
        )
        if prior:
            if prior.get("file_url"):
                extracted, why = extract_text_from_url(
                    prior["file_url"], prior.get("file_name", "")
                )
                if extracted:
                    file_used = prior.get("file_name") or prior["file_url"]
                    parts.append(extracted)
                    print(f"📄 Used prior file: {file_used} ({count_words(extracted)} words)")
            db_notes = clean_text(prior.get("notes") or "")
            if db_notes:
                parts.append(db_notes)

    # ── Empty submission guard ──
    if not parts:
        total_time = int((time.time() - start_time) * 1000)
        msg = ("We couldn't find any answer for this assignment. Upload your work "
               "as a PDF or Word document, or write your answer in the notes box, "
               "then click Submit again.")
        return {
            "success": True,
            "submission": {"submissionId": 0, "attemptNumber": 0},
            "feedback": _empty_feedback(msg, helpful=True),
            "processingTimeMs": total_time,
        }

    # Combine all sources
    combined = "\n\n".join(parts).strip()
    word_count = count_words(combined)
    print(f"📝 Final assignment answer: {word_count} words "
          f"(file={'yes' if file_used else 'no'}, typed={'yes' if cleaned_typed else 'no'})")

    # Persist the submission first (so the AI review has a row to attach to)
    submission = assignment_db_service.save_assignment_submission(
        req.assignmentId,
        req.studentId,
        cleaned_typed or None,
        req.fileUrl,
        req.fileName,
    )
    print(f"✅ Assignment submission saved: id={submission['submissionId']}, "
          f"attempt={submission['attemptNumber']}")

    # ── Pre-AI garbage check (very lenient for assignments — they can be short) ──
    if word_count < 30:
        msg = (f"Your submission is very short ({word_count} words). "
               "Please add more detail and resubmit so we can give you useful feedback.")
        partial = {
            "totalScore": 0, "grade": "—",
            "rubricScores": [], "strengths": [],
            "improvements": [
                "Expand on your reasoning — show your working.",
                "Reference specific facts, formulas, or sources from the lecture.",
                "Aim for at least a few paragraphs of substance.",
            ],
            "missingConcepts": [], "coveredConcepts": [],
            "suggestedModules": [], "detailedFeedback": msg,
            "wordCount": word_count, "wordCountMessage": "very short",
            "encouragement": "Add more detail and resubmit — you've got this.",
            "isGarbage": True, "garbageWarning": msg,
            "aiLikelihoodPercent": None, "humanLikelihoodPercent": None,
            "aiDetectionReason": "Not analysed (too short).", "aiVerdict": "uncertain",
            "scoreEmoji": "📝",
            "plagiarismFlag": "low", "needsMentorHelp": True,
        }
        try:
            assignment_db_service.update_assignment_submission_with_ai_results(
                submission["submissionId"], partial
            )
        except Exception as e:
            print(f"⚠️  DB update (short-submission path) failed: {e}")
        return _build_response(submission, partial, msg, start_time)

    # ── Real AI review ──
    try:
        ai_analysis = ai_service.analyze_answer(
            case_study={
                "title":       assignment["title"],
                "description": assignment["description"],
                # Assignments have no `questions` column — pass empty list
                "questions":   assignment["questions"],
            },
            model_answer=assignment["modelAnswers"],   # always [] for assignments
            student_answer=combined,
            grading_rubric=assignment["gradingRubric"],
            key_concepts=assignment["keyConcepts"],    # always [] for assignments
        )
    except Exception as e:
        print(f"⚠️  AI review unavailable: {e}")
        return {
            "success":       True,
            "partialReview": True,
            "message": (
                "Your assignment has been saved successfully! "
                "Our AI reviewer is temporarily unavailable, but a faculty member "
                "has been notified and will review your submission personally."
            ),
            "submission": submission,
        }

    # Concept overlay (assignments don't ship with key_concepts but the AI can
    # still surface "covered/missing" topics from the rubric criteria themselves)
    rubric_topics = [c.get("name", "") for c in assignment["gradingRubric"]["criteria"]]
    if rubric_topics:
        concept_check = find_mentioned_concepts(combined, rubric_topics)
        ai_analysis["conceptsCovered"] = list(set(
            (ai_analysis.get("conceptsCovered") or []) + concept_check["mentioned"]
        ))
        ai_analysis["conceptsMissing"] = [
            c for c in concept_check["missing"]
            if c not in (ai_analysis.get("conceptsCovered") or [])
        ]

    scores = scoring_service.calculate_scores(
        ai_analysis, assignment["gradingRubric"], word_count,
        assignment["wordLimitMin"], assignment["wordLimitMax"],
    )
    feedback = feedback_service.generate_feedback(
        scores, ai_analysis, word_count,
        assignment["wordLimitMin"], assignment["wordLimitMax"],
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
        "scoreEmoji":             feedback["scoreEmoji"],
        "aiLikelihoodPercent":    feedback["aiLikelihoodPercent"],
        "humanLikelihoodPercent": feedback["humanLikelihoodPercent"],
        "aiDetectionReason":      feedback["aiDetectionReason"],
        "aiVerdict":              feedback["aiVerdict"],
        "isGarbage":              feedback["isGarbage"],
        "garbageWarning":         feedback["garbageWarning"],
        "needsMentorHelp":        scores["totalScore"] < 40,
        "encouragement":          feedback["studentFeedback"]["encouragement"],
    }

    try:
        assignment_db_service.update_assignment_submission_with_ai_results(
            submission["submissionId"], result
        )
    except Exception as db_err:
        print(f"⚠️  DB update failed after AI review: {db_err} — returning result anyway")

    return _build_response(
        submission, result, feedback["studentFeedback"]["summary"], start_time
    )


# ---------- GET /api/review/assignment-submission/{id} ---------------------

@router.get("/assignment-submission/{submission_id}")
async def get_assignment_submission(submission_id: int, student_id: int):
    """Re-fetch a past assignment review (for the View Details / re-show flow)."""
    row = assignment_db_service.get_assignment_submission_by_id(submission_id, student_id)
    if not row:
        raise HTTPException(status_code=404, detail="Submission not found")

    feedback = None
    if row.get("feedback"):
        try:
            feedback = json.loads(row["feedback"]) if isinstance(row["feedback"], str) else row["feedback"]
        except Exception:
            feedback = None

    return {
        "success": True,
        "submission": {
            "submissionId": row["id"],
            "assignmentId": row["assignment_id"],
            "title":        row.get("assignment_title"),
            "totalMarks":   row.get("total_marks", 100),
            "status":       row.get("status"),
            "grade":        row.get("grade"),
            "submittedAt":  str(row["submitted_at"]) if row.get("submitted_at") else None,
            "fileName":     row.get("file_name"),
            "fileUrl":      row.get("file_path"),
            "notes":        row.get("notes"),
        },
        "feedback": feedback,
    }


# ---------- helpers --------------------------------------------------------

def _build_response(submission: dict, result: dict, summary: str, start_time: float) -> dict:
    total_time = int((time.time() - start_time) * 1000)
    return {
        "success":    True,
        "submission": submission,
        "feedback": {
            "score":                  result["totalScore"],
            "grade":                  result["grade"],
            "scoreEmoji":             result.get("scoreEmoji", "📝"),
            "summary":                summary,
            "rubricScores":           result.get("rubricScores", []),
            "strengths":              result.get("strengths", []),
            "improvements":           result.get("improvements", []),
            "missingConcepts":        result.get("missingConcepts", []),
            "coveredConcepts":        result.get("coveredConcepts", []),
            "suggestions":            result.get("suggestedModules", []),
            "detailedFeedback":       result.get("detailedFeedback", ""),
            "wordCount":              result.get("wordCount", 0),
            "wordCountMessage":       result.get("wordCountMessage", ""),
            "encouragement":          result.get("encouragement", ""),
            "aiLikelihoodPercent":    result.get("aiLikelihoodPercent"),
            "humanLikelihoodPercent": result.get("humanLikelihoodPercent"),
            "aiDetectionReason":      result.get("aiDetectionReason", ""),
            "aiVerdict":              result.get("aiVerdict", "uncertain"),
            "isGarbage":              bool(result.get("isGarbage")),
            "garbageWarning":         result.get("garbageWarning", ""),
        },
        "processingTimeMs": total_time,
    }


def _empty_feedback(msg: str, helpful: bool = True) -> dict:
    return {
        "score": 0, "grade": "—", "scoreEmoji": "📝",
        "summary": msg,
        "rubricScores": [], "strengths": [],
        "improvements": [
            "Upload your assignment as a PDF or Word document.",
            "Or write your answer in the notes box below the upload area.",
            "Make sure the file is under 10 MB and not password-protected.",
        ] if helpful else [],
        "missingConcepts": [], "coveredConcepts": [], "suggestions": [],
        "detailedFeedback": msg,
        "wordCount": 0, "wordCountMessage": "",
        "encouragement": "Upload your work and click Submit — we'll take it from there.",
        "aiLikelihoodPercent": None, "humanLikelihoodPercent": None,
        "aiDetectionReason": "Not analysed.", "aiVerdict": "uncertain",
        "isGarbage": False, "garbageWarning": "",
    }