"""Home reader endpoints (04 Sep 2026) — staff only.

GET  /api/review/links/blocked  what our browser could not open, one row per
                                (submission, url), latest attempt per learner
POST /api/review/links/seed     what a home browser saw for one of them

The home reader (tools/home_reader.py) is a dumb courier: it opens a page
and posts what it saw. Every judgement — "is this a sign-in wall", "is this
the site's own shell" — stays here, in the same pure functions our own
renderer uses, so a seeded page can never be trusted more than a rendered
one. See app/services/link_seed_service.py.
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.database import tquery
from app.routes.assignment_review import get_tenant
from app.routes.review_jobs import _require_staff
from app.services import link_renderer, link_seed_service, student_notices
from app.tenants import Tenant
from app.utils.submission_intake import find_urls, is_web_page

router = APIRouter(prefix="/api/review/links", tags=["link-seeds"])

# The refusal wordings that mean OUR reader, not the learner, was the
# problem. Reader-blocked (grade_guard.READER_BLOCKED_MESSAGE) and the two
# guard refusals the sweeper already treats as ours.
_OUR_WORDINGS = ("lets us read it",
                 "could not finish reviewing this attempt",
                 "could not complete a fair review")

_URL_MAX = 700


def links_of(notes: str, file_path: str, file_name: str = "") -> list:
    """Every URL intake would try to OPEN for this row, in order. Pure.

    Reuses intake's own two rules: URLs in the notes box, and a stored
    file_path that is really a page (the LMS "Link submission" field).
    """
    urls = find_urls(notes or "")
    fp = (file_path or "").strip()
    if fp and is_web_page(fp, file_name) and fp not in urls:
        urls.append(fp)
    return [u for u in urls if len(u) <= _URL_MAX]


def blocked_rows(tenant, course_id: Optional[int], limit: int) -> list:
    """Ungraded rows refused for OUR reasons, latest attempt per learner."""
    ours = " OR ".join(["s.feedback LIKE %s"] * len(_OUR_WORDINGS))
    params: list = [f"%{w}%" for w in _OUR_WORDINGS]
    course = ""
    if course_id:
        course = " AND a.course_id = %s"
        params.append(int(course_id))
    rows = tquery(tenant, f"""
        SELECT s.id, s.assignment_id, s.student_id, s.notes, s.file_path,
               s.file_name, s.submitted_at, a.title
          FROM assignment_submissions s
          JOIN assignments a ON a.id = s.assignment_id
         WHERE s.grade IS NULL
           AND COALESCE(s.status, '') NOT IN ('draft', 'returned')
           AND s.feedback LIKE '%%notGraded%%'
           AND ({ours}){course}
         ORDER BY s.submitted_at DESC, s.id DESC
    """, tuple(params)) or []
    latest: dict = {}
    for r in rows:
        latest.setdefault((r["assignment_id"], r["student_id"]), r)
    picked = sorted(latest.values(), key=lambda r: r["id"])
    return picked[:max(0, limit)]


@router.get("/blocked")
def blocked_links(course_id: Optional[int] = None, limit: int = 400,
                  tenant: Tenant = Depends(get_tenant),
                  x_admin_key: str = Header(default="")):
    _require_staff(x_admin_key)
    out = []
    for r in blocked_rows(tenant, course_id, limit):
        for url in links_of(r.get("notes") or "", r.get("file_path") or "",
                            r.get("file_name") or ""):
            out.append({"submissionId": int(r["id"]),
                        "assignmentId": int(r["assignment_id"]),
                        "assignment": r.get("title") or "",
                        "studentId": int(r["student_id"]),
                        "url": url,
                        "host": urlparse(url).hostname or ""})
    return {"count": len(out), "links": out}


class SeedBody(BaseModel):
    submissionId: int
    url: str
    title: str = ""
    text: str = ""
    screenshotB64: str = ""
    source: str = "home-reader"


@router.post("/seed")
def seed_link(body: SeedBody, tenant: Tenant = Depends(get_tenant),
              x_admin_key: str = Header(default="")):
    _require_staff(x_admin_key)
    url = body.url.strip()
    bad = link_seed_service.validate(url, body.title, body.text, body.screenshotB64)
    if bad:
        raise HTTPException(status_code=400, detail=bad)

    # The same two refusals our own renderer applies, BEFORE anything is
    # stored: a home browser that landed on a sign-in wall or the site's
    # shell has not read the work either.
    gate = (link_renderer.interstitial_reason(body.title, body.text)
            or link_renderer.note_page(url, body.text, body.screenshotB64))
    if gate:
        # A home browser saw the same wall a visitor would. When that wall
        # is the learner's to fix — private link, page gone — say so on the
        # row now, with the share steps for that site; the "site blocked our
        # reader" notice it carried was wrong the moment a person was refused.
        notice = student_notices.unreadable_link_notice(url, gate)
        row = (link_seed_service.tell_learner(tenant, body.submissionId, notice)
               if notice else "untouched")
        return {"accepted": False, "why": gate, "row": row}

    link_seed_service.store(tenant, link_seed_service.Seed(
        url=url, title=body.title, text=body.text,
        screenshot_b64=body.screenshotB64, source=body.source))
    # Our own earlier verdict on this url may still sit in process memory
    # for up to an hour; it must not outrank the seed.
    link_renderer.forget_link(url)
    row = link_seed_service.reoffer(tenant, body.submissionId,
                                    f"seeded by {body.source}")
    return {"accepted": True, "words": len(body.text.split()),
            "screenshot": bool(body.screenshotB64), "row": row}
