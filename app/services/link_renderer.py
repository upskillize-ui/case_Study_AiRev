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
import os
import threading
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

_render_lock = threading.Lock()


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


def _render_raw(url: str) -> Tuple[Optional[Rendered], str]:
    """One browser, one page, one harvest. Runs under _render_lock."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, ("link rendering needs the playwright package "
                      "(pip install playwright && playwright install chromium)")

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                ctx = browser.new_context(
                    viewport={"width": 1280, "height": 900},
                    user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                "AppleWebKit/537.36 (KHTML, like Gecko) "
                                "Chrome/126.0 Safari/537.36"),
                )
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

                texts = []
                for frame in page.frames:
                    try:
                        t = frame.evaluate(
                            "() => document.body ? document.body.innerText : ''")
                        if t and t.strip():
                            texts.append(t.strip())
                    except Exception:
                        continue          # cross-origin frame — its loss

                shot = ""
                try:
                    shot = base64.b64encode(
                        page.screenshot(type="jpeg", quality=70)).decode()
                except Exception:
                    pass                  # text alone can still carry a review

                return Rendered(title=page.title() or "",
                                text="\n\n".join(texts),
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
    with _render_lock:                    # one Chromium at a time, ever
        return _render_raw(url)


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


def read_rendered_link(url: str) -> Tuple[str, str]:
    """(reviewable_text, why_empty) — the one call intake makes.

    Vision spend is decided here: a text-rich page (a Notion doc, a Gamma
    outline) is carried by its own words for free; a visual page's screenshot
    goes through the same OCR the uploaded-image path trusts.
    """
    rendered, why = render_link(url)
    if rendered is None:
        return "", why
    if not rendered.text.strip() and not rendered.screenshot_b64:
        return "", "the page rendered empty"

    ocr_text = ""
    if needs_vision(rendered):
        from app.utils.file_extractor import _ocr_with_claude
        ocr_text, _ocr_why = _ocr_with_claude(
            [("image/jpeg", rendered.screenshot_b64)],
            kind="a published web page or app the learner built, rendered in a browser")

    text = compose_submission_text(rendered, ocr_text)
    if len(text.split()) < 8:
        return "", "the page rendered but showed almost nothing"
    return text, ""
