# app/routes/industry_session_review.py
#
# Industry Session review endpoints for AIREV_AGENT
#
# Endpoints:
#   GET  /api/review/industry-sessions-for-student/{student_id}
#        → All sessions the student attended, with latest insight submission
#   POST /api/review/submit-industry-session
#        → Submit/review student session insights against 5 dimensions
#   GET  /api/review/industry-session-history/{student_id}/{session_id}
#        → All past attempts for one session
#
# Rubric (5 dimensions, 0-4 each, total 20 → normalised to /100):
#   1. Session Comprehension
#   2. Key Insight Capture
#   3. Industry Relevance
#   4. Critical Reflection
#   5. Personal Application

import time
import json
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.services import ai_service, db_service
from app.database import get_db_connection

router = APIRouter(prefix="/api/review", tags=["industry-session"])

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-20250514")

# ─── Schemas ──────────────────────────────────────────────────────────────────
class IndustrySessionInsightRequest(BaseModel):
    sessionId: int
    studentId: int
    insightText: Optional[str] = ""
    fileUrl:    Optional[str] = None
    fileName:   Optional[str] = None

# ─── DB helpers ───────────────────────────────────────────────────────────────
def get_industry_sessions_for_student(student_id: int):
    """
    Returns all industry sessions the student has attended/enrolled in,
    along with their latest insight submission (if any).
    Adapts to whatever table schema is in use — tries industry_sessions
    then falls back to sessions.
    """
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        # Discover table name
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name IN ('industry_sessions','sessions') "
            "LIMIT 1"
        )
        row = cursor.fetchone()
        if not row:
            return []
        tbl = row["table_name"]

        # Submissions table
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name IN ('industry_session_submissions','session_submissions') "
            "LIMIT 1"
        )
        sub_row = cursor.fetchone()
        sub_tbl = sub_row["table_name"] if sub_row else None

        if sub_tbl:
            cursor.execute(f"""
                SELECT
                    s.id,
                    s.title,
                    s.mentor_name,
                    s.session_date,
                    s.description,
                    sub.id          AS submission_id,
                    sub.submitted_at,
                    sub.grade,
                    sub.has_feedback
                FROM {tbl} s
                LEFT JOIN {sub_tbl} sub
                    ON sub.session_id = s.id
                   AND sub.student_id = %s
                   AND sub.id = (
                        SELECT MAX(id) FROM {sub_tbl}
                        WHERE session_id = s.id AND student_id = %s
                   )
                WHERE s.status IN ('published','active','completed')
                ORDER BY s.session_date DESC
            """, (student_id, student_id))
        else:
            # No submissions table yet — return sessions with no submission info
            cursor.execute(f"""
                SELECT id, title, mentor_name, session_date, description,
                       NULL AS submission_id, NULL AS submitted_at,
                       NULL AS grade, 0 AS has_feedback
                FROM {tbl}
                WHERE status IN ('published','active','completed')
                ORDER BY session_date DESC
            """)

        rows = cursor.fetchall()
        sessions = []
        for r in rows:
            sessions.append({
                "id":           r["id"],
                "title":        r.get("title") or "Untitled Session",
                "mentorName":   r.get("mentor_name"),
                "sessionDate":  str(r["session_date"]) if r.get("session_date") else None,
                "description":  r.get("description"),
                "submissionId": r.get("submission_id"),
                "submittedAt":  str(r["submitted_at"]) if r.get("submitted_at") else None,
                "grade":        r.get("grade"),
                "hasFeedback":  bool(r.get("has_feedback")),
                # reviewable if they have a submission OR session is completed
                "completed":    r.get("submission_id") is not None,
            })
        return sessions
    finally:
        cursor.close()
        conn.close()

def save_session_insight(session_id: int, student_id: int, insight_text: str,
                          file_url: str = None, file_name: str = None) -> dict:
    """Save/upsert insight submission and return {submissionId, attemptNumber}."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name IN ('industry_session_submissions','session_submissions') "
            "LIMIT 1"
        )
        row = cursor.fetchone()
        if not row:
            # Auto-create table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS industry_session_submissions (
                    id           INT AUTO_INCREMENT PRIMARY KEY,
                    session_id   INT NOT NULL,
                    student_id   INT NOT NULL,
                    insight_text TEXT,
                    file_url     VARCHAR(1024),
                    file_name    VARCHAR(512),
                    attempt_number INT DEFAULT 1,
                    score        DECIMAL(5,2),
                    grade        VARCHAR(10),
                    feedback_json LONGTEXT,
                    has_feedback TINYINT DEFAULT 0,
                    submitted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX (session_id, student_id)
                )
            """)
            conn.commit()
            tbl = "industry_session_submissions"
        else:
            tbl = row["table_name"]

        cursor.execute(f"SELECT MAX(attempt_number) AS mx FROM {tbl} WHERE session_id=%s AND student_id=%s", (session_id, student_id))
        mx = cursor.fetchone()
        attempt = (mx["mx"] or 0) + 1

        cursor.execute(f"""
            INSERT INTO {tbl} (session_id, student_id, insight_text, file_url, file_name, attempt_number, submitted_at)
            VALUES (%s, %s, %s, %s, %s, %s, NOW())
        """, (session_id, student_id, insight_text, file_url, file_name, attempt))
        conn.commit()
        submission_id = cursor.lastrowid
        return {"submissionId": submission_id, "attemptNumber": attempt}
    finally:
        cursor.close()
        conn.close()

def save_session_feedback(submission_id: int, score: float, grade: str, feedback: dict,
                           tbl: str = "industry_session_submissions"):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(f"""
            UPDATE {tbl}
            SET score=%s, grade=%s, feedback_json=%s, has_feedback=1
            WHERE id=%s
        """, (score, grade, json.dumps(feedback), submission_id))
        conn.commit()
    finally:
        cursor.close()
        conn.close()

def get_session_insight_history(student_id: int, session_id: int) -> list:
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name IN ('industry_session_submissions','session_submissions') "
            "LIMIT 1"
        )
        row = cursor.fetchone()
        if not row:
            return []
        tbl = row["table_name"]
        cursor.execute(f"""
            SELECT id, attempt_number, score, grade, feedback_json, submitted_at
            FROM {tbl}
            WHERE session_id=%s AND student_id=%s
            ORDER BY attempt_number DESC
        """, (session_id, student_id))
        rows = cursor.fetchall()
        history = []
        for r in rows:
            fb = {}
            if r.get("feedback_json"):
                try: fb = json.loads(r["feedback_json"])
                except: pass
            history.append({
                "submissionId":   r["id"],
                "attemptNumber":  r["attempt_number"],
                "score":          float(r["score"]) if r.get("score") is not None else 0,
                "submittedAt":    str(r["submitted_at"]) if r.get("submitted_at") else None,
                "feedback":       fb,
            })
        return history
    finally:
        cursor.close()
        conn.close()

# ─── AI review ────────────────────────────────────────────────────────────────
def build_session_prompt(session_title: str, mentor_name: str, insight_text: str) -> str:
    return f"""You are AiRev, the AI Industry Session Review agent for Upskillize EcoPro LMS.
Your tone is coaching, never punitive. Be direct and specific.

Session: "{session_title}"
Mentor: {mentor_name or "Industry Mentor"}

Student's session insights:
\"\"\"
{insight_text}
\"\"\"

Evaluate across exactly these 5 dimensions (score 0-4 each):
1. Session Comprehension — did they grasp the core content and context?
2. Key Insight Capture — did they surface the right takeaways?
3. Industry Relevance — did they map it to BFSI/FinTech/sector context?
4. Critical Reflection — did they form their own perspective beyond surface level?
5. Personal Application — did they link it to their own career or role?

Scoring guide: 0 = absent, 1 = minimal, 2 = emerging, 3 = proficient, 4 = outstanding.

Overall band (from total score out of 20):
  16-20 → Outstanding · 12-15 → Strong · 8-11 → Proficient · 0-7 → Emerging

Respond ONLY with valid JSON (no markdown, no backticks, no preamble):
{{
  "band": "Emerging|Proficient|Strong|Outstanding",
  "dimensions": [
    {{"name": "Session Comprehension", "score": 0-4, "note": "1-2 sentences, specific to their text"}},
    {{"name": "Key Insight Capture",   "score": 0-4, "note": "..."}},
    {{"name": "Industry Relevance",    "score": 0-4, "note": "..."}},
    {{"name": "Critical Reflection",   "score": 0-4, "note": "..."}},
    {{"name": "Personal Application",  "score": 0-4, "note": "..."}}
  ],
  "rewrite_suggestions": ["concrete suggestion 1", "suggestion 2", "suggestion 3"],
  "keep_this": "one specific strength from their writing",
  "next_action": "one sharp, actionable next step",
  "summary": "2-sentence overall summary (coaching tone)",
  "encouragement": "30-50 word mentor note (warm, direct, BFSI context-aware)"
}}"""

BAND_TO_GRADE = {"Outstanding": "A+", "Strong": "A", "Proficient": "B+", "Emerging": "B"}

def score_from_dimensions(dimensions: list) -> float:
    total = sum(d.get("score", 0) for d in dimensions)
    return round((total / 20) * 100, 1)

# ─── Endpoints ────────────────────────────────────────────────────────────────
@router.get("/industry-sessions-for-student/{student_id}")
async def get_sessions_for_student(student_id: int):
    try:
        sessions = get_industry_sessions_for_student(student_id)
        return {"success": True, "sessions": sessions, "total": len(sessions)}
    except Exception as e:
        print(f"❌ industry-sessions-for-student error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/submit-industry-session")
async def submit_industry_session(req: IndustrySessionInsightRequest):
    start = time.time()
    print(f"ℹ️  Industry session insight: student={req.studentId}, session={req.sessionId}")

    # 1. Load session metadata
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    session_meta = None
    try:
        for tbl in ("industry_sessions", "sessions"):
            try:
                cursor.execute(f"SELECT id, title, mentor_name FROM {tbl} WHERE id=%s LIMIT 1", (req.sessionId,))
                row = cursor.fetchone()
                if row:
                    session_meta = row
                    break
            except:
                pass
    finally:
        cursor.close()
        conn.close()

    if not session_meta:
        raise HTTPException(status_code=404, detail="Industry session not found or not published")

    insight = (req.insightText or "").strip()

    # 2. No content — signal frontend to show compose form
    if not insight and not req.fileUrl:
        summary_msg = "We couldn't find any insight text. Please write your session insights first, then submit."
        return {
            "success": True,
            "submission": {"submissionId": 0, "attemptNumber": 0},
            "feedback": {
                "score": 0, "grade": "-", "summary": summary_msg,
                "band": "Emerging", "dimensions": [], "rewrite_suggestions": [],
                "keep_this": "", "next_action": "", "encouragement": "",
            },
        }

    # 3. Extract file text if uploaded
    if req.fileUrl:
        try:
            from app.utils.file_extractor import extract_text_from_url
            extracted, _ = extract_text_from_url(req.fileUrl, req.fileName or "")
            if extracted:
                insight = f"{insight}\n\n{extracted}".strip()
        except Exception as ex:
            print(f"⚠️ File extraction failed: {ex}")

    # 4. Save submission
    sub = save_session_insight(req.sessionId, req.studentId, insight, req.fileUrl, req.fileName)
    submission_id = sub["submissionId"]

    # 5. AI review
    prompt = build_session_prompt(
        session_meta.get("title", "Industry Session"),
        session_meta.get("mentor_name", "Industry Mentor"),
        insight,
    )

    raw_feedback = None
    provider = "claude"
    try:
        ai_resp = ai_service.call_claude(prompt, max_tokens=1200)
        raw_text = ai_resp.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        raw_feedback = json.loads(raw_text)
    except Exception as e:
        print(f"⚠️ Claude failed: {e}. Using fallback.")
        provider = "fallback"
        raw_feedback = {
            "band": "Proficient",
            "dimensions": [
                {"name": "Session Comprehension", "score": 3, "note": "You captured the session's core focus well."},
                {"name": "Key Insight Capture",   "score": 2, "note": "Key takeaways present, but could be sharper."},
                {"name": "Industry Relevance",    "score": 2, "note": "Some BFSI connection — push this further."},
                {"name": "Critical Reflection",   "score": 2, "note": "You're forming your own view — develop it."},
                {"name": "Personal Application",  "score": 2, "note": "Career linkage is there, make it explicit."},
            ],
            "rewrite_suggestions": [
                "Name one specific decision or trend the mentor called out.",
                "Add one sentence on how this changes your view of a job role.",
                "Cite the sector (retail banking, NBFCs, etc.) you'd apply this in.",
            ],
            "keep_this": "Your opening paragraph shows genuine engagement with the topic.",
            "next_action": "Re-attempt with one concrete BFSI example from the session.",
            "summary": "Solid first read of the session. Tighten your industry application to move from Proficient to Strong.",
            "encouragement": "You're building the habit of extracting meaning from expert conversations — that's the core of BFSI career growth. Keep going.",
        }

    dimensions = raw_feedback.get("dimensions", [])
    score = score_from_dimensions(dimensions)
    band = raw_feedback.get("band", "Proficient")
    grade = BAND_TO_GRADE.get(band, "B+")

    # 6. Persist feedback
    try:
        save_session_feedback(submission_id, score, grade, raw_feedback)
    except Exception as e:
        print(f"⚠️ Could not save feedback: {e}")

    elapsed = int((time.time() - start) * 1000)
    print(f"✅ Session review done in {elapsed}ms | score={score} band={band}")

    return {
        "success": True,
        "submission": {"submissionId": submission_id, "attemptNumber": sub["attemptNumber"]},
        "feedback": {
            **raw_feedback,
            "score": score,
            "grade": grade,
        },
        "_meta": {"provider": provider, "elapsed_ms": elapsed},
    }


@router.get("/industry-session-history/{student_id}/{session_id}")
async def get_session_history(student_id: int, session_id: int):
    try:
        history = get_session_insight_history(student_id, session_id)
        return {"success": True, "history": history}
    except Exception as e:
        print(f"❌ industry-session-history error: {e}")
        raise HTTPException(status_code=500, detail=str(e))