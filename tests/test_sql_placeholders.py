# tests/test_sql_placeholders.py
# ---------------------------------------------------------------------------
# A '%' THAT WAS NOT A PLACEHOLDER (04 Sep 2026, seen live).
#
# PyMySQL formats every parameterised statement with Python's % operator, so
# a literal percent inside the SQL — `LIKE 'our outage %'` — must be written
# `%%`. The attempt ledger (sweeper.attempts_spent) had a bare one; on every
# sweep the query failed with "not enough arguments for format string", the
# code failed OPEN as designed, and the two-attempt cost ceiling was silently
# never applied. The fake DB in the other tests never formats, so it could
# not see this. This one does exactly what the driver does.
# ---------------------------------------------------------------------------

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def _driver_format(sql: str, params):
    """What PyMySQL does before sending: escape each param, then `sql % params`."""
    quoted = tuple("'%s'" % str(p).replace("'", "''") for p in params)
    return sql % quoted            # raises TypeError on a bare '%'


def _capture(module, attr):
    """Swap module.tquery for a recorder that formats like the driver."""
    calls = []
    def fake(tenant, sql, params=()):
        calls.append(_driver_format(sql, params))
        return []
    original = getattr(module, attr)
    setattr(module, attr, fake)
    return calls, lambda: setattr(module, attr, original)


def test_attempt_ledger_query_survives_driver_formatting():
    from app.services import sweeper_service as sweeper
    calls, restore = _capture(sweeper, "tquery")
    try:
        sweeper.attempts_spent("t", [1, 2, 3])
    finally:
        restore()
    assert calls, "the ledger query was never issued"
    assert "NOT LIKE 'our outage %'" in calls[0]         # the literal the DB sees


def test_sweep_selection_query_survives_driver_formatting():
    from app.services import sweeper_service as sweeper
    calls, restore = _capture(sweeper, "tquery")
    try:
        sweeper.find_unreviewed("t", None, 5)
    finally:
        restore()
    assert calls, "the selection query was never issued"
    assert "LIKE '%notGraded%'" in calls[0]               # the %% pairs became literal %
    assert "%%" not in calls[0]


def test_orphan_and_live_queries_survive_driver_formatting():
    from app.services import review_job_service as jobs
    calls, restore = _capture(jobs, "tquery")
    jobs._tables_ready.add("t")
    try:
        jobs.orphaned_jobs("t", 15)
        jobs.claim_live_item("t")
    finally:
        restore()
    assert len(calls) == 2
