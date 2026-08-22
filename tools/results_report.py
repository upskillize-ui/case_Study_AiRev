#!/usr/bin/env python3
"""
results_report.py — the COMPLETE roster for one assignment, one row per
student: name, email, marks, band, the feedback they received, and — for
anyone not graded — the reason in plain English.

STRICTLY READ-ONLY. SELECT statements only; no AI calls; nothing written to
the database. Output opens directly in Excel:

    results_report_<assignment_id>.csv

Ranjana, 22 Aug: "Make a list of all 347 students with name, email id and
score and feedback and reason for not reviewing if some students have issue
in submissions." problem_report covers only the ungraded; this is the whole
class on one sheet — the graded majority AND the problem rows, sortable,
ready for messaging or faculty records.

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname
    python tools\\results_report.py --assignment-id 19

Reuses submission_report's connection + schema-probing helpers (the
project's never-SELECT-unconfirmed-columns rule) instead of copying them.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from submission_report import connect, existing_columns, pick_name_source


# ---------- pure shaping (unit-testable without a DB) -----------------------

def feedback_fields(raw_feedback) -> dict:
    """What the CSV shows from a stored feedback JSON. Pure, never raises.

    Graded rows -> marks come from the grade column; here we pull the short
    coaching text (summary + hard truth + first pointers). Not-graded rows
    stamped by the agent carry notGraded + a learner-readable message — that
    message IS the reason column.
    """
    fb = raw_feedback
    if isinstance(fb, str):
        try:
            fb = json.loads(fb)
        except (ValueError, TypeError):
            fb = {}
    if not isinstance(fb, dict):
        fb = {}

    points = [str(p) for p in (fb.get("feedbackPoints") or []) if str(p).strip()]
    summary_bits = [str(fb.get("summary") or "").strip()]
    summary_bits += points[:2]
    hard = str(fb.get("hardTruth") or "").strip()
    if hard:
        summary_bits.append(f"Bottom line: {hard}")
    feedback_text = " | ".join(b for b in summary_bits if b)[:600]

    not_graded_reason = ""
    if fb.get("notGraded") or (fb.get("message") and not fb.get("rubricScores")):
        not_graded_reason = str(fb.get("message") or fb.get("summary")
                                or fb.get("detailedFeedback") or "").strip()[:400]

    return {
        "band": str(fb.get("grade") or "").strip(),
        "ai_percent": fb.get("aiLikelihoodPercent", ""),
        "feedback": feedback_text,
        "not_graded_reason": not_graded_reason,
    }


def reason_for(row: dict, fb: dict) -> str:
    """One plain-English reason per ungraded student; '' for graded rows."""
    if row.get("grade") is not None:
        return ""
    if fb["not_graded_reason"]:
        return fb["not_graded_reason"]           # the agent's own stamped note
    has_content = bool(row.get("notes_len") or row.get("file_ref"))
    if not has_content:
        return ("Nothing to review was stored - no file and no written "
                "answer. Please submit your work again.")
    return ("Not reviewed yet - the next review run will pick this up "
            "automatically. No action needed from the student.")


def shape(rows: list) -> list:
    """Graded first (highest marks on top), then the problem rows."""
    def key(r):
        g = r.get("grade")
        return (0, -float(g)) if g is not None else (1, 0.0)
    return sorted(rows, key=lambda r: (key(r), str(r.get("student_name") or "")))


# ---------- main ------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Full per-student results roster "
                                             "for one assignment (read-only).")
    ap.add_argument("--assignment-id", type=int, required=True)
    ap.add_argument("--out", default=None,
                    help="output CSV (default results_report_<id>.csv)")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:  set AIREV_DB_URL=...paste your value here...")

    conn = connect(db_url)
    join_sql, name_expr = pick_name_source(conn)

    students_cols = existing_columns(conn, "students")
    users_cols = existing_columns(conn, "users")
    if "email" in students_cols:
        email_expr = "st.email"
    elif "user_id" in students_cols and "email" in users_cols:
        email_expr = "u.email"
        if "users u" not in join_sql:
            join_sql += " LEFT JOIN users u ON u.id = st.user_id"
    else:
        email_expr = "''"

    sql = f"""
        SELECT s.id AS submission_id, s.student_id,
               {name_expr}  AS student_name,
               {email_expr} AS email,
               s.grade, s.feedback, s.status, s.submitted_at,
               CHAR_LENGTH(COALESCE(s.notes, '')) AS notes_len,
               COALESCE(s.file_path, '') AS file_ref
        FROM assignment_submissions s
        {join_sql}
        WHERE s.assignment_id = %s
        ORDER BY s.submitted_at DESC, s.id DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (args.assignment_id,))
        raw = cur.fetchall() or []
    conn.close()

    latest = {}
    for r in raw:                                  # newest attempt per student
        latest.setdefault(r["student_id"], r)
    rows = shape(list(latest.values()))

    out_path = args.out or f"results_report_{args.assignment_id}.csv"
    graded = ungraded = 0
    # utf-8-sig so Excel opens Marathi/Hindi names correctly.
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["student_name", "email", "student_id", "submitted_at",
                    "marks", "band", "ai_percent_advisory", "feedback",
                    "reason_if_not_reviewed"])
        for r in rows:
            fb = feedback_fields(r.get("feedback"))
            reason = reason_for(r, fb)
            if r.get("grade") is not None:
                graded += 1
            else:
                ungraded += 1
            w.writerow([
                r.get("student_name") or "", r.get("email") or "",
                r.get("student_id"), str(r.get("submitted_at") or ""),
                r.get("grade") if r.get("grade") is not None else "",
                fb["band"] if r.get("grade") is not None else "",
                fb["ai_percent"] if r.get("grade") is not None else "",
                fb["feedback"] if r.get("grade") is not None else "",
                reason,
            ])

    print(f"Assignment {args.assignment_id}: {len(rows)} students "
          f"({graded} graded, {ungraded} not graded)")
    print(f"File: {out_path}")
    print("Nothing was modified. This script only reads.")


if __name__ == "__main__":
    main()
