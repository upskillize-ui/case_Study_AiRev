#!/usr/bin/env python3
"""
all_submissions_report.py — every submission for every Day of one course,
straight from the live database. STRICTLY READ-ONLY: nothing but SELECTs.

v2 (26 Aug): name from users via COALESCE (students.name was empty for all
3,324 rows); student_id + submitted content columns; industry sessions only
from session_feedback (the LMS table real students write to); attendance CSV
for the session; everything filtered to the course's enrolled students.

    set AIREV_DB_URL=...paste your value here...
    python tools\\all_submissions_report.py            (course 55 by default)

Outputs, next to the repo root:
    submissions_all_days.csv    day, type, student_id, student_name, email,
                                submitted_date, submitted_time, file_or_link,
                                submitted_text
    submissions_summary.csv     per-day counts
    session_attendance.csv      who attended each industry session (when an
                                attendance table exists — probed, never assumed)

Why counts here differ from the admin page: the admin card counts every row
including 'draft' rows created the moment a student merely OPENS the
assignment (they hold time-spent). This report counts real submissions only.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from submission_report import connect, existing_columns

MAX_TEXT = 800          # submitted_text is truncated to keep Excel usable


def person_exprs(conn) -> tuple:
    """(join_sql, name_expr, email_expr) — COALESCE across every source that
    exists, because a column can EXIST yet be empty (students.name was blank
    for all 3,324 rows on 26 Aug; the real names live in users.name)."""
    students = existing_columns(conn, "students")
    users = existing_columns(conn, "users")
    join = "LEFT JOIN students st ON st.id = s.student_id"
    name_parts, email_parts = [], []
    if "user_id" in students and users:
        join += " LEFT JOIN users u ON u.id = st.user_id"
        for c in ("name", "full_name"):
            if c in users:
                name_parts.append(f"NULLIF(u.{c}, '')")
        if "email" in users:
            email_parts.append("NULLIF(u.email, '')")
    for c in ("name", "full_name"):
        if c in students:
            name_parts.append(f"NULLIF(st.{c}, '')")
    if "email" in students:
        email_parts.append("NULLIF(st.email, '')")
    name = f"COALESCE({', '.join(name_parts)}, '')" if name_parts else "''"
    email = f"COALESCE({', '.join(email_parts)}, '')" if email_parts else "''"
    return join, name, email


def enrolled_filter(conn, course_id: int) -> str:
    """SQL fragment limiting s.student_id to the course's enrolled students.
    Probed across the table names in use; '' when none exists (then the
    assignment join's course_id already scopes assignments, and sessions
    stay unfiltered rather than silently empty)."""
    for table in ("enrollments", "course_enrollments", "student_courses"):
        cols = existing_columns(conn, table)
        if "student_id" in cols and "course_id" in cols:
            return (f" AND s.student_id IN (SELECT e.student_id FROM {table} e "
                    f"WHERE e.course_id = {int(course_id)})")
    print("  (no enrollment table found — session rows not course-filtered)")
    return ""


def split_when(value) -> tuple:
    """(date, time) as text from a DATETIME/str/None. Pure."""
    if value is None:
        return ("", "")
    s = str(value)
    if " " in s:
        d, _, t = s.partition(" ")
        return (d, t[:8])
    return (s[:10], "")


def clip(text, limit: int = MAX_TEXT) -> str:
    """Excel-safe single-cell text. Pure."""
    s = " ".join(str(text or "").split())
    return s[:limit] + ("…" if len(s) > limit else "")


def fetch_assignment_rows(conn, course_id: int) -> list:
    join, name, email = person_exprs(conn)
    sql = (f"SELECT a.title AS day, s.student_id, {name} AS student_name, "
           f"       {email} AS email, s.submitted_at, "
           f"       s.file_path AS file_or_link, s.notes AS submitted_text "
           f"FROM assignment_submissions s "
           f"JOIN assignments a ON a.id = s.assignment_id {join} "
           f"WHERE a.course_id = %s AND s.status <> 'draft' "
           f"ORDER BY a.id, s.submitted_at")
    with conn.cursor() as cur:
        cur.execute(sql, (course_id,))
        return [dict(r, type="assignment") for r in (cur.fetchall() or [])]


def fetch_session_rows(conn, course_id: int) -> list:
    """Industry-session insights from session_feedback ONLY — the LMS table
    real students write to. (industry_session_submissions held a handful of
    AiRev test rows; per Ranjana 26 Aug it is excluded.)"""
    cols = existing_columns(conn, "session_feedback")
    if "student_id" not in cols or "session_id" not in cols:
        print("  (session_feedback table not found — no session rows)")
        return []
    join, name, email = person_exprs(conn)
    text_col = next((c for c in ("key_takeaway", "feedback", "comments")
                     if c in cols), None)
    when = next((c for c in ("created_at", "submitted_at") if c in cols), None)
    sessions = existing_columns(conn, "industry_sessions")
    day = ("COALESCE(i.title, CONCAT('session ', s.session_id))"
           if "title" in sessions else "CONCAT('session ', s.session_id)")
    sql = (f"SELECT {day} AS day, s.student_id, {name} AS student_name, "
           f"       {email} AS email, "
           f"       {f's.{when}' if when else 'NULL'} AS submitted_at, "
           f"       '' AS file_or_link, "
           f"       {('s.' + text_col) if text_col else chr(39)*2} AS submitted_text " 
           f"FROM session_feedback s "
           f"LEFT JOIN industry_sessions i ON i.id = s.session_id {join} "
           f"WHERE 1=1 {enrolled_filter(conn, course_id)} "
           f"ORDER BY s.session_id")
    with conn.cursor() as cur:
        cur.execute(sql)
        return [dict(r, type="industry_session")
                for r in (cur.fetchall() or [])]


def fetch_attendance(conn, course_id: int) -> list:
    """Attendance for industry sessions, from whichever table exists."""
    join, name, email = person_exprs(conn)
    sessions = existing_columns(conn, "industry_sessions")
    day = ("COALESCE(i.title, CONCAT('session ', s.session_id))"
           if "title" in sessions else "CONCAT('session ', s.session_id)")
    for table in ("industry_session_attendance", "session_attendance",
                  "session_attendees", "industry_session_attendees"):
        cols = existing_columns(conn, table)
        if "student_id" not in cols or "session_id" not in cols:
            continue
        status = next((c for c in ("status", "attended", "attendance_status")
                       if c in cols), None)
        when = next((c for c in ("joined_at", "created_at", "marked_at")
                     if c in cols), None)
        sql = (f"SELECT {day} AS session, s.student_id, {name} AS student_name, "
               f"       {email} AS email, "
               f"       {('s.' + status) if status else chr(39) + 'present' + chr(39)} AS status, " 
               f"       {f's.{when}' if when else 'NULL'} AS at_time "
               f"FROM {table} s "
               f"LEFT JOIN industry_sessions i ON i.id = s.session_id {join} "
               f"WHERE 1=1 {enrolled_filter(conn, course_id)} "
               f"ORDER BY s.session_id")
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall() or []
            print(f"  attendance read from '{table}' ({len(rows)} rows)")
            return rows
        except Exception as e:
            print(f"  (skipped {table}: {e})")
    print("  (no attendance table found — session_attendance.csv not written; "
          "tell me the table name if one exists)")
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", type=int, default=55)
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:  set AIREV_DB_URL=...paste your value here...")
    conn = connect(db_url)
    rows = (fetch_assignment_rows(conn, args.course)
            + fetch_session_rows(conn, args.course))
    attendance = fetch_attendance(conn, args.course)
    conn.close()

    counts: dict = {}
    with open("submissions_all_days.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["day", "type", "student_id", "student_name", "email",
                    "submitted_date", "submitted_time", "file_or_link",
                    "submitted_text"])
        for r in rows:
            d, t = split_when(r.get("submitted_at"))
            day = str(r.get("day") or "")
            w.writerow([day, r["type"], r.get("student_id"),
                        str(r.get("student_name") or ""),
                        str(r.get("email") or ""), d, t,
                        str(r.get("file_or_link") or ""),
                        clip(r.get("submitted_text"))])
            counts[(day, r["type"])] = counts.get((day, r["type"]), 0) + 1

    with open("submissions_summary.csv", "w", newline="",
              encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["day", "type", "submissions"])
        for (day, kind), n in sorted(counts.items()):
            w.writerow([day, kind, n])

    if attendance:
        with open("session_attendance.csv", "w", newline="",
                  encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["session", "student_id", "student_name", "email",
                        "status", "time"])
            for r in attendance:
                d, t = split_when(r.get("at_time"))
                w.writerow([str(r.get("session") or ""), r.get("student_id"),
                            str(r.get("student_name") or ""),
                            str(r.get("email") or ""),
                            str(r.get("status") or ""), f"{d} {t}".strip()])

    print(f"Wrote submissions_all_days.csv ({len(rows)} rows), "
          f"submissions_summary.csv ({len(counts)} groups)"
          + (f", session_attendance.csv ({len(attendance)} rows)."
             if attendance else "."))
    for (day, kind), n in sorted(counts.items()):
        print(f"  {n:>5}  {kind:<18} {day[:60]}")


if __name__ == "__main__":
    main()
