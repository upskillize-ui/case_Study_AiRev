#!/usr/bin/env python
"""
file_check.py — why a stored submission file could not be read. (READ-ONLY)

Day 07 (assignment 24, 23 Aug): forty-odd rows came back "no_readable_content"
and nobody could say why. Two completely different faults were hiding under
one label, and telling them apart by guessing has already cost a week.

    set AIREV_DB_URL=...paste your value here...
    python tools\\file_check.py --assignment-id 24
    python tools\\file_check.py --assignment-id 24 --limit 20

What it separates:

  NO LINK STORED   the row's file reference is the words "Link submission" —
                   a LABEL, not an address. The learner used the LMS link
                   option and the LMS saved the caption instead of the URL.
                   The agent is not failing to open it; there is nothing to
                   open. AN LMS BUG — no agent change can fix it.

  DOWNLOAD FAILED  a real file, a real address, and the server refused us
                   (403) or the file is gone (404). Ours or the LMS's.

  UNREADABLE       downloaded fine, and nothing came out. A corrupt file, a
                   password-protected PDF, a photo of nothing. Usually the
                   learner's to fix, and now we can say so.

  EMPTY ROW        no file and no typed text at all.

  READS FINE       it works — so this row's problem is somewhere else.

Nothing is written, nothing is graded, no student row is touched.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
# The repo ROOT too: this tool imports the agent's own reader (app.utils),
# which the other tools do not. Without it the script dies with
# "No module named 'app'" the first time anyone runs it.
sys.path.insert(0, os.path.dirname(_HERE))

from results_report import resolve_identities
from submission_report import connect

NO_LINK = "NO LINK STORED"
DOWNLOAD_FAILED = "DOWNLOAD FAILED"
UNREADABLE = "UNREADABLE"
EMPTY_ROW = "EMPTY ROW"
READS_FINE = "READS FINE"

# What the LMS writes when the learner picks its link option and no address
# is captured. Matched exactly, not loosely: a real file called
# "Link submission notes.pdf" must not be swept into the LMS-bug pile.
_LABEL_NOT_A_URL = {"link submission", "link", "url", "link submitted",
                    "submission link"}


def is_label_not_a_url(file_ref: str, file_name: str) -> bool:
    """Is the stored 'file' actually just a caption? Pure."""
    for value in (file_ref, file_name):
        text = (value or "").strip().lower()
        if text and text in _LABEL_NOT_A_URL:
            return True
    ref = (file_ref or "").strip()
    return bool(ref) and not ref.lower().startswith(("http://", "https://", "/"))


def verdict_for(file_ref: str, file_name: str, typed_chars: int,
                read_text: str, why: str) -> tuple:
    """(verdict, detail) for one row. Pure — the reading is done by the caller.

    Order matters: the LMS bug is checked FIRST, because a label that is not
    a URL will of course fail to download, and reporting that as a download
    failure sends everyone hunting the wrong fault.
    """
    if not (file_ref or file_name) and typed_chars <= 0:
        return EMPTY_ROW, "nothing was stored for this student at all"
    if (file_ref or file_name) and is_label_not_a_url(file_ref, file_name):
        return NO_LINK, (f'the stored reference is "{(file_name or file_ref)[:40]}" '
                         f"— a label, not an address. LMS side.")
    if read_text.strip():
        return READS_FINE, f"{len(read_text.split())} words read"
    low = (why or "").lower()
    if any(k in low for k in ("403", "404", "http", "download", "timeout",
                              "refused", "could not be retrieved")):
        return DOWNLOAD_FAILED, why or "the file could not be fetched"
    return UNREADABLE, why or "downloaded, but nothing could be read from it"


def rows_for(conn, assignment_id: int) -> list:
    """Latest attempt per student, with whatever file reference it holds."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT s.id, s.student_id, s.grade,
                   COALESCE(s.file_path, '') AS file_ref,
                   COALESCE(s.file_name, '') AS file_name,
                   CHAR_LENGTH(COALESCE(s.notes, '')) AS typed_chars
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
        description="Why stored submission files could not be read (read-only).")
    ap.add_argument("--assignment-id", type=int, required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    if not os.getenv("AIREV_DB_URL"):
        sys.exit("Set AIREV_DB_URL first:  set AIREV_DB_URL=...paste your value here...")

    from app.utils.file_extractor import extract_text_from_url

    conn = connect(os.getenv("AIREV_DB_URL"))
    rows = rows_for(conn, args.assignment_id)
    if args.limit:
        rows = rows[:args.limit]
    who = resolve_identities(conn, [r["student_id"] for r in rows])
    conn.close()

    print(f"\nAssignment {args.assignment_id}: checking {len(rows)} row(s). "
          f"Nothing is written.\n")

    tally: dict = {}
    results = []
    for i, row in enumerate(rows, 1):
        text, why = "", ""
        if row["file_ref"] and not is_label_not_a_url(row["file_ref"],
                                                      row["file_name"]):
            try:
                text, why = extract_text_from_url(row["file_ref"],
                                                  row["file_name"])
            except Exception as e:            # never let one row stop the sweep
                text, why = "", f"{type(e).__name__}: {e}"[:120]
        verdict, detail = verdict_for(row["file_ref"], row["file_name"],
                                      row["typed_chars"], text, why)
        tally[verdict] = tally.get(verdict, 0) + 1
        results.append((row, verdict, detail))
        print(f"  [{i}/{len(rows)}] student {row['student_id']:<6} "
              f"{verdict:<16} {detail[:64]}")

    out_path = args.out or f"file_check_{args.assignment_id}.csv"
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["student_name", "email", "student_id", "submission_id",
                    "file_name", "typed_chars", "verdict", "detail"])
        for row, verdict, detail in results:
            person = who.get(row["student_id"], {})
            w.writerow([person.get("name", ""), person.get("email", ""),
                        row["student_id"], row["id"], row["file_name"],
                        row["typed_chars"], verdict, detail])

    print(f"\nSummary:")
    for verdict, count in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {verdict}")
    if tally.get(NO_LINK):
        print(f"\n  The {tally[NO_LINK]} NO LINK STORED rows are an LMS bug: the "
              f"link option saved a caption\n  instead of the address. No agent "
              f"change can read a link that was never stored.\n  Those learners "
              f"must paste the URL into the answer box, or the LMS form must be "
              f"fixed.")
    print(f"\nFile: {out_path}")
    print("Nothing was modified. This script only reads.")


if __name__ == "__main__":
    main()
