# tests/test_orphan_jobs.py
# ---------------------------------------------------------------------------
# THE ORPHANED JOB (04 Sep 2026).
#
# The worker is a thread of the Space process. A restart kills it and leaves
# its job 'running' with every unfinished item 'pending' — and start_job()
# then answers 409 to every Grade-all press, for ever. Four such jobs held
# 557 items on the night of 03 Sep while the admin pressed the button and
# saw nothing happen.
#
# These pin the reaper: a stale running job is closed as 'finished' (never
# 'aborted' — that is the provider-down signal that puts the sweep on a
# six-hour cool-off), its pending items are marked with the ledger's own
# "our outage" phrase so the rows keep their attempts, and the job THIS
# process is draining is never touched however long it takes.
#
# Pure: the DB is a recorder.
# ---------------------------------------------------------------------------

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.services import review_job_service as jobs
from app.services import sweeper_service


class _DB:
    """Records every write; answers the orphan SELECT with `stale`."""
    def __init__(self, stale):
        self.stale = stale
        self.writes = []

    def query(self, tenant, sql, params=()):
        if "state = 'running'" in sql and "NOT EXISTS" in sql:
            return list(self.stale)
        return []

    def execute(self, tenant, sql, params=()):
        self.writes.append((" ".join(sql.split()), tuple(params)))


def _with(db, fn):
    oq, oe = jobs.tquery, jobs.texecute
    jobs.tquery, jobs.texecute = db.query, db.execute
    jobs._tables_ready.add("t")
    try:
        fn()
    finally:
        jobs.tquery, jobs.texecute = oq, oe


class _T:
    id = "t"


def test_a_stale_job_is_closed_and_its_rows_re_offered():
    db = _DB([{"id": 821, "note": "sweep — submissions with no mark"}])
    def run():
        assert jobs.reap_orphans(_T(), stale_minutes=15) == 1
        item_w = [w for w in db.writes if "review_job_items" in w[0]]
        job_w = [w for w in db.writes if "UPDATE review_jobs SET state" in w[0]]
        assert len(item_w) == 1 and "state = 'pending'" in item_w[0][0]
        assert item_w[0][1][0].startswith("our outage")      # ledger exclusion phrase
        assert item_w[0][1][1] == 821
        assert len(job_w) == 1 and job_w[0][1][0] == "finished"
        assert "worker died" in job_w[0][1][1]
    _with(db, run)


def test_the_job_this_process_is_draining_is_never_reaped():
    db = _DB([{"id": 900, "note": "live — auto-review on submit"},
              {"id": 901, "note": "assignment 14"}])
    def run():
        jobs._worker_running, jobs._worker_job_id = True, 900
        try:
            assert [j["id"] for j in jobs.orphaned_jobs(_T())] == [901]
            assert jobs.reap_orphans(_T()) == 1
        finally:
            jobs._worker_running, jobs._worker_job_id = False, None
    _with(db, run)


def test_nothing_stale_means_nothing_written():
    db = _DB([])
    def run():
        assert jobs.reap_orphans(_T()) == 0
        assert db.writes == []
    _with(db, run)


def test_a_dead_database_never_raises():
    def boom(*a, **k):
        raise RuntimeError("db down")
    oq = jobs.tquery
    jobs.tquery = boom
    try:
        assert jobs.reap_orphans(_T()) == 0
    finally:
        jobs.tquery = oq


def test_finished_not_aborted_so_the_sweep_is_not_cooled_off():
    # The reaper's closing state must not be the one cooling_off() keys on.
    import inspect
    src = inspect.getsource(jobs.reap_orphans)
    assert '"finished"' in src and '"aborted"' not in src.split("Returns")[1].split("try:")[1]


def test_orphan_detail_matches_the_ledger_exclusion():
    # sweeper.attempts_spent() excludes details starting with 'our outage '.
    assert jobs.ORPHAN_DETAIL.startswith("our outage ")
    src = __import__("inspect").getsource(sweeper_service.attempts_spent)
    assert "our outage" in src


def test_startup_and_start_job_and_sweep_all_call_the_reaper():
    root = os.path.dirname(_HERE)
    main_src = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    route_src = open(os.path.join(root, "app", "routes", "review_jobs.py"), encoding="utf-8").read()
    sweep_src = open(os.path.join(root, "app", "services", "sweeper_service.py"), encoding="utf-8").read()
    assert "reap_orphans(t, stale_minutes=0)" in main_src          # every running job is dead after a restart
    assert 'id="resume_sweep"' in main_src                         # and the rows are picked up in minutes
    assert "resume_after_restart()" not in main_src                # the old one-job resume is gone…
    assert "def resume_after_restart" not in route_src             # …not just unplugged
    assert "jobs.reap_orphans(tenant)" in route_src                # before the 409 check
    assert route_src.index("jobs.reap_orphans(tenant)") < route_src.index("status_code=409")
    assert "jobs.reap_orphans(tenant)" in sweep_src
    assert sweep_src.index("jobs.reap_orphans(tenant)") < sweep_src.index("if cooling_off(tenant)")
