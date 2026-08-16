#!/usr/bin/env python3
"""
dedupe_submissions.py — remove the duplicate rows a correction run created.

DRY RUN BY DEFAULT. Nothing is deleted until --run is passed, and even then
every row is copied to a backup table FIRST, in the same transaction.

WHY THIS EXISTS. /api/review/submit-assignment INSERTs a new submission row on
every call. That is correct for a learner re-attempting, and wrong for a staff
re-score: on 14 Aug assignment 14 went from 49 rows to 95 in one run.
bulk_review.py --redo now routes to the in-place regrade endpoint so it cannot
happen again — this tool cleans up what the earlier runs left.

WHAT IT KEEPS. The NEWEST row per (assignment_id, student_id). That row holds
the most recent review, so keeping it preserves the current mark; the older
rows are the superseded copies.

WHAT IT REFUSES. A row carrying a grade that the newest row does not have is
never silently dropped — if the newest row is ungraded and an older one is
graded, the pair is reported and SKIPPED, because deleting a real mark to keep
an empty one is the one outcome nobody wants.

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname

    python tools/dedupe_submissions.py --assignment-id 14
    python tools/dedupe_submissions.py --assignment-id 14 --run
    python tools/dedupe_submissions.py                       # every assignment
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


# ---------- pure decision logic (unit-tested; no DB) ------------------------

def plan(rows: list) -> dict:
    """Decide what to keep, drop and skip. Pure — the whole risk lives here.

    rows: [{id, assignment_id, student_id, grade}] for ONE (assignment,
    student), any order.

    Returns {"keep": id, "drop": [ids], "skip_reason": str or None}.

    The newest id wins because it holds the latest review. The one exception is
    the case that would destroy a real mark: if the row we would keep has no
    grade while an older one does, nothing is dropped and the pair is reported.
    """
    if len(rows) < 2:
        return {"keep": rows[0]["id"] if rows else None, "drop": [], "skip_reason": None}

    ordered = sorted(rows, key=lambda r: int(r["id"]), reverse=True)
    keep, older = ordered[0], ordered[1:]

    graded_older = [r for r in older if r.get("grade") is not None]
    if keep.get("grade") is None and graded_older:
        return {"keep": keep["id"], "drop": [],
                "skip_reason": (f"newest row {keep['id']} has no grade while older "
                                f"row(s) {[r['id'] for r in graded_older]} do — "
                                f"re-review before removing anything")}

    return {"keep": keep["id"], "drop": [r["id"] for r in older], "skip_reason": None}


def group_by_learner(rows: list) -> dict:
    out = {}
    for r in rows:
        out.setdefault((r["assignment_id"], r["student_id"]), []).append(r)
    return out


def backup_table_name(stamp: str) -> str:
    """Timestamp supplied by the caller — never generated here, so the name is
    deterministic in tests and visible in the log before anything runs."""
    safe = "".join(c for c in stamp if c.isalnum() or c == "_")
    return f"airev_dupe_backup_{safe}"


# ---------- database work ---------------------------------------------------

def fetch_duplicates(conn, assignment_id=None) -> list:
    sql = f"""
        SELECT s.id, s.assignment_id, s.student_id, s.grade, s.submitted_at
          FROM {TABLE} s
          JOIN (SELECT assignment_id, student_id
                  FROM {TABLE}
                 GROUP BY assignment_id, student_id
                HAVING COUNT(*) > 1) d
            ON d.assignment_id = s.assignment_id AND d.student_id = s.student_id
    """
    params = ()
    if assignment_id:
        sql += " WHERE s.assignment_id = %s"
        params = (assignment_id,)
    sql += " ORDER BY s.assignment_id, s.student_id, s.id DESC"
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return list(cur.fetchall() or [])


def execute_plan(conn, drop_ids: list, backup: str) -> int:
    """Copy to a backup table, then delete — one transaction, backup first.

    CREATE TABLE ... LIKE then INSERT ... SELECT, never CREATE TABLE AS SELECT:
    Aiven runs with sql_require_primary_key=ON and rejects the latter (error
    3750). Learned the hard way on this database.
    """
    if not drop_ids:
        return 0
    placeholders = ",".join(["%s"] * len(drop_ids))
    with conn.cursor() as cur:
        cur.execute(f"CREATE TABLE IF NOT EXISTS {backup} LIKE {TABLE}")
        cur.execute(f"INSERT INTO {backup} SELECT * FROM {TABLE} "
                    f"WHERE id IN ({placeholders})", tuple(drop_ids))
        cur.execute(f"SELECT COUNT(*) AS n FROM {backup} "
                    f"WHERE id IN ({placeholders})", tuple(drop_ids))
        saved = cur.fetchone()["n"]
        if saved != len(drop_ids):
            conn.rollback()
            raise SystemExit(f"ABORTED: backed up {saved} of {len(drop_ids)} rows. "
                             f"Nothing deleted.")
        cur.execute(f"DELETE FROM {TABLE} WHERE id IN ({placeholders})", tuple(drop_ids))
        removed = cur.rowcount
    conn.commit()
    return removed


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Remove duplicate submission rows (dry run unless --run).")
    ap.add_argument("--assignment-id", type=int)
    ap.add_argument("--run", action="store_true", help="actually delete")
    ap.add_argument("--stamp", default="", help="backup table suffix (default: today)")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:\n"
                 "  cmd:  set AIREV_DB_URL=mysql://user:pass@host:port/db")

    stamp = args.stamp or __import__("datetime").date.today().isoformat().replace("-", "")
    backup = backup_table_name(stamp)

    conn = connect(db_url)
    try:
        rows = fetch_duplicates(conn, args.assignment_id)
        groups = group_by_learner(rows)

        drop_ids, skipped = [], []
        for (aid, sid), members in sorted(groups.items()):
            decision = plan(members)
            if decision["skip_reason"]:
                skipped.append((aid, sid, decision["skip_reason"]))
                continue
            drop_ids.extend(decision["drop"])

        print(f"\nLearners with duplicates : {len(groups)}")
        print(f"Rows that would be removed: {len(drop_ids)}")
        print(f"Backup table              : {backup}")
        if skipped:
            print(f"\nSKIPPED — a grade would be lost ({len(skipped)}):")
            for aid, sid, why in skipped[:20]:
                print(f"  assignment {aid} student {sid}: {why}")

        for (aid, sid), members in sorted(groups.items())[:10]:
            d = plan(members)
            print(f"  assignment {aid} student {sid}: keep {d['keep']}, "
                  f"drop {d['drop'] or '-'}")
        if len(groups) > 10:
            print(f"  ... and {len(groups) - 10} more learners")

        if not args.run:
            print("\nDRY RUN — nothing deleted. Re-run with --run to execute.\n")
            return

        removed = execute_plan(conn, drop_ids, backup)
        print(f"\nRemoved {removed} rows. Every one is in {backup}.")
        print(f"To undo:  INSERT INTO {TABLE} SELECT * FROM {backup};\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
