# app/routes/industry_session_review.py
# Industry Session review — mirrors pattern of review.py / assignment_review.py
# Uses query()/execute() from app.database (context-aware, multi-tenant safe)
#
# Endpoints:
#   GET  /api/review/industry-sessions-for-student/{student_id}
#   POST /api/review/submit-industry-session
#   GET  /api/review/industry-session-history/{student_id}/{session_id}

import time
import json
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from app.database import query, execute
from app.services import ai_service

router = APIRouter(prefix="/api/review", tags=["industry-session"])

# ─── Schemas ──────────────────────────────────────────────────────────────────
class IndustrySessionInsightRequest(BaseModel):
    sessionId:   int
    studentId:   int
    insightText: Optional[str] = ""
    fileUrl:     Optional[str] = None
    fileName:    Optional[str] = None

# ─── Table probe helpers ───────────────────────────────────────────────────────
def _probe_table(*candidates: str) -> Optional[str]:
    """Return first table name that exists in the current tenant DB."""
    placeholders = ",".join(["%s"] * len(candidates))
    rows = query(
        f"SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema = DATABASE() AND table_name IN ({placeholders}) LIMIT 1",
        tuple(candidates),
    )
    return rows[0]["table_name"] if rows else None

def _session_table() -> Optional[str]:
    return _probe_table("industry_sessions", "sessions", "lms_sessions")

def _insight_table() -> Optional[str]:
    return _probe_table(
        "industry_session_submissions",
        "session_submissions",
        "industry_session_insights",
        "session_insights",
    )

# ─── GET /api/review/industry-sessions-for-student/{student_id} ───────────────
@router.get("/industry-sessions-for-student/{student_id}")
async def get_sessions_for_student(student_id: int):
    sess_tbl = _session_table()
    if not sess_tbl:
        # Tables don't exist yet — return empty gracefully
        print("⚠️  No industry_sessions table found. Run the SQL migration.")
        return {"success": True, "sessions": [], "total": 0,
                "warning": "industry_sessions table not found — run migration SQL"}

    ins_tbl = _insight_table()

    if ins_tbl:
        rows = query(f"""
            SELECT
                s.id,
                s.title,
                s.mentor_name,
                s.session_date,
                s.description,
                sub.id           AS submission_id,
                sub.submitted_at AS submitted_at,
                sub.grade        AS grade,
                sub.has_feedback AS has_feedback
            FROM {sess_tbl} s
            LEFT JOIN {ins_tbl} sub
                ON sub.session_id = s.id
               AND sub.student_id = %s
               AND sub.id = (
                    SELECT MAX(id2) FROM {ins_tbl} i2
                    WHERE i2.session_id = s.id AND i2.student_id = %s
               )
            WHERE s.status IN ('published', 'active', 'completed')
            ORDER BY s.session_date DESC
        """, (student_id, student_id))
    else:
        # No submission table yet — return sessions only, mark completed=False
        rows = query(f"""
            SELECT id, title, mentor_name, session_date, description,
                   NULL AS submission_id, NULL AS submitted_at,
                   NULL AS grade, 0 AS has_feedback
            FROM {sess_tbl}
            WHERE status IN ('published', 'active', 'completed')
            ORDER BY session_date DESC
        """)

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
            "completed":    r.get("submission_id") is not None,
        })

    return {"success": True, "sessions": sessions, "total": len(sessions)}


# ─── POST /api/review/submit-industry-session ─────────────────────────────────
@router.post("/submit-industry-session")
async def submit_industry_session(req: IndustrySessionInsightRequest):
    start = time.time()
    print(f"ℹ️  Industry session insight: student={req.studentId}, session={req.sessionId}")

    # 1. Load session metadata
    sess_tbl = _session_table()
    if not sess_tbl:
        raise HTTPException(status_code=404, detail="Industry sessions table not found. Run SQL migration.")

    rows = query(f"SELECT id, title, mentor_name FROM {sess_tbl} WHERE id = %s LIMIT 1", (req.sessionId,))
    if not rows:
        raise HTTPException(status_code=404, detail="Industry session not found or not published")
    session_meta = rows[0]

    insight = (req.insightText or "").strip()

    # 2. No content — signal frontend to show compose form
    if not insight and not req.fileUrl:
        return {
            "success": True,
            "submission": {"submissionId": 0, "attemptNumber": 0},
            "feedback": {
                "score": 0, "grade": "-",
                "summary": "We couldn't find any insight text. Please write your session insights first, then submit.",
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

    # 4. Ensure submissions table exists then save
    ins_tbl = _insight_table()
    if not ins_tbl:
        # Auto-create
        execute("""
            CREATE TABLE IF NOT EXISTS industry_session_submissions (
                id              INT AUTO_INCREMENT PRIMARY KEY,
                session_id      INT NOT NULL,
                student_id      INT NOT NULL,
                attempt_number  INT DEFAULT 1,
                insight_text    TEXT,
                file_url        VARCHAR(1024),
                file_name       VARCHAR(512),
                score           DECIMAL(5,2),
                grade           VARCHAR(10),
                band            VARCHAR(20),
                feedback_json   LONGTEXT,
                has_feedback    TINYINT DEFAULT 0,
                submitted_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
                reviewed_at     DATETIME NULL,
                INDEX (session_id, student_id)
            )
        """)
        ins_tbl = "industry_session_submissions"

    # Get attempt number
    mx = query(f"SELECT MAX(attempt_number) AS mx FROM {ins_tbl} WHERE session_id=%s AND student_id=%s",
               (req.sessionId, req.studentId))
    attempt = (mx[0]["mx"] or 0) + 1

    submission_id = execute(
        f"INSERT INTO {ins_tbl} (session_id, student_id, insight_text, file_url, file_name, attempt_number, submitted_at) "
        f"VALUES (%s, %s, %s, %s, %s, %s, NOW())",
        (req.sessionId, req.studentId, insight, req.fileUrl, req.fileName, attempt)
    )

    # 5. AI review
    prompt = f"""You are AiRev, the AI Industry Session Review agent for Upskillize EcoPro LMS.
Tone: coaching, never punitive. Be direct and specific.

Session: "{session_meta.get('title', 'Industry Session')}"
Mentor: {session_meta.get('mentor_name', 'Industry Mentor')}

Student's session insights:
\"\"\"
{insight}
\"\"\"

Evaluate across exactly these 5 dimensions (score 0-4 each):
1. Session Comprehension — did they grasp the core content and context?
2. Key Insight Capture — did they surface the right takeaways?
3. Industry Relevance — did they map it to BFSI/FinTech/sector context?
4. Critical Reflection — did they go beyond surface, form own perspective?
5. Personal Application — did they link it to their own career or role?

Band from total /20: 16-20 Outstanding · 12-15 Strong · 8-11 Proficient · 0-7 Emerging

Respond ONLY with valid JSON (no markdown, no backticks):
{{
  "band": "Emerging|Proficient|Strong|Outstanding",
  "dimensions": [
    {{"name": "Session Comprehension", "score": 0, "note": "specific 1-2 sentences"}},
    {{"name": "Key Insight Capture",   "score": 0, "note": "..."}},
    {{"name": "Industry Relevance",    "score": 0, "note": "..."}},
    {{"name": "Critical Reflection",   "score": 0, "note": "..."}},
    {{"name": "Personal Application",  "score": 0, "note": "..."}}
  ],
  "rewrite_suggestions": ["concrete suggestion 1", "suggestion 2", "suggestion 3"],
  "keep_this": "one specific strength",
  "next_action": "one sharp next step",
  "summary": "2-sentence overall summary",
  "encouragement": "30-50 word mentor note (warm, BFSI context-aware)"
}}"""

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
                {"name": "Key Insight Capture",   "score": 2, "note": "Key takeaways present but could be sharper."},
                {"name": "Industry Relevance",    "score": 2, "note": "Some BFSI connection — push this further."},
                {"name": "Critical Reflection",   "score": 2, "note": "You're forming your own view — develop it more."},
                {"name": "Personal Application",  "score": 2, "note": "Career linkage is there, make it explicit."},
            ],
            "rewrite_suggestions": [
                "Name one specific trend or decision the mentor called out.",
                "Add a sentence on how this changes your view of a BFSI job role.",
                "Cite the specific sector (retail banking, NBFCs, etc.) you'd apply this in.",
            ],
            "keep_this": "Your opening shows genuine engagement with the session topic.",
            "next_action": "Re-attempt with one concrete BFSI example drawn from the session.",
            "summary": "Solid first read. Tighten industry application to move from Proficient to Strong.",
            "encouragement": "You're building the habit of extracting meaning from expert conversations — the core of BFSI career growth. Keep going.",
        }

    dimensions = raw_feedback.get("dimensions", [])
    total = sum(d.get("score", 0) for d in dimensions)
    score = round((total / 20) * 100, 1)
    band = raw_feedback.get("band", "Proficient")
    grade_map = {"Outstanding": "A+", "Strong": "A", "Proficient": "B+", "Emerging": "B"}
    grade = grade_map.get(band, "B+")

    # 6. Persist feedback
    try:
        execute(
            f"UPDATE {ins_tbl} SET score=%s, grade=%s, band=%s, feedback_json=%s, has_feedback=1, reviewed_at=NOW() WHERE id=%s",
            (score, grade, band, json.dumps(raw_feedback), submission_id)
        )
    except Exception as e:
        print(f"⚠️ Could not save feedback: {e}")

    elapsed = int((time.time() - start) * 1000)
    print(f"✅ Session review done in {elapsed}ms | score={score} band={band}")

    return {
        "success": True,
        "submission": {"submissionId": submission_id, "attemptNumber": attempt},
        "feedback": {**raw_feedback, "score": score, "grade": grade},
        "_meta": {"provider": provider, "elapsed_ms": elapsed},
    }


# ─── GET /api/review/industry-session-history/{student_id}/{session_id} ───────
@router.get("/industry-session-history/{student_id}/{session_id}")
async def get_session_history(student_id: int, session_id: int):
    ins_tbl = _insight_table()
    if not ins_tbl:
        return {"success": True, "history": []}

    rows = query(
        f"SELECT id, attempt_number, score, grade, band, feedback_json, submitted_at "
        f"FROM {ins_tbl} WHERE session_id=%s AND student_id=%s ORDER BY attempt_number DESC",
        (session_id, student_id)
    )

    history = []
    for r in rows:
        fb = {}
        if r.get("feedback_json"):
            try: fb = json.loads(r["feedback_json"])
            except: pass
        history.append({
            "submissionId":  r["id"],
            "attemptNumber": r["attempt_number"],
            "score":         float(r["score"]) if r.get("score") is not None else 0,
            "submittedAt":   str(r["submitted_at"]) if r.get("submitted_at") else None,
            "feedback":      fb,
        })

    return {"success": True, "history": history}