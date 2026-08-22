#!/usr/bin/env python3
"""
show_review.py — print the stored review for one or more students, exactly
as it sits in the database.

STRICTLY READ-ONLY. One SELECT, no AI, nothing written.

Why (22 Aug, Day 04): eleven students carry a bare "You scored 0 out of 10."
with no reasons, no rubric rows and no pointers — a mark with nothing behind
it, which the scoring rule forbids outright. Patching that blind would be
guessing. This shows what was actually stored, so the fix answers the real
defect.

    set AIREV_DB_URL=...paste your value here...
    python tools\\show_review.py --assignment-id 21 --students 473,1311,687
    python tools\\show_review.py --assignment-id 21 --empty-feedback-only

--empty-feedback-only finds them for you: every graded row whose feedback
carries a score but no substantiation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from submission_report import connect


def as_dict(raw) -> dict:
    """Stored feedback -> dict. Never raises. Pure."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            out = json.loads(raw)
            return out if isinstance(out, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def is_unsubstantiated(fb: dict) -> bool:
    """A score with nothing behind it: no rubric rows, no feedback points,
    no hard truth, no detailed text. Pure — this is the exact shape of the
    eleven Day-04 rows that read only 'You scored 0 out of 10.'"""
    if not fb:
        return False
    has_mark = fb.get("scoreMarks") is not None or fb.get("score") is not None
    substance = (fb.get("rubricScores") or fb.get("rubricBreakdown")
                 or fb.get("feedbackPoints") or fb.get("hardTruth")
                 or fb.get("detailedFeedback") or fb.get("improvements"))
    return bool(has_mark) and not substance


def outline(fb: dict) -> list:
    """The keys that matter, with sizes — what IS there, not a wall of JSON."""
    interesting = ("grade", "score", "scoreMarks", "outOf", "summary",
                   "hardTruth", "aiLikelihoodPercent", "notGraded", "message")
    lines = []
    for k in interesting:
        if k in fb:
            lines.append(f"      {k:<20} {str(fb[k])[:110]}")
    for k in ("rubricScores", "rubricBreakdown", "feedbackPoints",
              "improvements", "strengths"):
        v = fb.get(k)
        if isinstance(v, list):
            lines.append(f"      {k:<20} {len(v)} item(s)"
                         + (f"  e.g. {str(v[0])[:80]}" if v else "  <-- EMPTY"))
    extra = sorted(set(fb) - set(interesting) - {"rubricScores", "rubricBreakdown",
                                                 "feedbackPoints", "improvements",
                                                 "strengths"})
    if extra:
        lines.append(f"      (other keys: {', '.join(extra)[:150]})")
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="Print stored reviews (read-only).")
    ap.add_argument("--assignment-id", type=int, required=True)
    ap.add_argument("--students", default="", help="comma-separated student ids")
    ap.add_argument("--empty-feedback-only", action="store_true",
                    help="only rows with a mark and no substantiation")
    ap.add_argument("--full", action="store_true", help="dump the whole JSON")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:  set AIREV_DB_URL=...paste your value here...")

    wanted = {int(s) for s in args.students.replace(" ", "").split(",") if s}

    conn = connect(db_url)
    with conn.cursor() as cur:
        cur.execute("""
            SELECT id, student_id, grade, feedback, status, submitted_at,
                   CHAR_LENGTH(COALESCE(notes, '')) AS notes_len,
                   COALESCE(file_path, '') AS file_ref
            FROM assignment_submissions
            WHERE assignment_id = %s
            ORDER BY submitted_at DESC, id DESC""", (args.assignment_id,))
        raw = cur.fetchall() or []
    conn.close()

    latest: dict = {}
    for r in raw:
        latest.setdefault(r["student_id"], r)

    shown = 0
    for sid, r in sorted(latest.items()):
        if wanted and sid not in wanted:
            continue
        fb = as_dict(r.get("feedback"))
        if args.empty_feedback_only and not is_unsubstantiated(fb):
            continue
        shown += 1
        print(f"\n=== student {sid}  submission {r['id']}  grade={r['grade']} "
              f"status={r.get('status')} ===")
        print(f"    typed {r['notes_len']} chars | file: {r['file_ref'][:60] or '-'}")
        if not fb:
            print("    (no stored feedback at all)")
            continue
        if args.full:
            print(json.dumps(fb, indent=2, ensure_ascii=False)[:4000])
        else:
            print("\n".join(outline(fb)))

    if not shown:
        print("No matching rows." if wanted else
              "No row carries a mark without substantiation. Nothing to fix.")
    else:
        print(f"\n{shown} row(s) shown. Nothing was modified. This script only reads.")


if __name__ == "__main__":
    main()
