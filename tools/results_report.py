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

Reuses submission_report's connection helper. Identity columns are probed
here with real zero-row SELECTs rather than trusted from information_schema,
and the roster is assembled join-free — one schema surprise blanks a cell
instead of killing the run.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from submission_report import connect

# Identity columns, in preference order. Nothing here is trusted from
# information_schema: 22 Aug this report died with
#   (1054, "Unknown column 'st.user_id' in 'on clause'")
# on a column information_schema had just listed. Every column below is
# PROVED with a zero-row SELECT before it enters the real query, and the
# roster is assembled from separate small queries instead of one join — a
# schema surprise now blanks one column instead of killing the run.
NAME_COLS = ("name", "full_name", "student_name", "fullname")
EMAIL_COLS = ("email", "email_id", "user_email", "email_address")
ID_CHUNK = 400


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


# ---------- identity lookup (probed, join-free) -----------------------------

def usable_columns(conn, table: str, wanted) -> list:
    """The candidates that actually SELECT from this table. Never raises.

    A zero-row SELECT is the only honest probe: it proves the table exists,
    the column exists, and the account may read it — all three at once.
    """
    ok = []
    for col in wanted:
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT `{col}` FROM `{table}` LIMIT 0")
                cur.fetchall()
            ok.append(col)
        except Exception:
            continue
    return ok


def fetch_identity_rows(conn, table: str, key: str, ids: list,
                        cols: list) -> list:
    """SELECT key + cols for these ids, in chunks. Never raises."""
    if not ids or not cols:
        return []
    out = []
    select = ", ".join(f"`{c}`" for c in [key] + cols)
    for i in range(0, len(ids), ID_CHUNK):
        chunk = ids[i:i + ID_CHUNK]
        marks = ", ".join(["%s"] * len(chunk))
        try:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {select} FROM `{table}` "
                            f"WHERE `{key}` IN ({marks})", tuple(chunk))
                out.extend(cur.fetchall() or [])
        except Exception:
            return out
    return out


def _first_value(row: dict, cols: list) -> str:
    for c in cols:
        v = row.get(c)
        if v is not None and str(v).strip():
            return str(v).strip()
    return ""


def resolve_identities(conn, student_ids: list) -> dict:
    """{student_id: {"name": ..., "email": ...}} — students first, then any
    gaps filled from users (via students.user_id when that column is real,
    otherwise treating the student id as the user id, which some LMS rows do).

    Missing tables, missing columns and permission errors all degrade to a
    blank cell. A roster with no email column is still a roster.
    """
    ids = [i for i in student_ids if i is not None]
    who: dict = {i: {"name": "", "email": ""} for i in ids}
    if not ids:
        return who

    s_names = usable_columns(conn, "students", NAME_COLS)
    s_emails = usable_columns(conn, "students", EMAIL_COLS)
    s_link = usable_columns(conn, "students", ("user_id",))
    if usable_columns(conn, "students", ("id",)):
        for row in fetch_identity_rows(conn, "students", "id", ids,
                                       s_names + s_emails + s_link):
            rec = who.get(row.get("id"))
            if rec is None:
                continue
            rec["name"] = _first_value(row, s_names)
            rec["email"] = _first_value(row, s_emails)
            if s_link and row.get("user_id") is not None:
                rec["user_id"] = row["user_id"]

    gaps = [i for i, rec in who.items() if not (rec["name"] and rec["email"])]
    if not gaps:
        return who

    u_names = usable_columns(conn, "users", NAME_COLS)
    u_emails = usable_columns(conn, "users", EMAIL_COLS)
    if not (u_names or u_emails) or not usable_columns(conn, "users", ("id",)):
        return who

    # student -> user id when the link exists; else the ids are the same space
    to_user = {who[i].get("user_id", i): i for i in gaps}
    for row in fetch_identity_rows(conn, "users", "id", list(to_user.keys()),
                                   u_names + u_emails):
        rec = who.get(to_user.get(row.get("id")))
        if rec is None:
            continue
        rec["name"] = rec["name"] or _first_value(row, u_names)
        rec["email"] = rec["email"] or _first_value(row, u_emails)
    return who


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
    sql = """
        SELECT s.id AS submission_id, s.student_id,
               s.grade, s.feedback, s.status, s.submitted_at,
               CHAR_LENGTH(COALESCE(s.notes, '')) AS notes_len,
               COALESCE(s.file_path, '') AS file_ref
        FROM assignment_submissions s
        WHERE s.assignment_id = %s
        ORDER BY s.submitted_at DESC, s.id DESC
    """
    with conn.cursor() as cur:
        cur.execute(sql, (args.assignment_id,))
        raw = cur.fetchall() or []

    latest = {}
    for r in raw:                                  # newest attempt per student
        latest.setdefault(r["student_id"], r)

    identities = resolve_identities(conn, list(latest.keys()))
    conn.close()
    for sid, r in latest.items():
        who = identities.get(sid, {})
        r["student_name"] = who.get("name", "")
        r["email"] = who.get("email", "")
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
