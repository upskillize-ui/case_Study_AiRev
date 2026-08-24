"""The tool that answers "will live submission work?" before a student asks it.

Ranjana, 24 Aug: "today onwards live submission will work right? test first."

Tomorrow every submission takes the enqueue path. The first time it runs must
not be the first time it is tried — and the tool that proves it must itself be
safe to run against the live cohort.
"""

import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import live_check


SRC = inspect.getsource(live_check)


# ── it must be safe to run on the live cohort ───────────────────────────────

def test_it_never_submits_work_on_a_learners_behalf():
    """The one thing this tool must never do. It queues an EXISTING row."""
    assert "/api/review/jobs/enqueue" in SRC
    for forbidden in ("submit-assignment", "submit-capstone",
                      "submit-industry-session", "/api/review/submit"):
        assert forbidden not in SRC, forbidden


def test_it_touches_exactly_one_submission():
    assert "--student-id" in SRC and "required=True" in SRC
    assert "--limit" not in SRC, "a test that can review a cohort is not a test"


def test_it_is_staff_keyed_so_no_learner_is_billed():
    assert '"x-admin-key": admin_key' in SRC
    assert 'sys.exit("Set AIREV_API_KEY and AIREV_ADMIN_KEY first.")' in SRC


# ── it must diagnose, not just fail ─────────────────────────────────────────

def test_the_queue_being_switched_off_is_named_as_such():
    """The likeliest reason it will not work tomorrow, and the one a stack
    trace would hide."""
    assert "REVIEW_JOBS_ENABLED=1" in SRC
    assert "the queue is OFF" in SRC


def test_a_refused_admin_key_says_which_setting_to_check():
    assert "ADMIN_JOB_KEY" in SRC


def test_a_student_who_never_submitted_is_explained_not_crashed():
    assert "no submission found for student" in SRC


def test_a_stuck_worker_is_reported_with_where_to_look():
    assert "may be stuck" in SRC and "[JOB" in SRC


# ── it must measure the thing that matters to a learner ─────────────────────

def test_it_times_the_part_the_learner_actually_waits_for():
    """The enqueue call. Everything after it happens while they are away."""
    enqueue = SRC.index("/api/review/jobs/enqueue")
    assert "took_ms" in SRC[enqueue - 400:enqueue + 400]


def test_it_complains_if_the_learner_would_wait_too_long():
    assert "took_ms > 5000" in SRC
    assert "never wait this" in SRC


def test_it_reports_when_the_feedback_actually_lands():
    assert "Their feedback appears about" in SRC


# ── it must say what is still switched off ──────────────────────────────────

def test_a_pass_still_lists_the_settings_not_yet_on():
    """Passing this tool does NOT mean tomorrow works — the LMS half is
    separate, and saying so is the difference between a green tick and a
    working system."""
    tail = SRC[SRC.index("Still to switch on"):]
    assert "AIREV_AUTO_REVIEW=1" in tail
    assert "AIREV_AUTO_REVIEW_COURSES" in tail
    assert "student.js" in tail


def test_it_gives_up_rather_than_hanging_forever():
    assert live_check.GIVE_UP_AFTER > 0
    assert "while time.time() < deadline" in SRC
