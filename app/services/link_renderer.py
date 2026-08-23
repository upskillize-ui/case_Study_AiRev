# app/services/link_renderer.py
# ---------------------------------------------------------------------------
# THE AGENT'S OWN BROWSER — for links that only exist once a browser runs them.
#
# Verified 22 Aug against live pages: a claude.ai artifact link serves every
# plain reader the identical "Claude Artifact" boilerplate; the student's
# actual work is BUILT in the visitor's browser by JavaScript. The same is
# true of Gamma decks, Lovable apps, Notion pages and most published-link
# deliverables this course assigns. Ranjana: "build the browser to open link
# and read."
#
# So: headless Chromium (Playwright) loads the page exactly as a student's
# friend's phone would, waits for the app to draw itself, then harvests BOTH
# the rendered text (page + every iframe — artifacts render inside iframes)
# and a viewport screenshot for the vision reviewer. What comes back feeds
# the SAME intake pipeline as an uploaded screenshot would — no second
# scoring path.
#
# Safety, in the order it matters:
#   1. FLAG-GATED. Without LINK_RENDER_ENABLED=1 nothing here runs and no
#      browser is installed into memory — deploying this changes nothing.
#   2. ONE RENDER AT A TIME, process-wide. A fresh browser per render inside
#      a lock: ~1s startup cost per link, zero thread-affinity bugs (sync
#      Playwright objects are not shareable across threads), and cpu-basic's
#      16GB never holds more than one Chromium.
#   3. SSRF-GUARDED twice: check_public_url() vets the submitted URL before
#      any browser starts, and in-page requests to loopback/private/literal
#      -IP hosts are blocked by route interception. (Known limit, same as the
#      downloader: DNS answers that change mid-flight can slip the net — the
#      browser holds no credentials and runs in Chromium's sandbox.)
#   4. HARD TIMEOUTS. A page gets LINK_RENDER_TIMEOUT_MS total; a hung
#      render returns a reason, never a stuck review thread.
#   5. HONEST FAILURE. Anything that goes wrong returns (None, why) and the
#      caller falls back to the existing behaviour — confirmed-but-unread
#      link, student asked for a screenshot. Rendering can only ADD.
# ---------------------------------------------------------------------------

from __future__ import annotations

import base64
import ipaddress
import hashlib
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urlparse

from app.utils.url_guard import check_public_url

LINK_RENDER_WAIT_MS = int(os.getenv("LINK_RENDER_WAIT_MS", "6000"))
LINK_RENDER_TIMEOUT_MS = int(os.getenv("LINK_RENDER_TIMEOUT_MS", "30000"))
# Below this many rendered words the page is judged visual (a game, a poster
# app) and the screenshot goes to the vision reviewer as well. Above it, the
# text alone carries the submission and the OCR call's cost is not spent.
LINK_RENDER_OCR_MIN_WORDS = int(os.getenv("LINK_RENDER_OCR_MIN_WORDS", "80"))
# A challenge page ("Just a moment...") clears itself a few seconds later.
# Harvest, and if what came back is a gate rather than the work, wait and
# harvest again — only on pages that need it, so a normal page pays nothing.
LINK_RENDER_SETTLE_MS = int(os.getenv("LINK_RENDER_SETTLE_MS", "5000"))
LINK_RENDER_SETTLE_TRIES = int(os.getenv("LINK_RENDER_SETTLE_TRIES", "5"))
# Cloudflare's challenge clears on a reload more often than on waiting: after
# the first settle attempt fails, load the page once more before giving up.
LINK_RENDER_RELOAD_ON_CHALLENGE = os.getenv(
    "LINK_RENDER_RELOAD_ON_CHALLENGE", "1").strip().lower() not in ("0", "false", "no")
# Minimum gap between two renders of the SAME host. The Day 04 audit opened
# 73 Notion pages back to back and 23 came back as "Just a moment..." — while
# 26 of their neighbours read fine. Successes and failures interleave, which
# is the shape of rate limiting, not of a broken renderer. A few seconds of
# space between visits costs a link day nothing and stops us tripping it.
LINK_RENDER_HOST_GAP_MS = int(os.getenv("LINK_RENDER_HOST_GAP_MS", "5000"))
# How long a worker will wait for the browser to be free before giving up on
# rendering and letting the review proceed without it. Day 05 (22 Aug) had two
# items take 3,916,989ms and 3,941,830ms — 65 MINUTES each — and the run's
# failure rate exploded straight after them. `with _render_lock:` blocks
# forever by design, so one wedged Chromium parks every other worker behind
# it for as long as it stays wedged. A queue with no exit is not a queue.
LINK_RENDER_LOCK_WAIT_S = float(os.getenv("LINK_RENDER_LOCK_WAIT_S", "120"))
# Above this many words the page is long enough to be real work, and a
# stray "sign in" in a student's own text must not be read as a gate.
LINK_INTERSTITIAL_MAX_WORDS = int(os.getenv("LINK_INTERSTITIAL_MAX_WORDS", "200"))

# Claimed browser identity. Playwright's headless UA says "HeadlessChrome",
# which Notion answers with "Your browser is not compatible" — verified live
# 22 Aug on three real student links. This is the honest version string of
# the Chromium that Playwright 1.49 actually ships, in the four-part form
# every real Chrome sends; sites that version-check now get an answer they
# can parse instead of one they reject.
CHROME_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# What a gate, a challenge or an error page says — none of it the student's
# work. Live 22 Aug: two Notion links returned "Your browser is not
# compatible with Notion" and one returned Cloudflare's "Just a moment...",
# and all three would have been scored as the submission. A page matching
# any of these is reported unreadable WITH ITS REASON, so the student can be
# told what to fix instead of being graded on Notion's error text.
# What a gate, a challenge or an error page says — none of it the student's
# work. Live 22 Aug: two Notion links returned "Your browser is not
# compatible with Notion", one returned Cloudflare's "Just a moment...", and
# after those were fixed a third returned Notion's sign-in wall — "Sign in
# to see this page in Darshana Gaikwad's space" — which the first phrase
# list missed and reported as 251 readable words.
#
# STRONG phrases are unmistakable and match at any length: no student's
# portfolio contains Notion's own sign-in copy. WEAK phrases could occur in
# real writing ("page not found" in a note about a broken link), so they
# only count on a page too short to be the work.
_STRONG_INTERSTITIALS = (
    (("sign in to see this page", "sign in to see this",
      "you need access to view", "request access to view",
      "you don't have access to this page",
      # Google's own wall, verbatim from student 130's NotebookLM link
      # (23 Aug): title "Sign in - Google Accounts", body "Use your Google
      # Account ... Forgot email? ... Not your computer? Use Guest mode".
      # 186 words of login form, reported READABLE by the first phrase list
      # — which means every Day 05 "READABLE" verdict needs re-checking.
      "use your google account", "forgot email", "use guest mode",
      "sign in - google accounts", "couldn't sign you in",
      "choose an account", "to continue to google"),
     "this link is private — it opens a sign-in page instead of the work. "
     "The page has to be published to the web (Share -> Publish) and the "
     "published link submitted"),
    (("just a moment", "verifying you are human", "checking your browser",
      "verify you are human", "attention required", "ddos protection",
      "needs to review the security"),
     "the page was still showing a human-check (Cloudflare) when the "
     "browser gave up"),
    # Google's SIGNED-OUT APP SHELL. Not a login form and not an error — the
    # product's own marketing chrome, served to anyone without a session.
    # Day 07 (assignment 24, 23 Aug): SEVENTEEN different share.gemini.google
    # links each returned exactly 120 words of it, and the audit called every
    # one of them READABLE. Seventeen students would have been marked on
    # Google's page furniture. Identical word counts across distinct URLs is
    # the tell; these phrases are the proof.
    (("gemini - direct access to google ai",
      "direct access to google ai",
      "meet gemini, your personal ai assistant",
      "sign in to gemini", "try gemini advanced",
      "google apps\nsign in"),
     "the link opened Gemini's own page instead of your shared work — a "
     "Gemini share link only shows the conversation to people who are "
     "signed in to your account"),
    (("your browser is not compatible", "unsupported browser",
      "browser is not supported", "upgrade to the latest browser"),
     "the site refused to open this link in the reviewer's browser"),
    (("enable javascript", "requires javascript", "javascript is disabled",
      "javascript to run"),
     "the page never rendered — it asked for JavaScript it had been given"),
)

_WEAK_INTERSTITIALS = (
    (("sign in to continue", "log in to continue", "you need access",
      "request access", "permission to access", "sign up to view",
      "new user? sign up", "no access to this page", "ask for access",
      "continue with google", "verifying..."),
     "this link is private — it opens a sign-in page instead of the work. "
     "The page has to be published to the web (Share -> Publish) and the "
     "published link submitted"),
    (("page not found", "no longer exists", "content does not exist",
      "this page does not exist", "404 error"),
     "the page no longer exists at that address"),
)

_render_lock = threading.Lock()
_last_visit: dict = {}                # host -> monotonic seconds of last render


def wait_needed(last_at: float, now: float, gap_ms: int) -> float:
    """Seconds to pause before visiting this host again. Pure.

    Zero when the host is new or the gap has already passed; never negative,
    and never longer than the gap itself (a clock that jumped must not park
    a review for an hour).
    """
    if last_at <= 0 or gap_ms <= 0:
        return 0.0
    remaining = (gap_ms / 1000.0) - (now - last_at)
    return min(max(remaining, 0.0), gap_ms / 1000.0)


# Values that mean "leave the browser switched off". Everything else means
# on. Deliberately inverted: the old rule accepted only 1/true/yes, so a
# well-meant LINK_RENDER_ENABLED=3 ("three browsers, please") read as OFF
# and a whole link-day cohort would have been reviewed with every published
# page unread — a silent failure, the worst kind. This is a switch, not a
# count; the number of reviews running at once is reviewday's concurrency
# argument, and the browser is one-at-a-time by design regardless.
_OFF_VALUES = {"", "0", "false", "no", "off", "none", "null", "disabled"}


def enabled() -> bool:
    """The flag. Absent or explicitly off means this subsystem is inert."""
    return os.getenv("LINK_RENDER_ENABLED", "").strip().lower() not in _OFF_VALUES


@dataclass
class Rendered:
    title: str
    text: str                 # page + iframe innerText, joined
    screenshot_b64: str       # JPEG of the viewport, "" when capture failed
    final_url: str


def _flatten(text: str) -> str:
    """Lowercase, straighten typographic quotes, collapse whitespace. Pure.

    Notion writes "You're almost there!" with a curly apostrophe, and the
    line breaks inside a rendered page fall wherever the layout puts them —
    so a phrase like "sign in to see this page" arrives split across lines
    with a character no phrase list would match. Flatten first, then look.
    """
    flat = (text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    return " ".join(flat.split())


def interstitial_reason(title: str, text: str) -> str:
    """A gate, a challenge or an error page instead of the work? Pure.

    Returns the plain-English reason, or "" when the page looks like real
    content. Strong phrases (Notion's own sign-in copy, Cloudflare's
    challenge) are decisive at any length; weak ones are only trusted on a
    page too short to be a submission, so a portfolio that happens to say
    "sign in" keeps its grade.
    """
    blob = _flatten(f"{title or ''} {text or ''}")
    for phrases, reason in _STRONG_INTERSTITIALS:
        if any(p in blob for p in phrases):
            return reason
    if len((text or "").split()) > LINK_INTERSTITIAL_MAX_WORDS:
        return ""
    for phrases, reason in _WEAK_INTERSTITIALS:
        if any(p in blob for p in phrases):
            return reason
    return ""


def _blocked_host(host: str) -> bool:
    """In-page requests the browser must not make. Pure.

    Literal private/loopback IPs and obvious internal names. Public hostnames
    pass — the page needs its own CDNs to draw itself.
    """
    h = (host or "").strip("[]").lower()
    if not h or h == "localhost" or h.endswith((".local", ".internal")):
        return True
    try:
        return not ipaddress.ip_address(h).is_global
    except ValueError:
        return False                      # a hostname, not an IP literal


def _harvest_text(page) -> str:
    """Every frame's visible text, joined. Artifacts render inside iframes,
    so the top document alone is not the page."""
    texts = []
    for frame in page.frames:
        try:
            t = frame.evaluate(
                "() => document.body ? document.body.innerText : ''")
            if t and t.strip():
                texts.append(t.strip())
        except Exception:
            continue                      # cross-origin frame — its loss
    return "\n\n".join(texts)


def _render_raw(url: str) -> Tuple[Optional[Rendered], str]:
    """One browser, one page, one harvest. Runs under _render_lock."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, ("link rendering needs the playwright package "
                      "(pip install playwright && playwright install chromium)")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=[
                "--no-sandbox",
                # /dev/shm is tiny in a container; without this Chromium
                # dies part-way through drawing a heavy page.
                "--disable-dev-shm-usage",
                # Removes the automation banner flag sites fingerprint on.
                # We are reading a page the learner published publicly, in
                # a real browser engine — the claim is accurate.
                "--disable-blink-features=AutomationControlled",
            ])
            try:
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=CHROME_UA,
                    locale="en-IN",
                    timezone_id="Asia/Kolkata",
                    extra_http_headers={"Accept-Language": "en-IN,en;q=0.9"},
                )
                # navigator.webdriver is the other automation tell. Sites
                # that see it serve a challenge instead of the page.
                ctx.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined})")
                page = ctx.new_page()
                page.route("**/*", lambda route: (
                    route.abort()
                    if _blocked_host(urlparse(route.request.url).hostname or "")
                    else route.continue_()))
                page.set_default_timeout(LINK_RENDER_TIMEOUT_MS)
                page.goto(url, wait_until="domcontentloaded",
                          timeout=LINK_RENDER_TIMEOUT_MS)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass                  # busy pages never go idle — fine
                page.wait_for_timeout(LINK_RENDER_WAIT_MS)

                text = _harvest_text(page)
                title = page.title() or ""
                # A challenge page resolves itself; give it the chance
                # before calling the link unreadable. Costs nothing on a
                # page that came back clean the first time.
                reloaded = False
                for _ in range(LINK_RENDER_SETTLE_TRIES):
                    if not interstitial_reason(title, text):
                        break
                    page.wait_for_timeout(LINK_RENDER_SETTLE_MS)
                    if LINK_RENDER_RELOAD_ON_CHALLENGE and not reloaded:
                        reloaded = True
                        try:
                            page.reload(wait_until="domcontentloaded",
                                        timeout=LINK_RENDER_TIMEOUT_MS)
                            page.wait_for_timeout(LINK_RENDER_WAIT_MS)
                        except Exception:
                            pass          # the wait alone still gets a turn
                    text = _harvest_text(page)
                    title = page.title() or ""

                shot = ""
                try:
                    shot = base64.b64encode(
                        page.screenshot(type="jpeg", quality=70)).decode()
                except Exception:
                    pass                  # text alone can still carry a review

                return Rendered(title=title, text=text,
                                screenshot_b64=shot,
                                final_url=page.url), ""
            finally:
                browser.close()
    except Exception as e:
        return None, f"the page could not be rendered ({type(e).__name__})"


def render_link(url: str) -> Tuple[Optional[Rendered], str]:
    """Render one submitted link. Returns (Rendered, "") or (None, why).

    The SSRF check runs BEFORE any browser starts — a learner-supplied URL
    pointing at our own infrastructure is refused for the same reasons and by
    the same rule as in the downloader and the link fetcher.
    """
    if not enabled():
        return None, "link rendering is switched off"
    ok, why = check_public_url(url)
    if not ok:
        return None, why
    host = (urlparse(url).hostname or "").lower()
    # One Chromium at a time — but never an unbounded wait. A review that
    # cannot get the browser reports that plainly and is left un-graded by
    # the caller, which is recoverable. A worker parked for an hour is not.
    if not _render_lock.acquire(timeout=LINK_RENDER_LOCK_WAIT_S):
        return None, ("the browser was busy with another page for longer than "
                      f"{int(LINK_RENDER_LOCK_WAIT_S)}s — not rendered")
    try:
        pause = wait_needed(_last_visit.get(host, 0.0),
                            time.monotonic(), LINK_RENDER_HOST_GAP_MS)
        if pause:
            time.sleep(pause)             # do not trip the host's rate limit
        try:
            return _render_raw(url)
        finally:
            _last_visit[host] = time.monotonic()
    finally:
        _render_lock.release()


# ─── what the marker receives ───────────────────────────────────────────────

def compose_submission_text(r: Rendered, ocr_text: str = "") -> str:
    """The rendered page as reviewable text, provenance stated plainly."""
    parts = ["[PUBLISHED PAGE OPENED IN A BROWSER"
             + (f" — title: {r.title}" if r.title else "") + "]"]
    if r.text.strip():
        parts.append(r.text.strip())
    if ocr_text.strip():
        parts.append("[WHAT THE PAGE LOOKS LIKE ON SCREEN]\n" + ocr_text.strip())
    return "\n\n".join(parts)


def needs_vision(r: Rendered) -> bool:
    """Thin rendered text = a visual page (game, poster app): the screenshot
    must be READ, not just captured. Pure."""
    return bool(r.screenshot_b64) and len(r.text.split()) < LINK_RENDER_OCR_MIN_WORDS


# ---------------------------------------------------------------------------
# THE SHELL DETECTOR — the general form of the Day 07 failure.
#
# Seventeen distinct share.gemini.google links each returned exactly 120 words.
# Distinct URLs cannot serve identical pages unless what came back belongs to
# the SITE, not to the learner: a signed-out app shell, a marketing page, a
# generic error frame.
#
# The phrase lists above only catch shells we have already met. This catches
# the ones we have not, including the next tool the syllabus adopts — which is
# the point, because being one tool behind is how this system keeps failing.
#
# Deliberately narrow: identical text, not merely similar, and only across
# DIFFERENT urls. Two learners who genuinely submit the same page (a shared
# team artifact) is the one false positive, and refusing to grade a duplicate
# is the safe direction to be wrong in.
# ---------------------------------------------------------------------------

_SHELL_MIN_WORDS = 8          # below this, "the page rendered empty" already fires
_seen_pages: dict = {}        # normalised text -> first url that served it


def page_signature(text: str) -> str:
    """Stable fingerprint of a page's readable text. Pure."""
    return hashlib.sha1(_flatten(text or "").encode("utf-8")).hexdigest()


def duplicate_shell_reason(seen: dict, url: str, text: str) -> str:
    """Has this exact page already been served for a DIFFERENT url? Pure.

    `seen` maps signature -> first url. Returns the learner-facing reason, or
    "" when this page is unique (or too short for the check to mean anything).
    """
    words = len((text or "").split())
    if words < _SHELL_MIN_WORDS:
        return ""
    sig = page_signature(text)
    first = seen.get(sig)
    if first and first != url:
        return ("the link opened the tool's own page rather than your work — "
                "we know because another student's different link returned "
                "exactly the same page")
    return ""


def note_page(url: str, text: str) -> str:
    """Record this page and report whether it is a shared shell. Not pure.

    Process-wide and deliberately unbounded within a run: a cohort sweep is
    the unit of comparison, and the map is small (one entry per distinct page).
    """
    reason = duplicate_shell_reason(_seen_pages, url, text)
    if reason:
        print(f"[link] SHELL: {url} returned the same page as "
              f"{_seen_pages.get(page_signature(text))} — not graded")
        return reason
    if len((text or "").split()) >= _SHELL_MIN_WORDS:
        _seen_pages.setdefault(page_signature(text), url)
    return ""


def forget_pages() -> None:
    """Clear the shell map. For tests, and for a long-lived process that wants
    each sweep judged on its own."""
    _seen_pages.clear()


# ---------------------------------------------------------------------------
# THE LINK CACHE — open each page once per run, not once per student.
#
# Two learners pasting the same class link meant opening it twice. A dead link
# was re-opened on every sweep, and Day 05 was swept three times. The browser
# is the slowest thing in the system and the one most likely to be rate
# limited, so every avoided visit is both time and a smaller chance of being
# blocked on the visits that matter.
#
# Process-lifetime with a TTL: long enough that one cohort sweep opens a page
# once, short enough that a learner who publishes their page and resubmits an
# hour later is not told about yesterday's sign-in wall.
# ---------------------------------------------------------------------------

LINK_CACHE_TTL_S = float(os.getenv("LINK_CACHE_TTL_S", "3600"))
_link_cache: dict = {}                 # url -> (stored_at, text, why)


def cache_lookup(cache: dict, url: str, now: float,
                 ttl: float = LINK_CACHE_TTL_S) -> Optional[Tuple[str, str]]:
    """The remembered (text, why) for this url, or None. Pure.

    A zero or negative TTL disables the cache entirely — the honest way to
    turn it off, rather than a second flag to forget about.
    """
    if ttl <= 0:
        return None
    entry = cache.get(url)
    if not entry:
        return None
    stored_at, text, why = entry
    if now - stored_at > ttl:
        return None
    return text, why


def cache_store(cache: dict, url: str, now: float, text: str, why: str) -> None:
    """Remember what this url gave us. Not pure (mutates `cache`)."""
    cache[url] = (now, text, why)


def forget_links() -> None:
    """Drop every cached page. For tests, and for a sweep that wants a fresh
    look at links it was told about yesterday."""
    _link_cache.clear()


# Reasons that describe US, not the learner's page. Never cached: the next
# read must get a real answer, not a rerun of our bad minute.
_OUR_FAULT_MARKERS = ("busy with another page", "not rendered", "timed out",
                      "timeout", "the browser", "playwright", "browser is not "
                      "installed", "render failed", "could not start")


def our_failure(why: str) -> bool:
    """Is this reason about our reach rather than the page? Pure."""
    low = (why or "").lower()
    return any(m in low for m in _OUR_FAULT_MARKERS)


def _remember(url: str, text: str, why: str) -> Tuple[str, str]:
    """Cache one verdict and return it, so no exit path can forget to."""
    cache_store(_link_cache, url, time.monotonic(), text, why)
    return text, why


def read_rendered_link(url: str) -> Tuple[str, str]:
    """(reviewable_text, why_empty) — the one call intake makes.

    Vision spend is decided here: a text-rich page (a Notion doc, a Gamma
    outline) is carried by its own words for free; a visual page's screenshot
    goes through the same OCR the uploaded-image path trusts.
    """
    hit = cache_lookup(_link_cache, url, time.monotonic())
    if hit is not None:
        print(f"[link] cache hit: {url}")
        return hit

    rendered, why = render_link(url)
    if rendered is None:
        # OUR failures are not facts about the page. Caching "the browser was
        # busy" would hand our timeout to every later student who pasted the
        # same link, and an hour later we would still be reporting it. Only
        # verdicts ABOUT THE PAGE are worth remembering.
        if our_failure(why):
            print(f"[link] not cached (our side): {why}")
            return "", why
        return _remember(url, "", why)
    if not rendered.text.strip() and not rendered.screenshot_b64:
        return _remember(url, "", "the page rendered empty")

    # Before any vision money is spent: is this the work, or a gate? A
    # Notion compatibility error is not a submission, and describing it in
    # detail would only make the fabrication more convincing.
    blocked = interstitial_reason(rendered.title, rendered.text)
    if blocked:
        return _remember(url, "", blocked)

    # A page identical to one already served for a different link is the
    # site's own shell, whatever it says. Checked before vision spend.
    shell = note_page(url, rendered.text)
    if shell:
        return _remember(url, "", shell)

    ocr_text = ""
    if needs_vision(rendered):
        from app.utils.file_extractor import _ocr_with_claude
        ocr_text, _ocr_why = _ocr_with_claude(
            [("image/jpeg", rendered.screenshot_b64)],
            kind="a published web page or app the learner built, rendered in a browser")

    text = compose_submission_text(rendered, ocr_text)
    if len(text.split()) < 8:
        return _remember(url, "", "the page rendered but showed almost nothing")
    return _remember(url, text, "")
