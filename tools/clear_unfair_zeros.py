#!/usr/bin/env python3
"""
clear_unfair_zeros.py — undo scores that the agent never actually earned the
right to give, so those submissions can be reviewed properly instead.

What it targets (auto-zeros): reviews written by the old "under 30 words =
0/100" short-circuit. Those never involved an AI call — the student was failed
by a length rule. Their signature in the DB: grade 0 AND feedback JSON
containing "very short" / "Not analysed (too short)".

What it does NOT touch by default: genuine AI reviews that scored low. Those
were real judgments. Use --include-ai-zeros only after reading them yourself,
and only if you have decided the rubric was wrong for that assignment.

Clearing sets grade=NULL, feedback=NULL, status='submitted' — no score is
invented, the attempt simply returns to the queue for a real review.

DRY RUN by default.

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname

    python tools/clear_unfair_zeros.py                    # show what qualifies
    python tools/clear_unfair_zeros.py --run              # clear auto-zeros
    python tools/clear_unfair_zeros.py --assignment-id 17 --run
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from urllib.parse import urlparse, unquote

try:
    import pymysql
except ImportError:
    sys.exit("Missing dep. Run:  pip install pymysql")

# Fingerprints of the length-rule auto-zero (no AI call was ever made).
AUTO_ZERO_MARKERS = ("very short", "Not analysed (too short)", "too short to evaluate")

# Only agent-written reviews carry this marker (set by review_payload.build()
# for BOTH assignments and case studies since 12 Aug 2026). A grade a human
# typed in has no such marker and is NEVER touched by this tool.
#
# BLAST RADIUS, read before running: case-study reviews written before
# 12 Aug 2026 have no marker, so this tool skips them. Reviews written after
# it DO carry the marker and --all-agent-reviews will clear them. That is the
# intended behaviour, but it changed — a --type casestudy run that matched
# nothing last week can match every recent row today. Dry-run first.
AGENT_MARKERS = ('"reviewedBy": "ai"', '"reviewedBy":"ai"')

TABLES = {
    "assignment": {"table": "assignment_submissions", "fk": "assignment_id",
                   "item_table": "assignments"},
    "casestudy":  {"table": "case_study_submissions", "fk": "case_study_id",
                   "item_table": "case_studies"},
}


def connect(db_url: str):
    u = urlparse(db_url)
    if not u.hostname:
        sys.exit("Could not parse AIREV_DB_URL")
    return pymysql.connect(
        host=u.hostname, port=u.port or 3306,
        user=unquote(u.username or ""), password=unquote(u.password or ""),
        database=(u.path or "/").lstrip("/"),
        cursorclass=pymysql.cursors.DictCursor, connect_timeout=20,
        ssl={"ssl": {}} if "aivencloud" in (u.hostname or "") else None,
    )


def find_zeros(conn, kind: str, item_id: int | None, max_score: int,
               include_ai: bool) -> list[dict]:
    """Low/zero-scored rows, tagged with whether an AI ever looked at them."""
    cfg = TABLES[kind]
    sql = f"""
        SELECT s.id, s.{cfg['fk']} AS item_id, s.student_id, s.grade,
               s.status, s.submitted_at, COALESCE(i.title,'') AS title,
               CAST(s.feedback AS CHAR) AS feedback
        FROM {cfg['table']} s
        JOIN {cfg['item_table']} i ON i.id = s.{cfg['fk']}
        WHERE s.grade IS NOT NULL AND s.grade <= %s
    """
    params: list = [max_score]
    if item_id:
        sql += f" AND s.{cfg['fk']} = %s"
        params.append(item_id)
    sql += " ORDER BY s.submitted_at DESC"

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    out = []
    skipped_human = 0
    for r in rows:
        # Belt-and-braces: this tool erases grades, so the score ceiling is
        # enforced in code as well as in SQL. A passing score must never be
        # reachable here, whatever the query returned.
        grade = r.get("grade")
        if grade is None or float(grade) > max_score:
            continue
        fb = r.get("feedback") or ""
        if not any(m in fb for m in AGENT_MARKERS):
            skipped_human += 1      # human-graded — leave it completely alone
            continue
        r["auto_zero"] = any(m in fb for m in AUTO_ZERO_MARKERS)
        if r["auto_zero"] or include_ai:
            r["kind"] = kind
            out.append(r)
    if skipped_human:
        print(f"  protected: {skipped_human} {kind} grade(s) NOT written by the agent "
              f"(faculty-marked) — untouched")
    return out


def clear(conn, kind: str, ids: list[int]) -> int:
    """Remove the score only. No score is invented; the attempt re-queues."""
    if not ids:
        return 0
    cfg = TABLES[kind]
    marks = ",".join(["%s"] * len(ids))
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {cfg['table']} SET grade = NULL, feedback = NULL, "
            f"status = 'submitted' WHERE id IN ({marks})", tuple(ids))
    conn.commit()
    return len(ids)


def main() -> None:
    ap = argparse.ArgumentParser(description="Clear unearned zero scores (dry run by default).")
    ap.add_argument("--type", choices=["assignment", "casestudy", "both"], default="assignment")
    ap.add_argument("--assignment-id", type=int)
    ap.add_argument("--case-study-id", type=int)
    ap.add_argument("--max-score", type=int, default=0,
                    help="treat scores <= this as candidates (default 0)")
    ap.add_argument("--include-ai-zeros", action="store_true",
                    help="ALSO clear low scores that came from a real AI review")
    ap.add_argument("--all-agent-reviews", action="store_true",
                    help="clear EVERY agent-written review at any score — use after a "
                         "scoring change invalidates past reviews")
    ap.add_argument("--out", default="cleared_zeros.csv")
    ap.add_argument("--run", action="store_true", help="actually clear them")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL (mysql://user:pass@host:port/dbname)")

    if args.all_agent_reviews:
        args.max_score = 100
        args.include_ai_zeros = True
        print("\nMODE: clearing EVERY agent-written review (any score). "
              "Faculty-entered grades are protected.")

    kinds = ["assignment", "casestudy"] if args.type == "both" else [args.type]
    conn = connect(db_url)
    found: list[dict] = []
    try:
        for k in kinds:
            item_id = args.assignment_id if k == "assignment" else args.case_study_id
            found.extend(find_zeros(conn, k, item_id, args.max_score,
                                    args.include_ai_zeros))

        auto = [r for r in found if r["auto_zero"]]
        ai   = [r for r in found if not r["auto_zero"]]

        print(f"\nScores <= {args.max_score} found: {len(found)}")
        print(f"  auto-zero (length rule, NO AI review) : {len(auto)}  <- always cleared")
        print(f"  scored by a real AI review            : {len(ai)}"
              f"  <- {'INCLUDED' if args.include_ai_zeros else 'left alone'}")

        show = found[:20]
        for r in show:
            tag = "AUTO-ZERO" if r["auto_zero"] else "ai-review"
            print(f"  [{tag}] {r['kind']} {r['item_id']} student {r['student_id']} "
                  f"grade={r['grade']} | {r['title'][:48]}")
        if len(found) > len(show):
            print(f"  ... and {len(found) - len(show)} more")

        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["kind", "submission_id", "item_id", "student_id",
                        "grade", "auto_zero", "title", "submitted_at"])
            for r in found:
                w.writerow([r["kind"], r["id"], r["item_id"], r["student_id"],
                            r["grade"], r["auto_zero"], r["title"], r["submitted_at"]])
        print(f"\nFull list written to {args.out}")

        if not args.run:
            print("DRY RUN — nothing changed. Re-run with --run to clear.\n")
            return

        total = 0
        for k in kinds:
            ids = [r["id"] for r in found if r["kind"] == k]
            total += clear(conn, k, ids)
        print(f"\nCleared {total} score(s). They are back in the review queue.")
        print("Re-run bulk_review.py to review them properly.\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
