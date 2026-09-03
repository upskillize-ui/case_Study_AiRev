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
from app.auth import require_admin
from pydantic import BaseModel

from app.services import (
    ai_service,
    scoring_service,
    feedback_service,
    assignment_db_service,
    grade_guard,
    review_pipeline,
    prefilter_service,
    rubric_service,
)

_PIPELINE_ON = os.getenv("REVIEW_PIPELINE", "on").lower() == "on"
# The legacy scorer marks against a different standard (see the fallback below).
# Off by default from 02 Sep 2026: one standard per assignment, or no mark.
_LEGACY_FALLBACK_ON = os.getenv("AIREV_LEGACY_FALLBACK", "off").lower() == "on"
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


# ASYNC, and it MUST stay async. FastAPI runs a SYNC dependency in a worker
# thread via anyio, which gives it a COPY of the contextvar context — so
# set_current_tenant() wrote the tenant into a context that was discarded the
# moment the dependency returned, and every query()/execute() in the request
# then fell through _resolve_url()'s "lms" default. Measured: a sync dep leaves
# the handler's contextvar None; an async dep propagates it. Effect while it
# was sync: an eaprep key read AND WROTE the lms production database.
async def get_tenant(x_api_key: str = Header(default="")) -> Tenant:
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
# STAFF ONLY — schedules an unattributed Claude knowledge build.
@router.post("/prepare/assignment/{assignment_id}",
             dependencies=[Depends(require_admin)])
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
        # Whose submission the links about to be opened belong to. Set before
        # reading them and cleared after, so one review's pictures can never be
        # filed against the next review's row.
        from app.services import link_shot_store
        link_shot_store.set_target("assignment", req.assignmentId, req.studentId)
        try:
            artefacts.extend(intake.from_links_in(cleaned_typed))
        finally:
            link_shot_store.clear_target()

    if not any(a.readable for a in artefacts):
        prior = assignment_db_service.get_latest_assignment_submission(
            tenant, req.assignmentId, req.studentId
        )
        if prior:
            # Coursework stores the upload in file_path (not file_url); relative
            # LMS paths resolve inside extract_text_from_url (resolve_lms_url).
            prior_url = prior.get("file_url") or prior.get("file_path")
            if prior_url:
                # May open a browser now (a link in the link field). Same
                # target rule as the notes scan above.
                from app.services import link_shot_store
                link_shot_store.set_target("assignment", req.assignmentId, req.studentId)
                try:
                    artefacts.append(intake.from_stored_file(
                        prior_url, prior.get("file_name", "")))
                finally:
                    link_shot_store.clear_target()
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
            msg = (f"We could not read your file ({file_error}). "
                   f"Re-attach it, or type your answer in the box, and submit again.")
        else:
            msg = ("We could not find your answer. Attach your work — PDF, Word, "
                   "Excel, image or text — or type it in the box, then submit again.")
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
            found += (f", and your attachment could not be read "
                      f"({grade_guard.learner_facing(file_error)})")
        elif req.fileData or req.fileUrl or req.fileName:
            found += ", and no readable text could be taken from your attachment"
        else:
            found += ", and no file was attached"
        msg = (f"We could only find {found}, so there are no marks yet. "
               f"Attach your file again, or write your answer in the box, "
               f"then submit.")
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
    adaptive_rubric = {"criteria": adaptive["criteria"],
                       "fingerprint": adaptive.get("fingerprint", "")}
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

    # A GENERIC RUBRIC IS NOT THIS TASK'S STANDARD (02 Sep 2026).
    #
    # derived=False means rubric derivation failed and the four-line fallback
    # (Task completion / Accuracy / Reasoning / Communication) is in play. That
    # is a DIFFERENT standard from the one every other student on this
    # assignment was judged against — the requirement list that varied 4/5/6/7
    # across one assignment. Derivation failure is an outage on our side and
    # transient, so the row is left ungraded and swept again, exactly like
    # every other of-our-making failure.
    if not adaptive.get("gradeable"):
        assignment_db_service.mark_not_graded(
            tenant, submission["submissionId"],
            "We could not read this task's requirements just now. Your work "
            "is saved and there are no marks yet. This is our side, not yours.")
        print(f"[ASSIGNMENT] NOT GRADED — rubric derivation unavailable for "
              f"assignment {req.assignmentId}; row left retryable")
        return {
            "success": True, "status": "pending_review", "notGraded": True,
            "submission": submission,
            "feedback": _empty_feedback(
                "Your work is saved. There are no marks yet.", helpful=True),
            "processingTimeMs": int((time.time() - start_time) * 1000),
        }

    # Pictures of the learner's own work, for the marker to actually look at.
    judge_images = intake.images_for_judge(artefacts)
    if judge_images:
        print(f"[ASSIGNMENT] handing the marker {len(judge_images)} "
              f"picture(s) of the work")

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
                # THE MARKER LOOKS AT THE WORK. images_for_judge has existed
                # since 23 Aug and no route ever called it, so every poster,
                # screenshot and rendered page was judged from its filename
                # and a three-word caption. On an image-first cohort that is
                # the whole explanation for a wall of near-zeros.
                images=judge_images,
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
            print(f"[ASSIGNMENT] Pipeline failed: {e}")
            if not _LEGACY_FALLBACK_ON:
                # THE SECOND MARKER (02 Sep 2026). The legacy path below scores
                # against a different standard: no evidence gates, no
                # requirement rows, no gate trace. A learner whose review threw
                # got a number nobody else on the assignment was measured by,
                # and nothing on the card said so. That is half of "two
                # students, same work, different marks".
                #
                # A pipeline failure is our outage and transient. Refuse, keep
                # the row retryable, and let the sweep serve it under the same
                # rules as everyone else. Set AIREV_LEGACY_FALLBACK=on to
                # restore the old behaviour instantly if this ever blocks a
                # cohort.
                assignment_db_service.mark_not_graded(
                    tenant, submission["submissionId"],
                    "Your work is saved. The reviewer stopped partway, so there "
                    "are no marks yet. This is our side, not yours.")
                return {
                    "success": True, "status": "pending_review",
                    "notGraded": True, "submission": submission,
                    "feedback": _empty_feedback(
                        "Your work is saved. There are no marks yet.", helpful=True),
                    "processingTimeMs": int((time.time() - start_time) * 1000),
                }
            print("[ASSIGNMENT] AIREV_LEGACY_FALLBACK=on — marking with the "
                  "legacy scorer (a DIFFERENT standard from the rest of this "
                  "assignment)")

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
            # Nothing notifies a faculty member, so nothing here says one was.
            "message": ("Your assignment is saved. The reviewer is unavailable "
                        "right now, so there are no marks yet."),
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


def _graded_by_human(row: dict) -> bool:
    """True when this row's grade came from a person, not the agent.

    AiRev stamps its own payloads (reviewedBy / rubricScores / authorship). A
    graded row whose feedback carries none of those markers was almost
    certainly typed by faculty, and the conservative reading of an ambiguous
    row is 'human' — refusing a regrade is recoverable, destroying a marker's
    grade is not.
    """
    if row.get("grade") is None:
        return False
    blob = row.get("feedback")
    if not blob:
        return True                       # a grade with no agent payload
    try:
        fb = json.loads(blob) if isinstance(blob, str) else blob
    except Exception:
        return True                       # unparseable — assume human
    if not isinstance(fb, dict):
        return True
    if str(fb.get("reviewedBy", "")).lower() in ("mentor", "faculty", "human"):
        return True
    agent_markers = ("rubricScores", "aiLikelihoodPercent", "howYouScored",
                     "authorship", "detailedFeedback")
    return not any(k in fb for k in agent_markers)


# ---------- POST /api/review/re-review/assignment/{submission_id} ----------
# Staff-only correction path. Re-scores a submission that ALREADY has a grade
# and writes the result back into THAT SAME ROW.
#
# WHY IT EXISTS. /submit-assignment INSERTs a new row for every review — right
# for a learner, because it preserves their attempt history. It is wrong for a
# correction run: re-running 422 rows through it would have inserted 422 extra
# submissions, inflated every learner's attempt count, and left the wrong grade
# sitting in history beside the right one. This route touches no other row and
# creates none.
#
# SAFETY RULES, in order of importance:
#   1. Admin key required. A learner cannot reach this route at all — a wrong
#      or missing key is 403 before any read, not a silently cheaper review.
#   2. Nothing readable => nothing written. A row whose file has gone missing
#      keeps the grade it has. Blanking it would replace a wrong score with no
#      score, which is worse for the learner.
#   3. dryRun reports exactly what WOULD be read — artefacts, word count,
#      current grade — and spends nothing. Run it first.
#   4. UPDATE only, via the same persistence the normal path uses.

@router.post("/re-review/assignment/{submission_id}",
             dependencies=[Depends(capacity_guard)])
def re_review_assignment(
    submission_id: int,
    dryRun: bool = False,
    force: bool = False,
    tenant: Tenant = Depends(get_tenant),
    x_admin_key: str = Header(default=""),
):
    if not ai_service.begin_run_billing(x_admin_key):
        raise HTTPException(
            status_code=403,
            detail="Re-review is staff-only. Send a valid X-Admin-Key header.")

    start_time = time.time()
    row = assignment_db_service.get_submission_for_regrade(tenant, submission_id)
    if not row:
        raise HTTPException(
            status_code=404,
            detail=f"Submission {submission_id} not found in tenant '{tenant.id}'")

    assignment = assignment_db_service.get_assignment_by_id(tenant, row["assignment_id"])
    if not assignment:
        raise HTTPException(
            status_code=404,
            detail=f"Assignment {row['assignment_id']} not found or inactive")

    max_marks = int(assignment.get("maxScore") or 100)
    previous_grade = row.get("grade")

    # A human mark is not ours to overwrite. grade/feedback are single columns
    # shared by AiRev and faculty, so a correction run sweeping an id range
    # would replace a hand-entered mark and the marker's comments with an AI
    # score — silently, because dryRun reports the grade but not who set it.
    # Refuse by default; `force=true` is a deliberate, logged override.
    if _graded_by_human(row) and not force:
        print(f"[REGRADE] submission {submission_id}: HUMAN-graded — refused "
              f"(pass force=true to override)")
        return {"success": False, "skipped": "human_graded",
                "submissionId": submission_id,
                "previousGrade": previous_grade,
                "detail": ("This submission carries a faculty grade. Re-review "
                           "would replace it. Re-run with force=true only if "
                           "that is what you intend.")}

    # Re-read the work from the row itself.
    #
    # If a previous review already assembled this row, its notes ARE the
    # finished intake output — manifest plus labelled item blocks. Reuse them
    # whole. Re-extracting the attachment as well nests one manifest inside
    # another and hands the marker a second copy of the OCR dressed up as the
    # learner's own typing, which is what dropped student 1126 from 6.8/10 to
    # 1.2/10 on identical input.
    #
    # Rows that have NOT been assembled — the ones this route mainly exists for,
    # where an image was attached and nothing was ever read from it — still go
    # through full extraction.
    artefacts: list[intake.Artefact] = []
    stored_notes = clean_text(row.get("notes") or "")
    already_assembled = intake.from_stored_submission(stored_notes)

    # A FAILURE ON RECORD IS NOT A RESULT (04 Sep 2026). Reuse is right only
    # when the old read was complete. If its manifest says an item "could not
    # be read", reusing it re-refuses the row with the same words no matter
    # what was fixed since — 376 rows on one course sat in exactly that loop,
    # and the 03 Sep requeue would have paid to walk them round it again.
    # records_failed_read / typed_text_from were written for this on 21 Aug
    # and never wired in. Re-open the sources; carry forward only the
    # learner's own typed words, never our manifest or our old OCR.
    reread_reason = ""
    if already_assembled and intake.records_failed_read(already_assembled[0]):
        stored_notes = intake.typed_text_from(already_assembled[1])
        already_assembled = None
        reread_reason = "stored assembly records a failed read"

    # READ ONCE, EVER (03 Sep 2026). Everything below the cache check is the
    # expensive part of a review — OCR, transcription, frame description, a
    # browser render — and it produces the same text every time the same
    # submission is read. The fingerprint covers what the learner controls
    # (notes, file, name) and how we read it (INTAKE_VERSION); a resubmit or an
    # intake fix misses, everything else hits. Rows that never got a readable
    # artefact are not cached, so a transient failure is never made permanent.
    from app.services import intake_cache
    stored_file = row.get("file_path") or row.get("file_url")
    fp = intake_cache.fingerprint(stored_notes, stored_file or "",
                                  row.get("file_name") or "")
    cached = None if (already_assembled or dryRun) else \
        intake_cache.recall(tenant, submission_id, fp)

    if already_assembled:
        manifest, content = already_assembled
    elif cached:
        artefacts = cached
        manifest, content = intake.render(artefacts)
        print(f"[REGRADE] submission {submission_id}: intake served from cache "
              f"({len(artefacts)} artefact(s), nothing re-read)")
    else:
        # Whose submission the pages about to be opened belong to. The target is
        # set around BOTH reads now: a link pasted into the submit form's link
        # field lives in file_path, and since 03 Sep from_stored_file opens it in
        # the browser like any other link — so its screenshot has to be filed
        # against this row too, or the share card for a link submission is
        # built with no picture.
        from app.services import link_shot_store
        link_shot_store.set_target(
            "assignment", row.get("assignment_id"), row.get("student_id"),
            submission_id=row.get("id") or submission_id)
        try:
            if stored_file:
                artefacts.append(intake.from_stored_file(
                    stored_file, row.get("file_name") or ""))
            if stored_notes:
                artefacts.append(intake.from_typed(stored_notes))
                # Open what the learner linked to. The submit path has always
                # done this; the regrade path did not, so a Day 06 row whose
                # whole submission is a suno.com link had that URL marked as if
                # it were the learner's prose — nothing to quote, every
                # criterion pinned at the no-evidence cap, a cohort that did the
                # work told it scored 2/10. Same guards as submit.
                artefacts.extend(intake.from_links_in(stored_notes))
        finally:
            link_shot_store.clear_target()
        manifest, content = intake.render(artefacts)
        if not dryRun:
            intake_cache.remember(tenant, submission_id, fp, artefacts)
        if reread_reason:
            print(f"[REGRADE] submission {submission_id}: re-read from source "
                  f"({reread_reason}) — {len(artefacts)} artefact(s), "
                  f"{sum(1 for a in artefacts if a.readable)} readable")
    word_count = count_words(content)
    inventory = ([{"kind": "stored", "label": "previously assembled submission",
                   "words": len(content.split()), "readable": bool(content),
                   "note": "reused; not re-extracted"}]
                 if already_assembled else
                 [{"kind": a.kind, "label": a.label,
                   "words": len(a.text.split()) if a.readable else 0,
                   "readable": a.readable, "note": "served from intake cache"}
                  for a in artefacts]
                 if cached else
                 [{"kind": a.kind, "label": a.label,
                   "words": len(a.text.split()) if a.readable else 0,
                   "readable": a.readable, "note": a.note} for a in artefacts])

    if not content:
        # THE ORPHAN LEAK (02 Sep 2026). This used to leave the row exactly as
        # it was: grade NULL, feedback untouched, no notGraded marker. So the
        # sweeper re-selected it the next night, and the night after, for ever,
        # while the student sat in "to be graded" and was never told anything.
        # That is why the ungraded list only ever grew.
        #
        # Rule 2 still holds — no MARK is invented — but the row is RESOLVED:
        # the learner is told what we could not open and what to do about it,
        # and grade stays NULL so their corrected resubmission flows through
        # the normal path. A graded row is never touched.
        why = intake.first_error(artefacts) or "no stored work found"
        # OUR OUTAGE IS NOT THEIR FAULT (02 Sep 2026). Live on the Space right
        # now: the OCR provider is answering 503 to everything, so intake
        # records "could not be read" for files that are perfectly fine. Writing
        # "re-attach your work" onto those rows tells a learner their file is
        # broken when ours is. Stay silent, leave the row retryable, and say so
        # in the log instead.
        ours = grade_guard.reads_as_our_outage(why)
        if previous_grade is None and not dryRun and not ours:
            # learner_facing(): a learner is told a plain reason, never an
            # exception class, an HTTP envelope or a request id. On 03 Sep a
            # card read "OCR failed on anthropic: BadRequestError: Error code:
            # 400 - {...'You have reached your specified API usage limits'...}"
            # followed by "re-attach your work". Ours, in writing, on hers.
            #
            # A site that refused our robot is neither: not the learner's
            # fault, not an outage to retry. Its own sentence, stamped. See
            # grade_guard.reader_blocked.
            assignment_db_service.mark_not_graded(
                tenant, submission_id,
                grade_guard.READER_BLOCKED_MESSAGE if grade_guard.reader_blocked(why) else
                f"We could not open your file ({grade_guard.learner_facing(why)}), "
                f"so there are no marks yet. Re-attach your work, or type your "
                f"answer in the box, and submit again.")
        print(f"[REGRADE] submission {submission_id}: nothing readable ({why}) — "
              + ("OUR outage, learner not told, row retryable"
                 if ours else "learner told, row left ungraded"))
        return {"success": False, "skipped": "no_readable_content",
                # WHOSE FAULT WAS IT (03 Sep 2026). The caller has to be able to
                # tell "this student's file is unreadable" from "our provider is
                # down and every file is unreadable". Without this flag the batch
                # worker counted a dead provider as an ordinary policy skip, so
                # the consecutive-failure brake never came on and one sweep paid
                # to rediscover the outage a thousand times. See make_review_one.
                "ours": ours,
                "submissionId": submission_id,
                "previousGrade": previous_grade,
                "artefacts": inventory,
                "detail": why}

    if intake.is_unassessable(manifest, content):
        # The deliverable exists; we could not open it. Scoring it anyway is an
        # assertion about work nobody read — the fabrication rule, pointed the
        # other way. Leave the row untouched and say so plainly, so the learner
        # is asked for a description rather than handed a mark they didn't earn.
        detail = ("The work was submitted as a link or file we could not open, "
                  "and there is no written answer to judge.")
        # Same rule as above: if the read failed because OUR side was down, the
        # learner hears nothing and the row waits for the next sweep.
        first_why = intake.first_error(artefacts) or ""
        ours = grade_guard.reads_as_our_outage(f"{first_why} {manifest}")
        if previous_grade is None and not dryRun and not ours:
            assignment_db_service.mark_not_graded(
                tenant, submission_id,
                grade_guard.READER_BLOCKED_MESSAGE
                if grade_guard.reader_blocked(f"{first_why} {manifest}") else
                "Your link or file would not open for us, and there is no "
                "written answer with it, so there are no marks yet. Add a few "
                "lines about what you made, or attach the file itself, then "
                "submit again.")
        print(f"[REGRADE] submission {submission_id}: deliverable present but "
              f"unreadable ({intake.substantive_words(content)} words of answer) — "
              + ("OUR outage, learner not told, row retryable"
                 if ours else "learner told, row left ungraded"))
        return {"success": False, "skipped": "unassessable_deliverable",
                "ours": ours,          # see the note on no_readable_content
                "submissionId": submission_id,
                "previousGrade": previous_grade,
                "artefacts": inventory,
                "detail": ("The work was submitted as a link or file we could not "
                           "open, and there is no written answer to judge. Ask the "
                           "learner to add a few lines describing what they made "
                           "and how, then re-review.")}

    if dryRun:
        return {"success": True, "dryRun": True,
                "submissionId": submission_id,
                "studentId": row["student_id"],
                "assignmentId": row["assignment_id"],
                "previousGrade": previous_grade, "outOf": max_marks,
                "wordCount": word_count, "artefacts": inventory,
                "detail": "Readable. No AI call made, no row written."}

    adaptive = rubric_service.get_or_derive(
        tenant, "assignment", row["assignment_id"], assignment)
    if not adaptive.get("gradeable"):
        # Same standard for every learner on this task, or no mark. See
        # rubric_service.get_or_derive.
        if previous_grade is None:
            assignment_db_service.mark_not_graded(
                tenant, submission_id,
                "We could not read this task's requirements just now. Your work "
                "is saved and there are no marks yet. This is our side, not yours.")
        print(f"[REGRADE] submission {submission_id}: requirements unavailable "
              f"({adaptive.get('reason', 'unknown')}) — left ungraded")
        return {"success": False, "skipped": "requirements_unavailable",
                "submissionId": submission_id,
                "previousGrade": previous_grade,
                "artefacts": inventory,
                "detail": adaptive.get("reason", "requirements unavailable")}

    gate_overrides = ({} if adaptive.get("submissionKind") in ("written", "mixed")
                      else {"generic_answer_cap": 100})

    judge_images = intake.images_for_judge(artefacts)
    if judge_images:
        print(f"[REGRADE] handing the marker {len(judge_images)} picture(s) "
              f"of the work")

    r = review_pipeline.review_with_knowledge(
        scope_type="assignment", scope_id=row["assignment_id"],
        raw_source=assignment,
        rubric={"criteria": adaptive["criteria"],
                "fingerprint": adaptive.get("fingerprint", "")},
        student_answer=f"{manifest}\n{content}".strip(), word_count=word_count,
        word_limit_min=adaptive["wordMin"], word_limit_max=adaptive["wordMax"],
        gate_overrides=gate_overrides, student_id=row["student_id"],
        images=judge_images,
    )
    if r is None:
        raise HTTPException(status_code=503,
                            detail="Reviewer unavailable — row left unchanged.")

    # Same persistence the normal path uses, pointed at the EXISTING row.
    # attemptNumber is echoed from the row so nothing downstream invents a
    # new attempt.
    submission = {"submissionId": submission_id,
                  "attemptNumber": row.get("attempt_number") or 1}
    response = _pipeline_assignment_response(
        tenant, submission, r, word_count, start_time, max_marks=max_marks)
    response["reReviewed"] = True
    response["previousGrade"] = previous_grade
    response["artefacts"] = inventory
    # Report what was STORED, not what was computed. The old line printed the
    # response's scoreMarks even when the guard had refused the write, so the
    # log read "None -> 0.0/100" for a submission that was correctly left
    # ungraded — and anyone reading the log concluded a real essay had been
    # zeroed.
    if response.get("notGraded"):
        print(f"[REGRADE] submission {submission_id}: {previous_grade} -> NOT GRADED "
              f"(guard refused; {word_count} words, {len(artefacts)} artefact(s))")
    else:
        print(f"[REGRADE] submission {submission_id}: {previous_grade} -> "
              f"{response['feedback'].get('scoreMarks')}/{max_marks} "
              f"({word_count} words, {len(artefacts)} artefact(s))")
    return response


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
    # The headline is the score said in words. It reads the SAME rows that
    # were summed into the mark, so "you fully did 2 of the 5 things" can
    # never sit beside a number that disagrees with it. (Concept counts fed
    # this sentence until 02 Sep and were a separate model judgement — see
    # scoring_service.build_summary.)
    summary = scoring_service.build_summary(
        awarded, max_marks, requirements=scores["rubricBreakdown"])

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
    # THE RETURN VALUE IS THE POINT. update_... runs grade_guard and answers
    # False when it REFUSED to store a mark — the row is then stamped
    # not-graded with an explanation for the learner.
    #
    # This call used to discard that answer and build the response from
    # `result` regardless, so a guarded submission came back to the caller as
    # a real score: `success: true`, `scoreMarks: 0.0`, a full feedback card.
    # The database said "not graded, this is our side, not yours"; the API said
    # "zero". On the live submit path that response IS the student's screen, so
    # the guard protected the record and the learner saw the zero anyway —
    # which is the complaint the guard exists to prevent, arriving by the one
    # route the guard could not close.
    #
    # A refused mark must look refused everywhere.
    wrote = False
    try:
        wrote = assignment_db_service.update_assignment_submission_with_ai_results(
            tenant, submission["submissionId"], result, max_marks)
    except Exception as db_err:
        print(f"[ASSIGNMENT] DB update failed after pipeline review: {db_err}")

    if not wrote:
        msg = ("We could not complete a fair review of this attempt, so no "
               "marks have been recorded. This is our side, not yours — "
               "nothing you submitted is lost, and it will be reviewed again.")
        print(f"[ASSIGNMENT] ⛔ NOT GRADED: submission "
              f"{submission['submissionId']} — response carries no score")
        return {
            "success": True,
            "notGraded": True,
            "submission": submission,
            "feedback": _empty_feedback(msg, helpful=False),
            "processingTimeMs": int((time.time() - start_time) * 1000),
        }

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