#!/usr/bin/env python3
"""
wrong_reviews.py — which students hold a mark that cannot be defended.

STRICTLY READ-ONLY. One SELECT per assignment, no AI, nothing written.

Why this exists (Ranjana, 23 Aug): "if we are do re-review do only those
students who gets wrong reviews not for all for previous review like day-1,
3, 4, 5 like that."

Re-running a whole day costs money, costs hours, and disturbs marks that were
already fair. This finds — from the stored evidence alone, never from a guess
— exactly which rows are untrustworthy and says WHY for each one. What comes
out is a student list narrow enough to re-run with confidence.

    set AIREV_DB_URL=...paste your value here...
    python tools\\wrong_reviews.py --assignment-id 21
    python tools\\wrong_reviews.py --assignment-id 21 --ids-only

The four faults it can prove, each one a live incident:

  MARKED BLIND     the row carries a grade while its own record says the
                   page or file could not be read. 21 Day-04 rows scored
                   0.0-4.7 this way, for pages nobody ever opened.

  NO SUBSTANCE     a score with nothing behind it — no reasons, no evidence,
                   no improvements. The 11 bare Day-04 zeros, and student
                   220 on Day 07.

  PLACEHOLDER      the per-requirement note says "No assessment available"
                   while a number sits beside it. An absence of judgement
                   wearing the shape of one.

  OLD RULES        graded before the requirements engine replaced the
                   invented rubric, so the standard it was judged against
                   was never in the brief. Days 03, 04 and 05.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from results_report import resolve_identities
from show_review import as_dict
from submission_report import connect

# The rules version at which requirements replaced the invented rubric.
# Anything graded below this was judged against criteria the task never
# stated — see rubric_service.RUBRIC_VERSION history.
FIRST_TRUSTWORTHY_RULES = 6

MARKED_BLIND = "MARKED BLIND"
NO_SUBSTANCE = "NO SUBSTANCE"
PLACEHOLDER = "PLACEHOLDER"
OLD_RULES = "OLD RULES"

_PLACEHOLDER_NOTES = ("no assessment available", "not assessed", "unavailable")
_UNREAD_PHRASES = ("could not be read", "could not be retrieved",
                   "not rendered", "could not open", "did not open",
                   "not readable through automated access")


def _requirement_rows(fb: dict) -> list:
    """The per-requirement table, whichever vocabulary wrote it. Pure."""
    faculty = fb.get("facultyView") or {}
    return (faculty.get("requirements") or faculty.get("rubricScores")
            or fb.get("rubricScores") or [])


def has_mark(fb: dict, grade) -> bool:
    """Does this row actually carry a mark? Pure.

    A row the guard refused (grade NULL, notGraded) is the system working —
    it must never appear in a re-review list.
    """
    if fb.get("notGraded"):
        return False
    return grade is not None


def marked_blind(fb: dict) -> bool:
    """Graded while its own record says we could not read the work. Pure."""
    blob = " ".join(str(fb.get(k) or "") for k in
                    ("detailedFeedback", "summary", "message",
                     "garbageWarning", "wordCountMessage")).lower()
    audit = fb.get("audit") or {}
    blob += " " + str(audit.get("manifest") or "").lower()
    return any(p in blob for p in _UNREAD_PHRASES)


def no_substance(fb: dict) -> bool:
    """A score with nothing behind it. Pure."""
    substance = (_requirement_rows(fb) or fb.get("feedbackPoints")
                 or fb.get("hardTruth") or fb.get("detailedFeedback")
                 or fb.get("improvements") or fb.get("strengths"))
    return not substance


def has_placeholder_judgement(fb: dict) -> bool:
    """A number beside 'No assessment available'. Pure."""
    for row in _requirement_rows(fb):
        if not isinstance(row, dict):
            continue
        note = str(row.get("note") or row.get("judgment") or "").strip().lower()
        if any(note.startswith(p) for p in _PLACEHOLDER_NOTES):
            return True
    return False


def graded_under_old_rules(fb: dict) -> bool:
    """Judged against criteria the task never stated. Pure.

    A review written before the audit record existed carries no version at
    all — and everything from that era predates the requirements engine, so
    the absence IS the evidence.
    """
    audit = fb.get("audit") or {}
    version = audit.get("rulesVersion", audit.get("rubricVersion"))
    if version is None:
        return True
    try:
        return int(version) < FIRST_TRUSTWORTHY_RULES
    except (TypeError, ValueError):
        return True


def verdicts_for(fb: dict, grade) -> list:
    """Every fault this row can be PROVEN to have. Pure.

    Ordered most-serious first, so a caller taking one reason takes the worst.
    Returns [] for a row that is fine, and for any row that carries no mark —
    there is nothing to re-review when nothing was written.
    """
    if not has_mark(fb, grade):
        return []
    found = []
    if marked_blind(fb):
        found.append(MARKED_BLIND)
    if no_substance(fb):
        found.append(NO_SUBSTANCE)
    if has_placeholder_judgement(fb):
        found.append(PLACEHOLDER)
    if graded_under_old_rules(fb):
        found.append(OLD_RULES)
    return found


def graded_rows(conn, assignment_id: int) -> list:
    """Latest attempt per student that actually carries a mark."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.id, s.student_id, s.grade, s.feedback, s.submitted_at
            FROM assignment_submissions s
            WHERE s.assignment_id = %s
            ORDER BY s.submitted_at DESC, s.id DESC""", (assignment_id,))
        raw = cur.fetchall() or []
    latest: dict = {}
    for row in raw:
        latest.setdefault(row["student_id"], row)
    return sorted(latest.values(), key=lambda r: r["student_id"])


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Which students hold an indefensible mark (read-only).")
    ap.add_argument("--assignment-id", type=int, required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--ids-only", action="store_true",
                    help="print just the comma-separated student ids")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:  set AIREV_DB_URL=...paste your value here...")

    conn = connect(db_url)
    rows = graded_rows(conn, args.assignment_id)
    suspect = []
    for row in rows:
        reasons = verdicts_for(as_dict(row["feedback"]), row["grade"])
        if reasons:
            suspect.append((row, reasons))
    who = resolve_identities(conn, [r["student_id"] for r, _ in suspect])
    conn.close()

    if args.ids_only:
        print(",".join(str(r["student_id"]) for r, _ in suspect))
        return

    print(f"\nAssignment {args.assignment_id}: {len(rows)} student(s) hold a "
          f"mark; {len(suspect)} of those marks cannot be defended.\n")
    if not suspect:
        print("Nothing to re-review. Every stored mark has evidence behind it "
              "and was graded under the current rules.")
        return

    by_reason: dict = {}
    for row, reasons in suspect:
        by_reason.setdefault(reasons[0], []).append(row["student_id"])
    for reason, ids in by_reason.items():
        print(f"  {reason:<14} {len(ids):>4} student(s)")

    out_path = args.out or f"wrong_reviews_{args.assignment_id}.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["student_name", "email", "student_id", "submission_id",
                    "grade", "why_it_cannot_be_defended"])
        for row, reasons in suspect:
            person = who.get(row["student_id"], {})
            w.writerow([person.get("name", ""), person.get("email", ""),
                        row["student_id"], row["id"], row["grade"],
                        " + ".join(reasons)])

    untouched = len(rows) - len(suspect)
    print(f"\n  {untouched} mark(s) are sound and will NOT be touched.")
    print(f"\nFile: {out_path}")
    print("\nRe-review only these, in place:")
    print(f"  python tools\\bulk_review.py --assignment-id {args.assignment_id} "
          f"--redo --force --run --limit 500 --concurrency 3")
    print("\nNothing was modified. This script only reads.")


if __name__ == "__main__":
    main()
