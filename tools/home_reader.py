"""Home reader — open the links our Space cannot, from a home connection.

WHY. The Space's browser leaves from a datacenter IP; claude.ai, gamma.app,
Notion and some Lovable apps answer it with a Cloudflare human-check. The
same pages open at once from a home connection. This script runs on the
owner's PC, opens each blocked link in a real Chrome window, and posts what
a visitor sees (title, text, one screenshot) to the Space. Every judgement
about the page — sign-in wall, site shell — is made on the Space, by the
same functions its own renderer uses. This script is a courier.

ONE-TIME SETUP (PowerShell, inside Agent@4):
    pip install playwright requests
    python -m playwright install chromium

RUN:
    $env:AIREV_BASE      = "https://upskill25-airev-agent.hf.space"
    $env:LMS_API_KEY     = Read-Host "LMS_API_KEY"
    $env:ADMIN_JOB_KEY   = Read-Host "ADMIN_JOB_KEY"
    python tools/home_reader.py --course 55            # everything blocked
    python tools/home_reader.py --course 55 --host gamma.app --limit 20
    python tools/home_reader.py --dry-run              # list only, open nothing

A Chrome window opens and pages load one after another. Roughly 15 s a
page. It uses YOUR installed Google Chrome with its own separate profile
(kept under %LOCALAPPDATA%\airev-home-reader) — not signed in to anything,
so a private page is refused exactly as it would be for a visitor. Do not
sign in to it. If Cloudflare shows a "Verify you are human" CHECKBOX, click
it once; the profile remembers the clearance for the rest of that site.
Do not close the window while it runs — if it does close, the script
reopens it and carries on.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time
from urllib.parse import urlparse

import requests

BASE = os.getenv("AIREV_BASE", "https://upskill25-airev-agent.hf.space").rstrip("/")
API_KEY = os.getenv("LMS_API_KEY", "")
ADMIN_KEY = os.getenv("ADMIN_JOB_KEY", "")

CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser",
                    "verify you are human", "please wait")
SETTLE_S = 90           # how long to wait for a human-check to pass (or be clicked)
LOAD_TIMEOUT_MS = 45_000
VIEWPORT = {"width": 1366, "height": 900}
MAX_SHOT_PX = 5800      # tallest screenshot we send (vision models cap ~8000)
MAX_TEXT = 200_000
THIN_WORDS = 80         # under this the page is a picture: send the picture itself


def _headers() -> dict:
    return {"x-api-key": API_KEY, "x-admin-key": ADMIN_KEY}


def fetch_blocked(course: int | None, limit: int) -> list:
    params = {"limit": limit}
    if course:
        params["course_id"] = course
    r = requests.get(f"{BASE}/api/review/links/blocked", params=params,
                     headers=_headers(), timeout=60)
    r.raise_for_status()
    return r.json().get("links", [])


def post_seed(submission_id: int, url: str, title: str, text: str, shot_b64: str) -> dict:
    r = requests.post(f"{BASE}/api/review/links/seed", headers=_headers(), timeout=120,
                      json={"submissionId": submission_id, "url": url, "title": title,
                            "text": text[:MAX_TEXT], "screenshotB64": shot_b64,
                            "source": "home-reader"})
    if r.status_code == 400:
        return {"accepted": False, "why": r.json().get("detail", "rejected")}
    r.raise_for_status()
    return r.json()


def is_challenge(title: str) -> bool:
    low = (title or "").lower()
    return any(t in low for t in CHALLENGE_TITLES)


def read_page(page, url: str) -> tuple[str, str, str]:
    """(title, text, screenshot_b64) as a visitor would see them."""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
    except Exception as e:
        # A share page that redirects mid-load aborts the first navigation;
        # the second one lands.
        if "ERR_ABORTED" not in str(e):
            raise
        page.wait_for_timeout(2000)
        page.goto(url, wait_until="commit", timeout=LOAD_TIMEOUT_MS)
    # THE TAB THAT NEVER LEFT (04 Sep 2026, run 2). Every claude.ai/share
    # link and every Notion portfolio link came back with the text of the
    # FIRST such page read, and the Space refused them all as one shell:
    # the navigation had not happened and the old page was read again. A
    # fresh tab per link (see main) starts blank, so a navigation that did
    # not land is caught here instead of being posted as another student's
    # work.
    if page.url in ("", "about:blank"):
        raise RuntimeError("the page did not navigate")
    deadline = time.time() + SETTLE_S
    hinted = False
    while is_challenge(page.title()) and time.time() < deadline:
        if not hinted:
            print("      human-check showing - if the Chrome window shows a checkbox, click it once")
            hinted = True
        page.wait_for_timeout(1500)
    try:
        page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:
        pass                                  # busy pages never go idle; fine
    # Scroll to the bottom and back so lazy sections (Gamma cards, Notion
    # blocks) actually render before the text is read.
    for _ in range(6):
        page.mouse.wheel(0, 1400)
        page.wait_for_timeout(400)
    page.mouse.wheel(0, -20000)
    page.wait_for_timeout(1200)
    title = page.title()
    text = _settled_text(page)
    for frame in page.frames[1:]:
        try:
            text += "\n" + (frame.evaluate("() => document.body ? document.body.innerText : ''") or "")
        except Exception:
            continue
    shot = largest_image(page) if len(text.split()) < THIN_WORDS else b""
    if not shot:
        # One tall picture, capped: the marker's vision model refuses images
        # over ~8000 px a side, and a long Gamma deck scrolls further than that.
        height = int(page.evaluate("() => document.documentElement.scrollHeight") or VIEWPORT["height"])
        clip = {"x": 0, "y": 0, "width": VIEWPORT["width"], "height": min(height, MAX_SHOT_PX)}
        shot = page.screenshot(type="jpeg", quality=70, full_page=True, clip=clip)
    if len(shot) > 1_900_000:                 # keep under the Space's 2 MB cap
        shot = page.screenshot(type="jpeg", quality=45, full_page=False)
    return title, text.strip(), base64.b64encode(shot).decode("ascii")


CONTENT_WAIT_S = 25     # how long to wait for a page that draws its words late


def _settled_text(page) -> str:
    """The page's text once it has stopped growing.

    A claude.ai share page or a Notion site shows its frame first and draws
    the conversation seconds later. Run 3 captured the frame — identical for
    every link — and the Space rightly refused them all as one shell. Read
    again every two seconds until the text holds still with real words in it,
    or CONTENT_WAIT_S is up (a genuinely thin page costs that wait, no more).
    """
    last, deadline = None, time.time() + CONTENT_WAIT_S
    while True:
        text = _inner_text(page)
        if text == last and len(text.split()) >= THIN_WORDS:
            return text
        if time.time() >= deadline:
            return text
        last = text
        page.wait_for_timeout(2000)


def _inner_text(page) -> str:
    """Body text, retried once — a challenge redirect landing mid-read
    destroys the page's script context ("Execution context was destroyed")."""
    for attempt in (1, 2):
        try:
            return page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        except Exception:
            if attempt == 2:
                return ""
            page.wait_for_timeout(3000)
    return ""


def largest_image(page) -> bytes:
    """The biggest picture on the page, photographed whole — or b"".

    A ChatGPT image share, a Gemini poster, a Canva export: the page's text
    is the app's own sidebar and the WORK is one <img>. A viewport screenshot
    cuts it off at the fold; an element screenshot scrolls it into view and
    captures all of it, whatever its height.
    """
    try:
        idx = page.evaluate("""() => {
            const imgs = Array.from(document.images);
            let best = -1, area = 0;
            imgs.forEach((im, i) => {
                const a = (im.naturalWidth || 0) * (im.naturalHeight || 0);
                if (a > area && im.naturalWidth >= 400 && im.naturalHeight >= 400) { area = a; best = i; }
            });
            return best;
        }""")
        if idx is None or idx < 0:
            return b""
        el = page.locator("img").nth(idx)
        el.scroll_into_view_if_needed(timeout=5000)
        page.wait_for_timeout(600)
        return el.screenshot(type="jpeg", quality=80, timeout=20000)
    except Exception:
        return b""


PROFILE_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                           "airev-home-reader")


def open_browser(pw):
    """A persistent context in the user's installed Chrome, automation flags
    off. Persistent so a Cloudflare clearance (cf_clearance cookie) earned on
    one claude.ai page carries to the next hundred. Falls back to the bundled
    Chromium when Chrome is not installed."""
    kwargs = dict(user_data_dir=PROFILE_DIR, headless=False, viewport=VIEWPORT,
                  locale="en-IN", args=["--disable-blink-features=AutomationControlled"],
                  ignore_default_args=["--enable-automation"])
    try:
        return pw.chromium.launch_persistent_context(channel="chrome", **kwargs)
    except Exception as e:
        print(f"installed Chrome not available ({str(e).splitlines()[0][:80]}) - "
              f"using the bundled browser; Cloudflare may refuse it")
        return pw.chromium.launch_persistent_context(**kwargs)


def _read_in_fresh_tab(context, url: str) -> tuple[str, str, str]:
    """read_page in a tab opened for this link alone and closed after it."""
    page = context.new_page()
    try:
        return read_page(page, url)
    finally:
        try:
            page.close()
        except Exception:
            pass


def _browser_gone(err: Exception) -> bool:
    low = str(err).lower()
    return "has been closed" in low or "browser has been closed" in low or \
           "target closed" in low or "connection closed" in low


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--course", type=int, default=None)
    ap.add_argument("--host", default="", help="only links on this host (substring)")
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not args.dry_run and not (API_KEY and ADMIN_KEY):
        print("Set LMS_API_KEY and ADMIN_JOB_KEY in the environment first.")
        return 2

    links = fetch_blocked(args.course, args.limit)
    if args.host:
        links = [l for l in links if args.host.lower() in (l.get("host") or "").lower()]
    hosts: dict = {}
    for l in links:
        hosts[l["host"]] = hosts.get(l["host"], 0) + 1
    print(f"{len(links)} blocked link(s) to read: " +
          ", ".join(f"{h} ({n})" for h, n in sorted(hosts.items(), key=lambda x: -x[1])))
    if args.dry_run or not links:
        for l in links:
            print(f"  {l['submissionId']:>6}  {l['assignment'][:34]:<34}  {l['url']}")
        return 0

    from playwright.sync_api import sync_playwright

    tally = {"reoffered": 0, "graded": 0, "refused": 0, "failed": 0}
    t0 = time.time()
    with sync_playwright() as pw:
        context = open_browser(pw)
        for i, l in enumerate(links, 1):
            sid, url = l["submissionId"], l["url"]
            tag = f"[{i}/{len(links)}] {sid} {urlparse(url).hostname}"
            # One fresh tab per link. A private Gamma deck keeps redirecting
            # to its sign-in page after we have moved on, and that redirect
            # interrupted the NEXT link's navigation ("interrupted by another
            # navigation to https://gamma.app/...") — nine links lost on
            # run 2. A closed tab cannot interrupt anything.
            try:
                title, text, shot = _read_in_fresh_tab(context, url)
            except Exception as e:
                if _browser_gone(e):
                    # The window was closed or Chrome crashed. Reopen it
                    # and try this page once more before moving on.
                    print(f"{tag}: the browser closed - reopening it")
                    try:
                        context.close()
                    except Exception:
                        pass
                    context = open_browser(pw)
                    try:
                        title, text, shot = _read_in_fresh_tab(context, url)
                    except Exception as e2:
                        tally["failed"] += 1
                        print(f"{tag}: could not open - {str(e2).splitlines()[0][:120]}")
                        continue
                else:
                    tally["failed"] += 1
                    print(f"{tag}: could not open - {str(e).splitlines()[0][:120]}")
                    continue
            if is_challenge(title):
                tally["refused"] += 1
                print(f"{tag}: still a human-check after {SETTLE_S}s - skipped")
                continue
            try:
                res = post_seed(sid, url, title, text, shot)
            except Exception as e:
                tally["failed"] += 1
                print(f"{tag}: Space did not accept the seed - {e}")
                continue
            if res.get("accepted"):
                tally[res.get("row", "reoffered")] = tally.get(res.get("row", "reoffered"), 0) + 1
                print(f"{tag}: seeded, {res.get('words', 0)} words, row {res.get('row')}")
            else:
                tally["refused"] += 1
                print(f"{tag}: refused by the Space - {res.get('why')}")
        try:
            context.close()
        except Exception:
            pass

    mins = (time.time() - t0) / 60
    print(f"\nDone in {mins:.0f} min: {json.dumps(tally)}")
    print("Now fire the sweep (POST /api/review/jobs/sweep?limit=1200) - "
          "re-offered rows are graded in that run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
