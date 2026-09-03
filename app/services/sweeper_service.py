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

# ONE SUBMISSION, ONE REVIEW (03 Sep 2026).
#
# The sweep is a safety net for work that slipped through, not a retry loop.
# A row it cannot mark must eventually STOP being offered, or the net becomes
# the leak: every three hours it re-buys the same failure, paying full intake
# each time — OCR per page, Whisper per recording, a vision call per sampled
# video frame — and writing nothing.
#
# The stamp-based skip does not close this on its own. A read that fails
# because OUR provider is down deliberately writes no verdict (blaming a
# learner for our outage would be worse), so the row keeps whatever stamp it
# had and matches the sweep again, and again, for ever. That is the shape that
# emptied the budget.
#
# So the ceiling is on ATTEMPTS, not on verdicts, and it is counted from work
# already recorded: every queue item this submission has ever settled. Nothing
# new is stored. A row that has had its reviews and still has no mark waits for
# something to actually change — the learner resubmits (which clears feedback
# and resets nothing, but gives the marker different input), or a human presses
# re-review, which never consults this ceiling.
MAX_SWEEP_ATTEMPTS = int(os.getenv("SWEEP_MAX_ATTEMPTS", "2"))

# A REFUSAL IS RE-OFFERED ONLY WHEN THE THING THAT CAUSED IT MIGHT HAVE CHANGED
# (03 Sep 2026).
#
# The rules-version stamp sorted refusals by WHEN they were written, not by
# whether a retry could help. On 03 Sep that landed exactly backwards: 606 rows
# on the treadmill, of which 428 were verdicts about the SUBMISSION — a private
# link, nothing readable attached, work from a different brief — that will
# return the identical answer on every run until the learner resubmits; and
# 112 rows that genuinely deserved a retry sat parked because they happened to
# carry the current stamp.
#
# So the escape arm is now conditional on WHOSE refusal it was. These are the
# phrases our own failure paths write; a refusal that carries none of them is a
# verdict about the work and stays parked until the learner acts (their
# resubmit clears feedback, which makes the row fresh again). Each phrase is
# pinned to its writer by test_budget_burn, so a reworded message cannot
# silently turn a retryable failure into a permanent one.
OUR_REFUSAL_PHRASES = (
    "could not complete a fair review",        # grade guard, route envelope
    "could not finish reviewing this attempt", # grade guard, assignment_db_service
    "could not read this task",                # rubric cache unreadable
)

# A refusal that QUOTES A PROVIDER ERROR was never a verdict about the work —
# it is our outage that slipped past reads_as_our_outage() and got stamped as
# if it were a decision (03 Sep: the Anthropic monthly cap, HTTP 400, "You have
# reached your specified API usage limits"). These are re-offered whatever
# rules stamp they carry, because the only thing that needs to change for them
# to succeed is the provider coming back. The card message is also wrong on
# these rows; a successful re-review replaces it.
API_ERROR_PHRASES = (
    "request_id", "error code:", "usage limits", "invalid_request_error",
)


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
    # 'returned' (03 Sep 2026): an admin sent the row back to the learner with
    # a note — a private link, a file that would not open. It is waiting on the
    # STUDENT. The note carries notGraded without a rules stamp, so without
    # this clause the escape arm below would re-select every one of them and
    # the sweep would pay to re-refuse work the learner has been asked to fix.
    stamp = f'%"rulesVersion": {RULES_VERSION}%'
    sql = """
        SELECT s.id, s.assignment_id, s.student_id, s.submitted_at
        FROM assignment_submissions s
        JOIN assignments a ON a.id = s.assignment_id
        WHERE a.status = 'active'
          AND s.grade IS NULL
          AND COALESCE(s.status, '') NOT IN ('draft', 'returned')
          AND (CHAR_LENGTH(COALESCE(s.notes, '')) > 0
               OR COALESCE(s.file_path, '') <> '')
          AND (COALESCE(s.feedback, '') NOT LIKE '%%notGraded%%'
               OR (COALESCE(s.feedback, '') NOT LIKE %s
                   AND ({ours}))
               OR ({api_err}))
    """.format(
        ours=" OR ".join(["COALESCE(s.feedback, '') LIKE %s"] * len(OUR_REFUSAL_PHRASES)),
        api_err=" OR ".join(["COALESCE(s.feedback, '') LIKE %s"] * len(API_ERROR_PHRASES)))
    params: list = [stamp,
                    *[f"%{p}%" for p in OUR_REFUSAL_PHRASES],
                    *[f"%{p}%" for p in API_ERROR_PHRASES]]
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

    # Drop anything that has already had its attempts. One membership test per
    # row against a set built in a single query — never a query inside the loop.
    spent = attempts_spent(tenant, [r["id"] for r in picked])
    picked = [r for r in picked if spent.get(r["id"], 0) < MAX_SWEEP_ATTEMPTS]
    return picked[:max(0, limit)]


def attempts_spent(tenant, submission_ids: list) -> dict:
    """How many times the queue has already settled each submission.

    Reads review_job_items, which has recorded one row per attempt since the
    queue existed — so the ceiling costs no new table, no new column and no
    migration. Pending items are not counted: an attempt that has not finished
    has not been paid for yet.

    Returns {submission_id: attempts}; ids with no history are simply absent.
    Fails OPEN — an unreadable ledger must not stop the safety net, because a
    sweep that runs twice is a smaller problem than a cohort never marked.
    """
    if not submission_ids:
        return {}
    from app.services.review_job_service import ITEMS_TABLE
    marks = ", ".join(["%s"] * len(submission_ids))
    try:
        # An attempt that failed because OUR provider was down is not a try
        # the learner used up. review_one prefixes those "our outage — ", so
        # they are excluded here: a quota cap that lasts a week must not burn
        # through every row's two tries and park the cohort until resubmit.
        rows = tquery(tenant, f"""
            SELECT submission_id, COUNT(*) AS n
              FROM {ITEMS_TABLE}
             WHERE submission_id IN ({marks})
               AND state IN ('done', 'skipped', 'failed')
               AND detail NOT LIKE 'our outage %'
             GROUP BY submission_id
        """, tuple(submission_ids)) or []
        # Parsing is inside the guard on purpose. A driver that returns an
        # unexpected row shape is the same class of problem as one that cannot
        # answer at all, and neither is a reason to stop marking a cohort.
        return {int(r["submission_id"]): int(r["n"]) for r in rows}
    except Exception as e:
        print(f"   sweep: attempt ledger unreadable ({e}) — ceiling not applied")
        return {}


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


# After the brake stops a job for a dead provider, the next tick is three hours
# away and the provider is usually still dead. Eight rows per tick, eight ticks
# a day: 64 paid failures a day to rediscover an outage. So a tenant whose last
# sweep ABORTED stays quiet for a cool-off window. DB-only check, zero spend.
COOLOFF_HOURS = float(os.getenv("SWEEP_COOLOFF_HOURS", "6"))


def cooling_off(tenant, hours: float = COOLOFF_HOURS) -> bool:
    """Did this tenant's most recent sweep abort within the window? Fails
    CLOSED to False — an unreadable jobs table must not silence the net."""
    from app.services.review_job_service import JOBS_TABLE
    try:
        rows = tquery(tenant, f"""
            SELECT state FROM {JOBS_TABLE}
             WHERE note = %s
             ORDER BY id DESC LIMIT 1
        """, (SWEEP_NOTE,)) or []
        if not rows or rows[0]["state"] != "aborted":
            return False
        recent = tquery(tenant, f"""
            SELECT 1 AS hit FROM {JOBS_TABLE}
             WHERE note = %s AND state = 'aborted'
               AND updated_at >= NOW() - INTERVAL %s HOUR
             ORDER BY id DESC LIMIT 1
        """, (SWEEP_NOTE, hours)) or []
        return bool(recent)
    except Exception as e:
        print(f"   sweep: cool-off check failed ({e}) — sweeping anyway")
        return False


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
            if cooling_off(tenant):
                summary[tenant.id] = {"skipped": "cooling off after an aborted sweep"}
                print(f"   sweep [{tenant.id}]: last sweep aborted (provider down?) — "
                      f"waiting {COOLOFF_HOURS:g}h before trying again")
                continue
            summary[tenant.id] = sweep(tenant, make_review_one(tenant, admin_key),
                                       courses)
        except Exception as e:
            summary[tenant.id] = {"error": f"{type(e).__name__}: {e}"}
        print(f"   sweep [{tenant.id}]: {summary[tenant.id]}")
    return summary
