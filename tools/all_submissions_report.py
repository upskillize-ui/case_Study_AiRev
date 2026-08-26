#!/usr/bin/env python3
"""
all_submissions_report.py — every submission for every Day, straight from the
live database. STRICTLY READ-ONLY: nothing but SELECTs.

Covers course assignments (Day 1 ChatGPT ... today) AND industry sessions.
Two CSVs land next to this repo's root:

    submissions_all_days.csv   one row per submission:
        day, type, student_name, email, submitted_date, submitted_time
    submissions_summary.csv    one row per Day: how many submitted

Run on the machine that holds the DB URL:

    set AIREV_DB_URL=...paste your value here...
    python tools\\all_submissions_report.py            (course 55 by default)
    python tools\\all_submissions_report.py --course 55
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from submission_report import connect, existing_columns, pick_name_source


def pick_email_source(conn) -> tuple:
    """(join_sql, email_expr) mirroring pick_name_source's fallbacks:
    students.email -> users.email via students.user_id -> ''."""
    students = existing_columns(conn, "students")
    users = existing_columns(conn, "users")
    if "email" in students:
        return ("LEFT JOIN students se ON se.id = s.student_id", "se.email")
    if "user_id" in students and "email" in users:
        return ("LEFT JOIN students se ON se.id = s.student_id "
                "LEFT JOIN users ue ON ue.id = se.user_id", "ue.email")
    return ("", "''")


def split_when(value) -> tuple:
    """(date, time) as text from a DATETIME/str/None. Pure."""
    if value is None:
        return ("", "")
    s = str(value)
    if " " in s:
        d, _, t = s.partition(" ")
        return (d, t[:8])
    return (s[:10], "")


def fetch_assignment_rows(conn, course_id: int) -> list:
    name_join, name_expr = pick_name_source(conn)
    email_join, email_expr = pick_email_source(conn)
    sql = (f"SELECT a.id AS item_id, a.title AS day, "
           f"       {name_expr} AS student_name, {email_expr} AS email, "
           f"       s.submitted_at "
           f"FROM assignment_submissions s "
           f"JOIN assignments a ON a.id = s.assignment_id "
           f"{name_join} {email_join} "
           f"WHERE a.course_id = %s AND s.status <> 'draft' "
           f"ORDER BY a.id, s.submitted_at")
    with conn.cursor() as cur:
        cur.execute(sql, (course_id,))
        return [dict(r, type="assignment") for r in (cur.fetchall() or [])]


def fetch_session_rows(conn) -> list:
    """Industry-session insights, from whichever table this tenant has.
    Probed with existing_columns — never SELECT unconfirmed columns."""
    out = []
    name_join, name_expr = pick_name_source(conn)
    email_join, email_expr = pick_email_source(conn)
    sessions = existing_columns(conn, "industry_sessions")
    title_col = "title" if "title" in sessions else None

    for table, when_cols in (("industry_session_submissions",
                              ("submitted_at", "created_at")),
                             ("session_feedback",
                              ("created_at", "submitted_at"))):
        cols = existing_columns(conn, table)
        if not cols or "student_id" not in cols or "session_id" not in cols:
            continue
        when = next((c for c in when_cols if c in cols), None)
        when_expr = f"s.{when}" if when else "NULL"
        day_expr = (f"COALESCE(i.{title_col}, CONCAT('session ', s.session_id))"
                    if title_col else "CONCAT('session ', s.session_id)")
        sql = (f"SELECT s.session_id AS item_id, {day_expr} AS day, "
               f"       {name_expr} AS student_name, {email_expr} AS email, "
               f"       {when_expr} AS submitted_at "
               f"FROM {table} s "
               f"LEFT JOIN industry_sessions i ON i.id = s.session_id "
               f"{name_join} {email_join} "
               f"ORDER BY s.session_id, {when_expr}")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall() or []
        except Exception as e:
            print(f"  (skipped {table}: {e})")
            continue
        out.extend(dict(r, type=f"industry_session ({table})") for r in rows)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", type=int, default=55)
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:  set AIREV_DB_URL=...paste your value here...")
    conn = connect(db_url)

    rows = fetch_assignment_rows(conn, args.course) + fetch_session_rows(conn)
    conn.close()

    detail_path, summary_path = "submissions_all_days.csv", "submissions_summary.csv"
    counts: dict = {}
    with open(detail_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["day", "type", "student_name", "email",
                    "submitted_date", "submitted_time"])
        for r in rows:
            d, t = split_when(r.get("submitted_at"))
            day = str(r.get("day") or "")
            w.writerow([day, r["type"], str(r.get("student_name") or ""),
                        str(r.get("email") or ""), d, t])
            key = (day, r["type"])
            counts[key] = counts.get(key, 0) + 1

    with open(summary_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["day", "type", "submissions"])
        for (day, kind), n in sorted(counts.items()):
            w.writerow([day, kind, n])

    print(f"Wrote {detail_path} ({len(rows)} submissions) and {summary_path} "
          f"({len(counts)} day/type groups).")
    for (day, kind), n in sorted(counts.items()):
        print(f"  {n:>5}  {kind:<38} {day[:60]}")


if __name__ == "__main__":
    main()
