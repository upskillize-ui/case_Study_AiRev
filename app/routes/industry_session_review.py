# app/routes/industry_session_review.py
# FINAL — Robust, concurrent-safe, auto-discovers column names.
# Works with any existing industry_sessions table structure.
# Handles N students simultaneously — fully stateless per request.

import time
import json
import os
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional, Dict

from app.database import query, execute
from app.services import ai_service
from app.services.feedback_service import ai_verdict
from app.prompts import AI_DETECTION_CALIBRATION

router = APIRouter(prefix="/api/review", tags=["industry-session"])


# ─── Schema ───────────────────────────────────────────────────────────────────
class IndustrySessionInsightRequest(BaseModel):
    sessionId:   int
    studentId:   int
    insightText: Optional[str] = ""
    fileUrl:     Optional[str] = None
    fileName:    Optional[str] = None


# ─── Table & column discovery (cached per process, safe for concurrent use) ───
_table_cache: Dict[str, Optional[str]] = {}
_col_cache:   Dict[str, Dict[str, str]] = {}

def _probe_table(*candidates: str) -> Optional[str]:
    key = "|".join(candidates)
    if key not in _table_cache:
        ph = ",".join(["%s"] * len(candidates))
        rows = query(
            f"SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = DATABASE() AND table_name IN ({ph}) LIMIT 1",
            tuple(candidates),
        )
        if rows:
            # information_schema returns uppercase keys on some MySQL versions
            row = rows[0]
            val = row.get("table_name") or row.get("TABLE_NAME") or list(row.values())[0]
            _table_cache[key] = val
        else:
            _table_cache[key] = None
    return _table_cache[key]

def _session_table() -> Optional[str]:
    return _probe_table("industry_sessions", "sessions", "lms_sessions")

def _insight_table():
    return _probe_table(
        "industry_session_submissions", "session_submissions",
        "industry_session_insights", "session_insights",
    )

def _lms_insight_table() -> Optional[str]:
    """LMS stores student session insights in `session_feedback`.
    Columns: id, session_id, student_id, rating, key_takeaway, ..."""
    return _probe_table("session_feedback", "industry_session_insights", "session_insights")

def _get_columns(table: str) -> Dict[str, str]:
    """Return {column_name: data_type} for a table. Cached."""
    if table not in _col_cache:
        rows = query(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s",
            (table,)
        )
        result = {}
        for r in rows:
            # Handle both lowercase and uppercase keys from different MySQL versions
            col = r.get("column_name") or r.get("COLUMN_NAME") or ""
            dtype = r.get("data_type") or r.get("DATA_TYPE") or ""
            if col:
                result[col] = dtype
        _col_cache[table] = result
    return _col_cache[table]

def _col(table: str, *candidates: str) -> Optional[str]:
    """Return first candidate column that exists in the table."""
    cols = _get_columns(table)
    for c in candidates:
        if c in cols:
            return c
    return None

def _ensure_insight_table() -> str:
    tbl = _insight_table()
    if not tbl:
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
        # Bust cache so next call finds it
        _table_cache.clear()
        tbl = "industry_session_submissions"
    return tbl


# ─── GET /api/review/industry-sessions-for-student/{student_id} ───────────────
# Returns ALL published sessions for any student.
# New sessions added by faculty auto-appear here for all students.
@router.get("/industry-sessions-for-student/{student_id}")
async def get_sessions_for_student(student_id: int):
    sess_tbl = _session_table()
    if not sess_tbl:
        return {"success": True, "sessions": [], "total": 0,
                "warning": "No industry_sessions table found. Run migration SQL."}

    # Discover what columns actually exist in this table
    c_title    = _col(sess_tbl, "title", "session_title", "name")
    c_mentor   = _col(sess_tbl, "speaker", "mentor_name", "speaker_name", "instructor_name", "host_name")
    c_date     = _col(sess_tbl, "date", "session_date", "scheduled_date", "event_date")
    c_desc     = _col(sess_tbl, "description", "summary", "about")
    c_topics   = _col(sess_tbl, "key_topics", "topics")
    c_outline  = _col(sess_tbl, "session_outline", "outline", "agenda")
    c_status   = _col(sess_tbl, "status", "is_active", "published")
    c_id       = "id"

    # Debug: log discovered config once per process
    if "__logged__" not in _table_cache:
        print(f"ℹ️  Session table: {sess_tbl}")
        print(f"ℹ️  Columns: title={c_title} mentor={c_mentor} date={c_date} "
              f"desc={c_desc} topics={c_topics} status={c_status}")
        _table_cache["__logged__"] = True

    # Build SELECT list from available columns
    selects = [f"s.{c_id}"]
    if c_title:   selects.append(f"s.{c_title} AS title")
    if c_mentor:  selects.append(f"s.{c_mentor} AS mentor_name")
    if c_date:    selects.append(f"s.{c_date} AS session_date")
    if c_desc:    selects.append(f"s.{c_desc} AS description")
    if c_topics:  selects.append(f"s.{c_topics} AS key_topics")
    if c_outline: selects.append(f"s.{c_outline} AS session_outline")

    # Status filter
    if c_status:
        status_filter = f"AND s.{c_status} IN ('completed','live','upcoming','published','active')"
    else:
        status_filter = ""

    ins_tbl = _insight_table()
    lms_ins_tbl = _lms_insight_table()  # session_feedback in LMS

    # Show sessions where student has EITHER:
    #   (a) submitted an insight in the LMS coursework page (session_feedback table), OR
    #   (b) already done a review in AiRev (industry_session_submissions table)

    if ins_tbl and lms_ins_tbl:
        sql = f"""
            SELECT {', '.join(selects)},
                   sub.id           AS submission_id,
                   sub.submitted_at AS submitted_at,
                   sub.score        AS score,
                   sub.has_feedback AS has_feedback
            FROM {sess_tbl} s
            LEFT JOIN {ins_tbl} sub
                ON sub.session_id = s.{c_id}
               AND sub.student_id = %s
               AND sub.id = (
                    SELECT MAX(s2.id) FROM {ins_tbl} s2
                    WHERE s2.session_id = s.{c_id} AND s2.student_id = %s
               )
            WHERE (
                sub.id IS NOT NULL
                OR EXISTS (
                    SELECT 1 FROM {lms_ins_tbl} lms_chk
                    WHERE lms_chk.session_id = s.{c_id}
                      AND lms_chk.student_id = %s
                )
            )
            {status_filter}
            ORDER BY s.{c_id} DESC
        """
        rows = query(sql, (student_id, student_id, student_id))

    elif lms_ins_tbl:
        # No AiRev submissions yet — show sessions with LMS insights only
        sql = f"""
            SELECT {', '.join(selects)},
                   NULL AS submission_id, NULL AS submitted_at,
                   NULL AS score, 0 AS has_feedback
            FROM {sess_tbl} s
            WHERE EXISTS (
                SELECT 1 FROM {lms_ins_tbl} lms_chk
                WHERE lms_chk.session_id = s.{c_id}
                  AND lms_chk.student_id = %s
            )
            {status_filter}
            ORDER BY s.{c_id} DESC
        """
        rows = query(sql, (student_id,))

    elif ins_tbl:
        # Only AiRev table — show student's own AiRev submissions
        sql = f"""
            SELECT {', '.join(selects)},
                   sub.id           AS submission_id,
                   sub.submitted_at AS submitted_at,
                   sub.score        AS score,
                   sub.has_feedback AS has_feedback
            FROM {sess_tbl} s
            INNER JOIN {ins_tbl} sub
                ON sub.session_id = s.{c_id}
               AND sub.student_id = %s
            {status_filter}
            ORDER BY s.{c_id} DESC
        """
        rows = query(sql, (student_id,))

    else:
        rows = []

    sessions = []
    for r in rows:
        sessions.append({
            "id":           r[c_id],
            "title":        r.get("title") or f"Session #{r[c_id]}",
            "mentorName":   r.get("mentor_name"),
            "sessionDate":  str(r["session_date"]) if r.get("session_date") else None,
            "description":  r.get("description"),
            "submissionId": r.get("submission_id"),
            "submittedAt":  str(r["submitted_at"]) if r.get("submitted_at") else None,
            "grade":        r.get("score"),
            "hasFeedback":  bool(r.get("has_feedback")),
            # Reviewable — this row only appears because student submitted (guaranteed by query)
            "completed":    True,
        })

    print(f"ℹ️  Sessions for student {student_id}: found {len(sessions)} | "
          f"ins_tbl={ins_tbl} lms_ins_tbl={lms_ins_tbl}")
    return {"success": True, "sessions": sessions, "total": len(sessions)}


# ─── POST /api/review/submit-industry-session ─────────────────────────────────
# Concurrent-safe: each request is fully independent, no shared state.
@router.post("/submit-industry-session")
async def submit_industry_session(req: IndustrySessionInsightRequest):
    start = time.time()
    print(f"ℹ️  Session review start: student={req.studentId} session={req.sessionId}")

    # 1. Load session with discovered columns
    sess_tbl = _session_table()
    if not sess_tbl:
        raise HTTPException(status_code=404, detail="No industry_sessions table. Run migration SQL.")

    c_title    = _col(sess_tbl, "title", "session_title", "name") or "id"
    c_mentor   = _col(sess_tbl, "speaker", "mentor_name", "speaker_name", "instructor_name", "host_name")
    c_desc     = _col(sess_tbl, "description", "summary", "about")
    c_topics   = _col(sess_tbl, "key_topics", "topics")
    c_outline  = _col(sess_tbl, "session_outline", "outline", "agenda")
    c_transcript = _col(sess_tbl, "video_transcript", "transcript")
    c_examples = _col(sess_tbl, "examples_discussed", "examples")
    c_cases    = _col(sess_tbl, "case_studies", "cases")
    c_quotes   = _col(sess_tbl, "key_quotes", "quotes")
    c_assigns  = _col(sess_tbl, "assignments_given", "assignments")
    c_resources = _col(sess_tbl, "resources_shared", "resources")
    c_outcomes = _col(sess_tbl, "learning_outcomes", "outcomes")

    sel = ["id", f"{c_title} AS title"]
    if c_mentor:     sel.append(f"{c_mentor} AS mentor_name")
    if c_desc:       sel.append(f"{c_desc} AS description")
    if c_topics:     sel.append(f"{c_topics} AS key_topics")
    if c_outline:    sel.append(f"{c_outline} AS session_outline")
    if c_transcript: sel.append(f"{c_transcript} AS video_transcript")
    if c_examples:   sel.append(f"{c_examples} AS examples_discussed")
    if c_cases:      sel.append(f"{c_cases} AS case_studies")
    if c_quotes:     sel.append(f"{c_quotes} AS key_quotes")
    if c_assigns:    sel.append(f"{c_assigns} AS assignments_given")
    if c_resources:  sel.append(f"{c_resources} AS resources_shared")
    if c_outcomes:   sel.append(f"{c_outcomes} AS learning_outcomes")

    rows = query(f"SELECT {', '.join(sel)} FROM {sess_tbl} WHERE id = %s LIMIT 1", (req.sessionId,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"Session {req.sessionId} not found.")
    s = rows[0]

    def safe_json(v):
        if not v: return None
        if isinstance(v, (list, dict)): return v
        try: return json.loads(v)
        except: return v

    title       = str(s.get("title") or f"Session #{req.sessionId}")
    mentor      = s.get("mentor_name") or "Industry Mentor"
    key_topics  = safe_json(s.get("key_topics"))
    outline     = s.get("session_outline") or ""
    transcript  = s.get("video_transcript") or ""
    description = s.get("description") or ""
    examples    = s.get("examples_discussed") or ""
    cases       = s.get("case_studies") or ""
    quotes      = s.get("key_quotes") or ""
    assignments = s.get("assignments_given") or ""
    resources   = s.get("resources_shared") or ""
    outcomes    = s.get("learning_outcomes") or ""

    # 2. Build comprehensive session knowledge for AI
    parts = []
    if description:
        parts.append(f"📋 SESSION DESCRIPTION:\n{description}")
    if outcomes:
        parts.append(f"🎯 LEARNING OUTCOMES (what every attendee should walk away with):\n{outcomes}")
    if key_topics:
        t = key_topics if isinstance(key_topics, list) else [str(key_topics)]
        parts.append("🔑 KEY TOPICS COVERED:\n" + "\n".join(f"• {x}" for x in t))
    if outline:
        parts.append(f"📑 SESSION OUTLINE / AGENDA:\n{outline}")
    if examples:
        parts.append(f"💡 REAL-WORLD EXAMPLES MENTOR DISCUSSED:\n{examples}")
    if cases:
        parts.append(f"📊 CASE STUDIES / NAMED COMPANIES:\n{cases}")
    if quotes:
        parts.append(f"💬 KEY QUOTES FROM MENTOR:\n{quotes}")
    if assignments:
        parts.append(f"📝 ASSIGNMENT GIVEN IN SESSION:\n{assignments}")
    if resources:
        parts.append(f"📚 RESOURCES SHARED:\n{resources}")
    if transcript:
        parts.append(f"🎥 VIDEO TRANSCRIPT (excerpt):\n{transcript[:4000]}")

    session_knowledge = "\n\n".join(parts) if parts else (
        f"Session titled '{title}' by {mentor}. "
        "⚠️ No content metadata stored — review will be based on title only. "
        "Ask faculty to add key_topics, examples, and outline for richer reviews."
    )

    # 3. Get student insight — prefer explicit text, else pull from LMS session_feedback
    insight = (req.insightText or "").strip()

    if not insight:
        lms_tbl = _lms_insight_table()
        if lms_tbl:
            try:
                lms_rows = query(
                    f"SELECT key_takeaway, rating FROM {lms_tbl} "
                    f"WHERE session_id=%s AND student_id=%s ORDER BY id DESC LIMIT 1",
                    (req.sessionId, req.studentId)
                )
                if lms_rows:
                    takeaway = (lms_rows[0].get("key_takeaway") or "").strip()
                    if takeaway:
                        insight = takeaway
                        print(f"ℹ️  Pulled insight from LMS session_feedback ({len(insight)} chars)")
            except Exception as ex:
                print(f"⚠️ LMS insight fetch error: {ex}")
    if req.fileUrl:
        try:
            from app.utils.file_extractor import extract_text_from_url
            extracted, _ = extract_text_from_url(req.fileUrl, req.fileName or "")
            if extracted:
                insight = f"{insight}\n\n{extracted}".strip()
        except Exception as ex:
            print(f"⚠️ File extract error: {ex}")

    if not insight:
        # Nothing to review — this is a data condition, NOT a scored outcome.
        # status="needs_input" lets frontends render a compose prompt instead
        # of a 0/100 "Needs Improvement" score card, which reads as a failing
        # grade the student never earned. Legacy fields kept for older UIs.
        return {
            "success": True,
            "status": "needs_input",
            "needsInput": True,
            "submission": {"submissionId": 0, "attemptNumber": 0},
            "feedback": {
                "score": None, "grade": "-", "band": None,
                "summary": "Nothing to review yet — write your understanding of the session first, then submit.",
                "dimensions": [], "critical_gaps": [],
                "covered_well": [], "recommendations": [], "hard_truth": "",
            },
        }

    # 4. Save submission (concurrent-safe — each INSERT gets its own row)
    ins_tbl = _ensure_insight_table()
    mx = query(
        f"SELECT MAX(attempt_number) AS mx FROM {ins_tbl} WHERE session_id=%s AND student_id=%s",
        (req.sessionId, req.studentId)
    )
    attempt = (mx[0]["mx"] or 0) + 1
    submission_id = execute(
        f"INSERT INTO {ins_tbl} "
        f"(session_id, student_id, insight_text, file_url, file_name, attempt_number, submitted_at) "
        f"VALUES (%s, %s, %s, %s, %s, %s, NOW())",
        (req.sessionId, req.studentId, insight, req.fileUrl, req.fileName, attempt)
    )

    # 5. Build AI prompt — two-step: (1) understand session, (2) evaluate student
    topic_count = len(key_topics) if isinstance(key_topics, list) else "several"
    prompt = f"""You are AiRev, the AI Industry Session Review agent for Upskillize EcoPro LMS.

YOUR WORKFLOW IS TWO STEPS:

╔═══════════════════════════════════════════════════════════════════╗
║ STEP 1 — UNDERSTAND THE SESSION FIRST                             ║
║ Read the SESSION CONTENT below as if YOU attended it.             ║
║ Identify the 5-8 most important concepts the mentor taught.       ║
║ This is your ground truth — what every attendee SHOULD know.      ║
╚═══════════════════════════════════════════════════════════════════╝

╔═══════════════════════════════════════════════════════════════════╗
║ STEP 2 — EVALUATE STUDENT'S UNDERSTANDING                         ║
║ Now read the STUDENT INSIGHT.                                     ║
║ For EACH concept you identified in Step 1:                        ║
║   • Did the student mention it? (Yes / Partial / No)              ║
║   • Did they understand it correctly?                             ║
║   • Did they connect it to BFSI / their career?                   ║
║ Calculate comprehension % = (concepts grasped / total concepts).  ║
║ Be HONEST. If they only restated the title, comprehension is 5%.  ║
╚═══════════════════════════════════════════════════════════════════╝

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SESSION: "{title}"  ·  MENTOR: {mentor}

{session_knowledge}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

STUDENT'S WRITTEN UNDERSTANDING (Attempt #{attempt}):
\"\"\"{insight}\"\"\"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

REVIEW RULES — NON-NEGOTIABLE:
• Hard truth, no sugar coating. If understanding is shallow, name it.
• Every claim grounded in either the session content OR the student's exact words.
• covered_well lists ONLY genuine strengths — no false praise.
• critical_gaps name EXACT concepts from the session they missed.
• recommendations tied to THIS session's actual content, not generic advice.
• Comprehension % must match the depth — restating one line ≠ understanding.

{AI_DETECTION_CALIBRATION}

AUTHORSHIP RULE: aiLikelihoodPercent is ADVISORY ONLY — it must NOT influence
any dimension score or the band. Score the answer on its quality alone, then
estimate authorship separately.

DIMENSION SCORES (0=absent, 1=surface, 2=partial, 3=solid, 4=expert):
1. Session Comprehension — grasped the core argument?
2. Key Point Coverage — how many of the mentor's main points captured?
3. Industry Context — understood BFSI/sector implications?
4. Critical Thinking — went beyond restating, formed own view?
5. Practical Application — connected to real roles/career?

BAND: 16-20=Outstanding · 12-15=Strong · 8-11=Proficient · 0-7=Emerging

Return ONLY valid JSON (no markdown, no preamble, no backticks):
{{
  "comprehension_percentage": 0,
  "concept_coverage": [
    {{"concept": "Exact concept from session", "status": "covered|partial|missed", "evidence": "What student wrote or didn't"}}
  ],
  "band": "Emerging|Proficient|Strong|Outstanding",
  "dimensions": [
    {{"name": "Session Comprehension", "score": 0, "note": "evidence"}},
    {{"name": "Key Point Coverage",    "score": 0, "note": "X of Y points captured"}},
    {{"name": "Industry Context",      "score": 0, "note": "evidence"}},
    {{"name": "Critical Thinking",     "score": 0, "note": "evidence"}},
    {{"name": "Practical Application", "score": 0, "note": "evidence"}}
  ],
  "critical_gaps": ["Exact concept from session that student missed — and why it matters"],
  "covered_well":  ["Specific thing they got right with evidence from their text"],
  "recommendations": ["Actionable step tied to THIS session's content"],
  "hard_truth": "2-3 direct sentences. Name what's missing. No softening.",
  "summary": "2-sentence overall verdict including comprehension %",
  "next_action": "One sharp next step",
  "aiLikelihoodPercent": <0-100 integer, calibrated against the anchors above — advisory only, never affects scores>,
  "aiDetectionReason": "<one sentence: which 2-3 textual signals drove your estimate>"
}}"""

    raw = None
    provider = "claude"
    try:
        ai_resp = ai_service.call_claude(prompt, max_tokens=2000)
        clean = ai_resp.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        # Handle case where model adds preamble before JSON
        if not clean.startswith("{"):
            clean = clean[clean.find("{"):]
        raw = json.loads(clean)
    except Exception as e:
        print(f"⚠️ AI failed: {e} — using structured fallback")
        provider = "fallback"
        raw = {
            "comprehension_percentage": 0,
            "concept_coverage": [],
            "band": "Emerging",
            "dimensions": [
                {"name": "Session Comprehension", "score": 1, "note": "AI service temporarily unavailable. Please retry for full review."},
                {"name": "Key Point Coverage",    "score": 1, "note": "Retry for detailed evaluation."},
                {"name": "Industry Context",      "score": 1, "note": "Retry for detailed evaluation."},
                {"name": "Critical Thinking",     "score": 1, "note": "Retry for detailed evaluation."},
                {"name": "Practical Application", "score": 1, "note": "Retry for detailed evaluation."},
            ],
            "critical_gaps": ["Could not evaluate — AI service unavailable. Hit Re-analyze for full review."],
            "covered_well": [],
            "recommendations": ["Retry in a moment for the full AI-powered review."],
            "hard_truth": "AI service was temporarily unavailable. This is a placeholder score. Click Re-analyze to get real feedback.",
            "summary": "Temporary fallback. Retry for actual review.",
            "next_action": "Hit Re-analyze in AiRev for the full content-aware review.",
            "aiLikelihoodPercent": 50,
            "aiDetectionReason": "Unable to assess — AI service unavailable.",
        }

    dims  = raw.get("dimensions", [])
    total = sum(d.get("score", 0) for d in dims)
    score = round((total / 20) * 100, 1)
    band  = raw.get("band", "Emerging")
    # Honest letter mapping — Emerging (0-7/20) is a C, not a B.
    grade = {"Outstanding": "A+", "Strong": "A", "Proficient": "B+", "Emerging": "C"}.get(band, "C")

    # Authorship indicator — advisory only, never touches score/band/grade.
    try:
        ai_pct = int(round(float(raw.get("aiLikelihoodPercent", 50))))
    except (TypeError, ValueError):
        ai_pct = 50
    ai_pct = max(0, min(100, ai_pct))
    raw["aiLikelihoodPercent"]    = ai_pct
    raw["humanLikelihoodPercent"] = 100 - ai_pct
    raw["aiVerdict"]              = ai_verdict(ai_pct)
    raw["aiDetectionReason"]      = str(raw.get("aiDetectionReason", "") or "")

    # 6. Persist feedback
    try:
        execute(
            f"UPDATE {ins_tbl} "
            f"SET score=%s, grade=%s, band=%s, feedback_json=%s, has_feedback=1, reviewed_at=NOW() "
            f"WHERE id=%s",
            (score, grade, band, json.dumps(raw), submission_id)
        )
    except Exception as e:
        print(f"⚠️ Feedback save error: {e}")

    elapsed = int((time.time() - start) * 1000)
    print(f"✅ Session {req.sessionId} student {req.studentId} — {elapsed}ms | {score} {band}")

    return {
        "success": True,
        "submission": {"submissionId": submission_id, "attemptNumber": attempt},
        "feedback": {**raw, "score": score, "grade": grade},
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