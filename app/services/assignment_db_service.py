# app/services/assignment_db_service.py
# ---------------------------------------------------------------------------
# DB queries for assignments (separate table from case studies).
#
# CHANGED:
#   - save_assignment_submission: INSERTs a new row for every attempt.
#     The old version UPDATEd an existing row, which overwrote prior
#     attempts and made history impossible. Logs were showing
#     "saved submission id=10, attempt=1" repeatedly because of this.
#   - get_assignment_history: NEW. Returns all attempts by a student
#     on an assignment, newest first. Used by the frontend to show the
#     previous review on reopen with a Re-analyze button.
#
# Multi-tenant: every function takes an explicit `tenant` argument and uses
# tquery/texecute, NOT query/execute. Tenant is passed through the call chain
# explicitly so we can never accidentally hit the wrong DB.
# ---------------------------------------------------------------------------

import json
from app.database import tquery, texecute
from app.tenants import Tenant


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

    rows = tquery(
        tenant,
        "SELECT id, file_path, file_name, notes, status "
        "FROM assignment_submissions "
        "WHERE assignment_id = %s AND student_id = %s "
        "ORDER BY submitted_at DESC LIMIT 1",
        (assignment_id, student_id),
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
    rows = tquery(
        tenant,
        """SELECT notes, status FROM assignment_submissions
           WHERE assignment_id = %s AND student_id = %s
           ORDER BY id DESC""",
        (assignment_id, student_id),
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
    INSERTs a NEW row for every attempt. Never overwrites prior submissions.

    The old code UPDATEd the existing row, which erased history and made
    'attempt #1' show forever no matter how many times the student resubmitted.
    """
    submission_id = texecute(
        tenant,
        """INSERT INTO assignment_submissions
           (assignment_id, student_id, notes, file_path, file_name,
            status, submitted_at)
           VALUES (%s, %s, %s, %s, %s, 'submitted', NOW())""",
        (assignment_id, student_id, answer_text or "", file_url, file_name),
    )

    # Real attempt number = how many submissions this student now has.
    attempts = tquery(
        tenant,
        "SELECT COUNT(*) AS n FROM assignment_submissions "
        "WHERE assignment_id = %s AND student_id = %s",
        (assignment_id, student_id),
    )
    attempt_number = attempts[0]["n"] if attempts else 1

    return {"submissionId": submission_id, "attemptNumber": attempt_number}


def update_assignment_submission_with_ai_results(tenant: Tenant, submission_id: int, result: dict):
    feedback_payload = {
        "grade":            result.get("grade"),
        "totalScore":       result.get("totalScore"),
        "scoreEmoji":       result.get("scoreEmoji"),
        "rubricScores":     result.get("rubricScores", []),
        "strengths":        result.get("strengths", []),
        "improvements":     result.get("improvements", []),
        "missingConcepts":  result.get("missingConcepts", []),
        "coveredConcepts":  result.get("coveredConcepts", []),
        "suggestedModules": result.get("suggestedModules", []),
        "detailedFeedback": result.get("detailedFeedback", ""),
        "encouragement":    result.get("encouragement", ""),
        "wordCount":        result.get("wordCount"),
        "wordCountMessage": result.get("wordCountMessage", ""),
        "summary":          result.get("summary", ""),
        "aiLikelihoodPercent":    result.get("aiLikelihoodPercent"),
        "humanLikelihoodPercent": result.get("humanLikelihoodPercent"),
        "aiDetectionReason":      result.get("aiDetectionReason", ""),
        "aiVerdict":              result.get("aiVerdict", ""),
        "isGarbage":              bool(result.get("isGarbage")),
        "garbageWarning":         result.get("garbageWarning", ""),
        "reviewedBy":             "ai",
    }

    texecute(
        tenant,
        """UPDATE assignment_submissions SET
            grade    = %s,
            feedback = %s,
            status   = 'graded'
          WHERE id = %s""",
        (
            int(round(float(result.get("totalScore", 0)))),
            json.dumps(feedback_payload, ensure_ascii=False),
            submission_id,
        ),
    )


# ---------- HISTORY (NEW) -------------------------------------------------

def get_assignment_history(tenant: Tenant, assignment_id: int, student_id: int) -> list:
    """All attempts by this student on this assignment, newest first.

    Powers the 'previous review on reopen + Re-analyze' frontend flow.
    """
    if not student_id or student_id <= 0 or not assignment_id:
        return []

    rows = tquery(
        tenant,
        """SELECT id, grade, feedback, status, submitted_at, file_name, notes
           FROM assignment_submissions
           WHERE assignment_id = %s AND student_id = %s
           ORDER BY submitted_at DESC""",
        (assignment_id, student_id),
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
    id_match = ("student_id IN (%s, COALESCE((SELECT user_id FROM students "
                "WHERE id = %s LIMIT 1), -1))")
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
        (student_id, student_id, student_id, student_id),
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
            subs = tquery(tenant,
                          "SELECT COUNT(*) AS n FROM assignment_submissions "
                          "WHERE student_id IN (%s, COALESCE((SELECT user_id FROM "
                          "students WHERE id = %s LIMIT 1), -1))",
                          (student_id, student_id))
            print(f"[ASSIGNMENT] {len(rows)} assignments listed but ZERO submissions "
                  f"matched for student {student_id} (either id form) — "
                  f"assignment_submissions rows under both ids: "
                  f"{subs[0]['n'] if subs else '?'}. If the student did submit via "
                  f"Coursework, the LMS is writing to a different table.")
    except Exception as e:
        print(f"[ASSIGNMENT] diagnostics failed: {e}")


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