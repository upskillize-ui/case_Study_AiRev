"""The night lane (07 Sep 2026) — sweep reviews at half price.

Pinned:
  1. Off by default: create_message never touches the lane unless
     REVIEW_BATCH_LANE is on AND the caller is inside night_lane().
  2. Requests from several threads coalesce into ONE batch and each caller
     gets its own result back.
  3. A failed request, a batch that never ends, or a missing direct key
     falls through to the live chain — a review never fails because of the
     lane.
  4. The LANE is a property of the JOB, set in the thread that makes the
     calls: the sweep's batches ride it, a Grade-all and the live queue do
     not, and a live row claimed ahead of a night batch stays live.
"""
import os
import sys
import threading
import types

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import pytest

from app.services import ai_service, batch_lane


class _Result:
    def __init__(self, cid, ok=True):
        self.custom_id = cid
        self.result = types.SimpleNamespace(
            type="succeeded" if ok else "errored",
            message=types.SimpleNamespace(content=[], usage=None, custom_id=cid) if ok else None,
            error="boom" if not ok else None)


class _Batches:
    def __init__(self, fail_ids=(), never_end=False):
        self.created, self.fail_ids, self.never_end = [], set(fail_ids), never_end
    def create(self, requests):
        self.created.append(requests)
        return types.SimpleNamespace(id=f"b{len(self.created)}", processing_status="in_progress")
    def retrieve(self, bid):
        return types.SimpleNamespace(id=bid, processing_status="in_progress" if self.never_end else "ended")
    def results(self, bid):
        for req in self.created[int(bid[1:]) - 1]:
            yield _Result(req["custom_id"], ok=req["custom_id"] not in self.fail_ids)


def _collector(monkeypatch, batches):
    monkeypatch.setenv("BATCH_FLUSH_SECONDS", "0.2")
    monkeypatch.setenv("BATCH_POLL_SECONDS", "0.01")
    monkeypatch.setenv("BATCH_MAX_WAIT_SECONDS", "2")
    client = types.SimpleNamespace(messages=types.SimpleNamespace(batches=batches))
    return batch_lane.Collector(lambda: client, sleeper=lambda s: None)


def test_off_by_default_and_off_outside_the_lane(monkeypatch):
    monkeypatch.delenv("REVIEW_BATCH_LANE", raising=False)
    assert batch_lane.enabled() is False and batch_lane.active() is False
    monkeypatch.setenv("REVIEW_BATCH_LANE", "1")
    assert batch_lane.enabled() is True and batch_lane.active() is False
    with batch_lane.night_lane():
        assert batch_lane.active() is True
    assert batch_lane.active() is False


def test_many_callers_one_batch_each_gets_its_own_result(monkeypatch):
    batches = _Batches()
    col = _collector(monkeypatch, batches)
    out = {}
    def call(i):
        out[i] = col.submit({"model": "m", "messages": [{"role": "user", "content": str(i)}]})
    threads = [threading.Thread(target=call, args=(i,)) for i in range(5)]
    for t in threads: t.start()
    for t in threads: t.join(5)
    assert len(batches.created) == 1 and len(batches.created[0]) == 5
    assert sorted(out) == [0, 1, 2, 3, 4]
    # each caller got the message for ITS request, not the first one's
    ids = {batches.created[0][i]["custom_id"] for i in range(5)}
    assert {r.custom_id for r in out.values()} == ids
    assert col.batches_submitted == 1 and col.requests_submitted == 5


def test_a_failed_request_raises_for_that_caller_only(monkeypatch):
    col = _collector(monkeypatch, _Batches(fail_ids={"r0"}))
    got = {}
    def call(i):
        try:
            got[i] = col.submit({"messages": [i]})
        except batch_lane.BatchLaneError as e:
            got[i] = e
    ts = [threading.Thread(target=call, args=(i,)) for i in range(2)]
    for t in ts: t.start()
    for t in ts: t.join(5)
    errs = [v for v in got.values() if isinstance(v, batch_lane.BatchLaneError)]
    assert len(errs) == 1 and "boom" in str(errs[0])


def test_a_batch_that_never_ends_releases_its_callers(monkeypatch):
    col = _collector(monkeypatch, _Batches(never_end=True))
    with pytest.raises(batch_lane.BatchLaneError):
        col.submit({"messages": []})


def test_create_message_falls_through_to_live_when_the_lane_cannot_deliver(monkeypatch):
    monkeypatch.setenv("REVIEW_BATCH_LANE", "1")
    def bad(kwargs):
        raise batch_lane.BatchLaneError("no key")
    monkeypatch.setattr(batch_lane, "submit", bad)
    live = []
    class _Client:
        class messages:
            @staticmethod
            def create(**kw):
                live.append(kw); return "LIVE"
    monkeypatch.setattr(ai_service, "providers", lambda: [ai_service.Provider("anthropic", "k", "", False, True)])
    monkeypatch.setattr(ai_service, "_client_for", lambda p: _Client())
    with batch_lane.night_lane():
        assert ai_service.create_message(model="m", messages=[]) == ("LIVE", "anthropic")
    assert live and live[0]["model"] == "m"


def test_create_message_uses_the_lane_when_active(monkeypatch):
    monkeypatch.setenv("REVIEW_BATCH_LANE", "1")
    monkeypatch.setattr(batch_lane, "submit", lambda kwargs: "BATCHED")
    monkeypatch.setattr(ai_service, "providers", lambda: (_ for _ in ()).throw(AssertionError("live chain touched")))
    with batch_lane.night_lane():
        assert ai_service.create_message(model="m", messages=[]) == ("BATCHED", "anthropic-batch")


def test_the_sweep_runs_in_the_lane_and_grade_all_does_not():
    route = open(os.path.join(ROOT, "app", "routes", "review_jobs.py"), encoding="utf-8").read()
    sweep = open(os.path.join(ROOT, "app", "services", "sweeper_service.py"), encoding="utf-8").read()
    start = route.split("def start_job(")[1].split("def ")[0]
    assert "night=" not in start                                    # Grade-all: live path
    assert "night=batch_lane.enabled()" in route.split("def sweep_now(")[1]  # manual sweep
    assert "night=batch_lane.enabled()" in sweep                    # scheduled sweep
    assert "note=SWEEP_NOTE" in sweep


def test_night_for_reads_the_job_note(monkeypatch):
    from app.services import review_job_service as jobs
    notes = {1: jobs.SWEEP_NOTE, 2: "assignment 7", 3: jobs.LIVE_NOTE}
    monkeypatch.setattr(jobs, "tquery", lambda t, sql, params=(): [{"note": notes[params[0]]}])
    monkeypatch.setenv("REVIEW_BATCH_LANE", "1")
    assert jobs.night_for(object(), 1) is True
    assert jobs.night_for(object(), 2) is False
    assert jobs.night_for(object(), 3) is False
    monkeypatch.setenv("REVIEW_BATCH_LANE", "0")
    assert jobs.night_for(object(), 1) is False                     # lane off: nobody rides it


def test_run_one_sets_the_lane_in_the_calling_thread(monkeypatch):
    """The lane is a contextvar: it must be entered where the call is made.
    A live item claimed ahead of a night batch never rides it."""
    from app.services import review_job_service as jobs
    monkeypatch.setenv("REVIEW_BATCH_LANE", "1")
    monkeypatch.setattr(jobs, "mark_item", lambda *a, **k: None)
    seen = []
    def review_one(scope_id, submission_id):
        seen.append(batch_lane.active())
        return "done", "", 7.0
    jobs._run_one(object(), {"id": 1, "scope_id": 7, "submission_id": 11}, review_one, night=True)
    jobs._run_one(object(), {"id": 2, "scope_id": 7, "submission_id": 12, "live": True}, review_one, night=True)
    jobs._run_one(object(), {"id": 3, "scope_id": 7, "submission_id": 13}, review_one, night=False)
    assert seen == [True, False, False]
    assert batch_lane.active() is False                             # nothing leaks out
