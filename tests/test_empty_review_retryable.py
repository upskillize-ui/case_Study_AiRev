# tests/test_empty_review_retryable.py
# ---------------------------------------------------------------------------
# AN EMPTY REVIEW IS A FAILED CALL, NOT A VERDICT (04 Sep 2026, seen live).
#
# Provider cap at 03:30 UTC → model returned nothing → guard refused (right)
# → refusal stamped with the rules version (wrong): 233 rows parked as
# "decided under current rules, never retry" behind an outage that lifted
# two hours later. These pin: an empty-review refusal is written without the
# stamp, a content refusal keeps it, and the sweeper's own phrase list still
# matches the message either way.
# ---------------------------------------------------------------------------

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.services import grade_guard as gg
from app.services import assignment_db_service as dbs


def test_empty_review_reasons_are_retryable():
    assert gg.refusal_is_retryable("the reviewer produced no feedback at all — no strengths…")
    assert gg.refusal_is_retryable("the reviewer returned no judgement for any part of this task")


def test_content_refusals_are_not():
    for why in ["too little of the task was judged to store a mark",
                "a mark with nothing behind it: zero words reached the marker", ""]:
        assert not gg.refusal_is_retryable(why), why


def _capture(monkeypatch):
    writes = []
    monkeypatch.setattr(dbs, "texecute", lambda t, sql, params=(): writes.append(params))
    return writes


def test_mark_not_graded_omits_the_stamp_when_asked(monkeypatch):
    writes = _capture(monkeypatch)
    dbs.mark_not_graded("t", 1, "msg", stamp=False)
    payload = json.loads(writes[-1][0])
    assert payload["notGraded"] is True and "rulesVersion" not in payload


def test_mark_not_graded_stamps_by_default(monkeypatch):
    writes = _capture(monkeypatch)
    dbs.mark_not_graded("t", 1, "msg")
    payload = json.loads(writes[-1][0])
    assert "rulesVersion" in payload


def test_the_guard_write_path_passes_the_flag():
    src = open(os.path.join(os.path.dirname(_HERE), "app", "services", "assignment_db_service.py"),
               encoding="utf-8").read()
    body = src.split("def update_assignment_submission_with_ai_results(")[1].split("def ")[0]
    assert "refusal_is_retryable(why)" in body
    assert "stamp=not retry" in body


def test_sweeper_still_recognises_the_message_as_ours():
    from app.services import sweeper_service
    assert any(p in "We could not finish reviewing this attempt, so there are no marks yet."
               for p in sweeper_service.OUR_REFUSAL_PHRASES)
