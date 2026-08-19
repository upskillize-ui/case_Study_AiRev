#!/usr/bin/env python3
"""
submission_report.py — who submitted what, and how many times.

STRICTLY READ-ONLY. SELECT statements only; no AI calls. Writes two CSV files
(they open directly in Excel) and prints a summary:

  submission_report_full.csv        every (student, assignment) pair:
                                    student name, student id, assignment id,
                                    assignment title, number of submissions,
                                    latest submission time, current grade
  submission_report_multiples.csv   ONLY the pairs with more than one row —
                                    the reopened-assignment resubmitters

WHY. Assignments are being reopened, so one student can now hold several rows
on the same assignment. Review policy is LATEST ROW ONLY; this report is how
we see the multiples population before (and after) any run.

The student's name lives in a different table than the submission, and the
schema differs between deployments — so every column is PROBED via
information_schema before it is selected. Never SELECT a column you have not
confirmed exists (the project rule that exists because guessing broke things).

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname
    python tools\\submission_report.py
    python tools\\submission_report.py --assignment-id 17
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from urllib.parse import unquote, urlparse

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    sys.exit("Missing dependency. Run:  pip install pymysql")


def connect(db_url: str):
    u = urlparse(db_url)
    if not u.hostname:
        sys.exit(f"Could not parse AIREV_DB_URL: {db_url[:40]}...")
    return pymysql.connect(
        host=u.hostname, port=u.port or 3306,
        user=unquote(u.username or ""), password=unquote(u.password or ""),
        database=(u.path or "/").lstrip("/"),
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=20,
        ssl={"ssl": {}} if "aivencloud" in (u.hostname or "") else None,
    )


# ---------- schema probing (uppercase AND lowercase info_schema keys) -------

def existing_columns(conn, table: str) -> set:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
        rows = cur.fetchall() or []
    return {(r.get("COLUMN_NAME") or r.get("column_name") or "").lower()
            for r in rows}


def pick_name_source(conn) -> tuple:
    """(join_sql, name_expr) for the student's display name. Pure fallbacks:
    students.name -> students.full_name -> users.name via students.user_id ->
    '' (report still works, name column just stays blank)."""
    students = existing_columns(conn, "students")
    users = existing_columns(conn, "users")
    if "name" in students:
        return ("LEFT JOIN students st ON st.id = s.student_id", "st.name")
    if "full_name" in students:
        return ("LEFT JOIN students st ON st.id = s.student_id", "st.full_name")
    if "user_id" in students and "name" in users:
        return ("LEFT JOIN students st ON st.id = s.student_id "
                "LEFT JOIN users u ON u.id = st.user_id", "u.name")
    return ("", "''")


# ---------- pure shaping (unit-testable without a DB) -----------------------

def shape_rows(raw: list) -> list:
    """Sort: most submissions first, then assignment, then student."""
    return sorted(raw, key=lambda r: (-int(r["submissions"]),
                                      int(r["assignment_id"]),
                                      int(r["student_id"])))


def multiples_only(rows: list) -> list:
    return [r for r in rows if int(r["submissions"]) > 1]


def summarise(rows: list) -> dict:
    total_rows = sum(int(r["submissions"]) for r in rows)
    dups = multiples_only(rows)
    return {
        "pairs": len(rows),
        "total_submission_rows": total_rows,
        "students_with_multiples": len({r["student_id"] for r in dups}),
        "pairs_with_multiples": len(dups),
        "extra_rows": sum(int(r["submissions"]) - 1 for r in dups),
    }


# ---------- main ------------------------------------------------------------

FIELDS = ["student_name", "student_id", "assignment_id", "assignment_title",
          "submissions", "latest_submission", "current_grade"]


def write_csv(path: str, rows: list) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in FIELDS})


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Per-student submission counts (read-only; writes 2 CSVs).")
    ap.add_argument("--assignment-id", type=int, action="append",
                    help="repeatable; default is every assignment")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:\n"
                 "  cmd:  set AIREV_DB_URL=mysql://user:pass@host:port/db")

    conn = connect(db_url)
    try:
        join_sql, name_expr = pick_name_source(conn)
        sql = f"""
            SELECT {name_expr}                    AS student_name,
                   s.student_id                   AS student_id,
                   s.assignment_id                AS assignment_id,
                   COALESCE(a.title, '')          AS assignment_title,
                   COUNT(*)                       AS submissions,
                   MAX(s.submitted_at)            AS latest_submission,
                   MAX(s.grade)                   AS current_grade
            FROM assignment_submissions s
            LEFT JOIN assignments a ON a.id = s.assignment_id
            {join_sql}
            WHERE 1 = 1
        """
        params: list = []
        if args.assignment_id:
            sql += (" AND s.assignment_id IN ("
                    + ",".join(["%s"] * len(args.assignment_id)) + ")")
            params.extend(args.assignment_id)
        sql += f" GROUP BY s.assignment_id, s.student_id, {name_expr}"
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = list(cur.fetchall() or [])
    finally:
        conn.close()

    rows = shape_rows(raw)
    dups = multiples_only(rows)
    s = summarise(rows)

    write_csv("submission_report_full.csv", rows)
    write_csv("submission_report_multiples.csv", dups)

    print(f"\n(student, assignment) pairs      : {s['pairs']}")
    print(f"Total submission rows            : {s['total_submission_rows']}")
    print(f"Pairs with MORE THAN ONE row     : {s['pairs_with_multiples']}")
    print(f"Students holding multiples       : {s['students_with_multiples']}")
    print(f"Extra rows beyond one-per-pair   : {s['extra_rows']}")
    if dups:
        print("\nTop resubmitters:")
        for r in dups[:15]:
            print(f"  student {r['student_id']:<6} {str(r['student_name'] or '')[:24]:<26} "
                  f"assignment {r['assignment_id']:<4} x{r['submissions']}  "
                  f"latest {r['latest_submission']}")
        if len(dups) > 15:
            print(f"  ... and {len(dups) - 15} more — see submission_report_multiples.csv")
    else:
        print("\nNo student holds more than one row on any assignment.")
    print("\nFiles: submission_report_full.csv · submission_report_multiples.csv")
    print("Nothing was modified. This script only reads.\n")


if __name__ == "__main__":
    main()
