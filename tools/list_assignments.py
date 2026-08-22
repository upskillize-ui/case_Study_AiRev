#!/usr/bin/env python3
"""
list_assignments.py — every assignment's id and title, newest first.

STRICTLY READ-ONLY, one SELECT. The id printed here is what reviewday and
every tools\\ script take as --assignment-id.

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
            "SELECT a.id, a.title, a.status, a.due_date, "
            "       (SELECT COUNT(DISTINCT s.student_id) "
            "        FROM assignment_submissions s "
            "        WHERE s.assignment_id = a.id)       AS submitted, "
            "       (SELECT COUNT(DISTINCT s.student_id) "
            "        FROM assignment_submissions s "
            "        WHERE s.assignment_id = a.id "
            "          AND s.grade IS NOT NULL)          AS graded "
            "FROM assignments a ORDER BY a.id DESC")
        rows = cur.fetchall() or []
    conn.close()
    print(f"{'ID':>4}  {'submitted':>9}  {'graded':>6}  {'status':<9} TITLE")
    for r in rows:
        print(f"{r['id']:>4}  {r['submitted']:>9}  {r['graded']:>6}  "
              f"{str(r['status'] or ''):<9} {str(r['title'] or '')[:70]}")
    print("\nUse the ID with:  reviewday ID")
    print("Nothing was modified. This script only reads.")


if __name__ == "__main__":
    main()
