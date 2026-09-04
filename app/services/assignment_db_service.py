# app/services/assignment_db_service.py
# ---------------------------------------------------------------------------
# DB queries for assignments (separate table from case studies).
#
# CHANGED (18 Aug 2026):
#   - save_assignment_submission: atomic upsert, ONE row per
#     (assignment_id, student_id). The production DB now carries
#     UNIQUE KEY uq_submission (assignment_id, student_id) — added to
#     close the LMS duplicate-row race — so the previous
#     INSERT-per-attempt design threw 1062 on every resubmission made
#     through AiRev's own UI (live log: "Duplicate entry '17-731' for
#     key 'assignment_submissions.uq_submission'"). Latest-only is now
#     the policy AND the schema: a resubmit replaces the row in place,
#     exactly like the LMS Coursework handler.
#   - get_assignment_history: still returns rows newest first; with the
#     unique key there is at most one row per student, so "history" is
#     the latest attempt. Kept for the frontend contract.
#
# Multi-tenant: every function takes an explicit `tenant` argument and uses
# tquery/texecute, NOT query/execute. Tenant is passed through the call chain
# explicitly so we can never accidentally hit the wrong DB.
# ---------------------------------------------------------------------------

import json
from datetime import datetime, timezone

from app.database import tquery, texecute
from app.tenants import Tenant
from app.services import review_payload


# ---------- READ ----------------------------------------------------------

def get_assignment_by_id(tenant: Tenant, assignment_id: int) -> dict | None:
    """Read an assignment from the given tenant's DB."""
    rows = tquery(
        tenant,
        "SELECT * FROM assignments WHERE id = %s AND status = 'active'",
        (assignment_id,),
    )
    if not rows:
        return None
    a = rows[0]

    def jload(val, default):
        if val is None:
            return default
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return default
        return val

    raw_rubric = jload(a.get("rubric"), [])

    if isinstance(raw_rubric, list):
        criteria = []
        for c in raw_rubric:
            if isinstance(c, dict):
                max_score = c.get("maxScore", c.get("points", c.get("max", 25)))
                criteria.append({
                    "name": c.get("name", c.get("criterion", "Criterion")),
                    "maxScore": int(max_score),
                    "weight": float(max_score) / 100,
                })
            elif isinstance(c, str):
                criteria.append({"name": c, "maxScore": 25, "weight": 0.25})
    elif isinstance(raw_rubric, dict):
        criteria = raw_rubric.get("criteria", [])
        if not criteria:
            criteria = [
                {"name": k, "maxScore": int(v), "weight": float(v) / 100}
                for k, v in raw_rubric.items()
                if isinstance(v, (int, float))
            ]
    else:
        criteria = []

    if not criteria:
        criteria = [
            {"name": "Accuracy",      "maxScore": 30, "weight": 0.30},
            {"name": "Completeness",  "maxScore": 25, "weight": 0.25},
            {"name": "Reasoning",     "maxScore": 25, "weight": 0.25},
            {"name": "Presentation",  "maxScore": 20, "weight": 0.20},
        ]

    return {
        "id": a["id"],
        "courseId": a.get("course_id"),
        "title": a.get("title", ""),
        "description": a.get("description", "") or "",
        "questions": [],
        "modelAnswers": [],
        "gradingRubric": {"criteria": criteria},
        "keyConcepts": [],
        "maxScore": a.get("total_marks", 100),
        "wordLimitMin": 100,
        "wordLimitMax": 1500,
        "deadline": a.get("due_date"),
        "facultyId": a.get("faculty_id"),
    }


def get_all_assignments(tenant: Tenant, course_id: int) -> list:
    return tquery(
        tenant,
        "SELECT id, course_id, title, status, total_marks, due_date, created_at "
        "FROM assignments WHERE course_id = %s AND status = 'active' "
        "ORDER BY due_date IS NULL, due_date ASC, created_at DESC",
        (course_id,),
    )


def get_latest_assignment_submission(tenant: Tenant, assignment_id: int, student_id: int) -> dict | None:
    if not student_id or student_id <= 0:
        return None

    from app.database import DUAL_ID_MATCH
    rows = tquery(
        tenant,
        f"SELECT id, file_path, file_name, notes, status "
        f"FROM assignment_submissions "
        f"WHERE assignment_id = %s AND {DUAL_ID_MATCH} "
        f"ORDER BY submitted_at DESC LIMIT 1",
        (assignment_id, student_id, student_id, student_id),
    )
    if not rows:
        return None
    row = rows[0]
    return {
        "id": row["id"],
        "file_url": row.get("file_path"),
        "file_name": row.get("file_name"),
        "notes": row.get("notes"),
    }


# ---------- WRITE ---------------------------------------------------------

def get_attempt_state(tenant: Tenant, assignment_id: int, student_id: int) -> dict:
    """Attempt count + latest answer text for the re-review policy.

    A submission only counts once it was actually reviewed (status='graded').
    The AI-unavailable path leaves status='submitted', so it never consumes
    the student's single re-attempt.
    """
    from app.database import DUAL_ID_MATCH
    rows = tquery(
        tenant,
        f"""SELECT notes, status FROM assignment_submissions
           WHERE assignment_id = %s AND {DUAL_ID_MATCH}
           ORDER BY id DESC""",
        (assignment_id, student_id, student_id, student_id),
    )
    reviewed = sum(1 for r in rows if r.get("status") == "graded")
    return {
        "reviewedAttempts": reviewed,
        "latestAnswerText": (rows[0].get("notes") or "") if rows else "",
    }


def save_assignment_submission(
    tenant: Tenant,
    assignment_id: int,
    student_id: int,
    answer_text: str | None,
    file_url: str | None,
    file_name: str | None,
) -> dict:
    """
    Atomic upsert: ONE row per (assignment_id, student_id), enforced by the
    DB's UNIQUE KEY uq_submission. A resubmit replaces the row's content and
    clears the stale grade/feedback so the new review can't sit beside an old
    score. Mirrors the LMS Coursework handler — the two writers now share one
    shape, so neither can 1062 the other.

    `id = LAST_INSERT_ID(id)` is load-bearing: on the UPDATE branch of an
    upsert, cursor.lastrowid is otherwise meaningless — this trick makes
    texecute() return the EXISTING row's id, which the pipeline then writes
    the AI results into. Without it, a resubmit would grade the wrong row.

    attemptNumber: with one row per pair, row-counting can no longer number
    attempts. The value is display-only (log line + response echo); the
    re-review policy in the route reads status/notes BEFORE this write and is
    unaffected. Reported as 1 — the row IS the current attempt.
    """
    submission_id = texecute(
        tenant,
        """INSERT INTO assignment_submissions
           (assignment_id, student_id, notes, file_path, file_name,
            status, submitted_at)
           VALUES (%s, %s, %s, %s, %s, 'submitted', NOW())
           ON DUPLICATE KEY UPDATE
             id           = LAST_INSERT_ID(id),
             notes        = VALUES(notes),
             -- COALESCE: this upsert shares the row with the LMS Coursework
             -- writer, which stores the upload's URL FIRST. The agent's
             -- storeOnly call historically carried no fileUrl, so a plain
             -- VALUES() here overwrote that URL with NULL seconds after it
             -- was saved — the source of every "text but no stored file"
             -- row. A NULL from any writer must never erase a stored file.
             file_path    = COALESCE(VALUES(file_path), file_path),
             file_name    = COALESCE(VALUES(file_name), file_name),
             status       = 'submitted',
             submitted_at = NOW(),
             grade        = NULL,
             feedback     = NULL""",
        (assignment_id, student_id, answer_text or "", file_url, file_name),
    )

    return {"submissionId": submission_id, "attemptNumber": 1}


def _criteria_rows(result: dict) -> list:
    """Wherever this writer put the per-criterion table. Pure.

    The pipeline files it under facultyView; the legacy path leaves it at the
    top level. The guard must see it either way — a check that silently finds
    nothing would pass everything.
    """
    faculty = result.get("facultyView") or {}
    return (faculty.get("requirements")
            or faculty.get("rubricScores")      # pre-23-Aug writers
            or result.get("rubricScores")
            or [])


def update_assignment_submission_with_ai_results(tenant: Tenant, submission_id: int,
                                                result: dict, max_marks: int = 100,
                                                manifest: str = "",
                                                course_id=None) -> bool:
    """Persist the review, or refuse to. Returns True when a mark was written.

    `grade` is written in the ASSIGNMENT's own marks scale, not as a raw 0-100
    percentage. The rubric engine always works in percent; the assignment may
    be out of 10. Writing 100-scale numbers into a 10-mark field showed
    learners "0/100" on a 10-mark task and would have shown "70" out of 10 for
    a good answer. The percentage is kept in the feedback payload so the card
    can show both.

    THE GUARD LIVES HERE, at the single chokepoint every marking path passes
    through, because a rule enforced in the routes is a rule with as many
    holes as there are routes. grade_guard decides whether a number may exist
    at all; a refusal writes the learner an explanation instead of a mark.
    """
    from app.services import grade_guard, student_notices

    allowed, why = grade_guard.may_write_grade(
        criteria=_criteria_rows(result),
        manifest=manifest,
        proposed_score=result.get("totalScore"),
        words_read=int(result.get("wordCount") or 0),
        review=result,
    )
    if not allowed:
        retry = grade_guard.refusal_is_retryable(why)
        print(f"[GRADE GUARD] submission {submission_id}: NO MARK — {why}"
              + (" — left retryable (empty review)" if retry else ""))
        mark_not_graded(
            tenant, submission_id,
            # No "it will be reviewed again": that depends on a rules change,
            # and a date we cannot name is a promise we cannot keep.
            "We could not finish reviewing this attempt, so there are no "
            "marks yet. Your work is saved. This is our side, not yours.",
            # An empty review is a failed call, not a verdict: no rules stamp,
            # so the sweeper re-offers the row instead of parking it.
            stamp=not retry)
        return False

    awarded = scaled_marks(result.get("totalScore", 0), max_marks)
    feedback_payload = review_payload.build(
        result, max_marks, awarded, manifest=manifest,
        show_authorship=review_payload.authorship_visible(course_id))
    # Assignment-specific extras live here, not in the shared shape.
    feedback_payload["scoreEmoji"] = result.get("scoreEmoji")

    texecute(
        tenant,
        """UPDATE assignment_submissions SET
            grade    = %s,
            feedback = %s,
            status   = 'graded'
          WHERE id = %s""",
        (
            awarded,
            json.dumps(feedback_payload, ensure_ascii=False),
            submission_id,
        ),
    )
    return True


# ---------------------------------------------------------------------------
# WHAT WE TELL A LEARNER (02 Sep 2026, Ranjana).
#
# Short. Plain. True. Three beats and no more:
#     what happened  ·  what to do  ·  where their marks stand
#
# NEVER promise something the system does not do. "A mentor has been notified"
# sat in three routes today and nothing notified anyone; "you will hear back
# soon" had no sender behind it; "it will be reviewed again shortly" named a
# time we cannot keep. A learner who waits on a promise we broke stops
# believing the true messages too.
#
# No blame, no exclamation marks, no emojis.
# ---------------------------------------------------------------------------


def mark_not_graded(tenant: Tenant, submission_id: int, message: str,
                    card: dict | None = None, stamp: bool = True) -> None:
    """Record that this submission is deliberately NOT graded, and tell the
    LEARNER why on their own review card.

    Used for every student-side blocker — wrong-task work, an unreadable
    file, a submission with nothing to read. Policy (19 Aug): "if it student
    side fault show them what is the issue so they can re-submit or next time
    don't repeat same issue." A skip that lives only in a staff CSV teaches
    nobody; the row must carry the explanation the student will actually see.

    `card` is an optional renderable feedback shape (the route's
    _empty_feedback) so existing review components display the message
    without special-casing; notGraded/reviewedBy/message are stamped on top.

    grade=NULL + status='submitted' keeps the row out of _graded_by_human's
    protection (nothing human here) and back in the reviewable queue, so the
    learner's corrected resubmission flows through the normal upsert path.
    """
    # WHICH RULES REFUSED THIS (02 Sep 2026). Without a version stamp, the
    # sweeper's "skip rows already carrying a notGraded verdict" was permanent:
    # a row refused by a marker we have since FIXED could never be looked at
    # again, so every refusal accumulated for ever and the ungraded list only
    # grew. Stamping the rules version makes the skip conditional instead of
    # permanent — sweeper_service re-offers a refusal exactly once per rules
    # change, which is bounded spend and the only sane retry trigger: nothing
    # else about the row has changed, but the marker has.
    # `stamp=False` (04 Sep 2026): a refusal that is really OUR failed call
    # — the reviewer returned nothing — carries no version, so the sweeper's
    # escape arm (our refusal + no stamp) re-offers it next pass.
    from app.services.rubric_service import RUBRIC_VERSION
    payload = {
        **(card or {}),
        "notGraded": True,
        "reviewedBy": "airev",
        "message": message,
        **({"rulesVersion": RUBRIC_VERSION} if stamp else {}),
        "notGradedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    texecute(
        tenant,
        """UPDATE assignment_submissions SET
            grade    = NULL,
            feedback = %s,
            status   = 'submitted'
          WHERE id = %s""",
        (json.dumps(payload, ensure_ascii=False), submission_id),
    )


WRONG_TASK_MARK = "Not this task"      # the card's grade slot, LMS wording


def mark_wrong_task(tenant: Tenant, submission_id: int, what_it_is: str,
                    task_title: str, max_marks: int = 100) -> dict:
    """Record a corroborated wrong-task ruling as a ZERO, and tell the learner
    plainly. Returns the stored feedback payload.

    A ZERO, NOT A REFUSAL (04 Sep 2026, Ranjana: "he submitted wrong
    submission it should get zero"). Until today the ruling wrote
    grade=NULL + "Not graded", which read as our failure and left the row in
    the queue with nothing to wait for. The work for another assignment is
    not this assignment's work; that is a mark, and the mark is 0.

    The payload is shaped like the LMS's own zeroPayload (backend/services/
    reviewBlockers.js) — `zeroed` + `blocker` are what its /reopen endpoint
    reads, so staff can lift the 0 and the learner can submit the right
    work past the deadline. `reviewedBy: "ai"` keeps it OURS: a re-review
    with a corrected marker may overwrite it, which a mentor's 0 forbids.
    status='graded' locks resubmission (student.js), so the notice says how
    to get it reopened rather than "submit again".
    """
    from app.services import student_notices
    from app.services.rubric_service import RUBRIC_VERSION
    out_of = max(1, int(max_marks or 100))
    points = student_notices.wrong_task_points(what_it_is, task_title, out_of)
    text = " ".join(points)
    payload = {
        "zeroed": True, "reviewedBy": "ai", "blocker": "wrong_task",
        "wrongTask": {"declared": True, "whatItIs": what_it_is or ""},
        "grade": WRONG_TASK_MARK, "totalScore": 0, "scorePercent": 0,
        "scoreMarks": 0, "outOf": out_of,
        "message": text, "summary": points[0], "detailedFeedback": text,
        "feedbackPoints": points, "strengths": [], "improvements": [],
        "rulesVersion": RUBRIC_VERSION,
        "zeroedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    texecute(
        tenant,
        """UPDATE assignment_submissions SET
            grade    = 0,
            feedback = %s,
            status   = 'graded'
          WHERE id = %s""",
        (json.dumps(payload, ensure_ascii=False), submission_id),
    )
    return payload


def scaled_marks(percent, max_marks: int) -> float:
    """0-100 rubric percentage -> the assignment's own marks scale.

    Kept to one decimal so a 10-mark task can express 6.5 rather than
    collapsing every mid-band answer to the same integer.
    """
    try:
        pct = max(0.0, min(100.0, float(percent or 0)))
        marks = max(1, int(max_marks or 100))
    except (TypeError, ValueError):
        return 0.0
    return round(pct * marks / 100.0, 1)

# ---------- HISTORY (NEW) -------------------------------------------------

def get_assignment_history(tenant: Tenant, assignment_id: int, student_id: int) -> list:
    """All attempts by this student on this assignment, newest first.

    Powers the 'previous review on reopen + Re-analyze' frontend flow.
    """
    if not student_id or student_id <= 0 or not assignment_id:
        return []

    # DUAL_ID_MATCH: the frontend passes users.id while submissions may be
    # stored under students.id (or vice versa). A plain `student_id = %s` here
    # returned zero rows, so History showed "No attempts yet" despite a graded
    # attempt existing — match both ids like every other query in this module.
    from app.database import DUAL_ID_MATCH
    rows = tquery(
        tenant,
        f"""SELECT id, grade, feedback, status, submitted_at, file_name, notes
           FROM assignment_submissions
           WHERE assignment_id = %s AND {DUAL_ID_MATCH}
           ORDER BY submitted_at DESC""",
        (assignment_id, student_id, student_id, student_id),
    )

    out = []
    total = len(rows)
    for i, r in enumerate(rows):
        fb = r.get("feedback")
        if isinstance(fb, str):
            try:
                fb = json.loads(fb)
            except Exception:
                fb = None
        out.append({
            "submissionId":  r["id"],
            "attemptNumber": total - i,  # newest = highest number
            "score":         r.get("grade"),
            "status":        r.get("status"),
            "submittedAt":   str(r["submitted_at"]) if r.get("submitted_at") else None,
            "fileName":      r.get("file_name"),
            "feedback":      fb,
        })
    return out


# ---------- STUDENT-FACING LISTS ------------------------------------------

def get_student_assignments(tenant: Tenant, student_id: int) -> list:
    """List of assignments. Submission columns reflect the LATEST attempt.

    ID tolerance (live finding 19 Jul): the LMS Coursework module can store
    submissions under users.id while AiRev is called with students.id (the
    same dual-ID reality capstones handle). We match either, resolved via
    the students table. Self-diagnosing: an empty result logs WHY (no
    assignments? status mismatch? no submissions under either id?) so a
    blank AiRev hub is explained in one log line."""
    from app.database import DUAL_ID_MATCH as id_match
    rows = tquery(
        tenant,
        f"""SELECT
            a.id, a.title, a.description, a.due_date, a.total_marks, a.status,
            latest.id            AS submission_id,
            latest.grade         AS submission_grade,
            latest.feedback      AS submission_feedback,
            latest.status        AS submission_status,
            latest.submitted_at  AS submitted_at,
            latest.file_name     AS submitted_file_name
          FROM assignments a
          LEFT JOIN (
              SELECT s1.*
              FROM assignment_submissions s1
              INNER JOIN (
                  SELECT assignment_id, MAX(submitted_at) AS max_at
                  FROM assignment_submissions
                  WHERE {id_match}
                  GROUP BY assignment_id
              ) s2
                ON s1.assignment_id = s2.assignment_id
               AND s1.submitted_at  = s2.max_at
              WHERE s1.{id_match}
          ) latest
            ON latest.assignment_id = a.id
          WHERE a.status = 'active'
          ORDER BY
            CASE WHEN latest.status = 'graded'    THEN 3
                 WHEN latest.status = 'submitted' THEN 2
                 ELSE 1 END ASC,
            a.due_date IS NULL,
            a.due_date ASC,
            a.created_at DESC""",
        (student_id,) * 6,   # DUAL_ID_MATCH used twice, 3 params each
    )
    _diagnose_if_odd(tenant, student_id, rows)
    return rows


def _diagnose_if_odd(tenant: Tenant, student_id: int, rows: list) -> None:
    """One log line explaining an empty/submission-less hub. Cheap queries,
    only run when something looks wrong."""
    try:
        if not rows:
            statuses = tquery(tenant,
                              "SELECT status, COUNT(*) AS n FROM assignments GROUP BY status")
            print(f"[ASSIGNMENT] hub EMPTY for student {student_id} — no rows with "
                  f"status='active'. Statuses in assignments table: "
                  f"{[(s.get('status'), s.get('n')) for s in statuses]}")
        elif not any(r.get("submission_id") for r in rows):
            from app.database import DUAL_ID_MATCH
            subs = tquery(tenant,
                          f"SELECT COUNT(*) AS n FROM assignment_submissions "
                          f"WHERE {DUAL_ID_MATCH}",
                          (student_id,) * 3)
            print(f"[ASSIGNMENT] {len(rows)} assignments listed but ZERO submissions "
                  f"matched for student {student_id} (either id form) — "
                  f"assignment_submissions rows under both ids: "
                  f"{subs[0]['n'] if subs else '?'}. If the student did submit via "
                  f"Coursework, the LMS is writing to a different table.")
    except Exception as e:
        print(f"[ASSIGNMENT] diagnostics failed: {e}")


def get_submission_for_regrade(tenant: Tenant, submission_id: int) -> dict | None:
    """Fetch ONE submission by id, without a student_id, for a staff regrade.

    Deliberately separate from get_assignment_submission_by_id(), which pairs
    id with student_id so a learner can only ever read their own row. That
    guard must not be loosened for staff convenience — so staff get their own
    function, reachable only from the admin-key-gated regrade route.

    attempt_number is not a column — it is this row's ordinal among that
    learner's submissions for the same assignment. Computed here so a regrade
    echoes the attempt the learner actually sees, rather than defaulting to 1
    and telling them their third attempt was their first.
    """
    rows = tquery(
        tenant,
        """SELECT s.*, a.title AS assignment_title, a.total_marks,
                  (SELECT COUNT(*) FROM assignment_submissions p
                    WHERE p.assignment_id = s.assignment_id
                      AND p.student_id = s.student_id
                      AND p.id <= s.id) AS attempt_number
          FROM assignment_submissions s
          JOIN assignments a ON a.id = s.assignment_id
          WHERE s.id = %s""",
        (submission_id,),
    )
    return rows[0] if rows else None


def get_assignment_submission_by_id(tenant: Tenant, submission_id: int, student_id: int) -> dict | None:
    rows = tquery(
        tenant,
        """SELECT s.*, a.title AS assignment_title, a.total_marks
          FROM assignment_submissions s
          JOIN assignments a ON a.id = s.assignment_id
          WHERE s.id = %s AND s.student_id = %s""",
        (submission_id, student_id),
    )
    return rows[0] if rows else None