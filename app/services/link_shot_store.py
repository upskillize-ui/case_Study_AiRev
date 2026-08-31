# app/services/link_shot_store.py
# ---------------------------------------------------------------------------
# Keep the picture of a rendered link, so something other than this process can
# use it.
#
# WHY. link_renderer already opens every submitted link in a real browser and
# photographs it. That picture is the only honest visual of a Gamma deck, a
# Lovable site or a Claude artifact — and for this cohort it is very often the
# ONLY artefact at all: five consecutive live submissions on 31 Aug carried
# "Submitted link: https://..." and nothing else in the answer box.
#
# But it was kept in a module-level dict, eight at a time, evicted within
# minutes (LINK_SCREENSHOT_KEEP). Nothing outside the Space could ever see it,
# so the LMS was about to grow a SECOND headless browser to take the same
# photograph of the same page — a second rate limiter, a second SSRF guard, and
# two interstitial phrase lists to drift apart.
#
# So: render once, store the result, use it twice. The row also records the
# FAILURES, because "AiRev opened this link and could not read it" is exactly
# what a downstream consumer needs to know before putting it in front of anyone.
#
# HOW THE TARGET GETS HERE. A screenshot is taken deep inside link reading,
# which knows a URL and nothing else; the submission id lives at the top of the
# review. Rather than thread it through five call sites and every return path,
# the review sets a context target once — the same contextvar pattern
# app/database.py already uses for the tenant. Unset means store nothing, so
# canaries and ad-hoc renders write no rows.
#
# Env:
#   LINK_SHOT_STORE=1                     off unless set
#   CLOUDINARY_CLOUD_NAME / _API_KEY / _API_SECRET
#   LINK_SHOT_FOLDER                      default upskillize/link-shots
# ---------------------------------------------------------------------------

from __future__ import annotations

import contextvars
import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

FOLDER = os.getenv("LINK_SHOT_FOLDER", "upskillize/link-shots")
_OFF = {"", "0", "false", "no", "off"}

# (kind, item_id, student_id, submission_id) for the review running right now.
#
# The IDENTITY is (kind, item_id, student_id) — the assignment and the learner —
# not the submission row id. Both sides can compute it: the submit path knows
# assignmentId and studentId before any submission row is read, and the LMS
# knows them from the screen the student is standing on. Keying on the
# submission id would have meant the submit path filing every picture under an
# assignment id and the reader looking for a submission id.
#
# submission_id is carried when it is known, for convenience, never for lookup.
_target: contextvars.ContextVar[Optional[tuple]] = contextvars.ContextVar(
    "link_shot_target", default=None)

_schema_ready = False


def enabled() -> bool:
    """Off unless switched on. A Space without the flag writes nothing."""
    return os.getenv("LINK_SHOT_STORE", "").strip().lower() not in _OFF


def set_target(kind: str, item_id: int, student_id: int,
               submission_id: Optional[int] = None) -> None:
    """Whose work the next rendered links belong to."""
    _target.set((str(kind), int(item_id), int(student_id),
                 int(submission_id) if submission_id else None))


def clear_target() -> None:
    """Forget it. Always called when a review ends, so one review's links can
    never be filed against the next one's submission."""
    _target.set(None)


def current_target() -> Optional[tuple]:
    return _target.get()


# --- pure helpers ----------------------------------------------------------

def url_hash(url: str) -> str:
    """A fixed-width key for a URL. Pure.

    URLs run past what MySQL will index — a LinkedIn share link with its five
    utm parameters is 250 characters before it starts — so the unique key is
    the hash and the URL itself is stored beside it for reading.
    """
    return hashlib.sha1((url or "").strip().encode("utf-8")).hexdigest()


def public_id(kind: str, item_id: int, student_id: int, url: str) -> str:
    """Where this picture lives in Cloudinary. Pure and STABLE.

    Deterministic on purpose: a resubmit overwrites its own picture instead of
    littering the account with one asset per attempt, and a re-render after a
    fix replaces the failed shot rather than leaving both.
    """
    return f"{FOLDER}/{kind}_{int(item_id)}_{int(student_id)}_{url_hash(url)[:12]}"


def cloudinary_ready() -> bool:
    """All three credentials present. Pure-ish (reads env only)."""
    return all(os.getenv(k) for k in
               ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY",
                "CLOUDINARY_API_SECRET"))


def worth_recording(shot_b64: str, why: str) -> bool:
    """Is there anything to write down? Pure.

    A row is worth writing when there is a picture OR a reason there is not.
    Both are useful downstream: one is the artefact, the other is the warning
    not to build a post out of a link nobody could open. Silence is not.
    """
    return bool((shot_b64 or "").strip()) or bool((why or "").strip())


# --- the store -------------------------------------------------------------

def ensure_schema() -> None:
    """Create the table once per process. Safe to call repeatedly."""
    global _schema_ready
    if _schema_ready:
        return
    from app.database import execute
    execute("""CREATE TABLE IF NOT EXISTS submission_renders (
      id INT AUTO_INCREMENT PRIMARY KEY,
      kind VARCHAR(20) NOT NULL,
      item_id INT NOT NULL,
      student_id INT NOT NULL,
      submission_id INT NULL,
      url VARCHAR(700) NOT NULL,
      url_hash CHAR(40) NOT NULL,
      shot_url VARCHAR(600) NULL,
      title VARCHAR(300) NULL,
      final_url VARCHAR(700) NULL,
      unreadable VARCHAR(300) NULL,
      created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
      updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
      UNIQUE KEY uq_render (kind, item_id, student_id, url_hash),
      KEY idx_render_student (student_id),
      KEY idx_render_submission (submission_id)
    )""")
    _schema_ready = True


def _upload(shot_b64: str, kind: str, item_id: int, student_id: int,
            url: str) -> str:
    """Put the JPEG in Cloudinary, return its URL. "" on any failure."""
    if not shot_b64 or not cloudinary_ready():
        return ""
    try:
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(
            cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
            api_key=os.getenv("CLOUDINARY_API_KEY"),
            api_secret=os.getenv("CLOUDINARY_API_SECRET"),
            secure=True,
        )
        res = cloudinary.uploader.upload(
            b"data:image/jpeg;base64," + shot_b64.encode("ascii"),
            public_id=public_id(kind, item_id, student_id, url),
            overwrite=True,
            resource_type="image",
        )
        return str(res.get("secure_url") or "")
    except Exception as e:
        logger.warning("link shot upload failed (%s)", type(e).__name__)
        return ""


def remember(url: str, shot_b64: str = "", why: str = "",
             title: str = "", final_url: str = "") -> str:
    """File one rendered link against the review's submission. Never raises.

    Returns the stored picture's URL, or "" — including when the render failed,
    in which case the row still records WHY so nothing downstream has to guess.

    A review that has already succeeded must never be undone by a failure to
    keep a souvenir of it, so every path here swallows.
    """
    if not enabled():
        return ""
    target = _target.get()
    if not target or not worth_recording(shot_b64, why):
        return ""
    kind, item_id, student_id, submission_id = target
    try:
        ensure_schema()
        shot_url = _upload(shot_b64, kind, item_id, student_id, url)
        from app.database import execute
        execute(
            """INSERT INTO submission_renders
                 (kind, item_id, student_id, submission_id, url, url_hash,
                  shot_url, title, final_url, unreadable)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON DUPLICATE KEY UPDATE
                 submission_id = VALUES(submission_id),
                 shot_url   = COALESCE(NULLIF(VALUES(shot_url), ''), shot_url),
                 title      = VALUES(title),
                 final_url  = VALUES(final_url),
                 unreadable = VALUES(unreadable)""",
            (kind, item_id, student_id, submission_id,
             (url or "")[:700], url_hash(url), shot_url[:600] or None,
             (title or "")[:300] or None, (final_url or "")[:700] or None,
             (why or "")[:300] or None))
        if shot_url:
            logger.info("kept link shot for %s %s student %s: %s",
                        kind, item_id, student_id, url)
        return shot_url
    except Exception as e:
        logger.warning("could not keep link shot (%s)", type(e).__name__)
        return ""


def read_renders(kind: str, item_id: int, student_id: int) -> list:
    """Every stored render for one learner's work, newest first. Never raises."""
    try:
        from app.database import query
        return query(
            """SELECT url, shot_url, title, final_url, unreadable, updated_at
                 FROM submission_renders
                WHERE kind = %s AND item_id = %s AND student_id = %s
                ORDER BY id DESC""",
            (kind, int(item_id), int(student_id))) or []
    except Exception as e:
        logger.warning("could not read link shots (%s)", type(e).__name__)
        return []
