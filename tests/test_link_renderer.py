"""The agent's own browser — flag-gated, SSRF-guarded, honest on failure.

The browser itself is not exercised here (CI has no display and the sandbox
proxy resets browser traffic); everything AROUND it is: the flag, the guard
order, the vision-spend decision, the text composition, and the intake
wiring with its fallback. The real browser gets its canary on the Space via
POST /api/review/jobs/render-check with Ranjana's two live student links.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import link_renderer as lr
from app.utils import submission_intake as intake


ART = "https://claude.ai/public/artifacts/78cff8f0-32a7-4213-930c-120df82487b8"


def _rendered(text="", shot="x" * 100, title="EMI Quiz"):
    return lr.Rendered(title=title, text=text, screenshot_b64=shot,
                       final_url=ART)


# ── the flag: shipping this must change nothing ───────────────────────────

def test_off_means_inert(monkeypatch):
    monkeypatch.delenv("LINK_RENDER_ENABLED", raising=False)
    assert lr.enabled() is False
    rendered, why = lr.render_link(ART)
    assert rendered is None and "switched off" in why


def test_intake_never_touches_the_renderer_while_off(monkeypatch):
    monkeypatch.delenv("LINK_RENDER_ENABLED", raising=False)
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: pytest.fail("browser started while off"))
    monkeypatch.setattr(intake, "fetch_link", lambda url: ("", "browser-only"))
    arts = intake.from_links_in(ART)
    assert len(arts) == 1 and not arts[0].readable and arts[0].confirmed


# ── the guard runs before any browser exists ──────────────────────────────

def test_a_private_url_is_refused_without_launching(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(lr, "check_public_url",
                        lambda url: (False, "private address refused"))
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: pytest.fail("browser launched for a private URL"))
    rendered, why = lr.render_link("http://169.254.169.254/")
    assert rendered is None and "refused" in why


def test_in_page_request_blocking_is_pure_and_exact():
    assert lr._blocked_host("localhost") is True
    assert lr._blocked_host("127.0.0.1") is True
    assert lr._blocked_host("169.254.169.254") is True
    assert lr._blocked_host("10.2.3.4") is True
    assert lr._blocked_host("db.internal") is True
    assert lr._blocked_host("printer.local") is True
    assert lr._blocked_host("claude.ai") is False
    assert lr._blocked_host("cdn.example.com") is False
    assert lr._blocked_host("8.8.8.8") is False


# ── vision spend: screenshots are read only when the text is thin ─────────

def test_a_visual_page_sends_its_screenshot_to_vision():
    assert lr.needs_vision(_rendered(text="Play  Score: 0")) is True


def test_a_text_rich_page_costs_no_vision_call():
    doc = _rendered(text="word " * 200)
    assert lr.needs_vision(doc) is False


def test_read_rendered_link_ocrs_the_game_and_not_the_doc(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(lr, "check_public_url", lambda url: (True, ""))
    calls = []
    import app.utils.file_extractor as fe
    monkeypatch.setattr(fe, "_ocr_with_claude",
                        lambda images, kind: (calls.append(kind)
                                              or ("A quiz with three questions "
                                                  "about EMI calculation", "")))
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: (_rendered(text="Start Quiz"), ""))
    text, why = lr.read_rendered_link(ART)
    assert why == "" and "EMI Quiz" in text and "three questions" in text
    assert len(calls) == 1

    calls.clear()
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: (_rendered(text="content " * 150), ""))
    text, why = lr.read_rendered_link(ART)
    assert why == "" and calls == []      # rich text -> no OCR money spent


def test_provenance_is_stated_in_the_composed_text():
    text = lr.compose_submission_text(_rendered(text="Q1. What is EMI?"),
                                      ocr_text="A blue quiz interface")
    assert text.startswith("[PUBLISHED PAGE OPENED IN A BROWSER — title: EMI Quiz]")
    assert "Q1. What is EMI?" in text
    assert "[WHAT THE PAGE LOOKS LIKE ON SCREEN]" in text


def test_an_empty_render_reports_honestly(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(lr, "check_public_url", lambda url: (True, ""))
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: (lr.Rendered("", "", "", ART), ""))
    text, why = lr.read_rendered_link(ART)
    assert text == "" and "empty" in why


# ── the intake wiring: render beats preview, failure falls back ───────────

def test_a_rendered_link_becomes_a_readable_artefact(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(intake, "fetch_link", lambda url: ("", "browser-only"))
    monkeypatch.setattr(lr, "read_rendered_link",
                        lambda url: ("[PUBLISHED PAGE OPENED IN A BROWSER]\n"
                                     "My EMI quiz with three questions.", ""))
    arts = intake.from_links_in(ART)
    assert len(arts) == 1 and arts[0].readable
    assert "EMI quiz" in arts[0].text


def test_the_render_replaces_a_preview_only_body(monkeypatch):
    """fetch_link's metadata fallback ('[This link is a published page...')
    is two lines of preview; a real render must win over it."""
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(intake, "fetch_link",
                        lambda url: ("[This link is a published page whose "
                                     "content is rendered in the browser...]"
                                     "\nTitle: Claude Artifact", ""))
    monkeypatch.setattr(lr, "read_rendered_link",
                        lambda url: ("[PUBLISHED PAGE OPENED IN A BROWSER]\n"
                                     "Full rendered quiz content here.", ""))
    arts = intake.from_links_in(ART)
    assert "Full rendered quiz content" in arts[0].text


def test_render_failure_keeps_the_old_honest_fallback(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(intake, "fetch_link", lambda url: ("", ""))
    monkeypatch.setattr(lr, "read_rendered_link",
                        lambda url: ("", "the page could not be rendered (TimeoutError)"))
    arts = intake.from_links_in(ART)
    assert not arts[0].readable and arts[0].confirmed
    assert "rendered" in arts[0].note


def test_preview_marker_detection_is_pure():
    assert intake._is_preview_only("[This link is a published page …]") is True
    assert intake._is_preview_only("Real content of a real page") is False
    assert intake._is_preview_only("") is False


# ── Day 04: a fetch can succeed and still not be the work ─────────────────

NOTION = "https://tejasvini.notion.site/30-Days-30-AI-Tools-Portfolio"

# What a published-site shell serves a plain reader: real words, none of
# them the student's. It clears LINK_MIN_WORDS, so the old rule accepted it.
SITE_CHROME = ("30 Days 30 AI Tools Portfolio. Home About Contact. "
               "Built with Notion. This site uses cookies to improve your "
               "experience. Accept all cookies or manage preferences. "
               "Powered by Notion. Duplicate this template. Sign up free. "
               "Log in. Privacy policy. Terms of service.")

REAL_WORK = ("ChatGPT: I used it to draft my career plan and learned to give "
             "it a role and a constraint. " * 12)


def test_a_published_site_shell_is_opened_in_the_browser(monkeypatch):
    """The Day 04 failure mode: chrome long enough to pass for content."""
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(intake, "fetch_link", lambda url: (SITE_CHROME, ""))
    monkeypatch.setattr(lr, "read_rendered_link", lambda url: (REAL_WORK, ""))
    arts = intake.from_links_in(NOTION)
    assert arts[0].readable
    assert "career plan" in arts[0].text          # the work, not the footer
    assert "cookies" not in arts[0].text


def test_a_content_rich_fetch_never_starts_a_browser(monkeypatch):
    """Rendering is protection, not a toll — a page that already read fine
    must cost no browser and no time."""
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(intake, "fetch_link", lambda url: (REAL_WORK, ""))
    monkeypatch.setattr(lr, "read_rendered_link",
                        lambda url: pytest.fail("browser started for a rich page"))
    arts = intake.from_links_in(NOTION)
    assert arts[0].readable and "career plan" in arts[0].text


def test_a_thinner_render_never_replaces_a_better_fetch(monkeypatch):
    """A render that comes back emptier is a failed render, not an upgrade."""
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(intake, "fetch_link", lambda url: (SITE_CHROME, ""))
    monkeypatch.setattr(lr, "read_rendered_link",
                        lambda url: ("Loading...", ""))
    arts = intake.from_links_in(NOTION)
    assert "cookies" in arts[0].text              # kept the better of the two


def test_the_thin_and_richer_rules_are_pure():
    assert intake._is_thin_body("word " * 10) is True
    assert intake._is_thin_body("word " * 300) is False
    assert intake._is_thin_body("") is True
    assert intake._render_is_richer("a b c", "") is True
    assert intake._render_is_richer("a b c", "[This link is a published page]") is True
    assert intake._render_is_richer("a b", "one two three four") is False


# ── the switch is a switch, not a count ───────────────────────────────────

def test_anything_that_is_not_plainly_off_switches_the_browser_on(monkeypatch):
    """Ranjana, 22 Aug: 'what if i put 3 here?' Under the old rule 3 read as
    OFF and every published page on a link day would come back unread, with
    nothing in the log saying why. Only an explicit off value switches off."""
    for value in ("1", "3", "true", "TRUE", "yes", "on", "Y", "2"):
        monkeypatch.setenv("LINK_RENDER_ENABLED", value)
        assert lr.enabled() is True, value


def test_the_explicit_off_values_all_switch_it_off(monkeypatch):
    for value in ("0", "false", "FALSE", "no", "off", "", "  ", "none"):
        monkeypatch.setenv("LINK_RENDER_ENABLED", value)
        assert lr.enabled() is False, repr(value)


def test_an_absent_variable_is_still_off(monkeypatch):
    monkeypatch.delenv("LINK_RENDER_ENABLED", raising=False)
    assert lr.enabled() is False


# ── gates, challenges and error pages are never the submission ────────────
#
# Live 22 Aug, three real Day 04 student links. Two came back "Your browser
# is not compatible with Notion" and one came back Cloudflare's "Just a
# moment... Verifying...". All three were reported READABLE, and the run
# would have graded 123 students on Notion's error text. These are the
# pages, verbatim from that run.

NOTION_INCOMPATIBLE = ("Your browser is not compatible with Notion.\n"
                       "Please upgrade to the latest browser version, or "
                       "visit our help center for more information.")


def test_the_notion_compatibility_page_is_not_a_submission():
    assert "refused to open" in lr.interstitial_reason("Notion",
                                                       NOTION_INCOMPATIBLE)


def test_the_cloudflare_challenge_is_not_a_submission():
    reason = lr.interstitial_reason("Just a moment...", "Verifying...")
    assert "human-check" in reason


def test_a_private_link_says_so_instead_of_scoring_zero():
    reason = lr.interstitial_reason("Notion", "You need access to view this "
                                              "page. Request access.")
    assert "private" in reason


def test_a_real_portfolio_is_never_called_a_gate():
    work = ("My 30 Days 30 AI Tools portfolio. Day 01 ChatGPT: I learned to "
            "give the model a role before a task. Day 02 Claude: I built an "
            "EMI calculator artifact. Day 03 Perplexity: I checked three "
            "sources before trusting a number. " * 8)
    assert lr.interstitial_reason("My Portfolio", work) == ""


def test_a_long_page_that_merely_mentions_signing_in_is_still_work():
    """The false positive that would delete a real grade: a portfolio about
    AI tools naturally contains the words 'sign in'."""
    work = ("To use ChatGPT you sign in to continue with a Google account. " 
            "That was my first step. " * 40)
    assert lr.interstitial_reason("Portfolio", work) == ""


def test_an_interstitial_never_reaches_the_marker_and_never_buys_vision(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(lr, "check_public_url", lambda url: (True, ""))
    import app.utils.file_extractor as fe
    monkeypatch.setattr(fe, "_ocr_with_claude",
                        lambda images, kind: pytest.fail("paid to read an error page"))
    monkeypatch.setattr(lr, "_render_raw", lambda url: (
        lr.Rendered(title="Notion", text=NOTION_INCOMPATIBLE,
                    screenshot_b64="x" * 100, final_url=ART), ""))
    text, why = lr.read_rendered_link(ART)
    assert text == ""
    assert "refused to open" in why


def test_the_browser_claims_a_version_sites_can_parse():
    """Notion rejected 'Chrome/126.0'. Real Chrome sends four parts."""
    import re as _re
    assert _re.search(r"Chrome/\d+\.\d+\.\d+\.\d+ Safari", lr.CHROME_UA)
    assert "Headless" not in lr.CHROME_UA
