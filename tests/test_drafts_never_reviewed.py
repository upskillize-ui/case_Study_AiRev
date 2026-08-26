"""'draft' rows are not submissions — the LMS creates one the moment a
student merely OPENS an assignment. The agent must never review, zero,
or message a draft (Ranjana, 26 Aug). Silent exclusion, everywhere."""
import re


def _src(path):
    return open(path, encoding="utf-8").read()


def test_batch_picker_excludes_drafts():
    src = _src("app/routes/review_jobs.py")
    picker = src.split("FROM assignment_submissions s")[1][:300]
    assert "<> 'draft'" in picker


def test_single_enqueue_excludes_drafts():
    src = _src("app/routes/review_jobs.py")
    assert re.search(r"DUAL_ID_MATCH.*status, ''\) <> 'draft'", src, re.S)


def test_regrade_route_skips_drafts_silently():
    src = _src("app/routes/assignment_review.py")
    assert '"skipped": "draft"' in src
    # the skip happens right after the row load, BEFORE any grade write
    guard = src.index('"skipped": "draft"')
    load = src.index("get_submission_for_regrade")
    assert load < guard < load + 1200


def test_bulk_tool_excludes_drafts():
    src = _src("tools/bulk_review.py")
    assert "<> 'draft'" in src
