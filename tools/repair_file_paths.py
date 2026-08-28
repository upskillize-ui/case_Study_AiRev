#!/usr/bin/env python3
"""
repair_file_paths.py — give 314 students back the file the database lost.

WHY THIS EXISTS (28 Aug 2026)
-----------------------------
384 submissions across 16 days hold a FILE NAME with no FILE PATH. Every admin
screen and every tool shows a filename, because they read file_name; the review
route reads file_path and finds nothing. So the marker was handed a caption and
asked to judge an image it never received — it returned an empty review, the
grade guard correctly refused to write a mark, and the student was left
ungraded with no explanation. Those rows are a large share of the backlog.

The cause was a bare `file_path = VALUES(file_path)` in the LMS upsert: any
later write without a path — a text edit, a re-save, an upload that failed in
the browser — wrote NULL over a good path while file_name survived. Fixed at
source in student.js on 28 Aug; this tool repairs what was already lost.

314 of the 384 still carry a non-zero file_size, which means the upload DID
finish and only the URL was lost. Those files are still in Cloudinary. The LMS
uploads coursework as:

    folder    = upskillize/coursework
    public_id = cw_<users.id>_<epoch_ms>          (student.js:2445, 2546)

so the asset carries the learner's user id and the moment of upload. With the
file_size and file_type that survived on the row, that is three independent
keys to match a submission back to its file. This tool does that matching and
writes back ONLY file_path, ONLY where it is currently empty.

SAFETY
------
  * DRY RUN by default. Nothing is written until you pass --run.
  * The UPDATE carries `AND COALESCE(file_path,'') = ''`, so a row that has
    since regained a good path can never be overwritten by this tool.
  * No other column is touched. Not notes, not grade, not feedback, not status.
  * Cloudinary is only ever READ (Admin API, list resources).
  * Every decision is written to repair_file_paths.csv so the run is auditable
    before and after.

USAGE
-----
    set AIREV_DB_URL=...paste your value here...
    set CLOUDINARY_CLOUD_NAME=...
    set CLOUDINARY_API_KEY=...
    set CLOUDINARY_API_SECRET=...

    python tools\\repair_file_paths.py                    # dry run, all days
    python tools\\repair_file_paths.py --assignment-id 28 # one day
    python tools\\repair_file_paths.py --run              # write it
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from submission_report import connect                      # noqa: E402

try:
    import httpx
except ImportError:
    sys.exit("pip install httpx")

CLOUD_FOLDER = "upskillize/coursework"
# Cloudinary splits the library by resource_type and a prefix search must name
# one. Coursework can be any of the three: an image, a PDF or zip (raw), or a
# recording.
RESOURCE_TYPES = ("image", "raw", "video")
# How far apart the submission row and the Cloudinary asset may be and still be
# the same act. The upload happens seconds before the row is written; an hour
# is generous enough for a slow mobile upload and far too tight to collide with
# the same learner's NEXT day's work.
MAX_SKEW_SECONDS = 3600


# ── pure matching logic (unit-tested, no I/O) ──────────────────────────────

def _parse_ts(value) -> float:
    """Cloudinary sends ISO-8601 Z; MySQL sends a datetime. Both to epoch."""
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    text = str(value or "").strip().replace("Z", "+00:00")
    for form in (None, "%Y-%m-%d %H:%M:%S"):
        try:
            dt = (datetime.fromisoformat(text) if form is None
                  else datetime.strptime(text, form))
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).timestamp()
        except ValueError:
            continue
    return 0.0


def best_match(assets: list, file_size, submitted_at, file_name: str = ""):
    """Which Cloudinary asset is THIS submission's file? Pure.

    Ordered by how much the evidence proves, strongest first:
      1. exactly one asset of the right byte count — the bytes ARE the file
      2. several of that byte count — the one uploaded nearest the submission
      3. no byte match — nearest in time AND the same extension, within
         MAX_SKEW_SECONDS. A guess with two agreeing keys, or nothing.
    Returns (asset, reason) or (None, why_not).
    """
    if not assets:
        return None, "no assets for this learner"
    when = _parse_ts(submitted_at)

    def gap(a):
        return abs(_parse_ts(a.get("created_at")) - when)

    size = int(file_size or 0)
    if size > 0:
        exact = [a for a in assets if int(a.get("bytes") or 0) == size]
        if len(exact) == 1:
            return exact[0], "exact size"
        if exact:
            return min(exact, key=gap), "exact size, nearest in time"

    ext = (file_name or "").rsplit(".", 1)[-1].lower() if "." in (file_name or "") else ""
    near = [a for a in assets if gap(a) <= MAX_SKEW_SECONDS]
    if ext:
        same_ext = [a for a in near
                    if str(a.get("format", "")).lower() == ext]
        if same_ext:
            return min(same_ext, key=gap), "same extension, nearest in time"
    if near and not size:
        return min(near, key=gap), "nearest in time (no size recorded)"
    return None, "no asset within an hour of the submission"


# ── I/O ────────────────────────────────────────────────────────────────────

def orphans(conn, assignment_id=None) -> list:
    """Rows holding a file NAME but no file PATH. Read-only."""
    sql = """
        SELECT s.id, s.assignment_id, s.student_id, s.file_name, s.file_size,
               s.file_type, s.submitted_at, s.grade,
               COALESCE(a.title, '')  AS title,
               COALESCE(st.user_id, 0) AS user_id
        FROM assignment_submissions s
        JOIN assignments a  ON a.id = s.assignment_id
        LEFT JOIN students st ON st.id = s.student_id
        WHERE COALESCE(s.file_name, '') <> ''
          AND COALESCE(s.file_path, '') = ''
          AND COALESCE(s.status, '')   <> 'draft'
    """
    params: list = []
    if assignment_id:
        sql += " AND s.assignment_id = %s"
        params.append(assignment_id)
    sql += " ORDER BY s.assignment_id, s.id"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return cur.fetchall() or []


def cloudinary_assets(client, cloud: str, user_id: int) -> list:
    """Every coursework asset this learner ever uploaded. READ ONLY."""
    found: list = []
    prefix = f"{CLOUD_FOLDER}/cw_{user_id}_"
    for rtype in RESOURCE_TYPES:
        cursor = None
        while True:
            params = {"type": "upload", "prefix": prefix, "max_results": 500}
            if cursor:
                params["next_cursor"] = cursor
            try:
                r = client.get(
                    f"https://api.cloudinary.com/v1_1/{cloud}/resources/{rtype}",
                    params=params, timeout=30)
            except Exception as e:
                print(f"   ! cloudinary {rtype}: {type(e).__name__}")
                break
            if r.status_code != 200:
                if r.status_code in (401, 403):
                    sys.exit(f"Cloudinary rejected the credentials "
                             f"(HTTP {r.status_code}). Check CLOUDINARY_API_KEY "
                             f"and CLOUDINARY_API_SECRET.")
                break
            data = r.json()
            found.extend(data.get("resources") or [])
            cursor = data.get("next_cursor")
            if not cursor:
                break
    return found


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--assignment-id", type=int)
    ap.add_argument("--run", action="store_true",
                    help="actually write file_path (default is a dry run)")
    ap.add_argument("--out", default="repair_file_paths.csv")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first.")
    cloud = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    key = os.getenv("CLOUDINARY_API_KEY", "")
    secret = os.getenv("CLOUDINARY_API_SECRET", "")
    if not (cloud and key and secret):
        sys.exit("Set CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and "
                 "CLOUDINARY_API_SECRET (the same values the LMS uses).")

    conn = connect(db_url)
    rows = orphans(conn, args.assignment_id)
    if not rows:
        print("Nothing to repair — no row holds a file name without a path.")
        return

    print(f"{len(rows)} submission(s) hold a file name but no path.")
    print("DRY RUN — nothing will be written.\n" if not args.run
          else "WRITING file_path where it is currently empty.\n")

    client = httpx.Client(auth=(key, secret))
    by_user: dict = {}
    out, repaired, unmatched = [], 0, 0

    for r in rows:
        uid = int(r.get("user_id") or 0)
        if not uid:
            out.append({**_row_out(r), "result": "no users.id for this student"})
            unmatched += 1
            continue
        if uid not in by_user:
            by_user[uid] = cloudinary_assets(client, cloud, uid)
        asset, why = best_match(by_user[uid], r.get("file_size"),
                                r.get("submitted_at"), r.get("file_name") or "")
        if not asset:
            out.append({**_row_out(r), "result": f"NOT FOUND — {why}"})
            unmatched += 1
            continue

        url = asset.get("secure_url") or asset.get("url") or ""
        out.append({**_row_out(r), "result": f"matched ({why})", "url": url})
        repaired += 1
        if args.run and url:
            with conn.cursor() as cur:
                # The guard is the point: only ever fill an EMPTY path.
                cur.execute(
                    "UPDATE assignment_submissions SET file_path = %s "
                    "WHERE id = %s AND COALESCE(file_path, '') = ''",
                    (url, r["id"]))
            conn.commit()

    client.close()
    conn.close()

    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
        w.writeheader()
        w.writerows(out)

    print(f"\nmatched   : {repaired}")
    print(f"not found : {unmatched}")
    print(f"log       : {args.out}")
    if not args.run:
        print("\nDry run only — nothing was written. "
              "Read the CSV, then re-run with --run.")
    else:
        print("\nfile_path restored. Those rows can now be reviewed normally:")
        print("  python tools\\bulk_review.py --assignment-id N --limit 10 --run")


def _row_out(r: dict) -> dict:
    return {
        "submission_id": r["id"], "assignment_id": r["assignment_id"],
        "title": r.get("title", ""), "student_id": r["student_id"],
        "user_id": r.get("user_id"), "file_name": r.get("file_name"),
        "file_size": r.get("file_size"), "submitted_at": str(r.get("submitted_at") or ""),
        "result": "", "url": "",
    }


if __name__ == "__main__":
    main()
