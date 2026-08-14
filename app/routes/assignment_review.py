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
from fastapi import APIRouter, HTTPException, Depends, Header, BackgroundTasks
from app.services.capacity import capacity_guard
from pydantic import BaseModel

from app.services import (
    ai_service,
    scoring_service,
    feedback_service,
    assignment_db_service,
    review_pipeline,
    prefilter_service,
    rubric_service,
)

_PIPELINE_ON = os.getenv("REVIEW_PIPELINE", "on").lower() == "on"
MAX_REVIEWED_ATTEMPTS = int(os.getenv("MAX_REVIEWED_ATTEMPTS", "2"))
# Below this word count AND with no readable attachment we DECLINE to score,
# rather than award a 0 the student never earned. Image- and artifact-first
# tasks legitimately carry very little text.
MIN_REVIEWABLE_WORDS = int(os.getenv("MIN_REVIEWABLE_WORDS", "30"))
from app.utils.text_processor import (
    count_words, clean_text, find_mentioned_concepts
)
from app.utils import submission_intake as intake
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
    fileData: Optional[str] = None   # base64 file bytes — storage-free upload path
    # True = store the submission (text extracted once) WITHOUT scoring it.
    # Used by Coursework submits so the item lands in AiRev's New Review queue
    # and is reviewed only when the student clicks it there.
    storeOnly: bool = False
    # Which id space studentId is in. Absent/"users" = the browser default
    # (users.id, mapped to students.id server-side). "students" = the caller
    # already holds a students.id and it must NOT be remapped — see
    # canonical_student_id(). tools/bulk_review.py sets this; without it a
    # staff run grades the wrong learner on any ambiguous id.
    idSpace: Optional[str] = None


# ---------- GET /api/review/assignments/{student_id} -----------------------

@router.get("/assignments/{student_id}")
def list_student_assignments(student_id: int, tenant: Tenant = Depends(get_tenant)):
    from app.database import canonical_student_id
    student_id = canonical_student_id(student_id)
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


# ---------- POST /api/review/prepare/assignment/{id} -----------------------
# Trigger-1 webhook: faculty/admin calls this after creating or editing an
# assignment so the agent reads the questions and builds its knowledge pack
# BEFORE any student submits — "pre-ready", not lazy.
@router.post("/prepare/assignment/{assignment_id}")
def prepare_assignment(assignment_id: int, background_tasks: BackgroundTasks,
                             tenant: Tenant = Depends(get_tenant)):
    from app.services import knowledge_service
    assignment = assignment_db_service.get_assignment_by_id(tenant, assignment_id)
    if not assignment:
        raise HTTPException(status_code=404,
                            detail=f"Assignment {assignment_id} not found in tenant '{tenant.id}'")
    sources = knowledge_service.SOURCE_BUILDERS["assignment"](assignment)
    fresh_hash = knowledge_service.source_hash(sources)
    stored = knowledge_service.get_pack("assignment", assignment_id)
    if stored and stored["source_hash"] == fresh_hash:
        return {"success": True, "status": "ready", "version": stored["version"],
                "detail": "Knowledge already current."}
    background_tasks.add_task(
        knowledge_service.build_pack, "assignment", assignment_id, sources, fresh_hash)
    return {"success": True, "status": "building",
            "detail": "Knowledge build started."}


# ---------- POST /api/review/submit-assignment -----------------------------

@router.post("/submit-assignment", dependencies=[Depends(capacity_guard)])
def submit_and_review_assignment(
    req: SubmitAssignmentRequest,
    tenant: Tenant = Depends(get_tenant),
    x_admin_key: str = Header(default=""),
):
    # Who pays for this run — header-only authority, set before any AI spend.
    staff_run = ai_service.begin_run_billing(x_admin_key)
    if staff_run:
        print("[ASSIGNMENT] staff-initiated review — student will not be billed")
    from app.database import canonical_student_id
    req.studentId = canonical_student_id(req.studentId, req.idSpace)
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
    # storeOnly (Coursework submits): storage is not a review attempt, so the
    # policy does not apply — it runs when the stored text is actually reviewed.
    #
    # staff_run is exempt. The limit exists to stop a learner grinding the same
    # answer for a better mark; it was never meant to apply to US. Without this
    # exemption our own correction runs consumed the learner's attempts and
    # then locked them out: in the 13 Aug batch, students 336 and 109 came back
    # "blocked: attempt_limit" on a re-review THEY never requested, leaving a
    # wrong score standing with no way to replace it.
    if not req.storeOnly and not staff_run:
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

    # The assignment's own marks scale (10, 20, 100 ...). The rubric engine
    # scores in percent; grades are persisted in THIS scale.
    max_marks = int(assignment.get("maxScore") or 100)

    # ── Intake: read EVERY format a learner may have submitted ────────────
    # Typed text, an attached file of any supported type, and any link pasted
    # into the answer box (a published artifact, a hosted page, a shared doc)
    # are each recorded as an artefact — what arrived, what was read from it,
    # and, when nothing could be read, why. See app/utils/submission_intake.
    cleaned_typed = clean_text(req.answerText or "")
    artefacts: list[intake.Artefact] = []

    if req.fileData or req.fileUrl:
        artefacts.append(intake.from_upload(req.fileData, req.fileUrl, req.fileName or ""))

    if cleaned_typed:
        artefacts.append(intake.from_typed(cleaned_typed))
        artefacts.extend(intake.from_links_in(cleaned_typed))

    if not any(a.readable for a in artefacts):
        prior = assignment_db_service.get_latest_assignment_submission(
            tenant, req.assignmentId, req.studentId
        )
        if prior:
            # Coursework stores the upload in file_path (not file_url); relative
            # LMS paths resolve inside extract_text_from_url (resolve_lms_url).
            prior_url = prior.get("file_url") or prior.get("file_path")
            if prior_url:
                artefacts.append(intake.from_stored_file(
                    prior_url, prior.get("file_name", "")))
            db_notes = clean_text(prior.get("notes") or "")
            if db_notes:
                # No link scan here. Stored notes are ALREADY-ASSEMBLED intake
                # output from an earlier run — any link in them was opened then
                # and its content is already in the text. Re-scanning would
                # re-fetch every link on every re-review and duplicate it.
                artefacts.append(intake.from_typed(db_notes))

    file_error = intake.first_error(artefacts)
    manifest, content = intake.render(artefacts)
    deliverable = intake.has_deliverable(artefacts)

    if not content:
        total_time = int((time.time() - start_time) * 1000)
        if file_error:
            msg = (f"We received your file but couldn't read any text from it "
                   f"({file_error}). Please check it opens correctly, then re-attach "
                   f"it — or type your answer in the box — and submit again.")
        else:
            msg = ("We couldn't find any answer for this assignment. Attach your work "
                   "(PDF, Word, Excel, image, or text), or type your answer in the box, "
                   "then click Submit again.")
        return {
            "success": True,
            # Explicit no-content signal (same contract as industry sessions):
            # frontends render a compose prompt instead of a scored 0 card.
            "status": "needs_input",
            "needsInput": True,
            "submission": {"submissionId": 0, "attemptNumber": 0},
            "feedback": _empty_feedback(msg, helpful=True),
            "processingTimeMs": total_time,
        }

    # The manifest travels WITH the answer to the marker, so a produced
    # deliverable is visible as evidence instead of being inferred from a
    # caption. word_count stays on the learner's own content — counting the
    # manifest would let provenance text push a thin answer past the length
    # gates, which is the inverse of the bug this fixes.
    combined = f"{manifest}\n{content}".strip()
    word_count = count_words(content)
    print(f"[ASSIGNMENT] {word_count} words of content across "
          f"{len(artefacts)} artefact(s): "
          + ", ".join(f"{a.kind}{'' if a.readable else '(unread)'}" for a in artefacts))

    # A SHORT answer is not automatically a FAILING answer. Many tasks here are
    # image- or artifact-first (create an image, publish an artifact, share a
    # link), where the attachment IS the deliverable and the text is a caption.
    # The old rule scored those 0/100 without ever calling the AI — students
    # were failed by a word count for doing the task correctly.
    #
    # Decline to score ONLY when there is too little readable content, and say
    # exactly what was found. This runs BEFORE any DB write, so nothing is
    # stored, nothing is graded, and the item stays cleanly re-reviewable.
    # storeOnly is exempt: storage must accept short work — the gate applies
    # when the stored work is actually reviewed.
    if not req.storeOnly and word_count < MIN_REVIEWABLE_WORDS and not deliverable:
        found = f"{word_count} word{'' if word_count == 1 else 's'} of text"
        if file_error:
            found += f", and your attachment could not be read ({file_error})"
        elif req.fileData or req.fileUrl or req.fileName:
            found += ", and no readable text could be taken from your attachment"
        else:
            found += ", and no file was attached"
        msg = (f"We haven't scored this yet — we could only find {found}. "
               f"If your work is in a file, re-attach it (PDF, Word, image or text); "
               f"if it is written work, add your reasoning in the answer box. "
               f"No score has been recorded for this attempt.")
        print(f"[ASSIGNMENT] NOT SCORED (too little readable content): "
              f"words={word_count}, file_error={file_error or 'none'} — no row written")
        return {
            "success": True,
            "status": "needs_input",
            "needsInput": True,
            "submission": {"submissionId": 0, "attemptNumber": 0},
            "feedback": _empty_feedback(msg, helpful=True),
            "processingTimeMs": int((time.time() - start_time) * 1000),
        }

    # ── Reflexes: zero-token checks before any AI spend ────────────────────
    # storeOnly skips them: storage must never be withheld — the checks run
    # when the stored text is actually reviewed.
    reflex = {} if req.storeOnly else prefilter_service.check(
        "assignment", req.assignmentId, req.studentId, combined)
    if reflex and not reflex["ok"]:
        return {"success": False, "blocked": reflex["reason"],
                "message": reflex["message"]}

    # Persist the COMBINED text (typed + extracted file) as the submission's
    # stored answer. This is "read once, remember forever": the file is parsed
    # exactly once at submit time, so New Review and Re-analyze later read the
    # text straight from the DB with no dependency on the file still being
    # fetchable. file_url/file_name are still recorded for provenance.
    submission = assignment_db_service.save_assignment_submission(
        tenant,
        req.assignmentId,
        req.studentId,
        combined or None,
        req.fileUrl,
        req.fileName,
    )
    print(f"[ASSIGNMENT] saved submission id={submission['submissionId']}, "
          f"attempt={submission['attemptNumber']}")
    prefilter_service.record_fingerprint(
        "assignment", req.assignmentId, req.studentId,
        submission["submissionId"], combined, word_count,
        text_hash=reflex.get("text_hash"))

    if req.storeOnly:
        total_time = int((time.time() - start_time) * 1000)
        print(f"[ASSIGNMENT] 📥 Stored without review (storeOnly): "
              f"submission={submission['submissionId']}")
        return {
            "success": True, "stored": True, "status": "stored",
            "submission": submission,
            "message": "Submission stored. Open AiRev → New Review for AI feedback.",
            "processingTimeMs": total_time,
        }

    # ── Adaptive rubric ────────────────────────────────────────────────────
    # The agent designs criteria for THIS task (derived once per assignment,
    # cached, rebuilt when faculty edit it) instead of grading every task
    # against one fixed template. Word limits come from the same derivation,
    # so an image-first task is not penalised for a short caption.
    adaptive = rubric_service.get_or_derive(
        tenant, "assignment", req.assignmentId, assignment)
    adaptive_rubric = {"criteria": adaptive["criteria"]}
    word_min, word_max = adaptive["wordMin"], adaptive["wordMax"]
    # AUDIT: `assignment` is ALSO the knowledge-pack source. Mutating it here
    # changed the pack's content hash and staled every pack, so the derived
    # rubric travels separately and the task text stays untouched.

    # AUDIT: apply_gates decides "case specificity" by substring-matching
    # criterion NAMES ("evidence", "application", "practical" ...). With
    # free-text derived names that fired by accident, capping a criterion at
    # 40% and telling the student they "never engaged this case's facts" — on
    # tasks that have no case at all. Disable that gate for non-written
    # deliverables, where there is no source material to be specific about.
    gate_overrides = ({} if adaptive.get("submissionKind") in ("written", "mixed")
                      else {"generic_answer_cap": 100})

    # ── Evidence-gated pipeline (primary path) ─────────────────────────────
    if _PIPELINE_ON:
        try:
            r = review_pipeline.review_with_knowledge(
                scope_type="assignment", scope_id=req.assignmentId,
                raw_source=assignment, rubric=adaptive_rubric,
                student_answer=combined, word_count=word_count,
                word_limit_min=word_min,
                word_limit_max=word_max,
                gate_overrides=gate_overrides,
                student_id=req.studentId,
            )
            if r is not None:
                prefilter_service.flag_review_outcomes(
                    "assignment", req.assignmentId, req.studentId,
                    submission["submissionId"], r)
                _remember_student_assignment(req, submission, r)
                return _pipeline_assignment_response(
                    tenant, submission, r, word_count, start_time,
                    duplicate=any(f.get("flag") == "cohort_duplicate"
                                  for f in reflex.get("flags", [])),
                    max_marks=max_marks)
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
            grading_rubric=adaptive_rubric,
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

    # AUDIT: derived criterion names are task phrases, not concepts — feeding
    # them to the substring matcher produced bogus "missing concepts". Only
    # use them when the rubric was NOT derived.
    rubric_topics = ([] if adaptive.get("derived")
                     else [c.get("name", "") for c in adaptive_rubric["criteria"]])
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
        ai_analysis, adaptive_rubric, word_count,
        word_min, word_max,
    )
    feedback = feedback_service.generate_feedback(
        scores, ai_analysis, word_count, word_min, word_max, max_marks,
    )

    result = {
        "totalScore":             scores["totalScore"],
        "grade":                  scores["grade"],
        # Rubric rows carry BOTH units: weights out of 100 (what the engine
        # computed) and the same rows in the assignment's marks (what the
        # student is owed on screen).
        "rubricScores":           scoring_service.scale_rubric(
                                      scores["rubricBreakdown"], max_marks),
        "penaltyPercent":         scores.get("wordCountPenalty", 0),
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
            tenant, submission["submissionId"], result, max_marks
        )
    except Exception as db_err:
        print(f"[ASSIGNMENT] DB update failed after AI review: {db_err}")

    return _build_response(
        submission, result, feedback["studentFeedback"]["summary"], start_time, max_marks
    )


# ---------- GET /api/review/assignment-submission/{id} ---------------------

@router.get("/assignment-submission/{submission_id}")
def get_assignment_submission(
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
def assignment_history(
    assignment_id: int,
    student_id: int,
    tenant: Tenant = Depends(get_tenant),
):
    history = assignment_db_service.get_assignment_history(tenant, assignment_id, student_id)
    return {"success": True, "history": history}


# ---------- helpers --------------------------------------------------------

def _remember_student_assignment(req, submission, r):
    """Person-memory fold + stylometry check. Never affects the review."""
    from app.services import student_memory_service as smem
    try:
        ai_pct = r["authorship"]["aiLikelihoodPercent"]
        profile = smem.get_profile(req.studentId)
        if smem.authorship_shift(profile, ai_pct):
            prefilter_service.flag_exception(
                "assignment", req.assignmentId, req.studentId,
                submission["submissionId"], "authorship_shift",
                f"human-styled baseline (median ~{profile['aggregates'].get('ai_median')}% AI) "
                f"suddenly reads ~{ai_pct}% AI-written")
        smem.fold_review(req.studentId, "assignment", req.assignmentId,
                         r["scores"]["totalScore"], r["conceptsMissing"], ai_pct)
    except Exception as e:
        print(f"[ASSIGNMENT] person-memory update failed (review unaffected): {e}")


def _pipeline_assignment_response(tenant, submission, r, word_count, start_time,
                                  duplicate=False, max_marks: int = 100):
    """Persist + shape the assignment response from a pipeline result.
    Reuses _build_response for the envelope; adds the pipeline-only fields."""
    scores = r["scores"]
    grade = scoring_service.get_grade(scores["totalScore"])
    awarded = assignment_db_service.scaled_marks(scores["totalScore"], max_marks)
    summary = scoring_service.build_summary(
        awarded, max_marks,
        len(r["conceptsCovered"]),
        len(r["conceptsCovered"]) + len(r["conceptsMissing"]))

    result = {
        "totalScore":       scores["totalScore"],
        "grade":            grade,
        "rubricScores":     scoring_service.scale_rubric(
                                scores["rubricBreakdown"], max_marks),
        "penaltyPercent":   scores.get("wordCountPenalty", 0),
        "strengths":        r["strengths"],
        "improvements":     r["improvements"],
        "missingConcepts":  r["conceptsMissing"],
        "coveredConcepts":  r["conceptsCovered"],
        "suggestedModules": [],
        "detailedFeedback": r["detailedFeedback"],
        "feedbackPoints":   r.get("feedbackPoints", []),
        "hardTruth":        r.get("hardTruth", ""),
        "wordCount":        word_count,
        "wordCountMessage": scores["wordCountNote"],
        "scoreEmoji":       "",
        "encouragement":    "",
        "isGarbage":        r["isGarbage"],
        "garbageWarning":   r["garbageWarning"],
        "needsMentorHelp":  scores["totalScore"] < 40 or r["isGarbage"]
                            or r["authorship"]["aiLikelihoodPercent"] >= 90,
        "summary":          summary,
        "plagiarismFlag":   "high" if duplicate else "low",
        **r["authorship"],
    }
    try:
        assignment_db_service.update_assignment_submission_with_ai_results(
            tenant, submission["submissionId"], result, max_marks)
    except Exception as db_err:
        print(f"[ASSIGNMENT] DB update failed after pipeline review: {db_err}")

    print(f"[ASSIGNMENT] ✅ Pipeline review: score={scores['totalScore']} grade={grade} "
          f"path={r['decisions']['scoringPath']} gates={len(scores['gatesHit'])}")

    response = _build_response(submission, result, summary, start_time, max_marks)
    response["feedback"]["howYouScored"]   = r["howYouScored"]
    response["feedback"]["languageReport"] = r["languageReport"]
    response["feedback"]["factualErrors"]  = r["factualErrors"]
    response["_meta"] = {"pipeline": r["decisions"]}
    return response


def _build_response(submission: dict, result: dict, summary: str, start_time: float,
                    max_marks: int = 100) -> dict:
    total_time = int((time.time() - start_time) * 1000)
    awarded = assignment_db_service.scaled_marks(result.get("totalScore", 0), max_marks)
    return {
        "success":    True,
        "submission": submission,
        "feedback": {
            # `score` stays the 0-100 percentage (the progress ring is a
            # percentage arc); `scoreMarks` / `outOf` are the assignment's real
            # marks, which is what the student is actually graded on.
            "score":                  result["totalScore"],
            "scorePercent":           result["totalScore"],
            "scoreMarks":             awarded,
            "outOf":                  max_marks,
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
            "feedbackPoints":         result.get("feedbackPoints", []),
            "hardTruth":              result.get("hardTruth", ""),
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
        "feedbackPoints": [], "hardTruth": "",
        "wordCount": 0, "wordCountMessage": "",
        "encouragement": "Upload your work and click Submit — we'll take it from there.",
        "aiLikelihoodPercent": None, "humanLikelihoodPercent": None,
        "aiDetectionReason": "Not analysed.", "aiVerdict": "uncertain",
        "isGarbage": False, "garbageWarning": "",
    }