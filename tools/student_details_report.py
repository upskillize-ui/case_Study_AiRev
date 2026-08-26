#!/usr/bin/env python3
"""
student_details_report.py — full profile of every student enrolled in one
course, straight from the live database. STRICTLY READ-ONLY.

Exports EVERY column that exists on the students and users tables — mobile,
degree/class (11th, 12th, MBA...), year, division, college, whatever the
schema holds — except credential columns (password, token, otp, hash,
secret), which are never read.

    set AIREV_DB_URL=...paste your value here...
    python tools\\student_details_report.py                (course 55)
    python tools\\student_details_report.py --course 55

Output: student_details.csv next to the repo root, one row per enrolled
student, columns named students_<col> / users_<col>.
"""
from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from submission_report import connect, existing_columns

# Never exported, whatever the schema calls them.
_FORBIDDEN = ("password", "token", "otp", "secret", "hash", "salt")


def safe_columns(cols: set) -> list:
    """Alphabetical, credential-free column list. Pure."""
    return sorted(c for c in cols
                  if not any(bad in c.lower() for bad in _FORBIDDEN))


def enrollment_table(conn) -> str | None:
    for table in ("enrollments", "course_enrollments", "student_courses"):
        cols = existing_columns(conn, table)
        if "student_id" in cols and "course_id" in cols:
            return table
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", type=int, default=55)
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:  set AIREV_DB_URL=...paste your value here...")
    conn = connect(db_url)

    st_cols = safe_columns(existing_columns(conn, "students"))
    u_cols = safe_columns(existing_columns(conn, "users"))
    if not st_cols:
        sys.exit("No students table found — nothing to export.")
    enr = enrollment_table(conn)
    if not enr:
        sys.exit("No enrollment table found — cannot scope to the course.")

    select = ([f"st.{c} AS students_{c}" for c in st_cols]
              + [f"u.{c} AS users_{c}" for c in u_cols])
    join_users = ("LEFT JOIN users u ON u.id = st.user_id"
                  if "user_id" in st_cols and u_cols else "")
    sql = (f"SELECT {', '.join(select)} "
           f"FROM students st "
           f"{join_users} "
           f"WHERE st.id IN (SELECT e.student_id FROM {enr} e "
           f"                WHERE e.course_id = %s) "
           f"ORDER BY st.id")
    with conn.cursor() as cur:
        cur.execute(sql, (args.course,))
        rows = cur.fetchall() or []
    conn.close()

    if not rows:
        sys.exit(f"No students enrolled in course {args.course}.")

    headers = list(rows[0].keys())
    with open("student_details.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for r in rows:
            w.writerow(["" if r.get(h) is None else str(r.get(h)) for h in headers])

    print(f"Wrote student_details.csv — {len(rows)} students, "
          f"{len(headers)} columns.")
    print("Columns: " + ", ".join(headers))


if __name__ == "__main__":
    main()
