#!/usr/bin/env python3
"""
problem_report.py — every student the agent could NOT review, with the reason.

STRICTLY READ-ONLY. SELECT statements only; no AI calls. Writes one CSV
(opens directly in Excel) and prints a summary:

  problem_report.csv    one row per ungraded submission: student name and id,
                        assignment, what they submitted, WHO must act
                        (STUDENT or US), the reason in simple English, and
                        exactly what the student should do.

WHO-FIXES logic, in order:
  1. The row carries a stored not-graded note (written by the review run) —
     that IS the reason; the student sees the same words on their card.
  2. No file and no text — nothing was ever submitted to read. STUDENT.
  3. A file exists but the row was never scored and carries no note — the
     agent has not finished with it (server was busy, or the sweep has not
     reached it). US: re-run before messaging anyone about these.

Run AFTER a sweep for the cleanest split — the sweep stamps the notes that
turn "unknown" rows into named student-side reasons.

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname
    python tools\\problem_report.py --assignment-id 17
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, unquote, urlparse

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


def existing_columns(conn, table: str) -> set:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,))
        rows = cur.fetchall() or []
    return {(r.get("COLUMN_NAME") or r.get("column_name") or "").lower()
            for r in rows}


def pick_name_source(conn) -> tuple:
    """(join_sql, name_expr) — same fallback chain as submission_report."""
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


# ---------- pure classification (unit-testable without a DB) ----------------

ACT_STUDENT = "STUDENT"
ACT_US = "US (re-run the review)"

_FIX_UPLOAD = ("Upload your work again (image, PDF or Word), or type your "
               "answer in the box, then click Submit.")


def classify(row: dict) -> tuple:
    """(who_fixes, reason, what_student_should_do) for one ungraded row.

    The stored not-graded note wins: it was written by an actual review
    attempt that READ the row, so it names the real problem in the same
    simple words the student sees on their card. Everything else is
    inference from what is visibly present.
    """
    fb = row.get("feedback")
    if isinstance(fb, str) and fb.strip():
        try:
            fb = json.loads(fb)
        except Exception:
            fb = None
    if isinstance(fb, dict) and fb.get("notGraded") and fb.get("message"):
        return (ACT_STUDENT, fb["message"], fb["message"])

    has_file = bool((row.get("file_name") or "").strip()
                    or (row.get("file_path") or "").strip())
    typed = len((row.get("notes") or "").strip())

    if not has_file and typed == 0:
        return (ACT_STUDENT,
                "Nothing to read — no file and no written answer.",
                _FIX_UPLOAD)

    return (ACT_US,
            "Not reviewed yet — the agent did not finish this row "
            "(server was busy, or the sweep has not stamped it). "
            "Re-run before messaging this student.",
            "Nothing yet — we will review it again first.")


# ---------- probe: does the failing file actually exist? --------------------
#
# "no_readable_content" has three possible truths, and messaging students is
# only right for one of them:
#   FILE MISSING (404/410)  -> student-side: the upload is gone, they resubmit
#   FILE EXISTS  (200+bytes)-> AGENT-side: we can fetch it but could not read
#                              it — do NOT message; that is our reader to fix
#   SERVER ERROR / TIMEOUT  -> unknown: LMS backend was down; probe again
#
# The probe answers per file, so the messaging list is evidence, not a guess.

LMS_FILE_BASE_URL = os.getenv("LMS_FILE_BASE_URL",
                              "https://upskillize-lms-backend.onrender.com")


def resolve_file_url(ref: str) -> str:
    """Stored file reference -> fetchable URL ('' when it can't be one).
    Mirrors the agent's resolution: absolute http(s) as-is, '/path' onto the
    LMS file base. No allowlist here — this is a read-only staff probe."""
    ref = (ref or "").strip()
    if not ref:
        return ""
    if ref.startswith(("http://", "https://")):
        return ref
    if ref.startswith("/"):
        return LMS_FILE_BASE_URL.rstrip("/") + quote(ref, safe="/-._~")
    return ""


def probe_url(url: str, timeout: int = 15) -> tuple:
    """(status, verdict). status is an HTTP code or an error word."""
    req = urllib.request.Request(url, headers={
        "Range": "bytes=0-255", "User-Agent": "AiRev-staff-probe/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(256)
            code = getattr(resp, "status", 200)
            if body:
                return (code, "FILE EXISTS — agent-side reading gap. "
                              "Do NOT message this student; we fix the reader.")
            return (code, "empty response — file may be zero bytes")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return (e.code, "file missing on server — student must re-upload")
        if e.code in (401, 403):
            return (e.code, "access blocked by server — check LMS config (our side)")
        return (e.code, f"server error {e.code} — probe again later")
    except Exception as e:
        return ("error", f"unreachable ({type(e).__name__}) — probe again later")


FIELDS = ["student_name", "student_id", "assignment_id", "assignment_title",
          "file_name", "typed_chars", "submitted_at",
          "who_fixes", "reason", "what_student_should_do",
          "probe_status", "probe_verdict"]


def apply_probe(report_row: dict, status, verdict: str) -> dict:
    """Fold a probe result in — and RECLASSIFY when it contradicts the note.

    A row stamped 'could not open your file' whose file probes 200-with-bytes
    is not the student's fault: the file is right there, our reader failed.
    Moving it off the messaging list is the whole point of probing.
    """
    report_row["probe_status"] = status
    report_row["probe_verdict"] = verdict
    if verdict.startswith("FILE EXISTS"):
        report_row["who_fixes"] = "US (file exists — agent could not read it)"
        report_row["what_student_should_do"] = ("Nothing — the file is fine; "
                                                "we will fix our reader.")
    return report_row


def to_report_row(row: dict) -> dict:
    who, reason, action = classify(row)
    return {
        "student_name": row.get("student_name") or "",
        "student_id": row.get("student_id"),
        "assignment_id": row.get("assignment_id"),
        "assignment_title": row.get("assignment_title") or "",
        "file_name": row.get("file_name") or "",
        "typed_chars": len((row.get("notes") or "").strip()),
        "submitted_at": row.get("submitted_at") or "",
        "who_fixes": who,
        "reason": reason,
        "what_student_should_do": action,
        "probe_status": "",
        "probe_verdict": "",
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Ungraded submissions with reasons (read-only; one CSV).")
    ap.add_argument("--assignment-id", type=int, action="append",
                    help="repeatable; default is every assignment")
    ap.add_argument("--out", default="problem_report.csv")
    ap.add_argument("--probe", action="store_true",
                    help="HTTP-check every failing file: does it actually "
                         "exist? Distinguishes student-side (file gone) from "
                         "agent-side (file exists, reader failed).")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:\n"
                 "  cmd:  set AIREV_DB_URL=mysql://user:pass@host:port/db")

    conn = connect(db_url)
    try:
        join_sql, name_expr = pick_name_source(conn)
        sql = f"""
            SELECT {name_expr}           AS student_name,
                   s.student_id          AS student_id,
                   s.assignment_id       AS assignment_id,
                   COALESCE(a.title,'')  AS assignment_title,
                   s.file_name           AS file_name,
                   s.file_path           AS file_path,
                   s.notes               AS notes,
                   s.feedback            AS feedback,
                   s.submitted_at        AS submitted_at
            FROM assignment_submissions s
            LEFT JOIN assignments a ON a.id = s.assignment_id
            {join_sql}
            WHERE s.grade IS NULL
        """
        params: list = []
        if args.assignment_id:
            sql += (" AND s.assignment_id IN ("
                    + ",".join(["%s"] * len(args.assignment_id)) + ")")
            params.extend(args.assignment_id)
        sql += " ORDER BY s.assignment_id, s.student_id"
        with conn.cursor() as cur:
            cur.execute(sql, tuple(params))
            raw = list(cur.fetchall() or [])
    finally:
        conn.close()

    rows = []
    for r in raw:
        rep = to_report_row(r)
        if args.probe:
            url = resolve_file_url(r.get("file_path") or "")
            if url:
                status, verdict = probe_url(url)
                rep = apply_probe(rep, status, verdict)
                print(f"  probe student {rep['student_id']:<6} "
                      f"[{status}] {verdict[:70]}")
        rows.append(rep)

    student_side = [r for r in rows if r["who_fixes"] == ACT_STUDENT]
    ours = [r for r in rows if r["who_fixes"] != ACT_STUDENT]

    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        # Student-side problems first — that is the messaging list.
        for r in student_side + ours:
            w.writerow(r)

    print(f"\nUngraded submissions             : {len(rows)}")
    print(f"STUDENT must act (message these) : {len(student_side)}")
    print(f"US must act (re-run first)       : {len(ours)}")
    if student_side:
        print("\nStudents to message (first 20):")
        for r in student_side[:20]:
            print(f"  {str(r['student_name'] or '')[:24]:<26} id {r['student_id']:<6} "
                  f"assignment {r['assignment_id']:<4} — {r['reason'][:60]}")
        if len(student_side) > 20:
            print(f"  ... and {len(student_side) - 20} more — see {args.out}")
    print(f"\nFile: {args.out}")
    print("Nothing was modified. This script only reads.\n")


if __name__ == "__main__":
    main()
