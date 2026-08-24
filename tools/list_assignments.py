#!/usr/bin/env python3
"""
list_assignments.py — every assignment's id and title, newest first.

STRICTLY READ-ONLY, one SELECT. The id printed here is what reviewday and
every tools\\ script take as --assignment-id.

It also prints the COURSE id each assignment belongs to, and a summary of
courses at the end — because that is the value AIREV_AUTO_REVIEW_COURSES
needs, and hunting for it in the admin UI at 3am is not a good use of anyone's
night.

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname
    python tools\\list_assignments.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from submission_report import connect


def main() -> None:
    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:  set AIREV_DB_URL=...paste your value here...")
    conn = connect(db_url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.id, a.title, a.status, a.due_date, a.course_id, "
            "       (SELECT COUNT(DISTINCT s.student_id) "
            "        FROM assignment_submissions s "
            "        WHERE s.assignment_id = a.id)       AS submitted, "
            "       (SELECT COUNT(DISTINCT s.student_id) "
            "        FROM assignment_submissions s "
            "        WHERE s.assignment_id = a.id "
            "          AND s.grade IS NOT NULL)          AS graded "
            "FROM assignments a ORDER BY a.id DESC")
        rows = cur.fetchall() or []

        # The course each assignment sits in. Probed separately and tolerantly:
        # a missing courses table must cost the assignment listing nothing.
        course_names = {}
        try:
            cur.execute("SELECT id, title FROM courses")
            course_names = {r["id"]: str(r["title"] or "") for r in (cur.fetchall() or [])}
        except Exception:
            pass
    conn.close()
    print(f"{'ID':>4}  {'course':>6}  {'submitted':>9}  {'graded':>6}  "
          f"{'status':<9} TITLE")
    for r in rows:
        print(f"{r['id']:>4}  {str(r.get('course_id') or '-'):>6}  "
              f"{r['submitted']:>9}  {r['graded']:>6}  "
              f"{str(r['status'] or ''):<9} {str(r['title'] or '')[:70]}")

    # Which course ids actually carry assignments — the shortlist for the
    # auto-review allowlist.
    per_course = {}
    for r in rows:
        cid = r.get("course_id")
        if cid is None:
            continue
        entry = per_course.setdefault(cid, {"n": 0, "submitted": 0})
        entry["n"] += 1
        entry["submitted"] += int(r["submitted"] or 0)

    if per_course:
        print("\nCOURSES (this is the value AIREV_AUTO_REVIEW_COURSES wants):")
        for cid, info in sorted(per_course.items(),
                                key=lambda kv: -kv[1]["submitted"]):
            name = course_names.get(cid, "")
            print(f"  course {cid}: {info['n']} assignment(s), "
                  f"{info['submitted']} submission(s)"
                  + (f" — {name[:60]}" if name else ""))
        top = max(per_course.items(), key=lambda kv: kv[1]["submitted"])[0]
        print(f"\n  Most active is course {top}. If that is 30 Days 30 AI Tools:")
        print(f"      AIREV_AUTO_REVIEW_COURSES={top}")

    print("\nUse the ID with:  reviewday ID")
    print("Nothing was modified. This script only reads.")


if __name__ == "__main__":
    main()
