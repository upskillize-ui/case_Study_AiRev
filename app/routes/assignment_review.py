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
    review_pipeline,
    prefilter_service,
    rubric_service,
    student_notices,
    grade_guard,
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


def scoring_knobs(adaptive: dict) -> tuple[dict, int, int]:
    """(gate_overrides, word_min, word_max) for this task's submission kind.

    Written/mixed tasks keep their derived word limits and every gate — an
    essay may fairly be told it is too short or too long.

    Non-written tasks (image, deliverable, link) get the limits WAIVED
    (0 / 999999, the capstone idiom), not just lowered. The derived limits
    describe a typed caption, but word_count counts everything readable —
    including OCR text extracted from the learner's file. Student 405's
    image carried a complete 494-word plan and was fined 5 marks for
    'exceeding' a 150-word caption guide: the learner never wrote past any
    limit, we read a thorough deliverable and then charged her for its
    thoroughness. The generic-answer gate stays off for the same reason it
    always was (criterion-name matching misfires on non-written work).
    """
    if adaptive.get("submissionKind") in ("written", "mixed"):
        return {}, adaptive["wordMin"], adaptive["wordMax"]
    return {"generic_answer_cap": 100}, 0, 999999


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
        # The task text decides whether a link is WALKED (a built site, an
        # app, a game) or merely read. Passing it here is what makes Lovable
        # day judgeable on whether the thing works.
        artefacts.extend(intake.from_links_in(
            cleaned_typed,
            task_text=f"{assignment.get('title', '')} "
                      f"{assignment.get('description', '')}"))

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
        # One wording for this fault, everywhere it can happen — see
        # app/services/student_notices.py for why these left the routes.
        # A recording that could not be transcribed is its own fault with its
        # own fix — "re-attach the file" is useless advice when the file
        # arrived intact and we simply could not turn it into words.
        from app.services.submission_media import is_media
        if file_error and is_media(req.fileName or ""):
            kind = "video" if (req.fileName or "").lower().rsplit(".", 1)[-1] in \
                ("mp4", "mov", "avi", "mkv", "webm", "m4v", "3gp", "wmv", "flv") \
                else "audio"
            msg = student_notices.media_not_transcribed(kind)
        elif file_error:
            msg = student_notices.file_unreadable(file_error, req.fileName or "")
        else:
            msg = student_notices.nothing_submitted()
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
        msg = student_notices.too_little_content(
            word_count, file_error,
            had_attachment=bool(req.fileData or req.fileUrl or req.fileName))
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

    # A deliverable we could not open, with nothing readable beside it, must
    # not be scored — a mark there would measure OUR reach, not their work
    # (the Day 06 Suno failure: URL-only rows pinned at the no-evidence cap).
    # The regrade path has refused this since 19 Aug; the LIVE submit path
    # did not, and Day 02's deliverable is a Claude artifact link — a page
    # that only opens in a browser. Caught HERE, at submit time, the learner
    # can fix it in the same sitting instead of days later from a sweep.
    # Same ruling at submit time, where the learner can still fix it in the
    # same sitting: a publish-this task whose link never opened is not graded.
    if not req.storeOnly and intake.link_is_the_deliverable(
            f"{assignment.get('title', '')} {assignment.get('description', '')}"
    ) and intake.link_deliverable_unseen(artefacts):
        # The steps must match the tool THIS learner used. Sending Notion's
        # Share -> Publish to someone whose link was a Gemini share taught
        # them nothing and looked like we had not read their submission.
        msg = student_notices.link_never_opened(
            next((a.label for a in artefacts if a.kind == "link"), ""))
        print(f"[ASSIGNMENT] NOT SCORED (published link never opened): "
              f"{word_count} words typed beside an unopenable link")
        return {
            "success": True,
            "status": "needs_input",
            "needsInput": True,
            "submission": {"submissionId": 0, "attemptNumber": 0},
            "feedback": _empty_feedback(msg, helpful=True),
            "processingTimeMs": int((time.time() - start_time) * 1000),
        }

    if not req.storeOnly and intake.is_unassessable(manifest, content):
        msg = student_notices.link_opens_only_in_a_browser(
            next((a.label for a in artefacts if a.kind == "link"), ""))
        print(f"[ASSIGNMENT] NOT SCORED (unassessable deliverable): "
              f"{word_count} words beside an unreadable link/file — no review run")
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
    # AUDIT: `assignment` is ALSO the knowledge-pack source. Mutating it here
    # changed the pack's content hash and staled every pack, so the derived
    # rubric travels separately and the task text stays untouched.
    #
    # Gates + word limits both depend on the task's submission kind — one
    # decision, made once, in scoring_knobs() (shared with the regrade route).
    gate_overrides, word_min, word_max = scoring_knobs(adaptive)

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
                # What the learner MADE, as pictures — their uploads first,
                # then anything we rendered on their behalf.
                images=intake.images_for_judge(artefacts),
            )
            # THE WRONG LINK IS NOT A ZERO. Day 07: student 880 pasted
            # their Day-06 Suno song and student 188 pasted Gemini's own
            # advertisement page. The marker diagnosed both correctly — and
            # then the garbage path awarded 0.00/10 to each. Policy (22 Aug)
            # is that work we cannot judge gets NO grade and an explanation,
            # so the learner can send the right link tonight. A zero teaches
            # them nothing and cannot be undone from their side.
            if (r is not None and r.get("isGarbage")
                    and intake.link_is_the_deliverable(
                        f"{assignment.get('title', '')} "
                        f"{assignment.get('description', '')}")
                    and intake.deliverable_is_only_links(artefacts)):
                why = (r.get("garbageWarning") or "").strip()
                print(f"[ASSIGNMENT] NOT GRADED (link is not this task's "
                      f"work): {why[:120]} — no mark written")
                return {
                    "success": True,
                    "status": "needs_input",
                    "needsInput": True,
                    "submission": submission,
                    "feedback": _empty_feedback(
                        student_notices.link_is_not_the_work(
                            r.get("garbageReason")
                            or r.get("garbage_reason") or "", 
                            assignment.get("title", "")),
                        helpful=True),
                    "processingTimeMs": int((time.time() - start_time) * 1000),
                }
            if r is not None and r.get("wrongTask", {}).get("declared"):
                # Policy: wrong work is NOT graded — no score, low or
                # otherwise. The row stays status='submitted' with no grade
                # (the upsert already cleared any old one), so the learner can
                # attach the right work and the item remains reviewable.
                what = r["wrongTask"]["whatItIs"] or "work for a different task"
                print(f"[ASSIGNMENT] NOT GRADED (wrong task): {what} — "
                      f"no mark written")
                return {
                    "success": True,
                    "status": "wrong_task",
                    "needsInput": True,
                    "submission": submission,
                    "feedback": _empty_feedback(
                        student_notices.wrong_task(what, assignment["title"]),
                        helpful=True),
                    "processingTimeMs": int((time.time() - start_time) * 1000),
                }
            if r is not None:
                prefilter_service.flag_review_outcomes(
                    "assignment", req.assignmentId, req.studentId,
                    submission["submissionId"], r)
                _remember_student_assignment(req, submission, r)
                return _pipeline_assignment_response(
                    tenant, submission, r, word_count, start_time,
                    manifest=manifest,
                    duplicate=any(f.get("flag") == "cohort_duplicate"
                                  for f in reflex.get("flags", [])),
                    max_marks=max_marks)
        except Exception as e:
            # OUR OUTAGE MUST NOT BECOME THEIR GRADE. When the model never
            # answered — rate limit, timeout, no structured result — the
            # legacy marker often succeeds on the same input and writes a
            # number nobody judged. Falling back is right for a SHAPE
            # problem, never for an outage.
            if grade_guard.is_transport_failure(e):
                print(f"[ASSIGNMENT] NOT GRADED (reviewer unavailable): {e} — "
                      f"no mark written, submission stored")
                return {
                    "success": True,
                    "partialReview": True,
                    "status": "needs_input",
                    "submission": submission,
                    "feedback": _empty_feedback(
                        "Your work is saved. Our reviewer was unavailable just "
                        "now, so no marks have been recorded — this is our "
                        "side, not yours, and nothing you submitted is lost. "
                        "Your review will run automatically and appear here.",
                        helpful=True),
                    "message": "Saved. The review will run shortly.",
                    "processingTimeMs": int((time.time() - start_time) * 1000),
                }
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
    agent_markers = ("rubricScores", "facultyView", "aiLikelihoodPercent",
                     "howYouScored", "authorship", "detailedFeedback")
    return not any(k in fb for k in agent_markers)


def _tell_student_why(tenant, submission_id: int, message: str) -> None:
    """Write a student-side blocker onto the student's own review card.

    Policy (Ranjana, 19 Aug): "if it student side fault show them what is the
    issue so they can re-submit or next time don't repeat same issue." Only
    ever called for rows with NO grade — a row holding a real review must
    never have it replaced by an explanation of a later failed read. Failure
    to write the note must never break the skip response it accompanies.
    """
    try:
        assignment_db_service.mark_not_graded(
            tenant, submission_id, message,
            card=_empty_feedback(message, helpful=False))
    except Exception as e:
        print(f"[REGRADE] could not write student note on {submission_id}: {e}")


def _prior_word_count(row: dict) -> int:
    """How many words the row's LAST stored review actually read (0 if none).

    review_payload.build records wordCount in every feedback blob, so a graded
    row carries a receipt of how much content its review was based on.
    """
    blob = row.get("feedback")
    if not blob:
        return 0
    try:
        fb = json.loads(blob) if isinstance(blob, str) else blob
        return int(fb.get("wordCount") or 0) if isinstance(fb, dict) else 0
    except (ValueError, TypeError):
        return 0


def content_shrunk(prior_words: int, current_words: int) -> bool:
    """True when this regrade read materially less than the stored review saw.

    Live case, 19 Aug: student 1233 held a 7.1/10 from a run that read her
    image in full; a batch regrade re-fetched the image, got much less back
    (intermittent CDN/OCR), scored the remnant 2.7 and OVERWROTE the honest
    mark. Reading less than half of what a prior review read is not a changed
    judgement — it is a degraded copy of the input, and a degraded copy must
    never replace a mark earned on the full one.

    prior >= 60 keeps the guard off caption-sized rows, where a few words of
    natural OCR variance would trip a ratio test.
    """
    return prior_words >= 60 and current_words < prior_words * 0.5


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
    stored_file = row.get("file_path") or row.get("file_url")

    # A stored assembly that RECORDS a failed read, while the source file is
    # still on record, is not a result — it is a snapshot of the failure.
    # Reusing it replays that failure forever: the 21 Aug probe found ~110
    # "unreadable" rows whose files existed and served bytes the whole time
    # (transient fetches, the media-type bug). Discard the snapshot and read
    # the source again; only the learner's own TYPED TEXT blocks carry over,
    # never the old manifest (the 1126 nesting rule).
    if (already_assembled and stored_file
            and intake.records_failed_read(already_assembled[0])):
        print(f"[REGRADE] submission {submission_id}: stored assembly records "
              f"a failed read and the file is still on record — re-extracting")
        stored_notes = intake.typed_text_from(already_assembled[1])
        already_assembled = None

    if already_assembled:
        manifest, content = already_assembled
    else:
        if stored_file:
            artefacts.append(intake.from_stored_file(stored_file, row.get("file_name") or ""))
        if stored_notes:
            artefacts.append(intake.from_typed(stored_notes))
            # Open what the learner linked to. The submit path has always done
            # this; the regrade path did not, so a Day 06 row whose whole
            # submission is a suno.com link had that URL marked as if it were
            # the learner's prose — nothing to quote, every criterion pinned at
            # the no-evidence cap, a cohort that did the work told it scored
            # 2/10. Same call, same guards (url_guard, link budget) as submit.
            artefacts.extend(intake.from_links_in(
                stored_notes,
                task_text=f"{assignment.get('title', '')} "
                          f"{assignment.get('description', '')}"))
        manifest, content = intake.render(artefacts)
    word_count = count_words(content)
    inventory = ([{"kind": "stored", "label": "previously assembled submission",
                   "words": len(content.split()), "readable": bool(content),
                   "note": "reused; not re-extracted"}]
                 if already_assembled else
                 [{"kind": a.kind, "label": a.label,
                   "words": len(a.text.split()) if a.readable else 0,
                   "readable": a.readable, "note": a.note} for a in artefacts])

    if not content:
        # Rule 2. The grade (if any) stays; but a NEVER-graded row is a
        # student-side blocker the student cannot see from a staff CSV —
        # write the reason onto their card so they know to resubmit.
        print(f"[REGRADE] submission {submission_id}: nothing readable "
              f"({intake.first_error(artefacts) or 'no stored work'}) — row untouched")
        if previous_grade is None:
            # Simple English by policy: short words, one problem, one fix.
            _tell_student_why(tenant, submission_id, (
                "We could not open your file, and there was no written "
                "answer. No marks given yet. Please upload your work again "
                "(image, PDF or Word), or type your answer in the box, then "
                "click Submit."))
        return {"success": False, "skipped": "no_readable_content",
                "submissionId": submission_id,
                "previousGrade": previous_grade,
                "artefacts": inventory,
                "detail": intake.first_error(artefacts) or "no stored work found"}

    # Ranjana's ruling, 22 Aug (Day 04): when the DELIVERABLE is the published
    # page itself, a typed paragraph beside a link that will not open is a
    # description OF the work, not the work. 21 rows were marked 0.0-4.7 while
    # their own feedback said the page could not be read. A withheld mark can
    # still become a real score tonight; a recorded 2.5 cannot.
    task_text = f"{assignment.get('title', '')} {assignment.get('description', '')}"
    if (intake.link_is_the_deliverable(task_text)
            and intake.link_deliverable_unseen(artefacts)):
        print(f"[REGRADE] submission {submission_id}: published link never "
              f"opened on a publish-this task — no mark, row untouched")
        if previous_grade is None:
            _tell_student_why(tenant, submission_id, student_notices.link_never_opened(
                next((a.label for a in artefacts if a.kind == "link"), "")))
        return {"success": False, "skipped": "unreadable_published_link",
                "submissionId": submission_id,
                "previousGrade": previous_grade,
                "artefacts": inventory,
                "detail": ("The task asks for a published page and the link "
                           "submitted never opened — it asks visitors to sign "
                           "in. No mark: the work was never seen. The learner "
                           "must publish the page and resubmit the public link.")}

    if intake.is_unassessable(manifest, content):
        # The deliverable exists; we could not open it. Scoring it anyway is an
        # assertion about work nobody read — the fabrication rule, pointed the
        # other way. Leave the row untouched and say so plainly, so the learner
        # is asked for a description rather than handed a mark they didn't earn.
        print(f"[REGRADE] submission {submission_id}: deliverable present but "
              f"unreadable ({intake.substantive_words(content)} words of answer) "
              f"— row untouched")
        if previous_grade is None:
            _tell_student_why(tenant, submission_id,
                              student_notices.link_opens_only_in_a_browser(
                                  next((a.label for a in artefacts
                                        if a.kind == "link"), "")))
        return {"success": False, "skipped": "unassessable_deliverable",
                "submissionId": submission_id,
                "previousGrade": previous_grade,
                "artefacts": inventory,
                "detail": ("The work was submitted as a link or file we could not "
                           "open, and there is no written answer to judge. Ask the "
                           "learner to add a few lines describing what they made "
                           "and how, then re-review.")}

    # Rule 2, extended: reading LESS than the stored review saw is a fetch
    # problem, not a performance change — refuse to replace a mark earned on
    # the full input with one scored on a degraded copy. See content_shrunk.
    prior_words = _prior_word_count(row)
    if content_shrunk(prior_words, word_count) and not force:
        print(f"[REGRADE] submission {submission_id}: content shrank "
              f"{prior_words} -> {word_count} words — row untouched "
              f"(pass force=true to override)")
        return {"success": False, "skipped": "content_shrunk",
                "submissionId": submission_id,
                "previousGrade": previous_grade,
                "artefacts": inventory,
                "detail": (f"The stored review was based on {prior_words} words of "
                           f"readable content; this attempt could only read "
                           f"{word_count}. The file likely failed to fetch in "
                           f"full — the existing grade stands. Re-run later, or "
                           f"force=true to overwrite anyway.")}

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
    gate_overrides, word_min, word_max = scoring_knobs(adaptive)

    r = review_pipeline.review_with_knowledge(
        scope_type="assignment", scope_id=row["assignment_id"],
        raw_source=assignment, rubric={"criteria": adaptive["criteria"]},
        student_answer=f"{manifest}\n{content}".strip(), word_count=word_count,
        word_limit_min=word_min, word_limit_max=word_max,
        gate_overrides=gate_overrides, student_id=row["student_id"],
        images=intake.images_for_judge(artefacts),
    )
    if r is None:
        raise HTTPException(status_code=503,
                            detail="Reviewer unavailable — row left unchanged.")

    if r.get("wrongTask", {}).get("declared"):
        # Wrong work carries NO grade — including the wrong low one it may
        # hold from before this rule existed (student 1151's investment deck
        # was scored 1.2/10 on the 5-year-plan task; policy says it should
        # never have been scored at all). Clear the mark, tell the learner
        # what arrived, leave the row 'submitted' so the right work can come.
        what = r["wrongTask"]["whatItIs"] or "work for a different task"
        # Same wording the submit path uses — a learner who re-submits must
        # not be told two different stories about one fault.
        wrong_msg = student_notices.wrong_task(what, assignment.get("title", ""))
        assignment_db_service.mark_not_graded(
            tenant, submission_id, wrong_msg,
            card=_empty_feedback(wrong_msg, helpful=False))
        print(f"[REGRADE] submission {submission_id}: NOT GRADED (wrong task: "
              f"{what}) — grade cleared, was {previous_grade}")
        return {"success": False, "skipped": "wrong_task",
                "submissionId": submission_id,
                "previousGrade": previous_grade,
                "artefacts": inventory,
                "detail": (f"Recognized as {what}, not this assignment's task. "
                           f"Grade cleared per policy — wrong work is not "
                           f"graded. The learner should resubmit the correct "
                           f"deliverable.")}

    # Same persistence the normal path uses, pointed at the EXISTING row.
    # attemptNumber is echoed from the row so nothing downstream invents a
    # new attempt.
    submission = {"submissionId": submission_id,
                  "attemptNumber": row.get("attempt_number") or 1}
    response = _pipeline_assignment_response(
        tenant, submission, r, word_count, start_time, max_marks=max_marks,
        manifest=manifest)
    response["reReviewed"] = True
    response["previousGrade"] = previous_grade
    response["artefacts"] = inventory
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
                                  duplicate=False, max_marks: int = 100,
                                  manifest: str = ""):
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
        # Rubric framework is FACULTY-facing (policy, 18 Aug): the student
        # card shows total marks + pointwise feedback only; the per-criterion
        # table and scoring narrative live under facultyView, where the mentor
        # dashboard and any human re-review can still read them. Internal
        # scoring is unchanged — this moves presentation, not judgement.
        "facultyView":      {"rubricScores": scoring_service.scale_rubric(
                                 scores["rubricBreakdown"], max_marks),
                             "howYouScored": r["howYouScored"]},
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
            tenant, submission["submissionId"], result, max_marks,
            manifest=manifest)
    except Exception as db_err:
        print(f"[ASSIGNMENT] DB update failed after pipeline review: {db_err}")

    print(f"[ASSIGNMENT] ✅ Pipeline review: score={scores['totalScore']} grade={grade} "
          f"path={r['decisions']['scoringPath']} gates={len(scores['gatesHit'])}")

    response = _build_response(submission, result, summary, start_time, max_marks)
    # languageReport and factualErrors stay student-facing: grammar fixes and
    # factual corrections are feedback the learner can act on. The scoring
    # narrative (howYouScored) moved to facultyView with the rubric table.
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
            # No rubricScores here: the student card is total marks +
            # pointwise feedback (policy, 18 Aug). Rubric detail, if any,
            # travels under facultyView for staff surfaces only.
            "facultyView":            result.get("facultyView")
                                      or ({"rubricScores": result["rubricScores"]}
                                          if result.get("rubricScores") else {}),
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