# app/routes/review_jobs.py
# ---------------------------------------------------------------------------
# ONE CALL REVIEWS A WHOLE ASSIGNMENT — the Space does the batch, not a laptop.
#
# Day 01 was graded by a cmd window firing 19 windowed batches over several
# hours, dying when the terminal closed, racing a CSV lock, and losing its
# place on every deploy. Day 02 onward:
#
#     POST /api/review/jobs        {"assignmentId": 18, "below": 7}
#     GET  /api/review/jobs/{id}   -> {"percent": 62, "done": 251, ...}
#
# The queue itself (tables, worker, abort rules) lives in
# review_job_service and was built and tested earlier; this file only wires
# it to HTTP and to the existing regrade route. Thin by design: selection is
# one SQL pass, the review call is THE SAME re_review_assignment function
# the correction sweeps already trust — same human-grade refusal, same
# shrink guard, same wrong-task policy, same student-visible skip notes.
#
# Guard rails, in the order they matter:
#   1. REVIEW_JOBS_ENABLED must be set on the Space or every route here 403s.
#   2. X-Admin-Key must match ADMIN_JOB_KEY — learners cannot start cohort
#      runs or bill them to staff.
#   3. One worker process-wide; a second POST while one runs gets 409 with
#      the running job's id, never a silent second herd.
#   4. below-N sparing happens at selection, so a re-run costs only the rows
#      that actually need re-buying.
# ---------------------------------------------------------------------------

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel

from app.database import set_current_tenant, tquery
from app.routes.assignment_review import get_tenant, re_review_assignment
from app.services import ai_service
from app.services import review_job_service as jobs
from app.tenants import Tenant

router = APIRouter(prefix="/api/review/jobs", tags=["review-jobs"])

# Outcome classification lives in review_job_service with the brake it
# feeds — see outcome_state() there. Imported, not restated.



class JobRequest(BaseModel):
    assignmentId: int
    # below=N: re-review only rows ungraded or marked under N (the sweep17
    # semantics — rows at/above N keep their marks and cost nothing).
    # below=None: ungraded rows only, the cheapest and most common run.
    below: float | None = None


def _require_staff(x_admin_key: str) -> None:
    if not ai_service.begin_run_billing(x_admin_key):
        raise HTTPException(status_code=403,
                            detail="Batch review is staff-only. Send a valid "
                                   "X-Admin-Key header.")


def _require_enabled() -> None:
    if not jobs.jobs_enabled():
        raise HTTPException(status_code=403,
                            detail="Review jobs are disabled. Set "
                                   "REVIEW_JOBS_ENABLED=1 on the Space.")


def select_rows(tenant, assignment_id: int, below: float | None) -> list:
    """Latest attempt per student with content, filtered by `below`. One
    query, one pass — the same shape tools/bulk_review.py proved."""
    rows = tquery(tenant, """
        SELECT s.id, s.student_id, s.grade,
               CHAR_LENGTH(COALESCE(s.notes, ''))  AS notes_len,
               COALESCE(s.file_path, '')           AS file_ref
        FROM assignment_submissions s
        WHERE s.assignment_id = %s
          AND COALESCE(s.status, '') <> 'draft'
        ORDER BY s.submitted_at DESC, s.id DESC""", (assignment_id,)) or []
    # 'draft' rows are NOT submissions: the LMS creates one the moment a
    # student merely OPENS an assignment (to hold time-spent). They carry no
    # student work and are silently excluded — never reviewed, never zeroed,
    # never messaged (Ranjana, 26 Aug).

    latest: dict = {}
    for r in rows:
        latest.setdefault(r["student_id"], r)     # newest attempt wins

    picked = []
    for r in latest.values():
        if not (r["notes_len"] or r["file_ref"]):
            continue                              # nothing to review
        grade = r["grade"]
        if grade is not None and below is not None and float(grade) >= below:
            continue                              # mark kept, nothing re-bought
        if grade is not None and below is None:
            continue                              # ungraded-only run
        picked.append(r)
    return sorted(picked, key=lambda r: r["id"])


def make_review_one(tenant, admin_key: str):
    """The queue item -> regrade route adapter the worker calls all night.

    Runs on a worker thread, where FastAPI's request contextvars do not
    exist — so the tenant is pinned explicitly on EVERY call. Without that,
    query()/execute() inside the pipeline fall through to the 'lms' default,
    which for any other tenant means reading and writing the wrong bank's
    database (the exact bug main.py's async-dependency comment records).
    """
    def review_one(scope_id: int, submission_id: int):
        set_current_tenant(tenant)
        try:
            res = re_review_assignment(submission_id, dryRun=False, force=False,
                                       tenant=tenant, x_admin_key=admin_key)
        except HTTPException as e:
            if e.status_code == 404:
                return "skipped", f"gone: {e.detail}"[:255], None
            return "failed", f"HTTP {e.status_code}: {e.detail}"[:255], None

        state = jobs.outcome_state(res)
        if state == "done":
            score = (res.get("feedback") or {}).get("scoreMarks")
            prev = res.get("previousGrade")
            return "done", f"{prev} -> {score}", score
        reason = res.get("skipped") or "not reviewed"
        prefix = "our outage — " if res.get("ours") else ""
        return state, f"{prefix}{reason}: {res.get('detail', '')}"[:255], None
    return review_one


@router.post("")
def start_job(req: JobRequest, tenant: Tenant = Depends(get_tenant),
              x_admin_key: str = Header(default="")):
    _require_enabled()
    _require_staff(x_admin_key)

    rows = select_rows(tenant, req.assignmentId, req.below)
    if not rows:
        return {"success": True, "queued": 0,
                "detail": "Nothing to review — every latest attempt with "
                          "content is already at or above the threshold."}

    running = jobs.running_jobs(tenant)
    if jobs.worker_is_running() or running:
        raise HTTPException(
            status_code=409,
            detail=f"A review job is already running "
                   f"(job {running[0]['id'] if running else '?'}). One at a "
                   f"time keeps the caches warm and the Space alive — poll "
                   f"GET /api/review/jobs/{{id}} and start the next batch "
                   f"when it finishes.")

    note = (f"assignment {req.assignmentId}"
            + (f", redo below {req.below}" if req.below is not None else ""))
    job_id = jobs.create_job(
        tenant, "assignment",
        [(req.assignmentId, r["id"]) for r in rows], note=note)
    jobs.start_worker(tenant, job_id, make_review_one(tenant, x_admin_key))
    return {"success": True, "jobId": job_id, "queued": len(rows),
            "note": note,
            "poll": f"/api/review/jobs/{job_id}"}


class EnqueueRequest(BaseModel):
    """One submission, enqueued the moment the learner presses Submit."""
    assignmentId: int
    submissionId: int | None = None
    studentId: int | None = None


@router.post("/enqueue")
def enqueue_one(req: EnqueueRequest, tenant: Tenant = Depends(get_tenant),
                x_admin_key: str = Header(default="")):
    """Queue ONE submission for review and return immediately.

    This is the auto-review path. It writes one row and answers in
    milliseconds, so the learner's submit never waits on a 25-65 second
    review and never meets the capacity governor's "AiRev is at full
    capacity" — which is an error message for having done the work on time.

    Staff-keyed like every other job route: the learner's browser does not
    call this, their LMS does, with the admin key, so nobody is billed.
    """
    _require_enabled()
    _require_staff(x_admin_key)

    submission_id = req.submissionId
    if not submission_id:
        if not req.studentId:
            raise HTTPException(status_code=400,
                                detail="Send submissionId or studentId.")
        from app.database import DUAL_ID_MATCH
        # DUAL_ID_MATCH binds the learner's id THREE times — students.id,
        # users.id, and the mapping between them. Binding it twice is a
        # parameter-count mismatch, which MySQL answers with an error and
        # FastAPI turns into a 500. Every other caller passes it three times;
        # this one did not, and the enqueue route 500'd on its first real use.
        rows = tquery(
            tenant,
            f"SELECT id FROM assignment_submissions WHERE assignment_id = %s "
            f"AND ({DUAL_ID_MATCH}) AND COALESCE(status, '') <> 'draft' "
            f"ORDER BY submitted_at DESC, id DESC LIMIT 1",
            (req.assignmentId, req.studentId, req.studentId, req.studentId))
        if not rows:
            raise HTTPException(
                status_code=404,
                detail=f"No submission for student {req.studentId} on "
                       f"assignment {req.assignmentId}.")
        submission_id = int(rows[0]["id"])

    job_id, queued = jobs.enqueue_live(tenant, req.assignmentId, submission_id)
    jobs.start_worker(tenant, job_id, make_review_one(tenant, x_admin_key),
                      live=True)
    return {"success": True, "jobId": job_id, "submissionId": submission_id,
            "queued": queued,
            "detail": ("queued" if queued else
                       "already waiting — not queued twice"),
            "poll": f"/api/review/jobs/{job_id}"}


@router.post("/sweep")
def sweep_now(tenant: Tenant = Depends(get_tenant),
              x_admin_key: str = Header(default=""),
              limit: int | None = None):
    """Review everything that has work and no mark, right now.

    The same pass the scheduler runs every three hours. Use it after a deploy,
    after a Space restart, or any time the pending count looks wrong: it reads
    assignment_submissions directly, so it does not care WHY a row was missed.

    Rows already carrying a notGraded verdict are skipped — they were a
    decision, not a gap, and re-buying them nightly would spend real money to
    reach the same conclusion for ever.
    """
    _require_enabled()
    _require_staff(x_admin_key)
    from app.services import sweeper_service
    courses = [int(c) for c in
               os.getenv("AIREV_AUTO_REVIEW_COURSES", "").split(",")
               if c.strip().isdigit()] or None
    result = sweeper_service.sweep(
        tenant, make_review_one(tenant, x_admin_key), courses,
        limit if limit and limit > 0 else sweeper_service.MAX_PER_RUN)
    return {"success": True, **result,
            "poll": f"/api/review/jobs/{result.get('jobId')}"
                    if result.get("jobId") else None}


@router.get("")
def list_jobs(tenant: Tenant = Depends(get_tenant)):
    _require_enabled()
    return {"workerRunning": jobs.worker_is_running(),
            "running": jobs.running_jobs(tenant)}


@router.get("/{job_id}")
def job_status(job_id: int, tenant: Tenant = Depends(get_tenant)):
    _require_enabled()
    state = tquery(tenant,
                   f"SELECT state, note FROM {jobs.JOBS_TABLE} WHERE id = %s",
                   (job_id,))
    if not state:
        raise HTTPException(status_code=404, detail=f"No job {job_id}")
    p = jobs.progress(tenant, job_id)
    items = jobs.job_items(tenant, job_id)
    return {
        "jobId": job_id,
        "state": state[0]["state"], "note": state[0]["note"],
        "total": p.total, "done": p.done, "failed": p.failed,
        "skipped": p.skipped, "pending": p.pending, "percent": p.percent,
        # Full detail only for rows needing attention; done rows are counts.
        "attention": [{"submissionId": i["submission_id"], "state": i["state"],
                       "detail": i["detail"]}
                      for i in items if i["state"] in ("failed", "skipped")][:80],
    }


@router.post("/render-check")
def render_check(body: dict, tenant: Tenant = Depends(get_tenant),
                 x_admin_key: str = Header(default="")):
    """Staff canary for the link renderer: what would the agent read from
    this URL? No review, no DB write — open, render, report. Lets Ranjana
    verify a student link (Claude artifact, Gamma, Notion...) in seconds
    after flipping LINK_RENDER_ENABLED, before any cohort run trusts it."""
    _require_staff(x_admin_key)
    from app.services import link_renderer
    if not link_renderer.enabled():
        raise HTTPException(status_code=403,
                            detail="Set LINK_RENDER_ENABLED=1 on the Space first.")
    url = str(body.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Send {\"url\": \"...\"}")
    text, why = link_renderer.read_rendered_link(url)
    return {"url": url, "readable": bool(text),
            "words": len(text.split()), "preview": text[:1200], "why": why}


def resume_after_restart() -> None:
    """Called at startup: deploys restart the Space mid-cohort, and item state
    lives in the DB precisely so the job can pick itself back up. Only acts
    when the feature flag AND the admin key are configured — an unflagged
    Space stays inert, exactly as before this subsystem existed."""
    if not jobs.jobs_enabled():
        return
    admin_key = os.getenv("ADMIN_JOB_KEY", "")
    if not admin_key:
        print("   review-jobs: enabled but no ADMIN_JOB_KEY — cannot resume")
        return
    from app.tenants import TENANTS
    for tenant in TENANTS.values():
        try:
            running = jobs.running_jobs(tenant)
        except Exception:
            continue                      # tenant DB down — nothing to resume
        if running:
            job_id = running[0]["id"]
            print(f"   review-jobs: resuming job {job_id} "
                  f"(tenant {tenant.id}) after restart")
            jobs.start_worker(tenant, job_id, make_review_one(tenant, admin_key))
            return                        # one worker process-wide
