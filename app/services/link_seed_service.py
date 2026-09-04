"""Pages read for us from a home connection (04 Sep 2026).

WHY THIS EXISTS. The Space's browser leaves from a datacenter IP. claude.ai,
gamma.app, notion.site and some Lovable apps sit behind Cloudflare's bot
check and show it a "Just a moment..." page instead of the learner's work —
174 submissions on 04 Sep, some waiting a month. The same links open at once
from a home connection. So a small script on the owner's PC (tools/
home_reader.py) opens each blocked link in a real browser, captures what a
visitor sees, and posts it here. The review then proceeds exactly as if our
own browser had rendered the page: same gate checks, same OCR, same rubric.

WHAT IS STORED. One row per URL: title, page text, one JPEG screenshot. The
process-memory link cache dies on every restart; a seed must outlive the
restart that follows the push that needs it, so it is a table.

WHAT A SEED DOES TO THE ROW. Seeding nulls the submission's refusal (grade,
feedback, status back to 'submitted') and relabels its attempt-ledger items
as ours, so the next sweep re-offers it. Nothing is graded here.
"""
from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Optional

from app.database import texecute, tquery

TABLE = "airev_link_seeds"
MAX_TEXT_CHARS = 200_000
MAX_SHOT_CHARS = 2_800_000          # ~2 MB of JPEG, base64

_tables_ready: set = set()
_B64_RE = re.compile(r"^[A-Za-z0-9+/=\r\n]*$")


@dataclass(frozen=True)
class Seed:
    url: str
    title: str
    text: str
    screenshot_b64: str
    source: str


def ensure_table(tenant) -> None:
    key = getattr(tenant, "id", str(tenant))
    if key in _tables_ready:
        return
    texecute(tenant, f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            url            VARCHAR(700) NOT NULL PRIMARY KEY,
            title          VARCHAR(500) NOT NULL DEFAULT '',
            text           MEDIUMTEXT   NOT NULL,
            screenshot_b64 MEDIUMTEXT   NULL,
            source         VARCHAR(60)  NOT NULL DEFAULT 'home-reader',
            seeded_at      DATETIME     DEFAULT CURRENT_TIMESTAMP
        )
    """)
    _tables_ready.add(key)


def validate(url: str, title: str, text: str, screenshot_b64: str) -> str:
    """Why this payload cannot be a seed — or "". Pure."""
    if not url.startswith(("http://", "https://")) or len(url) > 700:
        return "url must be an http(s) link under 700 characters"
    if not text.strip() and not screenshot_b64.strip():
        return "a seed needs page text or a screenshot"
    if len(text) > MAX_TEXT_CHARS:
        return f"text over {MAX_TEXT_CHARS} characters"
    if len(screenshot_b64) > MAX_SHOT_CHARS:
        return "screenshot over 2 MB"
    if screenshot_b64 and not _B64_RE.match(screenshot_b64):
        return "screenshot is not base64"
    if screenshot_b64:
        try:
            base64.b64decode(screenshot_b64, validate=True)
        except Exception:
            return "screenshot is not base64"
    return ""


def store(tenant, seed: Seed) -> None:
    ensure_table(tenant)
    texecute(tenant, f"""
        INSERT INTO {TABLE} (url, title, text, screenshot_b64, source)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE title = VALUES(title), text = VALUES(text),
            screenshot_b64 = VALUES(screenshot_b64), source = VALUES(source),
            seeded_at = CURRENT_TIMESTAMP
    """, (seed.url, seed.title[:500], seed.text, seed.screenshot_b64 or None,
          seed.source[:60]))


def lookup(tenant, url: str) -> Optional[Seed]:
    """The seed for this url, or None. Fails closed — an unreadable seed
    table must never stop a review; the renderer simply runs."""
    if tenant is None:
        return None
    try:
        ensure_table(tenant)
        rows = tquery(tenant, f"""SELECT url, title, text, screenshot_b64, source
                                  FROM {TABLE} WHERE url = %s""", (url,))
    except Exception as e:
        print(f"[SEED] lookup failed for {url[:80]}: {e}")
        return None
    if not rows:
        return None
    r = rows[0]
    return Seed(url=r["url"], title=r.get("title") or "", text=r.get("text") or "",
                screenshot_b64=r.get("screenshot_b64") or "",
                source=r.get("source") or "")


def reoffer(tenant, submission_id: int, label: str) -> str:
    """Put one refused submission back in front of the sweep.

    Clears the refusal and marks every finished attempt in the ledger as
    ours (`our outage — ...`), which is what attempts_spent() excludes.
    Only rows that carry NO mark are touched: a graded row is never reset
    from here. Returns "reoffered", "graded" (untouched) or "missing".
    """
    from app.services.review_job_service import ITEMS_TABLE
    rows = tquery(tenant, "SELECT grade FROM assignment_submissions WHERE id = %s",
                  (int(submission_id),))
    if not rows:
        return "missing"
    if rows[0].get("grade") is not None:
        return "graded"
    texecute(tenant, """
        UPDATE assignment_submissions
           SET grade = NULL, feedback = NULL, status = 'submitted'
         WHERE id = %s AND grade IS NULL
    """, (int(submission_id),))
    texecute(tenant, f"""
        UPDATE {ITEMS_TABLE}
           SET detail = %s
         WHERE submission_id = %s
           AND state IN ('done', 'skipped', 'failed')
           AND detail NOT LIKE 'our outage %%'
    """, (f"our outage — {label}"[:180], int(submission_id)))
    try:
        from app.services import intake_cache
        intake_cache.forget(tenant, submission_id)
    except Exception as e:
        print(f"[SEED] intake cache not cleared for {submission_id}: {e}")
    return "reoffered"
