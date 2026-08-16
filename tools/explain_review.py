#!/usr/bin/env python3
"""
explain_review.py — why did THIS submission get THAT mark?

STRICTLY READ-ONLY. SELECT statements only. Never updates, deletes or commits.

A score on its own cannot be argued with. This prints the four things that
produced it, side by side:

  1. WHAT THE TASK ASKED   — the assignment's own title and description
  2. WHAT THE LEARNER SENT — the stored submission text, as the marker saw it
  3. WHAT EACH CRITERION EARNED — the rubric breakdown with the marks
  4. WHICH GATES FIRED     — the deterministic caps, and what they cut

Gates are the usual answer to "why is this so low". They are caps applied in
code, not model opinion:

    no_evidence      a criterion with zero supporting quotes is capped at 20%
    generic_answer   an application-type criterion with no specifics, capped 40%
    concept_coverage below half the must-cover concepts, TOTAL capped at 69
    factual errors   fixed deductions by severity

If the gates are quiet and the marks are still near zero, the answer genuinely
did not address the task — which is worth knowing too, because on 14 Aug the
likeliest explanation for assignment 14 was learners submitting Day 01's OTHER
task by mistake.

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname

    python tools/explain_review.py --assignment-id 14 --student-id 201
    python tools/explain_review.py --assignment-id 14 --student-id 201 --full
    python tools/explain_review.py --submission-id 3801
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from urllib.parse import unquote, urlparse

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    sys.exit("Missing dependency. Run:  pip install pymysql")


PREVIEW_CHARS = 1200
RULE = "─" * 78


# ---------- connection (same shape as the other tools) ----------------------

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


# ---------- pure helpers (unit-tested; no DB, no network) -------------------

def parse_feedback(blob) -> dict:
    """Feedback is stored as a JSON string. Unreadable is a finding, not a
    crash — a row whose payload will not parse is exactly the kind of row
    someone is asking about."""
    if not blob:
        return {}
    if isinstance(blob, dict):
        return blob
    try:
        parsed = json.loads(blob)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def describe_gates(gates: list) -> list:
    """One plain line per gate, naming what it cut and by how much."""
    out = []
    for g in gates or []:
        if not isinstance(g, dict):
            continue
        name = g.get("gate", "?")
        where = g.get("criterion", "?")
        frm, to = g.get("from"), g.get("to")
        detail = g.get("detail", "")
        line = f"{name:18} {where:42.42}"
        if frm is not None and to is not None:
            line += f" {frm}% -> {to}%"
        if detail:
            line += f"   ({detail})"
        out.append(line)
    return out


def verdict(marks, out_of, gates: list, answer_chars: int) -> str:
    """A one-line reading of the evidence, so the operator is not left to
    interpret four screens of output alone. States what the numbers support and
    nothing beyond it."""
    gate_names = {g.get("gate") for g in (gates or []) if isinstance(g, dict)}

    # ORDER MATTERS, and I got it wrong twice before the tests ran.
    # An empty submission was falling through to "no gate fired, the marker
    # judged it off-task" — which blames the learner for a row that holds
    # nothing. And an unreadable mark with a null out_of silently became 0%
    # rather than saying it could not be read.
    if not answer_chars:
        return "Nothing was stored for this submission — there was no text to mark."
    try:
        if marks is None or out_of in (None, 0):
            raise ValueError("no mark recorded")
        pct = (float(marks) / float(out_of)) * 100
    except (TypeError, ValueError):
        return "Could not read the mark — inspect the raw feedback below."

    if "no_evidence" in gate_names:
        return ("The marker found no quotable evidence for one or more criteria, "
                "so they were capped at 20%. Check whether the answer below "
                "actually addresses those criteria.")
    if "concept_coverage" in gate_names and pct <= 69:
        return ("Under half the must-cover concepts were addressed, so the TOTAL "
                "was capped at 69%. The mark below that cap came from the "
                "criteria themselves.")
    if pct < 20 and not gate_names:
        return ("No gate fired — this mark came from the criteria alone. The "
                "marker judged the answer as not addressing the task. Read the "
                "submission against the task text above before assuming a bug; "
                "learners do submit the wrong assignment.")
    return "Marks came from the criteria; gates listed above (if any) applied caps."


# ---------- reads -----------------------------------------------------------

SUBMISSION_SQL = """
    SELECT s.id, s.assignment_id, s.student_id, s.grade, s.status,
           s.submitted_at, s.notes, s.file_name,
           CAST(s.feedback AS CHAR) AS feedback,
           a.title, a.description, a.total_marks
      FROM assignment_submissions s
      JOIN assignments a ON a.id = s.assignment_id
"""


def fetch(conn, submission_id=None, assignment_id=None, student_id=None):
    if submission_id:
        sql, params = SUBMISSION_SQL + " WHERE s.id = %s", (submission_id,)
    else:
        sql = SUBMISSION_SQL + (" WHERE s.assignment_id = %s AND s.student_id = %s"
                                " ORDER BY s.id DESC LIMIT 1")
        params = (assignment_id, student_id)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall() or []
    return rows[0] if rows else None


# ---------- report ----------------------------------------------------------

def report(row: dict, full: bool = False) -> None:
    fb = parse_feedback(row.get("feedback"))
    out_of = fb.get("outOf") or row.get("total_marks") or 100
    marks = fb.get("scoreMarks", row.get("grade"))
    answer = (row.get("notes") or "").strip()

    print(f"\n{RULE}")
    print(f"submission {row['id']}  ·  student {row['student_id']}  ·  "
          f"assignment {row['assignment_id']}  ·  status {row.get('status')}")
    print(RULE)

    print("\n1. WHAT THE TASK ASKED")
    print(f"   {row.get('title')}")
    desc = (row.get("description") or "").strip()
    print("   " + (desc if full else desc[:400] + ("..." if len(desc) > 400 else "")))

    print(f"\n2. WHAT THE LEARNER SENT  ({len(answer)} chars"
          + (f", file: {row['file_name']}" if row.get("file_name") else "") + ")")
    if not answer:
        print("   (nothing stored)")
    else:
        shown = answer if full else answer[:PREVIEW_CHARS]
        for line in shown.splitlines():
            print("   " + line)
        if not full and len(answer) > PREVIEW_CHARS:
            print(f"   ... {len(answer) - PREVIEW_CHARS} more chars (--full to see all)")

    print(f"\n3. WHAT EACH CRITERION EARNED   →  {marks}/{out_of}"
          f"   grade {fb.get('grade', '?')}")
    rubric = fb.get("rubricScores") or []
    if not rubric:
        print("   (no rubric breakdown stored)")
    for r in rubric:
        print(f"   {str(r.get('criteria') or r.get('name'))[:44]:44} "
              f"{r.get('score')}/{r.get('maxScore')}  ({r.get('percentage')}%)")
        judgment = (r.get("judgment") or "").strip()
        if judgment:
            print(f"      {judgment[:150]}")

    gates = (fb.get("_meta") or {}).get("pipeline", {}).get("gatesHit") or fb.get("gatesHit") or []
    print("\n4. WHICH GATES FIRED")
    lines = describe_gates(gates)
    print("   (none — the mark came from the criteria alone)" if not lines
          else "\n".join("   " + l for l in lines))

    how = fb.get("howYouScored")
    if how:
        print("\n   HOW IT WAS SCORED (as shown to the learner)")
        for line in str(how).splitlines():
            print("   " + line)

    print(f"\n>> {verdict(marks, out_of, gates, len(answer))}\n")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Explain one submission's mark (read-only; writes nothing).")
    ap.add_argument("--submission-id", type=int)
    ap.add_argument("--assignment-id", type=int)
    ap.add_argument("--student-id", type=int)
    ap.add_argument("--full", action="store_true",
                    help="print the whole answer and task, not a preview")
    args = ap.parse_args()

    if not args.submission_id and not (args.assignment_id and args.student_id):
        sys.exit("Give --submission-id, or both --assignment-id and --student-id.")

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:\n"
                 "  PowerShell:  $env:AIREV_DB_URL = 'mysql://user:pass@host:port/db'\n"
                 "  cmd:         set AIREV_DB_URL=mysql://user:pass@host:port/db")

    conn = connect(db_url)
    try:
        row = fetch(conn, args.submission_id, args.assignment_id, args.student_id)
    finally:
        conn.close()

    if not row:
        sys.exit("No submission found for that id / assignment+student pair.")
    report(row, full=args.full)
    print("Nothing was modified. This script only reads.\n")


if __name__ == "__main__":
    main()
