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
import hashlib
from fastapi import APIRouter, HTTPException, BackgroundTasks
from app.models.schemas import SubmitAnswerRequest, TestReviewRequest, MentorApproveRequest
from app.services import ai_service, scoring_service, feedback_service, db_service
from app.services import knowledge_service, review_pipeline, prefilter_service
from app.utils.text_processor import (
    count_words, clean_text, calculate_text_overlap, find_mentioned_concepts, is_likely_copy
)
from app.utils.file_extractor import extract_text_from_url

router = APIRouter(prefix="/api/review", tags=["review"])

# Evidence-gated pipeline rollout flag. "on" = pipeline primary with legacy
# fallback on error; anything else = legacy only (instant rollback via env).
_PIPELINE_ON = os.getenv("REVIEW_PIPELINE", "on").lower() == "on"
MAX_REVIEWED_ATTEMPTS = int(os.getenv("MAX_REVIEWED_ATTEMPTS", "2"))


def _attempt_policy_block(case_study_id: int, student_id: int,
                          new_text: str) -> dict | None:
    """Re-review policy (server-enforced): max 2 reviewed attempts, and a
    re-attempt must be revised text — identical resubmission is rejected
    with zero AI cost. Returns a response dict when blocked, else None."""
    state = db_service.get_attempt_state(case_study_id, student_id)

    if state["reviewedAttempts"] >= MAX_REVIEWED_ATTEMPTS:
        return {
            "success": False, "blocked": "attempt_limit",
            "message": ("You've used your re-attempt for this case study. "
                        "Your final score stands — take the feedback into your next case study."),
        }

    if state["reviewedAttempts"] >= 1 and state["latestAnswerText"]:
        old_h = hashlib.sha256(state["latestAnswerText"].strip().lower().encode()).hexdigest()
        new_h = hashlib.sha256((new_text or "").strip().lower().encode()).hexdigest()
        if old_h == new_h:
            return {
                "success": False, "blocked": "identical_resubmission",
                "message": ("This is the same answer you already submitted. "
                            "Revise it using your feedback, then resubmit — "
                            "your one re-attempt should count."),
            }
    return None


# ── POST /api/review/submit ────────────────────────────────────────────────
@router.post("/submit")
async def submit_and_review(req: SubmitAnswerRequest, background_tasks: BackgroundTasks):
    start_time = time.time()
    print(f"ℹ️  New submission: student={req.studentId}, caseStudy={req.caseStudyId}")

    case_study = db_service.get_case_study_by_id(req.caseStudyId)
    if not case_study:
        raise HTTPException(status_code=404, detail="Case study not found or not published")

    blocked = _attempt_policy_block(req.caseStudyId, req.studentId, req.answerText or "")
    if blocked:
        print(f"ℹ️  Submission blocked: {blocked['blocked']} "
              f"(student={req.studentId}, caseStudy={req.caseStudyId})")
        return blocked

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

    # ── Reflexes: zero-token checks before any AI spend ────────────────────
    reflex = prefilter_service.check("case_study", req.caseStudyId, req.studentId, cleaned)
    if not reflex["ok"]:
        return {"success": False, "blocked": reflex["reason"],
                "message": reflex["message"]}

    text_overlap  = calculate_text_overlap(cleaned, case_study["description"])
    concept_check = find_mentioned_concepts(cleaned, case_study["keyConcepts"])

    submission = db_service.save_submission(
        req.caseStudyId, req.studentId, cleaned, word_count,
        file_url=req.fileUrl, file_name=req.fileName,
    )
    print(f"✅ Submission saved: id={submission['submissionId']}, "
          f"attempt={submission['attemptNumber']}")
    prefilter_service.record_fingerprint(
        "case_study", req.caseStudyId, req.studentId,
        submission["submissionId"], cleaned, word_count,
        text_hash=reflex.get("text_hash"))

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

    # ── Evidence-gated pipeline (primary path) ─────────────────────────────
    if _PIPELINE_ON:
        try:
            pipeline_response = _run_pipeline_review(
                case_study, req, submission, cleaned, word_count,
                text_overlap, start_time, background_tasks,
                reflex_flags=reflex.get("flags", []),
            )
            if pipeline_response is not None:
                return pipeline_response
        except Exception as e:
            print(f"⚠️  Pipeline review failed, falling back to legacy: {e}")

    # ── Legacy single-prompt path (fallback / flag off) ────────────────────
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


# ── POST /api/review/prepare/{scope_type}/{scope_id} ───────────────────────
# Optional LMS webhook: call after faculty saves/edits a question to build
# the knowledge pack immediately instead of on first student touch.
@router.post("/prepare/case_study/{case_study_id}")
async def prepare_case_study(case_study_id: int, background_tasks: BackgroundTasks):
    case_study = db_service.get_case_study_by_id(case_study_id)
    if not case_study:
        raise HTTPException(status_code=404, detail="Case study not found or not published")
    sources = knowledge_service.SOURCE_BUILDERS["case_study"](case_study)
    fresh_hash = knowledge_service.source_hash(sources)
    stored = knowledge_service.get_pack("case_study", case_study_id)
    if stored and stored["source_hash"] == fresh_hash:
        return {"success": True, "status": "ready", "version": stored["version"],
                "detail": "Knowledge already current."}
    background_tasks.add_task(
        knowledge_service.build_pack, "case_study", case_study_id, sources, fresh_hash)
    return {"success": True, "status": "building",
            "detail": "Knowledge build started. Check /knowledge-status."}


@router.get("/knowledge-status/{scope_type}/{scope_id}")
async def knowledge_status(scope_type: str, scope_id: int):
    stored = knowledge_service.get_pack(scope_type, scope_id)
    if not stored:
        return {"status": "absent"}
    return {"status": "ready", "version": stored["version"]}


def _find_capstone_deliverable(capstone_id: int, student_id: int):
    """Probe capstone-related tables for a stored file when the capstones row
    itself has none. EXISTS-style column checks only — never SELECT
    unconfirmed columns; handles uppercase information_schema keys."""
    from app.database import query
    try:
        tables = query(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name LIKE '%capstone%'")
        names = [(t.get("table_name") or t.get("TABLE_NAME")) for t in tables]
        for tbl in names:
            if not tbl or tbl == "capstones":
                continue
            cols = query(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = %s", (tbl,))
            colset = {(c.get("column_name") or c.get("COLUMN_NAME")) for c in cols}
            file_col = next((c for c in ("file_url", "file_path", "submission_file",
                                         "document_url", "file") if c in colset), None)
            link_col = next((c for c in ("capstone_id", "project_id") if c in colset), None)
            stu_col  = next((c for c in ("student_id", "user_id") if c in colset), None)
            name_col = "file_name" if "file_name" in colset else None
            if not (file_col and link_col):
                continue
            sql = (f"SELECT {file_col} AS f"
                   + (f", {name_col} AS n" if name_col else "")
                   + f" FROM {tbl} WHERE {link_col} = %s")
            params = [capstone_id]
            if stu_col:
                sql += f" AND ({stu_col} = %s OR {stu_col} = " \
                       f"(SELECT user_id FROM students WHERE id = %s LIMIT 1))"
                params += [student_id, student_id]
            sql += " LIMIT 1"
            rows = query(sql, tuple(params))
            if rows and rows[0].get("f"):
                print(f"[CAPSTONE] deliverable located via probe: table={tbl} col={file_col}")
                return rows[0]["f"], rows[0].get("n") or ""
        print(f"[CAPSTONE] probe found no deliverable for capstone {capstone_id} "
              f"in tables: {names}")
    except Exception as e:
        print(f"[CAPSTONE] deliverable probe failed: {e}")
    return None, None


def _remember_student(student_id, scope_type, scope_id, submission_id, r):
    """Fold the outcome into person-memory + stylometry trend check.
    Failures never affect the review."""
    from app.services import student_memory_service as smem
    try:
        ai_pct = r["authorship"]["aiLikelihoodPercent"]
        profile = smem.get_profile(student_id)
        if smem.authorship_shift(profile, ai_pct):
            prefilter_service.flag_exception(
                scope_type, scope_id, student_id, submission_id,
                "authorship_shift",
                f"human-styled baseline (median ~{profile['aggregates'].get('ai_median')}% AI) "
                f"suddenly reads ~{ai_pct}% AI-written")
        smem.fold_review(student_id, scope_type, scope_id,
                         r["scores"]["totalScore"], r["conceptsMissing"], ai_pct)
    except Exception as e:
        print(f"⚠️ person-memory update failed (review unaffected): {e}")


def _capstone_pipeline_response(capstone, r, word_count, start_time):
    """Persist + shape the capstone response from a pipeline result."""
    import time as _time
    from app.database import execute

    scores = r["scores"]
    grade = scoring_service.get_grade(scores["totalScore"])

    try:
        execute(
            "UPDATE capstones SET grade = %s, feedback = %s, status = 'graded' WHERE id = %s",
            (scores["totalScore"], json.dumps({
                "summary":          f"Scored {scores['totalScore']}/100 ({grade}).",
                "rubricScores":     scores["rubricBreakdown"],
                "strengths":        r["strengths"],
                "improvements":     r["improvements"],
                "detailedFeedback": r["detailedFeedback"],
                "howYouScored":     r["howYouScored"],
                "decisions":        r["decisions"],
            }, ensure_ascii=False), capstone["id"]),
        )
    except Exception as db_err:
        print(f"⚠️  Capstone DB update failed: {db_err}")

    total_time = int((_time.time() - start_time) * 1000)
    print(f"✅ Capstone pipeline review: score={scores['totalScore']} grade={grade} "
          f"path={r['decisions']['scoringPath']} time={total_time}ms")

    return {
        "success":    True,
        "submission": {"submissionId": capstone["id"], "attemptNumber": 1},
        "feedback": {
            "score":            scores["totalScore"],
            "grade":            grade,
            "summary":          f"Scored {scores['totalScore']}/100 ({grade}).",
            "rubricScores":     scores["rubricBreakdown"],
            "strengths":        r["strengths"],
            "improvements":     r["improvements"],
            "missingConcepts":  r["conceptsMissing"],
            "coveredConcepts":  r["conceptsCovered"],
            "suggestions":      [],
            "detailedFeedback": r["detailedFeedback"],
            "wordCount":        word_count,
            "wordCountMessage": scores["wordCountNote"],
            "encouragement":    "",
            "howYouScored":     r["howYouScored"],
            "languageReport":   r["languageReport"],
            "factualErrors":    r["factualErrors"],
            "isGarbage":        r["isGarbage"],
            "garbageWarning":   r["garbageWarning"],
            **r["authorship"],
        },
        "_meta":            {"pipeline": r["decisions"]},
        "mentorReport": {
            "score": scores["totalScore"], "grade": grade,
            "needsAttention": scores["totalScore"] < 40 or r["isGarbage"],
            "gatesHit": scores["gatesHit"],
            "rubricBreakdown": scores["rubricBreakdown"],
            "aiLikelihoodPercent": r["authorship"]["aiLikelihoodPercent"],
        },
        "processingTimeMs": total_time,
    }


def _run_pipeline_review(case_study, req, submission, cleaned, word_count,
                         text_overlap, start_time, background_tasks,
                         reflex_flags=None):
    """Evidence-gated review: recall the knowledge pack, run the staged
    pipeline, persist, respond. Returns the response dict, or None when no
    pack could be built (caller falls back to legacy)."""
    r = review_pipeline.review_with_knowledge(
        scope_type="case_study", scope_id=req.caseStudyId,
        raw_source=case_study, rubric=case_study["gradingRubric"],
        student_answer=cleaned, word_count=word_count,
        word_limit_min=case_study["wordLimitMin"],
        word_limit_max=case_study["wordLimitMax"],
        background_tasks=background_tasks, student_id=req.studentId,
    )
    if r is None:
        return None
    scores = r["scores"]
    _remember_student(req.studentId, "case_study", req.caseStudyId,
                      submission["submissionId"], r)
    grade = scoring_service.get_grade(scores["totalScore"])

    plagiarism = "low"
    if text_overlap >= 35:
        plagiarism = "high" if text_overlap >= 60 else "medium"
    if any(f.get("flag") == "cohort_duplicate" for f in (reflex_flags or [])):
        plagiarism = "high"  # identical to another student's submission

    prefilter_service.flag_review_outcomes(
        "case_study", req.caseStudyId, req.studentId,
        submission["submissionId"], r)

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
        "plagiarismFlag":   plagiarism,
        "needsMentorHelp":  scores["totalScore"] < 40 or r["isGarbage"]
                            or r["authorship"]["aiLikelihoodPercent"] >= 90,
        "scoreEmoji":       "",
        "summary":          summary,
        "encouragement":    "",
        "isGarbage":        r["isGarbage"],
        "garbageWarning":   r["garbageWarning"],
        **r["authorship"],
    }

    try:
        db_service.update_submission_with_ai_results(submission["submissionId"], result)
        db_service.update_performance_tracker(req.studentId, req.caseStudyId, scores["totalScore"])
        db_service.log_ai_review(submission["submissionId"],
                                 {"pipeline": r["decisions"]}, r)
    except Exception as db_err:
        print(f"⚠️  DB update failed after pipeline review: {db_err} — returning result anyway")

    total_time = int((time.time() - start_time) * 1000)
    print(f"✅ Pipeline review: score={scores['totalScore']} grade={grade} "
          f"path={r['decisions']['scoringPath']} gates={len(scores['gatesHit'])} "
          f"ai={r['authorship']['aiLikelihoodPercent']}% time={total_time}ms")

    return {
        "success":    True,
        "submission": submission,
        "feedback": {
            "score":            scores["totalScore"],
            "grade":            grade,
            "scoreEmoji":       "",
            "summary":          summary,
            "rubricScores":     scores["rubricBreakdown"],
            "strengths":        r["strengths"],
            "improvements":     r["improvements"],
            "missingConcepts":  r["conceptsMissing"],
            "coveredConcepts":  r["conceptsCovered"],
            "suggestions":      [],
            "detailedFeedback": r["detailedFeedback"],
            "wordCount":        word_count,
            "wordCountMessage": scores["wordCountNote"],
            "encouragement":    "",
            "howYouScored":     r["howYouScored"],
            "languageReport":   r["languageReport"],
            "factualErrors":    r["factualErrors"],
            "isGarbage":        r["isGarbage"],
            "garbageWarning":   r["garbageWarning"],
            **r["authorship"],
        },
        "_meta":            {"pipeline": r["decisions"]},
        "mentorReport": {
            "score": scores["totalScore"], "grade": grade,
            "needsAttention": result["needsMentorHelp"],
            "plagiarismRisk": plagiarism,
            "gatesHit": scores["gatesHit"],
            "rubricBreakdown": scores["rubricBreakdown"],
            "aiLikelihoodPercent": r["authorship"]["aiLikelihoodPercent"],
        },
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
    # DUAL_ID_MATCH: Coursework-module submissions can be stored under
    # users.id while AiRev is called with students.id — match either.
    # Same fix family as assignments (live finding 19 Jul).
    from app.database import query, DUAL_ID_MATCH
    rows = query(
        f"""SELECT cs.id, cs.course_id, cs.title, cs.description, cs.due_date,
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
                   SELECT case_study_id, MAX(submitted_at) AS max_at
                   FROM case_study_submissions
                   WHERE {DUAL_ID_MATCH}
                   GROUP BY case_study_id
               ) s2
                 ON s1.case_study_id = s2.case_study_id
                AND s1.submitted_at  = s2.max_at
               WHERE s1.{DUAL_ID_MATCH}
           ) latest
             ON latest.case_study_id = cs.id
           WHERE cs.status IN ('published', 'active')
           ORDER BY cs.created_at DESC""",
        (student_id,) * 6,   # DUAL_ID_MATCH used twice, 3 params each
    )
    if rows and not any(r.get("submission_id") for r in rows):
        from app.database import query as q2
        subs = q2("SELECT COUNT(*) AS n FROM case_study_submissions "
                  f"WHERE {DUAL_ID_MATCH}", (student_id,) * 3)
        print(f"ℹ️  Case-study hub: {len(rows)} studies, ZERO submissions matched "
              f"for student {student_id} (either id). Rows under both ids: "
              f"{subs[0]['n'] if subs else '?'}. If Coursework shows a submission, "
              f"the LMS writes case studies to a different table.")

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
        # Resolve relative LMS paths (e.g. /uploads/capstone/foo.pdf) to full URL.
        # The LMS backend serves /uploads as static files. Cloudinary URLs already
        # start with https:// so this is a no-op for those.
        resolved_url = file_url
        if file_url.startswith("/"):
            resolved_url = "https://upskillize-lms-backend.onrender.com" + file_url
            print(f"[CAPSTONE] resolved relative path to: {resolved_url}")
        from app.utils.file_extractor import extract_text_from_url
        extracted, why = extract_text_from_url(resolved_url, file_name or "")
        if extracted:
            print(f"📄 Extracted capstone file: {file_name} ({len(extracted.split())} words)")
            parts.append(extracted)
        elif why:
            print(f"📄 Capstone file extraction failed: {why}")

    # Fallback 2: the capstones row can lack file_url — some LMS versions
    # store uploads in a separate table. Probe candidates dynamically
    # (playbook: interrogate the data before suspecting the code).
    if not parts and not answer_text and not file_url and not capstone.get("file_url"):
        found_url, found_name = _find_capstone_deliverable(capstone_id, student_id)
        if found_url:
            if found_url.startswith("/"):
                found_url = "https://upskillize-lms-backend.onrender.com" + found_url
            from app.utils.file_extractor import extract_text_from_url
            extracted, why = extract_text_from_url(found_url, found_name or "")
            if extracted:
                print(f"📄 Extracted deliverable found by table probe: {found_url[:100]}")
                parts.append(extracted)
            elif why:
                print(f"📄 Probed deliverable extraction failed: {why}")

    # Fallback: read previously-saved capstone file
    if not parts and capstone.get("file_url"):
        prior_url = capstone["file_url"]
        if prior_url.startswith("/"):
            prior_url = "https://upskillize-lms-backend.onrender.com" + prior_url
            print(f"[CAPSTONE] fallback resolved relative path to: {prior_url}")
        from app.utils.file_extractor import extract_text_from_url
        extracted, why = extract_text_from_url(prior_url, "")
        if extracted:
            print(f"📄 Extracted prior capstone file: {prior_url}")
            parts.append(extracted)
        elif why:
            print(f"📄 Prior capstone file extraction failed: {why}")

    if not parts:
        # State exactly which sources were empty — a "no content" mystery
        # costs an hour; this line costs nothing. (Live finding 19 Jul:
        # capstone 39 row had no file_url despite an LMS submission.)
        print(f"[CAPSTONE] no content for capstone {capstone_id}: "
              f"request answerText={'yes' if answer_text else 'EMPTY'}, "
              f"request fileUrl={'yes' if file_url else 'EMPTY'}, "
              f"row file_url={capstone.get('file_url') or 'EMPTY'} — "
              f"check where the LMS stored this student's deliverable")
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

    # ── Evidence-gated pipeline (primary path — capstones get full rigor) ──
    if _PIPELINE_ON:
        try:
            r = review_pipeline.review_with_knowledge(
                scope_type="capstone", scope_id=capstone["id"],
                raw_source={**capstone, "gradingRubric": default_rubric},
                rubric=default_rubric, student_answer=cleaned,
                word_count=word_count, word_limit_min=0, word_limit_max=999999,
                student_id=student_id,
            )
            if r is not None:
                _remember_student(student_id, "capstone", capstone["id"],
                                  capstone["id"], r)
                return _capstone_pipeline_response(capstone, r, word_count, start_time)
        except Exception as e:
            print(f"⚠️  Capstone pipeline failed, falling back to legacy: {e}")

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