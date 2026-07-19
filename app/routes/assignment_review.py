# app/routes/assignment_review.py
# ---------------------------------------------------------------------------
# Multi-tenant assignment review endpoints.
#
# CHANGED:
#   - New endpoint: GET /api/review/assignment-history/{assignment_id}/{student_id}
#     Returns all of a student's attempts on an assignment, newest first.
#     Frontend uses this to show prior review on reopen + Re-analyze button.
#
# Every call to assignment_db_service passes the tenant EXPLICITLY — no
# reliance on contextvar (which can drop across async boundaries).
# ---------------------------------------------------------------------------

import time
import json
import os
import hashlib
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, Header
from pydantic import BaseModel

from app.services import (
    ai_service,
    scoring_service,
    feedback_service,
    assignment_db_service,
    review_pipeline,
)

_PIPELINE_ON = os.getenv("REVIEW_PIPELINE", "on").lower() == "on"
MAX_REVIEWED_ATTEMPTS = int(os.getenv("MAX_REVIEWED_ATTEMPTS", "2"))
from app.utils.text_processor import (
    count_words, clean_text, find_mentioned_concepts
)
from app.utils.file_extractor import extract_text_from_url
from app.tenants import resolve_tenant_by_key, Tenant
from app.database import set_current_tenant


router = APIRouter(prefix="/api/review", tags=["assignment-review"])


def get_tenant(x_api_key: str = Header(default="")) -> Tenant:
    """Local auth dep — resolves tenant from key for this router's handlers."""
    tenant = resolve_tenant_by_key(x_api_key)
    set_current_tenant(tenant)
    print(f"[ASSIGNMENT] tenant resolved: {tenant.id} (DB={tenant.database_url_env})")
    return tenant


class SubmitAssignmentRequest(BaseModel):
    assignmentId: int
    studentId: int
    answerText: Optional[str] = ""
    fileUrl: Optional[str] = None
    fileName: Optional[str] = None


# ---------- GET /api/review/assignments/{student_id} -----------------------

@router.get("/assignments/{student_id}")
async def list_student_assignments(student_id: int, tenant: Tenant = Depends(get_tenant)):
    rows = assignment_db_service.get_student_assignments(tenant, student_id)

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
            "id":               r["id"],
            "title":            r["title"],
            "description":      r.get("description"),
            "dueDate":          str(r["due_date"]) if r.get("due_date") else None,
            "totalMarks":       r.get("total_marks", 100),
            "status":           r.get("status"),
            "submissionId":     r.get("submission_id"),
            "submissionStatus": r.get("submission_status"),
            "submittedAt":      str(r["submitted_at"]) if r.get("submitted_at") else None,
            "grade":            r.get("submission_grade"),
            "submittedFile":    r.get("submitted_file_name"),
            "hasFeedback":      bool(feedback_blob),
            "reviewedBy":       (feedback_blob or {}).get("reviewedBy"),
        })

    return {"success": True, "assignments": out, "tenant": tenant.id}


# ---------- POST /api/review/submit-assignment -----------------------------

@router.post("/submit-assignment")
async def submit_and_review_assignment(
    req: SubmitAssignmentRequest,
    tenant: Tenant = Depends(get_tenant),
):
    start_time = time.time()
    print(f"[ASSIGNMENT][{tenant.id}] submission: student={req.studentId}, assignment={req.assignmentId}")

    assignment = assignment_db_service.get_assignment_by_id(tenant, req.assignmentId)
    if not assignment:
        try:
            from app.database import tquery
            available = tquery(
                tenant,
                "SELECT id, title, status FROM assignments LIMIT 10",
            )
            print(f"[ASSIGNMENT][{tenant.id}] assignment {req.assignmentId} not found. "
                  f"Available in this tenant DB: {available}")
        except Exception as diag_err:
            print(f"[ASSIGNMENT][{tenant.id}] diagnostic query failed: {diag_err}")
        raise HTTPException(
            status_code=404,
            detail=f"Assignment {req.assignmentId} not found or inactive in tenant '{tenant.id}'",
        )

    # ── Re-review policy: max 2 reviewed attempts, revised text required ───
    state = assignment_db_service.get_attempt_state(tenant, req.assignmentId, req.studentId)
    if state["reviewedAttempts"] >= MAX_REVIEWED_ATTEMPTS:
        return {"success": False, "blocked": "attempt_limit",
                "message": ("You've used your re-attempt for this assignment. "
                            "Your final score stands — carry the feedback into the next one.")}
    if state["reviewedAttempts"] >= 1 and state["latestAnswerText"] and (req.answerText or "").strip():
        old_h = hashlib.sha256(state["latestAnswerText"].strip().lower().encode()).hexdigest()
        new_h = hashlib.sha256(req.answerText.strip().lower().encode()).hexdigest()
        if old_h == new_h:
            return {"success": False, "blocked": "identical_resubmission",
                    "message": ("This is the same answer you already submitted. "
                                "Revise it using your feedback, then resubmit.")}

    cleaned_typed = clean_text(req.answerText or "")
    parts: list[str] = []
    file_used = None

    if req.fileUrl:
        extracted, why = extract_text_from_url(req.fileUrl, req.fileName or "")
        if extracted:
            file_used = req.fileName or req.fileUrl
            parts.append(extracted)
            print(f"[ASSIGNMENT] Extracted file: {file_used} ({count_words(extracted)} words)")
        elif why:
            print(f"[ASSIGNMENT] Upload extraction failed: {why}")

    if cleaned_typed:
        parts.append(cleaned_typed)

    if not parts:
        prior = assignment_db_service.get_latest_assignment_submission(
            tenant, req.assignmentId, req.studentId
        )
        if prior:
            if prior.get("file_url"):
                extracted, why = extract_text_from_url(
                    prior["file_url"], prior.get("file_name", "")
                )
                if extracted:
                    file_used = prior.get("file_name") or prior["file_url"]
                    parts.append(extracted)
            db_notes = clean_text(prior.get("notes") or "")
            if db_notes:
                parts.append(db_notes)

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

    combined = "\n\n".join(parts).strip()
    word_count = count_words(combined)
    print(f"[ASSIGNMENT] {word_count} words combined "
          f"(file={'yes' if file_used else 'no'}, typed={'yes' if cleaned_typed else 'no'})")

    submission = assignment_db_service.save_assignment_submission(
        tenant,
        req.assignmentId,
        req.studentId,
        cleaned_typed or None,
        req.fileUrl,
        req.fileName,
    )
    print(f"[ASSIGNMENT] saved submission id={submission['submissionId']}, "
          f"attempt={submission['attemptNumber']}")

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
            "scoreEmoji": "—",
            "plagiarismFlag": "low", "needsMentorHelp": True,
            "summary": msg,
        }
        try:
            assignment_db_service.update_assignment_submission_with_ai_results(
                tenant, submission["submissionId"], partial
            )
        except Exception as e:
            print(f"[ASSIGNMENT] DB update (short-submission path) failed: {e}")
        return _build_response(submission, partial, msg, start_time)

    # ── Evidence-gated pipeline (primary path) ─────────────────────────────
    if _PIPELINE_ON:
        try:
            r = review_pipeline.review_with_knowledge(
                scope_type="assignment", scope_id=req.assignmentId,
                raw_source=assignment, rubric=assignment["gradingRubric"],
                student_answer=combined, word_count=word_count,
                word_limit_min=assignment["wordLimitMin"],
                word_limit_max=assignment["wordLimitMax"],
            )
            if r is not None:
                return _pipeline_assignment_response(
                    tenant, submission, r, word_count, start_time)
        except Exception as e:
            print(f"[ASSIGNMENT] Pipeline failed, falling back to legacy: {e}")

    try:
        ai_analysis = ai_service.analyze_answer(
            case_study={
                "title":       assignment["title"],
                "description": assignment["description"],
                "questions":   assignment["questions"],
            },
            model_answer=assignment["modelAnswers"],
            student_answer=combined,
            grading_rubric=assignment["gradingRubric"],
            key_concepts=assignment["keyConcepts"],
        )
    except Exception as e:
        print(f"[ASSIGNMENT] AI review unavailable: {e}")
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
        "summary":                feedback["studentFeedback"]["summary"],
        "encouragement":          feedback["studentFeedback"]["encouragement"],
    }

    try:
        assignment_db_service.update_assignment_submission_with_ai_results(
            tenant, submission["submissionId"], result
        )
    except Exception as db_err:
        print(f"[ASSIGNMENT] DB update failed after AI review: {db_err}")

    return _build_response(
        submission, result, feedback["studentFeedback"]["summary"], start_time
    )


# ---------- GET /api/review/assignment-submission/{id} ---------------------

@router.get("/assignment-submission/{submission_id}")
async def get_assignment_submission(
    submission_id: int,
    student_id: int,
    tenant: Tenant = Depends(get_tenant),
):
    row = assignment_db_service.get_assignment_submission_by_id(tenant, submission_id, student_id)
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


# ---------- GET /api/review/assignment-history/{assignment_id}/{student_id} ----
# NEW: powers "show previous review on reopen" + Re-analyze flow.

@router.get("/assignment-history/{assignment_id}/{student_id}")
async def assignment_history(
    assignment_id: int,
    student_id: int,
    tenant: Tenant = Depends(get_tenant),
):
    history = assignment_db_service.get_assignment_history(tenant, assignment_id, student_id)
    return {"success": True, "history": history}


# ---------- helpers --------------------------------------------------------

def _pipeline_assignment_response(tenant, submission, r, word_count, start_time):
    """Persist + shape the assignment response from a pipeline result.
    Reuses _build_response for the envelope; adds the pipeline-only fields."""
    scores = r["scores"]
    grade = scoring_service.get_grade(scores["totalScore"])
    summary = (f"You scored {scores['totalScore']}/100 ({grade}). "
               f"{len(r['conceptsCovered'])} of "
               f"{len(r['conceptsCovered']) + len(r['conceptsMissing'])} core concepts engaged.")

    result = {
        "totalScore":       scores["totalScore"],
        "grade":            grade,
        "rubricScores":     scores["rubricBreakdown"],
        "strengths":        r["strengths"],
        "improvements":     r["improvements"],
        "missingConcepts":  r["conceptsMissing"],
        "coveredConcepts":  r["conceptsCovered"],
        "suggestedModules": [],
        "detailedFeedback": r["detailedFeedback"],
        "wordCount":        word_count,
        "wordCountMessage": scores["wordCountNote"],
        "scoreEmoji":       "",
        "encouragement":    "",
        "isGarbage":        r["isGarbage"],
        "garbageWarning":   r["garbageWarning"],
        "needsMentorHelp":  scores["totalScore"] < 40 or r["isGarbage"]
                            or r["authorship"]["aiLikelihoodPercent"] >= 90,
        "summary":          summary,
        "plagiarismFlag":   "low",
        **r["authorship"],
    }
    try:
        assignment_db_service.update_assignment_submission_with_ai_results(
            tenant, submission["submissionId"], result)
    except Exception as db_err:
        print(f"[ASSIGNMENT] DB update failed after pipeline review: {db_err}")

    print(f"[ASSIGNMENT] ✅ Pipeline review: score={scores['totalScore']} grade={grade} "
          f"path={r['decisions']['scoringPath']} gates={len(scores['gatesHit'])}")

    response = _build_response(submission, result, summary, start_time)
    response["feedback"]["howYouScored"]   = r["howYouScored"]
    response["feedback"]["languageReport"] = r["languageReport"]
    response["feedback"]["factualErrors"]  = r["factualErrors"]
    response["_meta"] = {"pipeline": r["decisions"]}
    return response


def _build_response(submission: dict, result: dict, summary: str, start_time: float) -> dict:
    total_time = int((time.time() - start_time) * 1000)
    return {
        "success":    True,
        "submission": submission,
        "feedback": {
            "score":                  result["totalScore"],
            "grade":                  result["grade"],
            "scoreEmoji":             result.get("scoreEmoji", "—"),
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
        "score": 0, "grade": "—", "scoreEmoji": "—",
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