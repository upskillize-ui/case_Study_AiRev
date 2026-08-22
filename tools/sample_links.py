#!/usr/bin/env python3
"""
sample_links.py — find the assignment by name and print real student links
from it, ready to paste into render_check.

STRICTLY READ-ONLY. Two SELECTs, no AI, nothing written.

    set AIREV_DB_URL=...paste your value here...
    python tools\\sample_links.py notion
    python tools\\sample_links.py notion --count 3

Prints the assignment id (what reviewday takes) and a few submitted URLs, so
the render check runs on real student work instead of a hand-picked example.
"""
from __future__ import annotations

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from submission_report import connect

URL_RE = re.compile(r"https?://[^\s<>\"'\]\)}]+", re.IGNORECASE)
TRAILING = ".,;:!?'\")]}>"


def urls_in(text: str) -> list:
    """Distinct URLs in one submission, trailing punctuation stripped. Pure."""
    seen, out = set(), []
    for raw in URL_RE.findall(text or ""):
        u = raw.rstrip(TRAILING)
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def pick_spread(rows: list, count: int) -> list:
    """One URL per student, spread across the list rather than the first N in
    a row — early submissions cluster around whoever posted first. Pure."""
    picked = []
    step = max(1, len(rows) // max(count, 1))
    for r in rows[::step]:
        found = urls_in(r.get("notes") or "")
        if found:
            picked.append((r["student_id"], found[0]))
        if len(picked) >= count:
            break
    return picked


def main() -> None:
    ap = argparse.ArgumentParser(description="Find an assignment by name and "
                                             "print real student links (read-only).")
    ap.add_argument("name", help="part of the title, e.g. notion")
    ap.add_argument("--count", type=int, default=3)
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:  set AIREV_DB_URL=...paste your value here...")

    conn = connect(db_url)
    with conn.cursor() as cur:
        cur.execute("SELECT id, title FROM assignments "
                    "WHERE title LIKE %s ORDER BY id DESC",
                    (f"%{args.name}%",))
        matches = cur.fetchall() or []

    if not matches:
        conn.close()
        sys.exit(f"No assignment title contains {args.name!r}. "
                 f"Run:  python tools\\list_assignments.py")
    if len(matches) > 1:
        print("More than one assignment matches — pick one and search for a "
              "longer part of its title:")
        for m in matches:
            print(f"  {m['id']:>4}  {m['title']}")
        conn.close()
        return

    a = matches[0]
    with conn.cursor() as cur:
        # '%%' because pymysql interpolates the args tuple into this string:
        # a lone % in a literal would be read as a placeholder.
        cur.execute("SELECT student_id, notes FROM assignment_submissions "
                    "WHERE assignment_id = %s AND notes LIKE '%%http%%' "
                    "ORDER BY id", (a["id"],))
        rows = cur.fetchall() or []
    conn.close()

    print(f"\nAssignment {a['id']}: {a['title']}")
    print(f"{len(rows)} submission(s) contain a link.\n")
    picked = pick_spread(rows, args.count)
    if not picked:
        print("No links found in these submissions — this is a file day, "
              "not a link day. No render check needed.")
        return
    for student_id, url in picked:
        print(f"  student {student_id}: {url}")
    print("\nCheck them with:")
    print("  python tools\\render_check.py " + " ".join(u for _, u in picked))
    print(f"\nThen review the whole assignment with:  reviewday {a['id']}")
    print("Nothing was modified. This script only reads.")


if __name__ == "__main__":
    main()
