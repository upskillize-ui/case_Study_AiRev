"""link_audit: open every ungraded link and say who must act.

The Day 04 failure this answers: 118 students ungraded, and problem_report
labelled 108 of them "US (re-run) — do NOT message this student" when the
sampled link was a PRIVATE Notion page. Re-running would have changed
nothing and the students would never have been told. These tests pin the
verdict rule, because getting it backwards either spams students whose work
is fine or silences students whose work nobody can see.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from link_audit import ACT_OK, ACT_STUDENT, ACT_US, ungraded_link_rows, verdict_for


def test_a_page_we_read_is_never_the_students_problem():
    act, why = verdict_for(True, 143, "")
    assert act == ACT_OK and "143 words" in why


def test_a_private_page_is_the_students_to_fix():
    act, why = verdict_for(False, 0, "this link is private — it opens a "
                                     "sign-in page instead of the work")
    assert act == ACT_STUDENT


def test_a_deleted_page_is_the_students_to_fix():
    act, _ = verdict_for(False, 0, "the page no longer exists at that address")
    assert act == ACT_STUDENT


def test_a_cloudflare_block_is_ours_not_theirs():
    """The student published correctly; the site refused our browser."""
    act, why = verdict_for(False, 0, "the page was still showing a human-check "
                                     "(Cloudflare) when the browser gave up")
    assert act == ACT_US and "not the student" in why


def test_an_unexplained_failure_defaults_to_us():
    """Telling a student to fix work that is fine is the worse error."""
    assert verdict_for(False, 0, "")[0] == ACT_US
    assert verdict_for(False, 0, "the page could not be rendered (TimeoutError)")[0] == ACT_US


def test_a_page_that_rendered_almost_nothing_is_not_called_readable():
    assert verdict_for(True, 3, "")[0] != ACT_OK


class FakeCursor:
    def __init__(self, rows): self.rows = rows
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=()): pass
    def fetchall(self): return self.rows


class FakeConn:
    def __init__(self, rows): self.rows = rows
    def cursor(self): return FakeCursor(self.rows)


def _row(sid, notes, grade=None, when="2026-08-20"):
    return {"id": sid, "student_id": sid, "grade": grade, "notes": notes,
            "file_ref": "", "submitted_at": when}


def test_only_ungraded_link_rows_are_audited():
    rows = ungraded_link_rows(FakeConn([
        _row(1, "https://a.notion.site/one"),
        _row(2, "https://b.notion.site/two", grade=7.5),   # already marked
        _row(3, "I wrote my answer here, no link at all"),
        _row(4, ""),
    ]), 21)
    assert [r["student_id"] for r in rows] == [1]


def test_only_the_newest_attempt_counts():
    """A student who resubmitted must be audited on the link they sent last."""
    rows = ungraded_link_rows(FakeConn([
        _row(9, "https://new.notion.site/fixed", when="2026-08-21"),
        _row(9, "https://old.notion.site/broken", when="2026-08-19"),
    ]), 21)
    assert len(rows) == 1 and "fixed" in rows[0]["url"]
