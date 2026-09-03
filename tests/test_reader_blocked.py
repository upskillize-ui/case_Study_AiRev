# tests/test_reader_blocked.py
# ---------------------------------------------------------------------------
# THE SITE THAT WOULD NOT LET A ROBOT IN (04 Sep 2026).
#
# claude.ai artifacts, perplexity.ai and chatgpt.com shares open for any
# person and refuse our headless browser (Cloudflare human-check, or the
# tool's signed-out shell). The old refusal read "We could not open your
# file (the page was still showing a human-check (Cloudflare) when the
# browser gave up) … re-attach your work" — jargon, wrong advice, and the
# LMS filed it as the student's fault. These pin: the phrase is recognised,
# it is NOT an outage (stamped, not looped), and the learner gets one plain
# sentence that says whose side it is on and what actually helps.
# ---------------------------------------------------------------------------

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.services import grade_guard as gg

LIVE = [
    "https://www.perplexity.ai/search/fbef1646…: the page was still showing a human-check (Cloudflare) when the browser gave up",
    "https://claude.ai/public/artifacts/134e4014…: the page was still showing a human-check (Cloudflare) when the browser gave up",
    "the link opened the tool's own page rather than your work — we know because another student's different link returned the same page",
]


def test_the_live_phrases_are_recognised():
    for why in LIVE:
        assert gg.reader_blocked(why), why


def test_it_is_not_an_outage_so_the_row_is_stamped_not_looped():
    for why in LIVE:
        assert not gg.reads_as_our_outage(why), why


def test_a_learners_own_failures_are_not_reader_blocked():
    for why in ["download HTTP 404 (attempt 2)", "password-protected PDF",
                "the file is empty", "link host could not be resolved", ""]:
        assert not gg.reader_blocked(why), why


def test_the_message_is_plain_and_says_whose_side():
    m = gg.READER_BLOCKED_MESSAGE
    assert "our side, not yours" in m
    assert "screenshot" in m.lower()
    for jargon in ("cloudflare", "headless", "robot", "http", "render"):
        assert jargon not in m.lower(), jargon
    assert not any(w in m.lower() for w in ("soon", "shortly", "within "))   # no promise of when


def test_both_refusal_branches_use_it():
    src = open(os.path.join(os.path.dirname(_HERE), "app", "routes", "assignment_review.py"),
               encoding="utf-8").read()
    assert src.count("grade_guard.READER_BLOCKED_MESSAGE") == 2
    assert src.count("grade_guard.reader_blocked(") == 2
