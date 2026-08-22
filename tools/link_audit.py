#!/usr/bin/env python3
"""
link_audit.py — for every UNGRADED submission that is just a link, open the
link in the agent's browser and record what is actually there.

STRICTLY READ-ONLY. SELECTs plus the Space's render-check endpoint; no
review is written, no grade changes, no DB write.

Why this exists (22 Aug, Day 04 / assignment 21): 118 students came back
ungraded and problem_report labelled 108 of them "US (re-run the review) —
do NOT message this student". That was wrong. 77 of those rows are a pasted
Notion URL and nothing else, and the sample we checked by hand was a PRIVATE
page: Notion's sign-in wall, not our failure. Messaging is exactly what
those students need, and re-running would have changed nothing.

Guessing which is which would be the same fabrication in a different coat.
This opens each link and says.

    set AIREV_DB_URL=...paste your value here...
    set AIREV_API_KEY=...the lms tenant key...
    set AIREV_ADMIN_KEY=...the admin job key...
    python tools\\link_audit.py --assignment-id 21

Writes link_audit_<id>.csv: name, email, the link, and the verdict —
STUDENT (their page is private / missing) or US (we could not read a page
that is genuinely public). Roughly 20-25s per link: the Space renders one
page at a time on purpose.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from results_report import resolve_identities
from sample_links import urls_in
from submission_report import connect

try:
    import httpx
except ImportError:
    sys.exit("Missing dep. Run:  pip install httpx")

AGENT = os.getenv("AIREV_URL", "https://upskill25-airev-agent.hf.space").rstrip("/")

# Verdicts. The split that decides who gets a message.
ACT_STUDENT = "STUDENT"
ACT_US = "US"
ACT_OK = "READABLE (should have been graded — re-run)"


def verdict_for(readable: bool, words: int, why: str) -> tuple:
    """(who_must_act, plain_reason) for one checked link. Pure.

    A page we opened and read is not a student problem — if it is still
    ungraded that is ours to re-run. A page that asks for a sign-in, or is
    gone, is theirs to fix and no re-run will change it. Anything else is
    ours until proven otherwise: the asymmetry is deliberate, because
    telling a student to fix work that is fine is worse than re-running.
    """
    if readable and words >= 8:
        return ACT_OK, f"The page opened and read fine ({words} words)."
    low = (why or "").lower()
    if "private" in low or "sign-in" in low or "sign in" in low:
        return ACT_STUDENT, why
    if "no longer exists" in low or "not exist" in low:
        return ACT_STUDENT, why
    if "human-check" in low or "cloudflare" in low:
        return ACT_US, why + " — the site blocked our browser, not the student."
    return ACT_US, why or "The page could not be opened, reason unrecorded."


def ungraded_link_rows(conn, assignment_id: int) -> list:
    """Latest ungraded attempt per student, with the first URL they pasted."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.id, s.student_id, s.grade, s.notes,
                   COALESCE(s.file_path, '') AS file_ref, s.submitted_at
            FROM assignment_submissions s
            WHERE s.assignment_id = %s
            ORDER BY s.submitted_at DESC, s.id DESC""", (assignment_id,))
        raw = cur.fetchall() or []
    latest: dict = {}
    for r in raw:
        latest.setdefault(r["student_id"], r)
    out = []
    for r in latest.values():
        if r["grade"] is not None:
            continue                       # already marked — not our problem
        found = urls_in(r["notes"] or "")
        if found:
            out.append({"student_id": r["student_id"], "url": found[0],
                        "submitted_at": r["submitted_at"]})
    return sorted(out, key=lambda r: r["student_id"])


def check(client, url: str, headers: dict) -> tuple:
    """(readable, words, why) from the Space. Never raises."""
    try:
        r = client.post(f"{AGENT}/api/review/jobs/render-check",
                        json={"url": url}, headers=headers)
    except Exception as e:
        return False, 0, f"the check itself failed ({type(e).__name__})"
    if r.status_code != 200:
        return False, 0, f"HTTP {r.status_code}: {r.text[:120]}"
    d = r.json()
    return bool(d.get("readable")), int(d.get("words") or 0), str(d.get("why") or "")


def main() -> None:
    ap = argparse.ArgumentParser(description="Open every ungraded student link "
                                             "and record what is there (read-only).")
    ap.add_argument("--assignment-id", type=int, required=True)
    ap.add_argument("--limit", type=int, default=0, help="check only the first N")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    api_key = os.getenv("AIREV_API_KEY", "")
    admin_key = os.getenv("AIREV_ADMIN_KEY", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:  set AIREV_DB_URL=...paste your value here...")
    if not (api_key and admin_key):
        sys.exit("Set AIREV_API_KEY and AIREV_ADMIN_KEY first")

    conn = connect(db_url)
    rows = ungraded_link_rows(conn, args.assignment_id)
    if args.limit:
        rows = rows[:args.limit]
    who = resolve_identities(conn, [r["student_id"] for r in rows])
    conn.close()

    if not rows:
        print(f"Assignment {args.assignment_id}: no ungraded submission "
              f"contains a link. Nothing to audit.")
        return

    est = len(rows) * 22 // 60
    print(f"Assignment {args.assignment_id}: {len(rows)} ungraded link "
          f"submission(s) to open. About {est or 1} minute(s) — the Space "
          f"renders one page at a time.\n")

    out_path = args.out or f"link_audit_{args.assignment_id}.csv"
    tally = {ACT_STUDENT: 0, ACT_US: 0, ACT_OK: 0}
    started = time.time()
    with httpx.Client(timeout=180) as client, \
            open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        headers = {"Content-Type": "application/json", "x-api-key": api_key,
                   "x-admin-key": admin_key}
        w = csv.writer(f)
        w.writerow(["student_name", "email", "student_id", "submitted_at",
                    "link", "who_must_act", "reason", "words_read"])
        for i, r in enumerate(rows, 1):
            readable, words, why = check(client, r["url"], headers)
            act, reason = verdict_for(readable, words, why)
            tally[act] += 1
            person = who.get(r["student_id"], {})
            w.writerow([person.get("name", ""), person.get("email", ""),
                        r["student_id"], str(r["submitted_at"] or ""),
                        r["url"], act, reason, words])
            f.flush()                      # survive a closed window
            print(f"  [{i}/{len(rows)}] student {r['student_id']:<6} {act:<9} "
                  f"{reason[:70]}")

    mins = int(time.time() - started) // 60
    print(f"\nChecked {len(rows)} links in {mins} minute(s).")
    print(f"  {tally[ACT_STUDENT]:>4}  STUDENT must act — their page is private "
          f"or gone. Message these.")
    print(f"  {tally[ACT_US]:>4}  US must act — we could not open a public page.")
    print(f"  {tally[ACT_OK]:>4}  READABLE — should have been graded; re-run these.")
    print(f"\nFile: {out_path}")
    print("Nothing was modified. This script only reads.")


if __name__ == "__main__":
    main()
