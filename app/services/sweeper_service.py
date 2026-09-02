# app/services/sweeper_service.py
# ---------------------------------------------------------------------------
# THE SAFETY NET: nothing a learner submits is ever silently lost.
#
# Why this exists (28 Aug 2026). Auto-review is fire-and-forget, and three
# independent leaks meant a submission could be accepted and then never
# reviewed, with no error anywhere:
#
#   1. The LMS hook has no retry. Every submit made while the Space was
#      rebuilding vanished — no queue row, no trace.
#   2. start_worker() returns False when ANY worker is already running, so a
#      live enqueue during a staff batch wrote its row and nobody drained it.
#   3. _finalize() closes a live job as "aborted" when rows are still pending,
#      and resume_after_restart() only resumes jobs in state 'running' — so
#      those rows were never looked at again by anything.
#
# Each of those could be fixed on its own. This is better: the sweeper does
# not trust the queue AT ALL. It reads assignment_submissions — the only
# record that matters — and asks one question: is there work here with no
# mark? Whatever caused the gap, this closes it.
#
# THE COST TRAP THIS AVOIDS. mark_not_graded() deliberately leaves
# grade=NULL and status='submitted' so a corrected resubmission flows through
# the normal path. A sweeper that only looked for "no grade" would therefore
# re-buy every unreadable-file and wrong-task row EVERY NIGHT, for ever, each
# run landing back in exactly the same state. So rows already carrying a
# notGraded verdict are skipped — and because the LMS upsert clears
# `feedback` on resubmit, a learner who fixes their file becomes sweepable
# again automatically. The discriminator IS the student's own action.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os
from typing import Callable, Optional

from app.database import tquery
from app.services.rubric_service import RUBRIC_VERSION as RULES_VERSION

# A runaway sweep must never be able to spend a night's budget. This is a
# ceiling per run, not a target: a healthy cohort sweeps single digits.
MAX_PER_RUN = int(os.getenv("SWEEP_MAX_PER_RUN", "200"))
SWEEP_NOTE = "sweep — submissions with no mark"


def find_unreviewed(tenant, course_ids: Optional[list] = None,
                    limit: int = MAX_PER_RUN) -> list:
    """Latest attempt per (assignment, student) that has work and no mark.

    One SQL pass, then one dict pass — the shape select_rows() proved. Pure
    apart from the read.
    """
    # THE PERMANENT-SKIP LEAK, CLOSED (02 Sep 2026).
    #
    # The old clause was `feedback NOT LIKE '%notGraded%'` — full stop. A row
    # refused by the marker was therefore skipped FOR EVER, whatever we later
    # fixed. Every guard refusal and every unreadable file accumulated, nothing
    # ever came back out, and the "to be graded" list could only grow. That is
    # the shape the cohort was seeing.
    #
    # The cost trap the original clause avoided is real and still avoided: a
    # blanket "retry everything ungraded" re-buys the same refusal every night
    # for ever. So the retry is triggered by the only thing that makes a
    # different outcome POSSIBLE — the marking rules changing. mark_not_graded
    # stamps rulesVersion into the refusal; a row is re-offered exactly once
    # per version bump, and rows refused under the CURRENT rules stay skipped.
    #
    # Rows stamped by no version at all (every refusal written before today)
    # match the "missing stamp" arm and get their one run under the new rules.
    # A LIKE pattern is a VALUE, not query text: single %, no doubling.
    stamp = f'%"rulesVersion": {RULES_VERSION}%'
    sql = """
        SELECT s.id, s.assignment_id, s.student_id, s.submitted_at
        FROM assignment_submissions s
        JOIN assignments a ON a.id = s.assignment_id
        WHERE a.status = 'active'
          AND s.grade IS NULL
          AND COALESCE(s.status, '') <> 'draft'
          AND (CHAR_LENGTH(COALESCE(s.notes, '')) > 0
               OR COALESCE(s.file_path, '') <> '')
          AND (COALESCE(s.feedback, '') NOT LIKE '%%notGraded%%'
               OR COALESCE(s.feedback, '') NOT LIKE %s)
    """
    params: list = [stamp]
    if course_ids:
        marks = ", ".join(["%s"] * len(course_ids))
        sql += f" AND a.course_id IN ({marks})"
        params.extend(course_ids)
    sql += " ORDER BY s.submitted_at DESC, s.id DESC"

    rows = tquery(tenant, sql, tuple(params)) or []
    latest: dict = {}
    for r in rows:
        latest.setdefault((r["assignment_id"], r["student_id"]), r)
    # Oldest first: a learner who has been waiting since Day 02 is served
    # before one who submitted an hour ago.
    picked = sorted(latest.values(), key=lambda r: r["id"])
    return picked[:max(0, limit)]


def sweep(tenant, review_one: Callable, course_ids: Optional[list] = None,
          limit: int = MAX_PER_RUN) -> dict:
    """Queue everything unreviewed and start the worker. Returns a summary."""
    from app.services import review_job_service as jobs

    rows = find_unreviewed(tenant, course_ids, limit)
    if not rows:
        return {"queued": 0, "detail": "nothing unreviewed"}

    job_id = jobs.create_job(
        tenant, "assignment",
        [(r["assignment_id"], r["id"]) for r in rows], note=SWEEP_NOTE)
    started = jobs.start_worker(tenant, job_id, review_one)
    return {"jobId": job_id, "queued": len(rows), "workerStarted": started,
            "detail": ("draining" if started else
                       "queued — a worker is busy; it will be picked up on "
                       "the next sweep or restart")}


def sweep_all_tenants() -> dict:
    """The scheduled entry point. Never raises — a tenant whose DB is down
    must not stop the others (eaprep has been offline for days)."""
    from app.routes.review_jobs import make_review_one
    from app.services import review_job_service as jobs
    from app.tenants import TENANTS

    admin_key = os.getenv("ADMIN_JOB_KEY", "")
    if not (jobs.jobs_enabled() and admin_key):
        print("   sweep: skipped (REVIEW_JOBS_ENABLED / ADMIN_JOB_KEY not set)")
        return {"skipped": "not configured"}

    courses = [int(c) for c in os.getenv("AIREV_AUTO_REVIEW_COURSES", "").split(",")
               if c.strip().isdigit()] or None
    summary: dict = {}
    for tenant in TENANTS.values():
        try:
            summary[tenant.id] = sweep(tenant, make_review_one(tenant, admin_key),
                                       courses)
        except Exception as e:
            summary[tenant.id] = {"error": f"{type(e).__name__}: {e}"}
        print(f"   sweep [{tenant.id}]: {summary[tenant.id]}")
    return summary
