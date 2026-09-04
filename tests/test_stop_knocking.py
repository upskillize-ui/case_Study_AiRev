"""Stop knocking (04 Sep 2026, Day 09 live): after N consecutive bot-checks
from one host, the host is not opened again in this process — the row gets
the same reader-blocked verdict without the goto, the reloads, the host gap
and the browser lock that made every Gamma row cost three to four minutes.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)

from app.services import link_renderer as lr
from app.services import grade_guard as g


def test_a_host_that_keeps_bot_checking_is_not_visited_again(monkeypatch):
    monkeypatch.setattr(lr, "_challenges", {})
    monkeypatch.setattr(lr, "LINK_RENDER_GIVE_UP_AFTER", 3)
    for _ in range(3):
        lr.note_challenge("gamma.app", True)
    assert lr.host_given_up("gamma.app") == 3
    assert lr.host_given_up("notion.site") == 0

    monkeypatch.setattr(lr, "enabled", lambda: True)
    monkeypatch.setattr(lr, "check_public_url", lambda u: (True, ""))
    monkeypatch.setattr(lr, "_render_raw", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not open the browser")))
    rendered, why = lr.render_link("https://gamma.app/docs/x")
    assert rendered is None
    assert "3 times in a row" in why
    # The verdict keeps the reader-blocked shape: stamped, never an outage.
    assert g.reader_blocked(why) and not g.reads_as_our_outage(why)


def test_a_clean_visit_resets_the_count(monkeypatch):
    monkeypatch.setattr(lr, "_challenges", {})
    monkeypatch.setattr(lr, "LINK_RENDER_GIVE_UP_AFTER", 3)
    lr.note_challenge("gamma.app", True); lr.note_challenge("gamma.app", True)
    lr.note_challenge("gamma.app", False)
    assert lr.host_given_up("gamma.app") == 0
