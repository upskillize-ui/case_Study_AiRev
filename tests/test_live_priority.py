# tests/test_live_priority.py
# ---------------------------------------------------------------------------
# A STUDENT WHO JUST SUBMITTED GOES FIRST (04 Sep 2026).
#
# One worker, process-wide. While a sweep or Grade-all batch drained (an hour
# for 200 rows) the live queue was refused a worker, so the learner who had
# just pressed Submit waited behind the whole backlog — then behind the next
# learner's submit, or the next three-hourly sweep. These pin the fix: the
# batch worker's own take() claims a live item before the next backlog row,
# under the cursor lock so two threads never take the same one; the serial
# drain does the same; and a batch drains the live queue before it exits.
#
# Pure: the DB is a dict, the reviewer records the order it was called in.
# ---------------------------------------------------------------------------

import os
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.services import review_job_service as jobs

BATCH, LIVE = 100, 200


class _DB:
    """Two jobs: a batch (100) and the open live job (200)."""
    def __init__(self, batch_ids, live_ids):
        self.items = {}
        for n, sid in enumerate(batch_ids, 1):
            self.items[n] = {"id": n, "job_id": BATCH, "scope_id": 1, "submission_id": sid,
                             "state": "pending", "detail": "", "score": None}
        for n, sid in enumerate(live_ids, 1000):
            self.items[n] = {"id": n, "job_id": LIVE, "scope_id": 2, "submission_id": sid,
                             "state": "pending", "detail": "", "score": None}
        self.jobs = {BATCH: "running", LIVE: "running"}
        self.lock = threading.Lock()

    def query(self, tenant, sql, params=()):
        with self.lock:
            if "JOIN review_jobs" in sql:                       # claim_live_item
                live = [i for i in self.items.values()
                        if i["job_id"] == LIVE and i["state"] == "pending"
                        and self.jobs[LIVE] == "running"]
                return [dict(min(live, key=lambda i: i["id"]))] if live else []
            if "FROM review_job_items WHERE job_id" in sql:     # job_items
                return [dict(i) for i in sorted(self.items.values(), key=lambda i: (i["scope_id"], i["id"]))
                        if i["job_id"] == params[0]]
            if "WHERE note = %s AND state = 'running'" in sql:  # open_live_job
                return [{"id": LIVE}] if self.jobs[LIVE] == "running" else []
            if "AND state = 'pending' LIMIT 1" in sql:          # has_pending
                return [{"id": 1}] if any(i["job_id"] == params[0] and i["state"] == "pending"
                                          for i in self.items.values()) else []
            return []

    def execute(self, tenant, sql, params=()):
        with self.lock:
            if sql.strip().startswith("UPDATE review_job_items SET state"):
                state, detail, score, item_id = params
                self.items[item_id].update(state=state, detail=detail, score=score)
            elif "UPDATE review_jobs SET state" in sql:
                self.jobs[params[2]] = params[0]


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


def _reviewer(order):
    def review_one(scope_id, submission_id):
        order.append(submission_id)
        return "done", "ok", 7.0
    return review_one


def test_serial_drain_takes_the_live_item_before_the_backlog():
    db = _DB(batch_ids=[11, 12, 13], live_ids=[99])
    order = []
    def run():
        final = jobs.drain(_T(), BATCH, _reviewer(order), pause=0, sleeper=lambda s: None)
        assert order[0] == 99, order                     # the learner first
        assert order[1:] == [11, 12, 13]
        assert final.finished and db.jobs[BATCH] == "finished"
        assert db.items[1000]["state"] == "done"
    _with(db, run)


def test_parallel_drain_takes_the_live_item_right_after_warm_up():
    db = _DB(batch_ids=[11, 12, 13, 14], live_ids=[99])
    order = []
    def run():
        jobs.drain_parallel(_T(), BATCH, _reviewer(order), workers=2, pause=0, sleeper=lambda s: None)
        # Warm-up row runs alone first (cache), then the live item is claimed
        # before any other backlog row is taken.
        assert order[0] == 11
        assert order.index(99) == 1, order
        assert sorted(order) == [11, 12, 13, 14, 99]
    _with(db, run)


def test_a_claimed_live_item_is_marked_running_so_no_thread_takes_it_twice():
    db = _DB(batch_ids=[], live_ids=[99])
    def run():
        item = jobs.claim_live_item(_T())
        assert item and item["submission_id"] == 99
        assert db.items[1000]["state"] == "running"
        assert jobs.claim_live_item(_T()) is None         # gone from the pending set
    _with(db, run)


def test_nothing_live_means_nothing_claimed():
    db = _DB(batch_ids=[11], live_ids=[])
    def run():
        assert jobs.claim_live_item(_T()) is None
        assert all(i["state"] == "pending" for i in db.items.values())
    _with(db, run)


def test_a_dead_live_query_never_stops_the_batch():
    def boom(*a, **k):
        raise RuntimeError("db down")
    oq = jobs.tquery
    jobs.tquery = boom
    try:
        assert jobs.claim_live_item(_T()) is None
    finally:
        jobs.tquery = oq


def test_the_batch_worker_drains_the_live_queue_before_it_exits():
    src = open(os.path.join(os.path.dirname(_HERE), "app", "services", "review_job_service.py"),
               encoding="utf-8").read()
    body = src.split("def start_worker(")[1]
    assert "draining live queue" in body
    assert body.index("finished —") < body.index("draining live queue")


# --- the live queue is not a batch (04 Sep 2026, seen live) -----------------
# One pending live item made POST /api/review/jobs answer 409 to every
# Grade-all press for the 15 minutes until it went stale. An open live job
# is the normal state between submits; only a real batch may say "busy".

def test_running_jobs_can_exclude_the_live_queue():
    class DB:
        def query(self, tenant, sql, params=()):
            if "FROM review_jobs" in sql and "WHERE state = 'running'" in sql:
                return [{"id": 823, "scope_type": "assignment", "state": "running", "note": jobs.LIVE_NOTE},
                        {"id": 824, "scope_type": "assignment", "state": "running", "note": "assignment 33"}]
            return []
        def execute(self, *a, **k):
            pass
    db = DB()
    def run():
        assert [j["id"] for j in jobs.running_jobs(_T())] == [823, 824]
        assert [j["id"] for j in jobs.running_jobs(_T(), include_live=False)] == [824]
    _with(db, run)


def test_start_job_ignores_the_live_queue_and_startup_resumes_it():
    root = os.path.dirname(_HERE)
    route = open(os.path.join(root, "app", "routes", "review_jobs.py"), encoding="utf-8").read()
    main = open(os.path.join(root, "main.py"), encoding="utf-8").read()
    start = route.split("def start_job(")[1].split("def ")[0]
    assert "running_jobs(tenant, include_live=False)" in start
    assert "def resume_live_queue" in route
    assert "resume_live_queue()" in main
    assert main.index("reap_orphans(t, stale_minutes=0)") < main.index("resume_live_queue()")
