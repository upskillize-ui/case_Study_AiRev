# app/services/db_service.py
# All database operations — connects to your MySQL on Avian Cloud

import json
from app.database import query, execute


# ===== CASE STUDIES =====

def get_case_study_by_id(case_study_id: int) -> dict | None:
    rows = query("SELECT * FROM case_studies WHERE id = %s AND status = 'published'", (case_study_id,))
    if not rows:
        return None
    cs = rows[0]
    return {
        "id": cs["id"],
        "courseId": cs["course_id"],
        "title": cs["title"],
        "description": cs["description"],
        "questions": json.loads(cs["questions"]) if isinstance(cs["questions"], str) else cs["questions"],
        "modelAnswers": json.loads(cs["model_answers"]) if isinstance(cs["model_answers"], str) else cs["model_answers"],
        "gradingRubric": json.loads(cs["grading_rubric"]) if isinstance(cs["grading_rubric"], str) else cs["grading_rubric"],
        "keyConcepts": json.loads(cs["key_concepts"]) if isinstance(cs["key_concepts"], str) else cs["key_concepts"],
        "maxScore": cs["max_score"],
        "wordLimitMin": cs["word_limit_min"],
        "wordLimitMax": cs["word_limit_max"],
        "deadline": cs.get("deadline"),
    }


def get_all_case_studies(course_id: int) -> list:
    return query(
        "SELECT id, course_id, title, status, max_score, deadline, created_at FROM case_studies WHERE course_id = %s ORDER BY created_at DESC",
        (course_id,),
    )


# ===== SUBMISSIONS =====

def save_submission(case_study_id: int, student_id: int, answer_text: str, word_count: int) -> dict:
    attempts = query(
        "SELECT COUNT(*) as count FROM case_study_submissions WHERE case_study_id = %s AND student_id = %s",
        (case_study_id, student_id),
    )
    attempt_number = attempts[0]["count"] + 1

    submission_id = execute(
        """INSERT INTO case_study_submissions 
           (case_study_id, student_id, attempt_number, answer_text, word_count, status) 
           VALUES (%s, %s, %s, %s, %s, 'submitted')""",
        (case_study_id, student_id, attempt_number, answer_text, word_count),
    )
    return {"submissionId": submission_id, "attemptNumber": attempt_number}


def update_submission_with_ai_results(submission_id: int, result: dict):
    execute(
        """UPDATE case_study_submissions SET 
            ai_score = %s, ai_grade = %s, ai_feedback = %s,
            ai_rubric_scores = %s, ai_missing_concepts = %s,
            ai_strengths = %s, ai_improvements = %s,
            ai_suggested_modules = %s, ai_plagiarism_risk = %s,
            ai_reviewed_at = NOW(), status = 'graded',
            is_flagged = %s, flag_reason = %s
          WHERE id = %s""",
        (
            result["totalScore"],
            result["grade"],
            json.dumps(result),
            json.dumps(result.get("rubricScores", [])),
            json.dumps(result.get("missingConcepts", [])),
            ". ".join(result.get("strengths", [])),
            ". ".join(result.get("improvements", [])),
            json.dumps(result.get("suggestedModules", [])),
            result.get("plagiarismFlag", "low"),
            result.get("needsMentorHelp", False) or result.get("plagiarismFlag") == "high",
            "Low score" if result.get("needsMentorHelp") else ("Plagiarism risk" if result.get("plagiarismFlag") == "high" else None),
            submission_id,
        ),
    )


# ===== PERFORMANCE TRACKER (Current + Best Score) =====

def update_performance_tracker(student_id: int, case_study_id: int, score: float):
    status = "completed" if score >= 70 else ("needs_help" if score < 40 else "in_progress")

    # Check if record exists
    existing = query(
        "SELECT id, best_score, first_attempt_score FROM student_performance_tracker WHERE student_id = %s AND case_study_id = %s",
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


# ===== MENTOR DASHBOARD =====

def get_mentor_dashboard(case_study_id: int) -> dict:
    submissions = query(
        """SELECT s.id AS submission_id, s.student_id, s.attempt_number,
            s.ai_score, s.ai_grade, s.ai_strengths, s.ai_improvements,
            s.ai_plagiarism_risk, s.is_flagged, s.flag_reason,
            s.mentor_approved, s.submitted_at,
            pt.current_score, pt.best_score, pt.total_attempts, pt.status AS student_status
          FROM case_study_submissions s
          LEFT JOIN student_performance_tracker pt 
            ON pt.student_id = s.student_id AND pt.case_study_id = s.case_study_id
          WHERE s.case_study_id = %s
          ORDER BY s.submitted_at DESC""",
        (case_study_id,),
    )

    scores = [float(s["ai_score"]) for s in submissions if s["ai_score"] and float(s["ai_score"]) > 0]
    avg = sum(scores) / len(scores) if scores else 0

    return {
        "classStats": {
            "totalSubmissions": len(submissions),
            "averageScore": round(avg, 2),
            "highestScore": max(scores) if scores else 0,
            "lowestScore": min(scores) if scores else 0,
            "above70": sum(1 for s in scores if s >= 70),
            "between40and70": sum(1 for s in scores if 40 <= s < 70),
            "below40": sum(1 for s in scores if s < 40),
            "flaggedCount": sum(1 for s in submissions if s["is_flagged"]),
            "pendingApproval": sum(1 for s in submissions if not s["mentor_approved"]),
        },
        "students": [
            {
                "submissionId": s["submission_id"],
                "studentId": s["student_id"],
                "currentScore": float(s["current_score"]) if s["current_score"] else None,
                "bestScore": float(s["best_score"]) if s["best_score"] else None,
                "aiScore": float(s["ai_score"]) if s["ai_score"] else 0,
                "grade": s["ai_grade"],
                "attempts": s["total_attempts"],
                "status": s["student_status"] or "in_progress",
                "plagiarismRisk": s["ai_plagiarism_risk"],
                "isFlagged": bool(s["is_flagged"]),
                "flagReason": s["flag_reason"],
                "mentorApproved": bool(s["mentor_approved"]),
                "submittedAt": str(s["submitted_at"]) if s["submitted_at"] else None,
            }
            for s in submissions
        ],
        "needsHelp": [
            {"studentId": s["student_id"], "score": float(s["ai_score"]) if s["ai_score"] else 0}
            for s in submissions
            if s["student_status"] == "needs_help" or (s["ai_score"] and float(s["ai_score"]) < 40)
        ],
    }


# ===== MENTOR ACTIONS =====

def mentor_approve_submission(submission_id: int, mentor_id: int, mentor_score: float | None, mentor_feedback: str | None):
    execute(
        """UPDATE case_study_submissions SET 
            mentor_id = %s, mentor_score = %s, mentor_feedback = %s,
            mentor_approved = TRUE, mentor_reviewed_at = NOW(), status = 'mentor_reviewed'
          WHERE id = %s""",
        (mentor_id, mentor_score, mentor_feedback, submission_id),
    )

    if mentor_score is not None:
        rows = query("SELECT student_id, case_study_id FROM case_study_submissions WHERE id = %s", (submission_id,))
        if rows:
            update_performance_tracker(rows[0]["student_id"], rows[0]["case_study_id"], mentor_score)


# ===== AI REVIEW LOGS =====

def log_ai_review(submission_id: int, meta: dict | None, raw_response=None, error: str | None = None):
    try:
        execute(
            """INSERT INTO ai_review_logs 
               (submission_id, ai_model_used, processing_time_ms, raw_ai_response, error_message, success)
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
        print(f"⚠️  Failed to log AI review: {e}")
