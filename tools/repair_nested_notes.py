#!/usr/bin/env python3
"""
repair_nested_notes.py — un-nest submission rows a double re-review corrupted.

DRY RUN BY DEFAULT. Nothing is written until --run, and every affected row is
copied to a backup table first, in the same transaction.

WHAT WENT WRONG. The regrade route read a row's stored notes AND re-extracted
its attachment. For a row already assembled by a previous review, that wrapped
the whole thing again:

    run 1   notes = M1 + [ITEM 1: IMAGE / ocr]
                       + [ITEM 2: TYPED TEXT / the learner's words]

    run 2   notes = M2 + [ITEM 1: IMAGE / ocr AGAIN]
                       + [ITEM 2: TYPED TEXT / "M1 + ITEM 1 + ITEM 2"]

So the marker read our own provenance text as the learner's essay and saw the
attachment twice. Student 1126 went 6.8/10 -> 1.2/10 on identical input.

WHY THIS IS RECOVERABLE. Each layer wraps the previous one WHOLE, so the
innermost assembly is still intact and still correct. Everything from the LAST
manifest header to the end of the text is exactly what run 1 produced. Nothing
needs re-extracting and nothing needs the AI.

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname

    python tools/repair_nested_notes.py --assignment-id 14
    python tools/repair_nested_notes.py --assignment-id 14 --run
    python tools/repair_nested_notes.py                      # scan everything
"""

from __future__ import annotations

import argparse
import os
import sys
from urllib.parse import unquote, urlparse

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    sys.exit("Missing dependency. Run:  pip install pymysql")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.utils.submission_intake import MANIFEST_HEADER  # noqa: E402

TABLE = "assignment_submissions"


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


# ---------- pure repair logic (unit-tested; no DB) --------------------------

def nesting_depth(notes: str) -> int:
    """How many manifests are stacked. 0 or 1 means nothing to repair."""
    return (notes or "").count(MANIFEST_HEADER)


def repair(notes: str) -> str:
    """Return the innermost assembly — the one run 1 produced.

    Everything from the LAST manifest header onwards is the deepest wrapping,
    and because each layer nested the previous WHOLE, that tail is the original
    intact. Returns the input unchanged when there is nothing to repair, so the
    function is safe to call on every row.
    """
    text = notes or ""
    if nesting_depth(text) < 2:
        return text
    return text[text.rfind(MANIFEST_HEADER):].strip()


def is_repairable(notes: str) -> bool:
    """A row is repairable only if the repair actually recovers content.

    A stack of bare headers with no items would 'repair' to a manifest and
    nothing else, which is worse than leaving it alone for a human to look at.
    """
    if nesting_depth(notes) < 2:
        return False
    fixed = repair(notes)
    return bool(fixed) and len(fixed) < len(notes) and "=== ITEM" in fixed


def backup_table_name(stamp: str) -> str:
    safe = "".join(c for c in stamp if c.isalnum() or c == "_")
    return f"airev_nested_backup_{safe}"


# ---------- database work ---------------------------------------------------

def fetch_candidates(conn, assignment_id=None) -> list:
    sql = (f"SELECT id, assignment_id, student_id, notes FROM {TABLE} "
           f"WHERE notes LIKE %s")
    params = [f"%{MANIFEST_HEADER}%"]
    if assignment_id:
        sql += " AND assignment_id = %s"
        params.append(assignment_id)
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall() or [])


def apply_repairs(conn, fixes: list, backup: str) -> int:
    """fixes: [(id, repaired_notes)]. Backup first, verify, then update."""
    if not fixes:
        return 0
    ids = [i for i, _ in fixes]
    placeholders = ",".join(["%s"] * len(ids))
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {backup} LIKE {TABLE}")
        cur.execute(f"INSERT INTO {backup} SELECT * FROM {TABLE} "
                    f"WHERE id IN ({placeholders})", tuple(ids))
        cur.execute(f"SELECT COUNT(*) AS n FROM {backup} "
                    f"WHERE id IN ({placeholders})", tuple(ids))
        saved = cur.fetchone()["n"]
        if saved != len(ids):
            conn.rollback()
            raise SystemExit(f"ABORTED: backed up {saved} of {len(ids)} rows. "
                             f"Nothing changed.")
        for row_id, text in fixes:
            cur.execute(f"UPDATE {TABLE} SET notes = %s WHERE id = %s",
                        (text, row_id))
    conn.commit()
    return len(fixes)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Un-nest doubly-wrapped submission notes (dry run unless --run).")
    ap.add_argument("--assignment-id", type=int)
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--stamp", default="")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:\n"
                 "  cmd:  set AIREV_DB_URL=mysql://user:pass@host:port/db")

    stamp = args.stamp or __import__("datetime").date.today().isoformat().replace("-", "")
    backup = backup_table_name(stamp)

    conn = connect(db_url)
    try:
        rows = fetch_candidates(conn, args.assignment_id)
        nested = [r for r in rows if nesting_depth(r["notes"]) >= 2]
        fixable = [r for r in nested if is_repairable(r["notes"])]
        stuck = [r for r in nested if not is_repairable(r["notes"])]

        print(f"\nRows carrying a manifest : {len(rows)}")
        print(f"Nested (2+ manifests)    : {len(nested)}")
        print(f"Repairable               : {len(fixable)}")
        print(f"Backup table             : {backup}")
        if stuck:
            print(f"\nNOT repairable ({len(stuck)}) — left alone for a human:")
            for r in stuck[:10]:
                print(f"  submission {r['id']} (student {r['student_id']})")

        for r in fixable[:8]:
            before, after = len(r["notes"]), len(repair(r["notes"]))
            print(f"  submission {r['id']} student {r['student_id']}: "
                  f"depth {nesting_depth(r['notes'])}, {before} -> {after} chars")
        if len(fixable) > 8:
            print(f"  ... and {len(fixable) - 8} more")

        if not args.run:
            print("\nDRY RUN — nothing written. Re-run with --run to apply.\n")
            return

        fixes = [(r["id"], repair(r["notes"])) for r in fixable]
        n = apply_repairs(conn, fixes, backup)
        print(f"\nRepaired {n} rows. Originals are in {backup}.")
        print(f"To undo:  UPDATE {TABLE} t JOIN {backup} b ON b.id = t.id "
              f"SET t.notes = b.notes;\n")
        print("Now re-review these rows so their marks reflect the clean text:")
        print(f"  python tools/bulk_review.py --redo "
              f"--assignment-id {args.assignment_id or '<id>'} --run\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
