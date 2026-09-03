# tests/test_gate_is_not_a_body.py
# ---------------------------------------------------------------------------
# THE SHELL THAT READ AS WORK (04 Sep 2026, seen live on job 827).
#
# fetch_link() of a chatgpt.com share returns ChatGPT's signed-out page; of a
# Cloudflare-fronted page, the challenge. Both are text, so both passed as a
# readable link and were graded as the learner's answer — three zeros for
# "doing 0 of 2 requirements" on ChatGPT's page furniture. The renderer had
# called them shell / human-check and was overruled by the non-empty body.
#
# Pinned here: an interstitial body is unread; a blocked verdict from the
# browser wins over any scraped text; a real page still reads.
# Pure — fetch and renderer are stubbed.
# ---------------------------------------------------------------------------

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.utils import submission_intake as intake
from app.services import link_renderer

URL = "https://chatgpt.com/s/m_6a759dd557a481918cdb129e95802153"
CF = "Just a moment... Enable JavaScript and cookies to continue. Verifying you are human. This may take a few seconds."
SHELL = ("the link opened the tool's own page rather than your work — we know "
         "because another student's different link returned the same page")
REAL = "In five years I see myself as a junior credit analyst at a cooperative bank in Latur. " * 6


def _run(monkeypatch, body, rendered=("", "", "", ""), url=URL):
    link_renderer.forget_pages()
    monkeypatch.setattr(intake, "fetch_link", lambda u: (body, "" if body else "empty"))
    monkeypatch.setattr(link_renderer, "enabled", lambda: True)
    monkeypatch.setattr(link_renderer, "read_rendered_page", lambda u, walk=False: rendered)
    monkeypatch.setattr(link_renderer, "task_wants_a_walkthrough", lambda t: False)
    return intake.from_links_in(url)[0]


def test_a_cloudflare_challenge_body_is_not_readable(monkeypatch):
    a = _run(monkeypatch, CF, rendered=("", "the page was still showing a human-check (Cloudflare) when the browser gave up", "", ""))
    assert not a.readable
    assert "human-check" in (a.note or "")
    assert a.confirmed                      # the link exists; its substance is unknown


def test_a_tool_shell_verdict_beats_the_scraped_body(monkeypatch):
    shell_text = "ChatGPT Log in Sign up for free What can I help with? Terms Privacy " * 12   # ~170 words
    a = _run(monkeypatch, shell_text, rendered=("", SHELL, "", ""))
    assert not a.readable
    assert "tool's own page" in (a.note or "")


def test_a_real_page_still_reads(monkeypatch):
    a = _run(monkeypatch, REAL, rendered=("", "", "", ""), url="https://haritha-sp.netlify.app/")
    assert a.readable and "credit analyst" in a.text


def test_a_js_app_host_is_never_graded_from_its_plain_html(monkeypatch):
    # The FIRST ChatGPT share of a run: no duplicate to compare against, no
    # phrase match — only the host says "this is an app shell".
    shell_text = "ChatGPT Log in Sign up for free What can I help with? Terms Privacy " * 12
    a = _run(monkeypatch, shell_text, rendered=("", "", "", ""))
    assert not a.readable
    assert "browser" in (a.note or "")
    # …but a real render of the same host is the work.
    b = _run(monkeypatch, shell_text, rendered=(REAL, "", "", ""))
    assert b.readable and "credit analyst" in b.text


def test_the_second_identical_shell_is_caught_even_on_an_unknown_host(monkeypatch):
    shell_text = "Welcome to NewTool. Sign up to start building. Pricing Docs Blog Careers " * 10
    first = _run(monkeypatch, shell_text, url="https://newtool.example/share/aaa")
    assert first.readable                      # nothing yet says it is a shell
    monkeypatch.setattr(intake, "fetch_link", lambda u: (shell_text, ""))
    second = intake.from_links_in("https://newtool.example/share/bbb")[0]
    assert not second.readable and "tool's own page" in (second.note or "")


def test_a_richer_render_still_wins(monkeypatch):
    a = _run(monkeypatch, "short teaser", rendered=(REAL, "", "", ""))
    assert a.readable and "credit analyst" in a.text


def test_the_gate_phrases_are_the_shared_definition():
    from app.services import grade_guard
    assert grade_guard.reader_blocked(SHELL)
    assert grade_guard.reader_blocked("the page was still showing a human-check (Cloudflare) when the browser gave up")
