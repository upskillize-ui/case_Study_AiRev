# app/services/db_service.py
# All database operations — connects to your MySQL on Avian Cloud.
#
# IMPORTANT: this file has been adapted to the LMS-created schema.
# The original code assumed columns like `attempt_number`, `answer_text`,
# `ai_score`, `ai_feedback`, etc. — none of which exist in the live tables.
# We map to the actual columns (`notes`, `grade`, `feedback`, `rubric_scores`,
# etc.) and pack the rich AI analysis into the `feedback` TEXT field as JSON.

import json
from app.database import query, execute


# ===== CASE STUDIES =====

def get_case_study_by_id(case_study_id: int) -> dict | None:
    """Read a case study, adapting to the LMS schema."""
    rows = query(
        "SELECT * FROM case_studies WHERE id = %s AND status = 'published'",
        (case_study_id,),
    )
    if not rows:
        return None
    cs = rows[0]

    def jload(val, default):
        if val is None:
            return default
        if isinstance(val, str):
            try:
                return json.loads(val)
            except Exception:
                return default
        return val

    questions = jload(cs.get("questions"), [])
    model_answers = []  # column doesn't exist in LMS schema

    # rubric: LMS stores [{"name":"X","points":25}, ...]
    # scoring_service expects {"criteria":[{"name":"X","maxScore":25}, ...]}
    raw_rubric = jload(cs.get("rubric_criteria"), [])
    if isinstance(raw_rubric, list):
        criteria = [
            {
                "name": c.get("name", "Criterion"),
                "maxScore": c.get("points", c.get("maxScore", 25)),
                "weight": (c.get("points", 25) / 100),
            }
            for c in raw_rubric
        ]
    elif isinstance(raw_rubric, dict):
        criteria = raw_rubric.get("criteria", [])
    else:
        criteria = []

    if not criteria:
        criteria = [
            {"name": "Understanding", "maxScore": 25, "weight": 0.25},
            {"name": "Application",   "maxScore": 25, "weight": 0.25},
            {"name": "Depth",         "maxScore": 25, "weight": 0.25},
            {"name": "Structure",     "maxScore": 25, "weight": 0.25},
        ]
    grading_rubric = {"criteria": criteria}

    key_concepts = [
        s for s in [
            cs.get("company_name"),
            cs.get("industry"),
            cs.get("learning_objectives"),
        ]
        if s and isinstance(s, str)
    ]

    word_limit = cs.get("word_limit") or 500
    word_limit_max = int(word_limit)
    word_limit_min = max(50, word_limit_max // 2)

    return {
        "id": cs["id"],
        "courseId": cs.get("course_id"),
        "title": cs.get("title", ""),
        "description": cs.get("description") or cs.get("learning_objectives") or "",
        "questions": questions,
        "modelAnswers": model_answers,
        "gradingRubric": grading_rubric,
        "keyConcepts": key_concepts,
        "maxScore": cs.get("total_marks", 100),
        "wordLimitMin": word_limit_min,
        "wordLimitMax": word_limit_max,
        "deadline": cs.get("due_date") or cs.get("deadline"),
    }


def get_all_case_studies(course_id: int) -> list:
    return query(
        "SELECT id, course_id, title, status, total_marks, due_date, created_at "
        "FROM case_studies WHERE course_id = %s ORDER BY created_at DESC",
        (course_id,),
    )


def get_latest_submission_file(case_study_id: int, student_id: int,
                               exclude_submission_id: int | None = None) -> dict | None:
    """
    Find the most recent submission row for this student+case study.
    Returns notes + file_url (if any). Used by the review pipeline to get
    the student's ACTUAL answer instead of whatever the frontend sends.

    Falls back to ANY submission for this case study if student_id doesn't match.
    """
    # Try exact match first (student + case study)
    sql = (
        "SELECT id, file_url, file_name, notes, student_id "
        "FROM case_study_submissions "
        "WHERE case_study_id = %s AND student_id = %s "
    )
    params: tuple = (case_study_id, student_id)
    if exclude_submission_id is not None:
        sql += "AND id <> %s "
        params = (case_study_id, student_id, exclude_submission_id)
    sql += "ORDER BY submitted_at DESC LIMIT 1"

    rows = query(sql, params)
    if rows:
        print(f"📄 Found submission id={rows[0]['id']} for student={student_id}, "
              f"notes={len(rows[0].get('notes') or '')} chars, "
              f"file={'yes' if rows[0].get('file_url') else 'no'}")
        return rows[0]

    # Fallback: any submission for this case study (handles student_id mismatch)
    fallback_sql = (
        "SELECT id, file_url, file_name, notes, student_id "
        "FROM case_study_submissions "
        "WHERE case_study_id = %s "
        "ORDER BY submitted_at DESC LIMIT 1"
    )
    rows = query(fallback_sql, (case_study_id,))
    if rows:
        print(f"📄 Fallback: found submission id={rows[0]['id']} from student={rows[0].get('student_id','?')} "
              f"(requested student={student_id}), "
              f"notes={len(rows[0].get('notes') or '')} chars, "
              f"file={'yes' if rows[0].get('file_url') else 'no'}")
        return rows[0]

    print(f"📄 No submissions found for case_study={case_study_id}")
    return None


# ===== SUBMISSIONS =====

def save_submission(case_study_id: int, student_id: int, answer_text: str, word_count: int) -> dict:
    """
    Insert or update a row in case_study_submissions.

    The LMS schema has a unique constraint on (case_study_id, student_id),
    so we can't insert multiple rows for the same student+case_study.
    If a row already exists, DON'T overwrite the notes — the student's
    original answer must be preserved. Just return the existing submission ID.
    """
    existing = query(
        "SELECT id FROM case_study_submissions "
        "WHERE case_study_id = %s AND student_id = %s",
        (case_study_id, student_id),
    )

    if existing:
        # Row exists — preserve the student's original notes, don't overwrite
        submission_id = existing[0]["id"]
        attempt_number = len(existing) + 1
    else:
        # First submission
        submission_id = execute(
            """INSERT INTO case_study_submissions
               (case_study_id, student_id, notes, status)
               VALUES (%s, %s, %s, 'submitted')""",
            (case_study_id, student_id, answer_text),
        )
        attempt_number = 1

    return {"submissionId": submission_id, "attemptNumber": attempt_number}


def update_submission_with_ai_results(submission_id: int, result: dict):
    """
    Persist AI analysis to the simpler LMS schema:
      grade           <- totalScore
      feedback        <- full structured AI result, JSON-encoded
      rubric_scores   <- {criterion_name: score, ...}
      status          <- 'reviewed'
      reviewed_at     <- NOW()
    """
    rubric_scores_dict = {
        r.get("criteria", f"Criterion {i+1}"): r.get("score", 0)
        for i, r in enumerate(result.get("rubricScores", []))
    }

    feedback_payload = {
        "grade":            result.get("grade"),
        "totalScore":       result.get("totalScore"),
        "scoreEmoji":       result.get("scoreEmoji"),
        "strengths":        result.get("strengths", []),
        "improvements":     result.get("improvements", []),
        "missingConcepts":  result.get("missingConcepts", []),
        "coveredConcepts":  result.get("coveredConcepts", []),
        "suggestedModules": result.get("suggestedModules", []),
        "detailedFeedback": result.get("detailedFeedback", ""),
        "plagiarismFlag":   result.get("plagiarismFlag", "low"),
        "needsMentorHelp":  bool(result.get("needsMentorHelp")),
        "wordCount":        result.get("wordCount"),
        "wordCountMessage": result.get("wordCountMessage", ""),
        # NEW — Human/AI detection + garbage flag
        "aiLikelihoodPercent":    result.get("aiLikelihoodPercent"),
        "humanLikelihoodPercent": result.get("humanLikelihoodPercent"),
        "aiDetectionReason":      result.get("aiDetectionReason", ""),
        "aiVerdict":              result.get("aiVerdict", ""),
        "isGarbage":              bool(result.get("isGarbage")),
        "garbageWarning":         result.get("garbageWarning", ""),
    }

    execute(
        """UPDATE case_study_submissions SET
            grade         = %s,
            feedback      = %s,
            rubric_scores = %s,
            status        = 'reviewed',
            reviewed_at   = NOW()
          WHERE id = %s""",
        (
            int(round(float(result.get("totalScore", 0)))),
            json.dumps(feedback_payload, ensure_ascii=False),
            json.dumps(rubric_scores_dict, ensure_ascii=False),
            submission_id,
        ),
    )


# ===== PERFORMANCE TRACKER (schema matches — unchanged) =====

def update_performance_tracker(student_id: int, case_study_id: int, score: float):
    status = "completed" if score >= 70 else ("needs_help" if score < 40 else "in_progress")

    existing = query(
        "SELECT id, best_score, first_attempt_score FROM student_performance_tracker "
        "WHERE student_id = %s AND case_study_id = %s",
        (student_id, case_study_id),
    )

    if existing:
        row = existing[0]
        new_best = max(float(row["best_score"]), score)
        improvement = new_best - float(row["first_attempt_score"])
        execute(
            """UPDATE student_performance_tracker SET
                current_score = %s, best_score = %s,
                total_attempts = total_attempts + 1, improvement = %s,
                last_attempt_at = NOW(), status = %s
              WHERE student_id = %s AND case_study_id = %s""",
            (score, new_best, improvement, status, student_id, case_study_id),
        )
    else:
        execute(
            """INSERT INTO student_performance_tracker
               (student_id, case_study_id, current_score, best_score, first_attempt_score,
                total_attempts, improvement, last_attempt_at, status)
              VALUES (%s, %s, %s, %s, %s, 1, 0, NOW(), %s)""",
            (student_id, case_study_id, score, score, score, status),
        )


def get_student_progress(student_id: int) -> dict:
    rows = query(
        """SELECT pt.case_study_id, cs.title AS case_study_title,
            pt.current_score, pt.best_score, pt.first_attempt_score,
            pt.total_attempts, pt.improvement, pt.status, pt.last_attempt_at
          FROM student_performance_tracker pt
          JOIN case_studies cs ON cs.id = pt.case_study_id
          WHERE pt.student_id = %s
          ORDER BY pt.last_attempt_at DESC""",
        (student_id,),
    )

    total = len(rows)
    avg_current = sum(float(r["current_score"]) for r in rows) / total if total > 0 else 0
    avg_best = sum(float(r["best_score"]) for r in rows) / total if total > 0 else 0

    return {
        "overallStats": {
            "totalCaseStudies": total,
            "averageCurrentScore": round(avg_current, 2),
            "averageBestScore": round(avg_best, 2),
            "completedCount": sum(1 for r in rows if r["status"] == "completed"),
            "needsHelpCount": sum(1 for r in rows if r["status"] == "needs_help"),
            "inProgressCount": sum(1 for r in rows if r["status"] == "in_progress"),
        },
        "caseStudies": [
            {
                "caseStudyId": r["case_study_id"],
                "title": r["case_study_title"],
                "currentScore": float(r["current_score"]),
                "bestScore": float(r["best_score"]),
                "firstAttemptScore": float(r["first_attempt_score"]),
                "totalAttempts": r["total_attempts"],
                "improvement": float(r["improvement"]),
                "status": r["status"],
                "lastAttempt": str(r["last_attempt_at"]) if r["last_attempt_at"] else None,
            }
            for r in rows
        ],
    }


# ===== MENTOR DASHBOARD (rewritten for LMS schema) =====

def get_mentor_dashboard(case_study_id: int) -> dict:
    submissions = query(
        """SELECT s.id AS submission_id, s.student_id,
            s.grade, s.feedback, s.rubric_scores, s.status AS submission_status,
            s.submitted_at, s.reviewed_at,
            pt.current_score, pt.best_score, pt.total_attempts,
            pt.status AS student_status
          FROM case_study_submissions s
          LEFT JOIN student_performance_tracker pt
            ON pt.student_id = s.student_id AND pt.case_study_id = s.case_study_id
          WHERE s.case_study_id = %s
          ORDER BY s.submitted_at DESC""",
        (case_study_id,),
    )

    scores = [float(s["grade"]) for s in submissions if s["grade"] is not None and float(s["grade"]) > 0]
    avg = sum(scores) / len(scores) if scores else 0

    def parse_feedback(raw):
        if not raw:
            return {}
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {"detailedFeedback": raw}
        return raw or {}

    return {
        "classStats": {
            "totalSubmissions": len(submissions),
            "averageScore": round(avg, 2),
            "highestScore": max(scores) if scores else 0,
            "lowestScore": min(scores) if scores else 0,
            "above70": sum(1 for s in scores if s >= 70),
            "between40and70": sum(1 for s in scores if 40 <= s < 70),
            "below40": sum(1 for s in scores if s < 40),
        },
        "students": [
            {
                "submissionId": s["submission_id"],
                "studentId":    s["student_id"],
                "currentScore": float(s["current_score"]) if s["current_score"] is not None else None,
                "bestScore":    float(s["best_score"]) if s["best_score"] is not None else None,
                "aiScore":      float(s["grade"]) if s["grade"] is not None else 0,
                "grade":        parse_feedback(s["feedback"]).get("grade"),
                "attempts":     s["total_attempts"] or 0,
                "status":       s["student_status"] or "in_progress",
                "submissionStatus": s["submission_status"],
                "submittedAt":  str(s["submitted_at"]) if s["submitted_at"] else None,
                "reviewedAt":   str(s["reviewed_at"])  if s["reviewed_at"]  else None,
            }
            for s in submissions
        ],
        "needsHelp": [
            {"studentId": s["student_id"], "score": float(s["grade"]) if s["grade"] is not None else 0}
            for s in submissions
            if s["student_status"] == "needs_help"
            or (s["grade"] is not None and float(s["grade"]) < 40)
        ],
    }


# ===== MENTOR ACTIONS (rewritten for LMS schema) =====

def mentor_approve_submission(submission_id: int, mentor_id: int,
                              mentor_score: float | None, mentor_feedback: str | None):
    """
    LMS schema doesn't have mentor_id / mentor_approved / mentor_reviewed_at.
    We store the mentor's score in `grade`, mentor's feedback (with the
    mentor_id tagged in JSON) in `feedback`, and mark status as 'reviewed'.
    """
    payload = {
        "mentorId": mentor_id,
        "mentorFeedback": mentor_feedback,
        "mentorScore": mentor_score,
    }
    if mentor_score is not None:
        execute(
            """UPDATE case_study_submissions SET
                grade = %s, feedback = %s, status = 'reviewed', reviewed_at = NOW()
              WHERE id = %s""",
            (
                int(round(float(mentor_score))),
                json.dumps(payload, ensure_ascii=False),
                submission_id,
            ),
        )
    else:
        execute(
            """UPDATE case_study_submissions SET
                feedback = %s, status = 'reviewed', reviewed_at = NOW()
              WHERE id = %s""",
            (json.dumps(payload, ensure_ascii=False), submission_id),
        )

    if mentor_score is not None:
        rows = query(
            "SELECT student_id, case_study_id FROM case_study_submissions WHERE id = %s",
            (submission_id,),
        )
        if rows:
            update_performance_tracker(rows[0]["student_id"], rows[0]["case_study_id"], mentor_score)


# ===== AI REVIEW LOGS (schema matches — unchanged) =====

def log_ai_review(submission_id: int, meta: dict | None,
                  raw_response=None, error: str | None = None):
    try:
        execute(
            """INSERT INTO ai_review_logs
               (submission_id, ai_model_used, processing_time_ms,
                raw_ai_response, error_message, success)
              VALUES (%s, %s, %s, %s, %s, %s)""",
            (
                submission_id,
                meta.get("model", "unknown") if meta else "unknown",
                meta.get("processingTimeMs", 0) if meta else 0,
                json.dumps(raw_response) if raw_response else None,
                error,
                error is None,
            ),
        )
    except Exception as e:
        print(f"WARN  Failed to log AI review: {e}")