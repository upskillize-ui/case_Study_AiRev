# tests/test_budget_burn.py
# ---------------------------------------------------------------------------
# WHERE $120 WENT WHILE A THOUSAND SUBMISSIONS STAYED UNMARKED.
#
# Three faults, each individually reasonable, compounding into an unbounded
# spend that produced nothing and was invisible while it happened:
#
#   1. THE BRAKE WAS DISARMED. should_abort() stops a job after eight
#      consecutive failures — "a dead provider or an expired key, and
#      continuing would spend the rest of the cohort budget discovering that
#      repeatedly". But a dead provider makes intake return `no_readable_content`
#      for EVERY row, and that label was classified as policy, not breakage, so
#      it reset the counter. The brake never came on.
#
#   2. THE SWEEP HAD NO CEILING. A read that fails because OUR side is down
#      deliberately writes no verdict — blaming a learner for our outage would
#      be worse. No verdict means no stamp, and no stamp means the row matches
#      the sweep again in three hours. For ever, at full intake cost each time:
#      OCR per page, Whisper per recording, a vision call per sampled frame.
#
#   3. NOTHING COUNTED IT. Every review arrives through the queue on the admin
#      key, so it is never billed to a learner and _report_usage returned
#      before writing. The LMS ledger was empty and correct at the same time.
#
# The rule these pin: ONE SUBMISSION, ONE REVIEW. The sweep is a safety net for
# work that slipped through, not a retry loop, and a row it cannot mark must
# stop being offered until something actually changes.
#
# Pure. No network, no model, no real database.
# ---------------------------------------------------------------------------

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from _stubs import import_with_stubs

sweeper = import_with_stubs("app.services.sweeper_service")
jobs = import_with_stubs("app.services.review_job_service")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} — {detail}")
        FAILURES.append(name)


# --- 1. the brake -----------------------------------------------------------
# outcome_state lives beside the brake it feeds, in the service layer.
# the rule here would test a copy, so it is imported through the same stub
# machinery the other suites use.
outcome_state = jobs.outcome_state


def test_our_outage_counts_as_breakage():
    print("\na dead provider trips the brake; a bad file does not")

    # The learner's file is genuinely unreadable. Ordinary. Policy skip.
    theirs = {"success": False, "skipped": "no_readable_content",
              "ours": False, "detail": "password-protected PDF"}
    check("a learner's unreadable file stays a skip",
          outcome_state(theirs) == "skipped", outcome_state(theirs))

    # Same label, our fault. This is the one that emptied the budget.
    ours = {"success": False, "skipped": "no_readable_content",
            "ours": True, "detail": "provider returned 503"}
    check("the SAME label counts as a failure when the fault is ours",
          outcome_state(ours) == "failed", outcome_state(ours))

    unassessable = {"success": False, "skipped": "unassessable_deliverable",
                    "ours": True, "detail": "transcription unavailable"}
    check("an unassessable deliverable during our outage is breakage too",
          outcome_state(unassessable) == "failed", outcome_state(unassessable))

    check("a faculty-graded row is never breakage",
          outcome_state({"success": False, "skipped": "human_graded"}) == "skipped")
    check("an unrecognised reason is still a failure",
          outcome_state({"success": False, "skipped": "something_new"}) == "failed")
    check("success is done",
          outcome_state({"success": True}) == "done")


def test_eight_of_ours_stops_the_job():
    print("\nthe brake actually engages on a run of our outages")
    consecutive = 0
    for _ in range(20):
        state = outcome_state({"success": False, "skipped": "no_readable_content",
                               "ours": True})
        consecutive = consecutive + 1 if state == "failed" else 0
        if jobs.should_abort(consecutive):
            break
    check("a job of a thousand rows stops within ten, not a thousand",
          jobs.should_abort(consecutive), f"consecutive={consecutive}")

    # And the old behaviour, restated so a regression is unmistakable: if these
    # rows had stayed 'skipped', the counter would sit at zero for ever.
    stuck = 0
    for _ in range(1000):
        stuck = stuck + 1 if "skipped" == "failed" else 0
    check("the disarmed brake never fired — this is what it cost",
          not jobs.should_abort(stuck), stuck)


# --- 2. the ceiling ---------------------------------------------------------

class LedgerDB:
    """Answers the sweep's row query, then the attempt-ledger query."""

    def __init__(self, rows, attempts):
        self.rows = rows
        self.attempts = attempts
        self.queries = []

    def __call__(self, tenant, sql, params=()):
        self.queries.append(sql)
        if "review_job_items" in sql:
            return [{"submission_id": sid, "n": n}
                    for sid, n in self.attempts.items()]
        return self.rows


def _sweep(rows, attempts):
    db = LedgerDB(rows, attempts)
    original = sweeper.tquery
    sweeper.tquery = db
    try:
        return db, sweeper.find_unreviewed("lms")
    finally:
        sweeper.tquery = original


def _row(i):
    return {"id": i, "assignment_id": 18, "student_id": 100 + i,
            "submitted_at": "2026-09-01"}


def test_a_row_is_offered_only_until_its_attempts_are_spent():
    print("\none submission, one review")
    rows = [_row(1), _row(2), _row(3)]
    db, picked = _sweep(rows, {1: 0, 2: sweeper.MAX_SWEEP_ATTEMPTS,
                               3: sweeper.MAX_SWEEP_ATTEMPTS + 5})
    ids = [r["id"] for r in picked]
    check("a fresh row is offered", 1 in ids, ids)
    check("a row at the ceiling is dropped", 2 not in ids, ids)
    check("a row past the ceiling is dropped", 3 not in ids, ids)
    check("the ledger is read once, not once per row",
          sum("review_job_items" in q for q in db.queries) == 1, db.queries)


def test_a_row_with_no_history_is_never_starved():
    print("\nno history means never tried, not already spent")
    _, picked = _sweep([_row(7)], {})
    check("an unknown submission is still offered",
          [r["id"] for r in picked] == [7], picked)


def test_an_unreadable_ledger_fails_open():
    print("\na broken ledger must not stop the safety net")

    class Broken(LedgerDB):
        def __call__(self, tenant, sql, params=()):
            if "review_job_items" in sql:
                raise RuntimeError("table gone")
            return self.rows

    db = Broken([_row(4)], {})
    original = sweeper.tquery
    sweeper.tquery = db
    try:
        picked = sweeper.find_unreviewed("lms")
    finally:
        sweeper.tquery = original
    check("the row is still offered when the ledger cannot be read",
          [r["id"] for r in picked] == [4], picked)


def test_pending_attempts_are_not_charged_to_the_row():
    print("\nan attempt that has not finished has not been paid for")
    db = LedgerDB([_row(5)], {})
    original = sweeper.tquery
    sweeper.tquery = db
    try:
        sweeper.find_unreviewed("lms")
    finally:
        sweeper.tquery = original
    ledger_sql = [q for q in db.queries if "review_job_items" in q][0]
    check("only settled states are counted",
          "'done', 'skipped', 'failed'" in ledger_sql, ledger_sql)
    check("pending is not counted", "pending" not in ledger_sql, ledger_sql)


def test_the_ceiling_is_small_and_configurable():
    print("\nthe ceiling is a retry budget, not a retry loop")
    check("the default is one review plus at most one retry",
          sweeper.MAX_SWEEP_ATTEMPTS <= 2, sweeper.MAX_SWEEP_ATTEMPTS)
    check("it is at least one, or nothing is ever reviewed",
          sweeper.MAX_SWEEP_ATTEMPTS >= 1, sweeper.MAX_SWEEP_ATTEMPTS)


# --- 3. the counter ---------------------------------------------------------

def test_unbilled_calls_are_still_counted():
    print("\nnot billed is not the same as not spent")
    ai = import_with_stubs("app.services.ai_service")

    class Usage:
        input_tokens, output_tokens = 1000, 200

    before = ai.spend_snapshot()
    ai._count_spend(Usage(), billed=False)
    after = ai.spend_snapshot()
    check("a staff run adds to the total",
          after["calls"] == before["calls"] + 1, (before, after))
    check("a staff run adds to the UNBILLED total",
          after["unbilled_calls"] == before["unbilled_calls"] + 1, after)
    check("its tokens are counted",
          after["unbilled_input_tokens"] - before["unbilled_input_tokens"] == 1000,
          after)

    ai._count_spend(Usage(), billed=True)
    later = ai.spend_snapshot()
    check("a learner's run counts in the total but not the unbilled one",
          later["calls"] == after["calls"] + 1
          and later["unbilled_calls"] == after["unbilled_calls"], later)

    check("the snapshot is a copy, not the live counters",
          ai.spend_snapshot() is not ai.spend_snapshot())

    class Broken:
        @property
        def input_tokens(self):
            raise ValueError("no usage on this response")

    ai._count_spend(Broken(), billed=False)
    check("a malformed usage object never breaks a review", True)


# --- 4. a row sent back to the learner is the learner's, not the sweep's -------

def test_returned_rows_are_never_swept():
    print("\na returned row waits on the student, not on us")
    db = LedgerDB([], {})
    original = sweeper.tquery
    sweeper.tquery = db
    try:
        sweeper.find_unreviewed("lms")
    finally:
        sweeper.tquery = original
    row_sql = [q for q in db.queries if "review_job_items" not in q][0]
    # The admin's send-back note carries notGraded and NO rules stamp, so the
    # stamp escape arm would re-select every returned row. The status clause
    # is the only thing standing between the sweep and 796 paid re-refusals.
    check("the sweep excludes status 'returned' outright",
          "NOT IN ('draft', 'returned')" in row_sql, row_sql)


for fn in [test_our_outage_counts_as_breakage,
           test_eight_of_ours_stops_the_job,
           test_a_row_is_offered_only_until_its_attempts_are_spent,
           test_a_row_with_no_history_is_never_starved,
           test_an_unreadable_ledger_fails_open,
           test_pending_attempts_are_not_charged_to_the_row,
           test_the_ceiling_is_small_and_configurable,
           test_unbilled_calls_are_still_counted,
           test_returned_rows_are_never_swept]:
    fn()

print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED: {', '.join(FAILURES)}")
    sys.exit(1)
print("budget burn: all checks passed")
