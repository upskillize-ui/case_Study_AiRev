#!/usr/bin/env python3
"""
render_check.py — ask the Space what its browser reads from one link.

    set AIREV_API_KEY=...paste the lms tenant key here...
    set AIREV_ADMIN_KEY=...paste the admin job key here...
    python tools\\render_check.py https://claude.ai/public/artifacts/...

Needs LINK_RENDER_ENABLED=1 on the Space. Read-only: nothing is reviewed,
nothing is written — it prints the title-line and first part of what the
agent's browser harvested, or the reason it could not.
"""
from __future__ import annotations

import os
import sys

try:
    import httpx
except ImportError:
    sys.exit("Missing dep. Run:  pip install httpx")

AGENT = os.getenv("AIREV_URL", "https://upskill25-airev-agent.hf.space").rstrip("/")


def main() -> None:
    if len(sys.argv) < 2:
        sys.exit("Usage: python tools\\render_check.py URL [URL ...]")
    api_key = os.getenv("AIREV_API_KEY", "")
    admin_key = os.getenv("AIREV_ADMIN_KEY", "")
    if not (api_key and admin_key):
        sys.exit("Set AIREV_API_KEY and AIREV_ADMIN_KEY first")
    headers = {"Content-Type": "application/json", "x-api-key": api_key,
               "x-admin-key": admin_key}
    with httpx.Client(timeout=120) as client:
        for url in sys.argv[1:]:
            r = client.post(f"{AGENT}/api/review/jobs/render-check",
                            json={"url": url}, headers=headers)
            if r.status_code != 200:
                print(f"\n{url}\n  HTTP {r.status_code}: {r.text[:200]}")
                continue
            d = r.json()
            print(f"\n{url}")
            if d["readable"]:
                print(f"  READABLE — {d['words']} words. First part:")
                print("  " + d["preview"][:800].replace("\n", "\n  "))
            else:
                print(f"  NOT readable: {d['why']}")


if __name__ == "__main__":
    main()
