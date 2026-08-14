# app/auth.py
# ---------------------------------------------------------------------------
# Staff authorisation, in ONE place.
#
# The tenant key (`x-api-key`) authenticates a TENANT, never a USER — and it is
# compiled into the public JavaScript bundle by Vite, so every student's
# browser holds it and it must be treated as public knowledge. It therefore
# authorises nothing beyond "this request came from a site we serve".
#
# Several routes were nonetheless staff-only in intent while being reachable by
# anyone holding that public key:
#
#   POST /api/review/mentor-approve/{id}    — took mentorId FROM THE BODY, so
#                                             the caller declared their own
#                                             identity, then overwrote any
#                                             submission's grade by primary key
#   GET  /api/review/exceptions             — the whole academic-integrity
#                                             queue, naming flagged learners
#   POST /api/review/exceptions/{id}/resolve— let a flagged learner dismiss
#                                             their own flag, with a
#                                             caller-supplied reviewer name
#   POST /api/review/test                   — an unmetered Claude proxy, billed
#                                             to us, attributed to nobody
#   POST /api/review/prepare/*              — an unattributed Claude call each
#
# require_admin is the same authority ai_service.begin_run_billing already
# trusts: a constant-time comparison against ADMIN_JOB_KEY. It FAILS CLOSED —
# an unset variable denies everyone rather than admitting everyone.
#
# This does NOT solve per-learner authorisation. Read requests scoped by a
# student id in the path are still trust-the-caller, because the service has no
# notion of who is calling. Closing that needs a signed per-user token from the
# LMS; see the audit note in docs. This module closes the STAFF-ONLY holes,
# which are the ones that let a student write.
# ---------------------------------------------------------------------------

import hmac
import os

from fastapi import Header, HTTPException


def _admin_key_matches(supplied: str) -> bool:
    """Constant-time compare against ADMIN_JOB_KEY.

    Both sides are stripped. Tenant keys are already stripped on both sides
    (a pasted trailing newline caused a full 401 outage on 13 Aug); the admin
    key was not, leaving the identical trap — a re-pasted secret with a
    trailing newline would lock staff out with no diagnostic.
    """
    expected = os.getenv("ADMIN_JOB_KEY", "").strip()
    if not expected:
        return False                      # unset => nobody is admin
    return hmac.compare_digest(str(supplied or "").strip(), expected)


async def require_admin(x_admin_key: str = Header(default="")) -> bool:
    """FastAPI dependency: staff only, or 403.

    async on purpose — a sync dependency runs in a worker thread with a COPY of
    the contextvar context, which is exactly how the tenant context was being
    lost. Keeping every dependency async removes the whole class of bug.
    """
    if not _admin_key_matches(x_admin_key):
        raise HTTPException(
            status_code=403,
            detail="Staff only. Send a valid X-Admin-Key header.")
    return True


def is_admin(x_admin_key: str = "") -> bool:
    """Non-raising variant, for routes that stay open but behave differently
    for staff."""
    return _admin_key_matches(x_admin_key)
