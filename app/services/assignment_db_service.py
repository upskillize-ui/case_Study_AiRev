# app/services/assignment_db_service.py
# ---------------------------------------------------------------------------
# DB queries for assignments (separate table from case studies).
#
# IMPORTANT: every function takes an explicit `tenant` argument and uses
# tquery/texecute, NOT query/execute. This bypasses the contextvar approach
# entirely — the tenant is passed through the call chain explicitly so we
# can never accidentally hit the wrong DB.
#
# Schema:
#   assignments(id, title, description, course_id, faculty_id, due_date,
#               rubric (json), status, total_marks, created_at, updated_at)
#   assignment_submissions(id, assignment_id, student_id, file_path, file_name,
#                          notes, grade, feedback, status, submitted_at)
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

def save_assignment_submission(
    tenant: Tenant,
    assignment_id: int,
    student_id: int,
    answer_text: str | None,
    file_url: str | None,
    file_name: str | None,
) -> dict:
    existing = tquery(
        tenant,
        "SELECT id FROM assignment_submissions "
        "WHERE assignment_id = %s AND student_id = %s "
        "ORDER BY submitted_at DESC LIMIT 1",
        (assignment_id, student_id),
    )

    if existing:
        submission_id = existing[0]["id"]
        texecute(
            tenant,
            """UPDATE assignment_submissions SET
                notes = %s,
                file_path = COALESCE(%s, file_path),
                file_name = COALESCE(%s, file_name),
                status = 'submitted',
                submitted_at = NOW()
              WHERE id = %s""",
            (answer_text or "", file_url, file_name, submission_id),
        )
    else:
        submission_id = texecute(
            tenant,
            """INSERT INTO assignment_submissions
               (assignment_id, student_id, notes, file_path, file_name, status)
               VALUES (%s, %s, %s, %s, %s, 'submitted')""",
            (assignment_id, student_id, answer_text or "", file_url, file_name),
        )

    attempts = tquery(
        tenant,
        "SELECT COUNT(*) as n FROM assignment_submissions "
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


# ---------- STUDENT-FACING LISTS ------------------------------------------

def get_student_assignments(tenant: Tenant, student_id: int) -> list:
    return tquery(
        tenant,
        """SELECT
            a.id, a.title, a.description, a.due_date, a.total_marks, a.status,
            s.id            AS submission_id,
            s.grade         AS submission_grade,
            s.feedback      AS submission_feedback,
            s.status        AS submission_status,
            s.submitted_at  AS submitted_at,
            s.file_name     AS submitted_file_name
          FROM assignments a
          LEFT JOIN assignment_submissions s
            ON s.assignment_id = a.id AND s.student_id = %s
          WHERE a.status = 'active'
          ORDER BY
            CASE WHEN s.status = 'graded'    THEN 3
                 WHEN s.status = 'submitted' THEN 2
                 ELSE 1 END ASC,
            a.due_date IS NULL,
            a.due_date ASC,
            a.created_at DESC""",
        (student_id,),
    )


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