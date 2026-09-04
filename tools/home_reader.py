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

A Chrome window opens and pages load one after another; leave it alone.
Roughly 15 s a page. The window is a fresh profile — it is not signed in
to anything, so a private page is refused exactly as it would be for a
visitor. Do not sign in to it.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
from urllib.parse import urlparse

import requests

BASE = os.getenv("AIREV_BASE", "https://upskill25-airev-agent.hf.space").rstrip("/")
API_KEY = os.getenv("LMS_API_KEY", "")
ADMIN_KEY = os.getenv("ADMIN_JOB_KEY", "")

CHALLENGE_TITLES = ("just a moment", "attention required", "checking your browser",
                    "verify you are human", "please wait")
SETTLE_S = 45           # how long to wait for a human-check to pass on its own
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
    page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT_MS)
    deadline = time.time() + SETTLE_S
    while is_challenge(page.title()) and time.time() < deadline:
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
    text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
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
        browser = pw.chromium.launch(headless=False)
        context = browser.new_context(viewport=VIEWPORT, locale="en-IN")
        page = context.new_page()
        for i, l in enumerate(links, 1):
            sid, url = l["submissionId"], l["url"]
            tag = f"[{i}/{len(links)}] {sid} {urlparse(url).hostname}"
            try:
                title, text, shot = read_page(page, url)
            except Exception as e:
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
        browser.close()

    mins = (time.time() - t0) / 60
    print(f"\nDone in {mins:.0f} min: {json.dumps(tally)}")
    print("Now fire the sweep (POST /api/review/jobs/sweep?limit=1200) - "
          "re-offered rows are graded in that run.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
