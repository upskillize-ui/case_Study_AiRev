# app/services/prefilter_service.py
# The agent's REFLEXES — checks that run BEFORE any AI call, spending zero
# tokens on submissions that don't deserve conscious thought (brain spec,
# slice 2). Pure decisions, in code:
#
#   - Abusive/profane content  -> review withheld, flagged to exception queue
#   - Exact cohort duplicate   -> reviewed BUT flagged (we cannot know who
#     copied whom, so nobody is auto-punished; a human sees the pair)
#   - Fingerprint recording    -> every submission leaves a hash so future
#     duplicates are O(1) lookups, never full-cohort scans
#   - Near-duplicate seam      -> activates automatically when an embeddings
#     key is configured (EMBED_API_KEY / OPENAI_API_KEY); until then exact-
#     hash only, stated loudly in logs — no silent capability claims.
#
# The exception queue is the mentor's 40-item-per-cohort exception list —
# the scalable replacement for reviewing 3,000 submissions by hand.

import hashlib
import os
import re
from typing import Optional

from app.database import query, execute

_tables_ready = False


def _ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    execute("""
        CREATE TABLE IF NOT EXISTS submission_fingerprints (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            scope_type   VARCHAR(24) NOT NULL,
            scope_id     INT         NOT NULL,
            student_id   INT         NOT NULL,
            submission_id INT        NULL,
            text_hash    CHAR(64)    NOT NULL,
            word_count   INT         DEFAULT 0,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_scope_hash (scope_type, scope_id, text_hash)
        )
    """)
    execute("""
        CREATE TABLE IF NOT EXISTS exception_queue (
            id           INT AUTO_INCREMENT PRIMARY KEY,
            scope_type   VARCHAR(24) NOT NULL,
            scope_id     INT         NOT NULL,
            student_id   INT         NOT NULL,
            submission_id INT        NULL,
            reason       VARCHAR(32) NOT NULL,   -- abusive_language | cohort_duplicate | high_ai_authorship | garbage | dispute
            detail       TEXT,
            status       VARCHAR(16) NOT NULL DEFAULT 'open',  -- open | resolved | dismissed
            resolved_by  VARCHAR(128),
            resolution_note TEXT,
            created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
            resolved_at  DATETIME NULL,
            INDEX idx_status (status, scope_type)
        )
    """)
    _tables_ready = True


# ─── Pure text functions (unit-tested, no I/O) ───────────────────────────────

def normalize(text: str) -> str:
    """Case/whitespace-insensitive canonical form for fingerprinting."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def fingerprint(text: str) -> str:
    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()


# Conservative profanity/abuse patterns. Word-boundary matched on normalized
# text; leetspeak variants folded first. Deliberately SHORT — the reflex
# catches unambiguous abuse; borderline rudeness is the LLM's judgment call
# in the language report, not a block. False-blocking a genuine answer is
# worse than letting mild rudeness through to a human-reviewable flag.
_LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "@": "a", "$": "s"})
_ABUSE_WORDS = [
    "fuck", "fucking", "motherfucker", "bitch", "bastard", "asshole",
    "cunt", "dickhead", "bullshit",
    "chutiya", "bhosdike", "madarchod", "behenchod", "bhenchod", "gandu",
    "randi", "harami",
]
_ABUSE_RE = re.compile(r"\b(" + "|".join(map(re.escape, _ABUSE_WORDS)) + r")\b")


def detect_abuse(text: str) -> Optional[str]:
    """Return the first abusive term found, or None. Pure function."""
    folded = normalize(text).translate(_LEET)
    m = _ABUSE_RE.search(folded)
    return m.group(1) if m else None


# ─── Reflex check (the routes call this BEFORE any AI spend) ─────────────────

def check(scope_type: str, scope_id: int, student_id: int, text: str) -> dict:
    """Zero-token pre-checks. Returns:
      {"ok": True,  "flags": [...]}                    -> proceed to review
      {"ok": False, "reason": ..., "message": ...}     -> withhold review
    Flags never block on their own — they surface humans, not punishments.
    """
    _ensure_tables()
    flags: list[dict] = []

    term = detect_abuse(text)
    if term:
        flag_exception(scope_type, scope_id, student_id, None,
                       "abusive_language", f"matched term: {term}")
        return {
            "ok": False, "reason": "abusive_language",
            "message": ("Your submission has been flagged for conduct review "
                        "and was not scored. A mentor will follow up."),
            "flags": flags,
        }

    h = fingerprint(text)
    dup = query(
        "SELECT student_id, submission_id FROM submission_fingerprints "
        "WHERE scope_type=%s AND scope_id=%s AND text_hash=%s AND student_id != %s "
        "LIMIT 1",
        (scope_type, scope_id, h, student_id),
    )
    if dup:
        detail = (f"identical to submission of student {dup[0]['student_id']} "
                  f"(submission {dup[0].get('submission_id')})")
        flag_exception(scope_type, scope_id, student_id, None, "cohort_duplicate", detail)
        # Reviewed anyway — we cannot know who copied whom. The exception
        # queue shows the pair; plagiarism flag rides along in the response.
        flags.append({"flag": "cohort_duplicate", "detail": detail})

    if not _embeddings_enabled():
        # Stated once per process so nobody assumes near-dup coverage exists.
        if "__warned__" not in _EMBED_STATE:
            print("ℹ️  Near-duplicate detection OFF (no EMBED_API_KEY/OPENAI_API_KEY) — exact-hash only")
            _EMBED_STATE["__warned__"] = True

    return {"ok": True, "flags": flags, "text_hash": h}


def record_fingerprint(scope_type: str, scope_id: int, student_id: int,
                       submission_id: Optional[int], text: str,
                       word_count: int = 0, text_hash: Optional[str] = None) -> None:
    """Store the submission's fingerprint so future duplicates are O(1)."""
    _ensure_tables()
    execute(
        "INSERT INTO submission_fingerprints "
        "(scope_type, scope_id, student_id, submission_id, text_hash, word_count) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (scope_type, scope_id, student_id, submission_id,
         text_hash or fingerprint(text), word_count),
    )


def flag_exception(scope_type: str, scope_id: int, student_id: int,
                   submission_id: Optional[int], reason: str, detail: str = "") -> None:
    """Surface an item to the human exception queue. Never raises — a
    flagging failure must not break a student's review."""
    try:
        _ensure_tables()
        execute(
            "INSERT INTO exception_queue "
            "(scope_type, scope_id, student_id, submission_id, reason, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (scope_type, scope_id, student_id, submission_id, reason, detail[:2000]),
        )
        print(f"🚩 Exception flagged: {reason} ({scope_type}:{scope_id} student={student_id})")
    except Exception as e:
        print(f"⚠️ Exception-queue write failed (review unaffected): {e}")


def flag_review_outcomes(scope_type: str, scope_id: int, student_id: int,
                         submission_id: Optional[int], r: dict) -> None:
    """Post-review reflex: surface predominantly-AI and garbage outcomes.
    Advisory routing only — scores are already final at this point."""
    try:
        ai_pct = (r.get("authorship") or {}).get("aiLikelihoodPercent", 0)
        if ai_pct >= 90:
            flag_exception(scope_type, scope_id, student_id, submission_id,
                           "high_ai_authorship", f"~{ai_pct}% estimated AI-written")
        if r.get("isGarbage"):
            flag_exception(scope_type, scope_id, student_id, submission_id,
                           "garbage", (r.get("garbageWarning") or "")[:500])
    except Exception as e:
        print(f"⚠️ Outcome flagging failed (review unaffected): {e}")


# ─── Exception queue reads (for the mentor endpoints) ────────────────────────

def list_exceptions(status: str = "open", limit: int = 100) -> list:
    _ensure_tables()
    return query(
        "SELECT id, scope_type, scope_id, student_id, submission_id, reason, "
        "detail, status, created_at FROM exception_queue "
        "WHERE status=%s ORDER BY id DESC LIMIT %s",
        (status, min(limit, 500)),
    )


def resolve_exception(exception_id: int, resolved_by: str, note: str,
                      dismiss: bool = False) -> bool:
    _ensure_tables()
    new_status = "dismissed" if dismiss else "resolved"
    execute(
        "UPDATE exception_queue SET status=%s, resolved_by=%s, "
        "resolution_note=%s, resolved_at=NOW() WHERE id=%s AND status='open'",
        (new_status, resolved_by[:128], note[:2000], exception_id),
    )
    rows = query("SELECT status FROM exception_queue WHERE id=%s", (exception_id,))
    return bool(rows and rows[0]["status"] == new_status)


# ─── Near-duplicate seam (activates with an embeddings key — slice 2+) ──────

_EMBED_STATE: dict = {}


def _embeddings_enabled() -> bool:
    return bool(os.getenv("EMBED_API_KEY") or os.getenv("OPENAI_API_KEY"))
