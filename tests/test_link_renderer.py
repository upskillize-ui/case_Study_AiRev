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


# ── the sign-in wall that got through the first phrase list ───────────────
#
# Live 22 Aug, student 1039's link. Reported READABLE, 251 words. Every one
# of those words was Notion's login form. Note the curly apostrophe and the
# line breaks — the reason it was missed.

NOTION_SIGNIN = """Skip to content
Skip to content
D
You’re almost there!
Sign in to see this page in Darshana Gaikwad’s space
Email
Use an organization email to easily collaborate with teammates
Continue
or continue with
Google
ChatGPT
Apple
Microsoft
Passkey
SSO
New user? Sign up
By continuing, you acknowledge that you understand and agree to the Terms
What is Notion?"""

# Student 337's link, same run: a real portfolio, must keep its grade.
REAL_PORTFOLIO = """30 Days 30 AI Tools : Overview
Chatgpt
What we learnt ? Uploading images in chatgpt. Making it colourful.
Generating our future photo with desired occupation.
Claude
What we learnt about Claude? Making games. Generating PPTs. Creating apps.
Perplexity
AI-powered search and answer engine, not just a chatbot. Gives sources and
citations for most answers so you can verify info. Great for research.
Notion AI
Revise the tool. Understand the tool. Practice the tool."""


def test_the_notion_signin_wall_is_caught_however_it_is_punctuated():
    reason = lr.interstitial_reason("Notion", NOTION_SIGNIN)
    assert "private" in reason
    assert "Publish" in reason                # tells the student the fix


def test_a_signin_wall_is_caught_even_when_it_runs_long():
    """It slipped through at 251 words. Length must not rescue a gate."""
    padded = NOTION_SIGNIN + ("\nterms and conditions and privacy policy" * 60)
    assert len(padded.split()) > lr.LINK_INTERSTITIAL_MAX_WORDS
    assert "private" in lr.interstitial_reason("Notion", padded)


def test_the_real_portfolio_from_the_same_run_still_grades():
    assert lr.interstitial_reason("30 Days 30 AI Tools : Overview",
                                  REAL_PORTFOLIO) == ""


def test_curly_apostrophes_and_line_breaks_never_hide_a_phrase():
    assert lr._flatten("You’re\nalmost   there!") == "you're almost there!"
    split_across_lines = "Sign in to see\nthis page in someone's space"
    assert "private" in lr.interstitial_reason("", split_across_lines)


def test_a_student_writing_about_signing_in_keeps_the_grade():
    """Weak phrases stay weak: a long page that discusses signing in to
    ChatGPT is a portfolio, not a login wall."""
    essay = ("To use ChatGPT you continue with Google and sign in to continue. "
             "I wrote about that on day one. " * 40)
    assert lr.interstitial_reason("My Portfolio", essay) == ""


# ── not tripping the host's rate limit ────────────────────────────────────
#
# The Day 04 audit opened 73 Notion pages back to back. 26 read fine, 23
# came back "Just a moment...", and the two kinds interleave through the
# run — the shape of rate limiting, not of a broken renderer. Spacing
# visits to one host costs a link day a couple of minutes and stops us
# manufacturing "US must act" rows out of our own impatience.

def test_a_host_we_have_not_visited_is_never_delayed():
    assert lr.wait_needed(0.0, 100.0, 5000) == 0.0


def test_a_second_visit_too_soon_waits_the_remainder():
    assert lr.wait_needed(100.0, 102.0, 5000) == pytest.approx(3.0)


def test_a_host_left_alone_long_enough_is_not_delayed():
    assert lr.wait_needed(100.0, 130.0, 5000) == 0.0


def test_the_gap_can_be_switched_off():
    assert lr.wait_needed(100.0, 100.5, 0) == 0.0


def test_a_clock_that_jumped_never_parks_a_review():
    """max one gap, whatever the clock says."""
    assert lr.wait_needed(9_999_999.0, 100.0, 5000) == 5.0


def test_a_challenged_page_is_reloaded_before_being_given_up_on():
    import inspect
    src = inspect.getsource(lr._render_raw)
    assert "page.reload" in src
    assert "reloaded" in src, "reload once, not on every settle pass"


def test_the_settle_budget_is_long_enough_for_a_cloudflare_challenge():
    assert lr.LINK_RENDER_SETTLE_TRIES * lr.LINK_RENDER_SETTLE_MS >= 20_000


# ── the browser queue must have an exit ───────────────────────────────────
#
# Day 05, 22 Aug: two items took 3,916,989ms and 3,941,830ms — 65 minutes
# each — and the run's failure rate exploded immediately after. `with
# _render_lock:` waits forever, so one wedged Chromium parks every other
# worker behind it for as long as it stays wedged.

def test_a_worker_gives_up_on_a_busy_browser_instead_of_waiting_forever(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(lr, "check_public_url", lambda url: (True, ""))
    monkeypatch.setattr(lr, "LINK_RENDER_LOCK_WAIT_S", 0.05)
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: pytest.fail("rendered while the lock was held"))
    lr._render_lock.acquire()                      # another worker is mid-render
    try:
        rendered, why = lr.render_link(ART)
    finally:
        lr._render_lock.release()
    assert rendered is None and "busy" in why


def test_the_lock_is_released_even_when_a_render_explodes(monkeypatch):
    """A crash inside the browser must not wedge every later review."""
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(lr, "check_public_url", lambda url: (True, ""))

    def boom(url):
        raise RuntimeError("chromium died")

    monkeypatch.setattr(lr, "_render_raw", boom)
    with pytest.raises(RuntimeError):
        lr.render_link(ART)
    assert lr._render_lock.acquire(timeout=0.1), "lock leaked after a crash"
    lr._render_lock.release()


def test_the_host_gap_still_applies_when_the_lock_was_free(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(lr, "check_public_url", lambda url: (True, ""))
    monkeypatch.setattr(lr, "_render_raw", lambda url: (_rendered(text="x " * 200), ""))
    slept = []
    monkeypatch.setattr(lr.time, "sleep", lambda s: slept.append(s))
    lr._last_visit.clear()
    lr.render_link(ART)                            # first visit: no pause
    assert slept == []
    lr.render_link(ART)                            # second: spaced
    assert slept and slept[0] > 0


# ── Google's sign-in wall ─────────────────────────────────────────────────
#
# Live 23 Aug, student 130's NotebookLM link. render_check reported
# "READABLE — 186 words". Every one of those words was Google's login form.
# The phrase list knew Notion's wording and not Google's, which means the
# Day 05 audit's READABLE verdicts were counting login pages as student work.

GOOGLE_SIGNIN = """Loading
Sign in
Use your Google Account
Email or phone
Forgot email?
Not your computer? Use Guest mode to sign in privately. Learn more about
using Guest mode
Next
Create account
English (United States)
Help
Privacy
Terms"""


def test_googles_sign_in_wall_is_not_a_submission():
    reason = lr.interstitial_reason("Sign in - Google Accounts", GOOGLE_SIGNIN)
    assert "private" in reason and "Publish" in reason


def test_the_google_wall_is_caught_by_body_alone():
    """The title is not always 'Sign in - Google Accounts'."""
    assert "private" in lr.interstitial_reason("", GOOGLE_SIGNIN)


def test_a_notebook_write_up_that_mentions_google_still_grades():
    work = ("My NotebookLM notebook on India's digital payments. I signed in "
            "with my Google account, uploaded three RBI circulars as sources, "
            "generated an Audio Overview and a Mind Map, then fixed two wrong "
            "figures the audio had invented. " * 6)
    assert lr.interstitial_reason("My notebook", work) == ""
