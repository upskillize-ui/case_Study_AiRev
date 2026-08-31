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
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple
from urllib.parse import urlparse

from app.utils.url_guard import check_public_url

logger = logging.getLogger(__name__)

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
# How many times the page may be loaded again while a challenge is showing.
#
# It was ONE, and not by intent: `reloaded` was set on the first settle attempt
# and never reset, so the remaining four attempts could only wait. A managed
# challenge clears on a fresh load far more often than on patience, and the
# fresh load is the cheap half of the loop.
LINK_RENDER_MAX_RELOADS = int(os.getenv("LINK_RENDER_MAX_RELOADS", "3"))
# A host that has just challenged us will challenge us again if we come
# straight back. Each recent challenge doubles that host's gap, up to a
# ceiling — so one Gamma link tripping the check slows Gamma down for the next
# few students instead of dragging all of them through the same failure.
LINK_RENDER_CHALLENGE_GAP_MAX_MS = int(
    os.getenv("LINK_RENDER_CHALLENGE_GAP_MAX_MS", "60000"))
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

# ---------------------------------------------------------------------------
# THE WALKTHROUGH (Phase 2).
#
# A Lovable site, a Claude artifact game, any built app: the deliverable is a
# thing that DOES something, and until now the agent judged it from one
# screenshot of whatever sat above the fold. That is a photograph of a front
# door standing in for a house.
#
# So: scroll the page and photograph each screen, then press the obvious
# controls and photograph what happens, and keep every JavaScript error the
# page throws. The marker then judges "four sections, working navigation, no
# errors" instead of "the words HOME and ABOUT appear".
#
# Strictly bounded. The browser handles one page at a time and a cohort sweep
# is already the slowest part of the day, so the whole walkthrough gets a
# wall-clock budget and a hard cap on clicks. It is OFF unless the caller asks
# for it, because most days do not submit apps.
# ---------------------------------------------------------------------------
WALKTHROUGH_MAX_SHOTS = int(os.getenv("WALKTHROUGH_MAX_SHOTS", "4"))
WALKTHROUGH_MAX_CLICKS = int(os.getenv("WALKTHROUGH_MAX_CLICKS", "2"))
WALKTHROUGH_BUDGET_S = float(os.getenv("WALKTHROUGH_BUDGET_S", "20"))
WALKTHROUGH_SETTLE_MS = int(os.getenv("WALKTHROUGH_SETTLE_MS", "900"))

# Controls worth pressing, in priority order. Deliberately conservative: a
# marker must never destroy a learner's work by clicking Delete, and must
# never post anything anywhere. Anything matching _NEVER_CLICK is skipped
# even if it also matches here.
_CLICK_CANDIDATES = (
    "button:visible", "[role=button]:visible", "nav a:visible",
    "a.btn:visible", "[data-testid*=start]:visible",
)
_NEVER_CLICK = re.compile(
    r"delete|remove|clear|reset|sign\s*out|log\s*out|logout|buy|pay|purchase|"
    r"checkout|subscribe|upgrade|submit|send|post|publish|share|download|"
    r"export|print|confirm|save|upload|invite|report|flag",
    re.I)


def worth_clicking(label: str) -> bool:
    """Is this control safe for a marker to press? Pure.

    The rule is do-no-harm, not thoroughness. We are a visitor on a stranger's
    published page: pressing Start or About tells us the app responds; pressing
    Delete, Buy or Submit changes their world. When in doubt, do not touch it —
    an unpressed button costs a little evidence, a pressed one can cost the
    learner their work.
    """
    text = (label or "").strip()
    if not text or len(text) > 40:
        return False
    return not _NEVER_CLICK.search(text)

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
_challenges: dict = {}                # host -> consecutive bot-checks seen


def host_gap_ms(base_ms: int, challenges: int, ceiling_ms: int) -> int:
    """The gap to leave before revisiting a host. Pure.

    Doubles per consecutive challenge, capped. Zero challenges leaves the base
    gap exactly as it was, so a normal link day pays nothing for this.
    """
    if base_ms <= 0:
        return 0
    gap = base_ms * (2 ** max(0, int(challenges)))
    return int(min(gap, max(base_ms, ceiling_ms)))


def note_challenge(host: str, challenged: bool) -> int:
    """Record whether this host just bot-checked us. Returns the new count."""
    h = (host or "").lower()
    if not h:
        return 0
    _challenges[h] = (_challenges.get(h, 0) + 1) if challenged else 0
    return _challenges[h]


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
    # PHASE 2 (23 Aug 2026): what the page did when someone USED it.
    # One screenshot of the fold is a photograph of a front door. A built
    # site or an app is judged on whether it works, and that cannot be seen
    # standing still.
    shots: list = field(default_factory=list)   # [(caption, jpeg_b64)]
    console_errors: list = field(default_factory=list)
    steps: list = field(default_factory=list)   # plain-English walkthrough log


def _flatten(text: str) -> str:
    """Lowercase, straighten typographic quotes, collapse whitespace. Pure.

    Notion writes "You're almost there!" with a curly apostrophe, and the
    line breaks inside a rendered page fall wherever the layout puts them —
    so a phrase like "sign in to see this page" arrives split across lines
    with a character no phrase list would match. Flatten first, then look.
    """
    flat = (text or "").lower().replace("\u2019", "'").replace("\u2018", "'")
    return " ".join(flat.split())


def interstitial_match(title: str, text: str) -> Tuple[str, str]:
    """(reason, the phrase that matched). Pure.

    The phrase is for the LOG, never for the student. "Cloudflare" and "a
    sign-in wall" are different failures needing different advice, and until
    the matched phrase was recorded nobody could tell from the outside which
    one a link had hit — the same blindness that made `OCR failed:
    BadRequestError` undiagnosable for days.
    """
    blob = _flatten(f"{title or ''} {text or ''}")
    for phrases, reason in _STRONG_INTERSTITIALS:
        for p in phrases:
            if p in blob:
                return reason, p
    if len((text or "").split()) > LINK_INTERSTITIAL_MAX_WORDS:
        return "", ""
    for phrases, reason in _WEAK_INTERSTITIALS:
        for p in phrases:
            if p in blob:
                return reason, p
    return "", ""


def interstitial_reason(title: str, text: str) -> str:
    """A gate, a challenge or an error page instead of the work? Pure.

    Returns the plain-English reason, or "" when the page looks like real
    content. Strong phrases (Notion's own sign-in copy, Cloudflare's
    challenge) are decisive at any length; weak ones are only trusted on a
    page too short to be a submission, so a portfolio that happens to say
    "sign in" keeps its grade.
    """
    return interstitial_match(title, text)[0]


def is_challenge(reason: str) -> bool:
    """Is this reason a bot-check rather than a private link? Pure.

    A challenge is worth waiting out and worth backing off for. A sign-in wall
    is not: no amount of patience will publish a private page, and retrying
    only spends the browser other students are queueing for.
    """
    return "human-check" in (reason or "")


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


def _render_raw(url: str, walk: bool = False) -> Tuple[Optional[Rendered], str]:
    """One browser, one page, one harvest. Runs under _render_lock.

    walk=True also USES the page — scrolls it, presses its safe controls and
    photographs each step. See the walkthrough notes at the top of the file.
    """
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
                # A site that throws on load is broken however good it looks.
                # Collected always: it costs nothing and it is the single most
                # objective quality signal a built page emits.
                console: list = []
                page.on("console", lambda m: (
                    console.append(m.text[:200])
                    if m.type == "error" and len(console) < 20 else None))
                page.on("pageerror", lambda e: (
                    console.append(str(e)[:200]) if len(console) < 20 else None))
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
                reloads = 0
                reason, phrase = interstitial_match(title, text)
                for attempt in range(LINK_RENDER_SETTLE_TRIES):
                    if not reason:
                        break
                    # A sign-in wall is not going to open by itself. Waiting on
                    # one only spends the browser every other review is queued
                    # behind, so stop and report it now.
                    if not is_challenge(reason):
                        break
                    page.wait_for_timeout(LINK_RENDER_SETTLE_MS)
                    if LINK_RENDER_RELOAD_ON_CHALLENGE and reloads < LINK_RENDER_MAX_RELOADS:
                        reloads += 1
                        try:
                            page.reload(wait_until="domcontentloaded",
                                        timeout=LINK_RENDER_TIMEOUT_MS)
                            page.wait_for_timeout(LINK_RENDER_WAIT_MS)
                        except Exception:
                            pass          # the wait alone still gets a turn
                    text = _harvest_text(page)
                    title = page.title() or ""
                    reason, phrase = interstitial_match(title, text)
                    if not reason:
                        logger.info("link cleared its check after %d wait(s), "
                                    "%d reload(s): %s", attempt + 1, reloads, url)
                        break

                # Say WHICH gate it was and where we ended up. Without this the
                # student is told "a human-check" and the operator is told
                # nothing at all.
                host = (urlparse(url).hostname or "").lower()
                note_challenge(host, bool(reason) and is_challenge(reason))
                if reason:
                    logger.warning(
                        "link unreadable [%s] matched=%r final_url=%s reloads=%d",
                        host, phrase, page.url, reloads)

                shot = ""
                try:
                    shot = base64.b64encode(
                        page.screenshot(type="jpeg", quality=70)).decode()
                except Exception:
                    pass                  # text alone can still carry a review

                shots, steps = [], []
                if walk:
                    shots, steps = _walk_the_page(page, shot)

                return Rendered(title=title, text=text,
                                screenshot_b64=shot,
                                final_url=page.url,
                                shots=shots, steps=steps,
                                console_errors=console[:10]), ""
            finally:
                browser.close()
    except Exception as e:
        return None, f"the page could not be rendered ({type(e).__name__})"


def _walk_the_page(page, first_shot: str) -> Tuple[list, list]:
    """Scroll, press the safe controls, photograph each step.

    Returns ([(caption, jpeg_b64)], [plain-English step log]). Never raises:
    a walkthrough that goes wrong must degrade to the single screenshot the
    caller already holds, not take down a review that had succeeded.

    Every step is bounded by a wall-clock budget, because the browser handles
    one page at a time and a cohort sweep is already the slowest part of a day.
    """
    shots, steps = [], []
    if first_shot:
        shots.append(("the page as it first loads", first_shot))
    deadline = time.monotonic() + WALKTHROUGH_BUDGET_S

    def snap(caption: str) -> None:
        if len(shots) >= WALKTHROUGH_MAX_SHOTS or time.monotonic() > deadline:
            return
        try:
            shots.append((caption, base64.b64encode(
                page.screenshot(type="jpeg", quality=70)).decode()))
        except Exception:
            pass

    # 1. See the whole page, not just the fold.
    try:
        height = page.evaluate("document.body.scrollHeight") or 0
        viewport = page.evaluate("window.innerHeight") or 900
        screens = max(0, min(WALKTHROUGH_MAX_SHOTS - 1,
                             int(height // max(viewport, 1))))
        for n in range(screens):
            if time.monotonic() > deadline:
                break
            page.evaluate(f"window.scrollTo(0, {viewport * (n + 1)})")
            page.wait_for_timeout(WALKTHROUGH_SETTLE_MS)
            snap(f"after scrolling down {n + 1} screen(s)")
        if screens:
            steps.append(f"the page is about {screens + 1} screens tall")
        else:
            steps.append("the page fits on one screen")
        page.evaluate("window.scrollTo(0, 0)")
    except Exception as e:
        steps.append(f"could not scroll the page ({type(e).__name__})")

    # 2. Press what a visitor would press.
    pressed = 0
    for selector in _CLICK_CANDIDATES:
        if pressed >= WALKTHROUGH_MAX_CLICKS or time.monotonic() > deadline:
            break
        try:
            elements = page.locator(selector)
            for i in range(min(elements.count(), 6)):
                if pressed >= WALKTHROUGH_MAX_CLICKS or time.monotonic() > deadline:
                    break
                el = elements.nth(i)
                label = (el.inner_text(timeout=1500) or "").strip()
                if not worth_clicking(label):
                    continue
                before = page.url
                el.click(timeout=2500, no_wait_after=True)
                page.wait_for_timeout(WALKTHROUGH_SETTLE_MS)
                pressed += 1
                moved = page.url != before
                steps.append(f'pressed "{label}"'
                             + (" and the page changed" if moved
                                else " and the page responded in place"))
                snap(f'after pressing "{label}"')
        except Exception:
            continue          # a control that will not be pressed is not news

    if not pressed:
        steps.append("no control on this page was safe to press "
                     "(nothing found, or everything was destructive)")
    return shots, steps


def render_link(url: str, walk: bool = False) -> Tuple[Optional[Rendered], str]:
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
        # A host that bot-checked us last time gets more room this time.
        gap = host_gap_ms(LINK_RENDER_HOST_GAP_MS, _challenges.get(host, 0),
                          LINK_RENDER_CHALLENGE_GAP_MAX_MS)
        pause = wait_needed(_last_visit.get(host, 0.0), time.monotonic(), gap)
        if pause:
            time.sleep(pause)             # do not trip the host's rate limit
        try:
            return _render_raw(url, walk=walk)
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


BUILT_THING = re.compile(
    # Days whose deliverable is a thing that DOES something. Walking a page
    # costs seconds on a browser that handles one at a time, so it runs only
    # where the answer to "does it work?" is part of the mark.
    r"\blovable\b|\bwebsite\b|\bweb\s?app\b|\bapp\b|\bgame\b|"
    r"\bdashboard\b|\bartifacts?\b|\bprototype\b|\blanding page\b|"
    r"\bdeploy(?:ed)?\b|\bbuild (?:a|an|your)\b|\bcustom gpts?\b",
    re.I)


def walkthrough_enabled() -> bool:
    """Is the walkthrough switched on for this deployment?

    OFF by default, deliberately. It is new code that presses buttons on
    learners' published pages, and the first night it runs must be a night
    somebody chose, not the night it happened to ship alongside a fix the
    cohort was waiting for. Set WALKTHROUGH_ENABLED=1 to turn it on.
    """
    return os.getenv("WALKTHROUGH_ENABLED", "").strip().lower() \
        in ("1", "true", "yes", "on")


def task_wants_a_walkthrough(task_text: str) -> bool:
    """Should the marker USE this page, not just look at it? Pure-ish.

    True for built things — a site, an app, a game, a dashboard. False for a
    Notion page or a shared doc, where scrolling and clicking tells you
    nothing the text has not already said, and false everywhere while the
    feature is switched off.
    """
    return walkthrough_enabled() and bool(BUILT_THING.search(task_text or ""))


def walkthrough_notes(r) -> str:
    """The walkthrough as a few plain lines for the marker. Pure.

    Deliberately factual. It reports what happened when the page was used —
    never whether that was good. The judgement stays with the marker, which
    is the same rule the intake manifest follows.
    """
    if r is None:
        return ""
    lines = []
    if getattr(r, "steps", None):
        lines.append("WHAT HAPPENED WHEN THE PAGE WAS USED:")
        lines += [f"- {step}" for step in r.steps]
    errors = getattr(r, "console_errors", None) or []
    if errors:
        lines.append(f"THE PAGE REPORTED {len(errors)} JAVASCRIPT ERROR(S) "
                     f"WHILE LOADING:")
        lines += [f"- {e}" for e in errors[:3]]
    elif getattr(r, "steps", None):
        lines.append("The page reported no JavaScript errors.")
    return "\n".join(lines)


def read_rendered_page(url: str, walk: bool = False) -> Tuple[str, str, str, str]:
    """(text, why_empty, screenshot_b64, walkthrough_notes).

    A built page is DESIGNED. Its text tells you what it says; only the
    picture tells you whether it looks finished, whether the layout holds,
    or whether the template's placeholder blocks are still sitting there.
    Lovable day is unmarkable without it.

    The screenshot is returned even when the text carried the page on its
    own, because those are different questions and the marker should have
    both. It is "" when capture failed or the page was refused.
    """
    text, why = read_rendered_link(url, walk=walk)
    if why:
        return text, why, "", ""
    return (text, why, _last_screenshot.get(url, ""),
            _last_notes.get(url, ""))


# The most recent screenshot per url, filled by read_rendered_link so the
# picture does not have to be threaded back through every return path. Bounded
# because a cohort sweep opens hundreds of pages and a JPEG is not small.
_last_screenshot: dict = {}
_SCREENSHOT_KEEP = int(os.getenv("LINK_SCREENSHOT_KEEP", "8"))
_screenshot_lock = threading.Lock()


def _keep_screenshot(url: str, b64: str) -> None:
    """Remember one page picture, evicting the oldest. Thread-safe.

    Reviews run concurrently. Without the lock, `len() >= KEEP` followed by
    `pop(next(iter(...)))` is a race: another worker can empty the dict in
    between and next() then raises StopIteration, which would break a review
    that had already succeeded. A screenshot is a nice-to-have; taking a
    working review down for one is not a trade worth making.
    """
    if not b64:
        return
    with _screenshot_lock:
        while len(_last_screenshot) >= _SCREENSHOT_KEEP:
            try:
                _last_screenshot.pop(next(iter(_last_screenshot)))
            except StopIteration:            # emptied under us; nothing to evict
                break
        _last_screenshot[url] = b64


_last_notes: dict = {}


def _keep_notes(url: str, notes: str) -> None:
    """The walkthrough log, kept beside its screenshot and bounded the same way."""
    if not notes:
        return
    with _screenshot_lock:
        while len(_last_notes) >= _SCREENSHOT_KEEP:
            try:
                _last_notes.pop(next(iter(_last_notes)))
            except StopIteration:
                break
        _last_notes[url] = notes


def forget_screenshots() -> None:
    with _screenshot_lock:
        _last_screenshot.clear()
        _last_notes.clear()


def read_rendered_link(url: str, walk: bool = False) -> Tuple[str, str]:
    """(reviewable_text, why_empty) — the one call intake makes.

    Vision spend is decided here: a text-rich page (a Notion doc, a Gamma
    outline) is carried by its own words for free; a visual page's screenshot
    goes through the same OCR the uploaded-image path trusts.
    """
    hit = cache_lookup(_link_cache, url, time.monotonic())
    if hit is not None:
        print(f"[link] cache hit: {url}")
        return hit

    rendered, why = render_link(url, walk=walk)
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

    # Past every refusal: this really is the learner's page, so keep the
    # picture for the marker to look at.
    _keep_screenshot(url, rendered.screenshot_b64)
    _keep_notes(url, walkthrough_notes(rendered))

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
