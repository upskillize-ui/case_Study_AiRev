#!/usr/bin/env python3
"""
bulk_review.py — faculty-side bulk trigger for AiRev reviews.

Runs from a terminal. Finds submissions that a student has made but that were
never AI-reviewed, and calls the SAME endpoints the student UI calls, so the
result is identical to the student clicking "New Review".

Safety by construction:
  - DRY RUN by default. Nothing is called until you pass --run.
  - Read-only against the DB. This script never writes or deletes a row;
    only the agent writes, exactly as it does for a student click.
  - --limit defaults to 10. You must raise it deliberately.
  - Rows with no reviewable content are skipped and reported, not sent
    (they would only burn a call and return "needs_input").

Usage:
    set AIREV_DB_URL=mysql://user:pass@host:port/dbname
    set AIREV_API_KEY=<the lms tenant key>

    python tools/bulk_review.py                       # dry run, assignments
    python tools/bulk_review.py --type both           # dry run, both types
    python tools/bulk_review.py --limit 5 --run       # review 5 for real
    python tools/bulk_review.py --assignment-id 11 --run

    Correction runs (assignments only) — re-score work that ALREADY has a
    grade, rewriting each row IN PLACE. Needs AIREV_ADMIN_KEY.

    python tools/bulk_review.py --redo --assignment-id 17            # list them
    python tools/bulk_review.py --redo --assignment-id 17 --limit 1 --run
    python tools/bulk_review.py --redo --assignment-id 17 --limit 50 --run

    --redo never touches the student submit endpoint, which INSERTs a new
    submission per call. It would otherwise leave every learner with a
    duplicate row and an inflated attempt count.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from urllib.parse import urlparse, unquote

try:
    import pymysql
    import httpx
except ImportError:
    sys.exit("Missing deps. Run:  pip install pymysql httpx")


DEFAULT_AGENT_URL = "https://upskill25-airev-agent.hf.space"

# Endpoint + payload shape per review type. Adding a type = adding an entry,
# never an if/elif ladder.
TYPES: dict[str, dict] = {
    "assignment": {
        "endpoint": "/api/review/submit-assignment",
        "id_key": "assignmentId",
        "table": "assignment_submissions",
        "item_table": "assignments",
        "item_fk": "assignment_id",
        "file_col": "file_path",
        "item_status": "active",
        # Staff correction path — rewrites the existing row instead of
        # inserting a new submission. Only assignments have one today.
        "regrade_endpoint": "/api/review/re-review/assignment",
    },
    "casestudy": {
        "endpoint": "/api/review/submit",
        "id_key": "caseStudyId",
        "table": "case_study_submissions",
        "item_table": "case_studies",
        "item_fk": "case_study_id",
        "file_col": "file_url",
        "item_status": "published",
    },
}

RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_TRIES = 4


@dataclass
class Pending:
    """One submission awaiting review."""
    review_type: str
    submission_id: int
    item_id: int
    student_id: int
    title: str
    notes_len: int
    current_grade: float | None = None
    file_name: str
    submitted_at: str

    @property
    def has_content(self) -> bool:
        return self.notes_len > 0 or bool(self.file_name)


def _fmt_score(res) -> str:
    """Marks the learner sees, with the percentage in brackets.

    Falls back to a bare percentage for older reviews saved before
    scoreMarks/outOf existed — never invents a denominator.
    """
    marks, out_of = res.extra.get("marks"), res.extra.get("outOf")
    if marks is None or not out_of:
        return f"{res.score}%"
    return f"{marks}/{out_of} ({res.score}%)"


@dataclass
class Result:
    pending: Pending
    ok: bool
    score: object = None
    grade: str = ""
    detail: str = ""
    ms: int = 0
    extra: dict = field(default_factory=dict)


# ── DB ────────────────────────────────────────────────────────────────────

def connect(db_url: str):
    """mysql://user:pass@host:port/dbname → live PyMySQL connection."""
    u = urlparse(db_url)
    if not u.hostname:
        sys.exit(f"Could not parse AIREV_DB_URL: {db_url[:40]}...")
    return pymysql.connect(
        host=u.hostname,
        port=u.port or 3306,
        user=unquote(u.username or ""),
        password=unquote(u.password or ""),
        database=(u.path or "/").lstrip("/"),
        cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=20,
        ssl={"ssl": {}} if "aivencloud" in (u.hostname or "") else None,
    )


def fetch_pending(conn, review_type: str, item_id: int | None,
                  redo: bool = False) -> list[Pending]:
    """Submissions with content but no grade yet, newest attempt per
    (item, student). One query + one pass — no per-row lookups.

    redo=True inverts the grade filter: it selects rows that ALREADY carry a
    grade, for a staff correction run. Those rows go to the regrade endpoint,
    which rewrites them in place — never to the student submit endpoint, which
    would insert a duplicate submission for every learner.
    """
    cfg = TYPES[review_type]
    grade_filter = "s.grade IS NOT NULL" if redo else "s.grade IS NULL"
    sql = f"""
        SELECT s.id, s.{cfg['item_fk']} AS item_id, s.student_id,
               COALESCE(i.title, '') AS title,
               CHAR_LENGTH(COALESCE(s.notes, '')) AS notes_len,
               COALESCE(s.file_name, '') AS file_name,
               COALESCE(s.{cfg['file_col']}, '') AS file_ref,
               s.grade AS current_grade,
               s.submitted_at
        FROM {cfg['table']} s
        JOIN {cfg['item_table']} i ON i.id = s.{cfg['item_fk']}
        WHERE {grade_filter}
    """
    params: list = []
    if item_id:
        sql += f" AND s.{cfg['item_fk']} = %s"
        params.append(item_id)
    sql += " ORDER BY s.submitted_at DESC, s.id DESC"

    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()

    # Newest row wins per (item, student) — dict keeps it O(n), no nested scan.
    latest: dict[tuple[int, int], Pending] = {}
    for r in rows:
        key = (r["item_id"], r["student_id"])
        if key in latest:
            continue
        latest[key] = Pending(
            review_type=review_type,
            submission_id=r["id"],
            item_id=r["item_id"],
            student_id=r["student_id"],
            title=(r["title"] or "")[:60],
            notes_len=int(r["notes_len"] or 0),
            current_grade=r.get("current_grade"),
            file_name=r["file_name"] or (r["file_ref"] or "")[:40],
            submitted_at=str(r["submitted_at"] or ""),
        )
    return list(latest.values())


# ── Agent call ────────────────────────────────────────────────────────────

def review_one(p: Pending, agent_url: str, api_key: str, timeout: int,
               redo: bool = False, dry: bool = False) -> Result:
    """POST with empty answerText — the agent reads the STORED submission,
    exactly like a student clicking New Review. Retries transient failures.

    redo=True targets the staff regrade endpoint instead, which rewrites the
    EXISTING row. The student submit endpoint inserts a new submission for
    every call — correct for a learner's re-attempt, wrong for a correction
    run, where it would leave every learner with a duplicate submission and an
    inflated attempt count.
    """
    cfg = TYPES[p.review_type]
    # student_id comes straight out of the submissions table, so it is a
    # students.id. Say so: the route otherwise runs it through the users.id ->
    # students.id mapping, which on an ambiguous value resolves to a DIFFERENT
    # learner and grades their submission instead.
    if redo:
        endpoint = f"{cfg['regrade_endpoint']}/{p.submission_id}"
        if dry:
            endpoint += "?dryRun=true"
        body = {}
    else:
        endpoint = cfg["endpoint"]
        body = {cfg["id_key"]: p.item_id, "studentId": p.student_id,
                "answerText": "", "idSpace": "students"}
    headers = {"Content-Type": "application/json", "x-api-key": api_key}
    # Staff-initiated: the agent skips student billing when this key is valid.
    # Without it, a faculty bulk run would debit 1400 learners' credits.
    admin_key = os.getenv("AIREV_ADMIN_KEY", "")
    if admin_key:
        headers["x-admin-key"] = admin_key
    started = time.time()
    last = "unknown error"

    for attempt in range(MAX_TRIES):
        if attempt:
            time.sleep(min(60, 5 * (2 ** (attempt - 1))))
        try:
            r = httpx.post(f"{agent_url}{endpoint}", json=body,
                           headers=headers, timeout=timeout)
        except Exception as e:
            last = f"network: {type(e).__name__}"
            continue

        if r.status_code in RETRY_STATUS:
            last = f"HTTP {r.status_code} (busy/transient)"
            wait = r.headers.get("Retry-After")
            if wait and wait.isdigit():
                time.sleep(min(120, int(wait)))
            continue
        if r.status_code >= 400:
            return Result(p, False, detail=f"HTTP {r.status_code}: {r.text[:120]}",
                          ms=int((time.time() - started) * 1000))

        data = r.json()
        ms = int((time.time() - started) * 1000)
        if data.get("skipped"):
            # Nothing readable in the row. The regrade route leaves the
            # existing grade alone rather than blanking it — report, move on.
            return Result(p, False, detail=f"skipped: {data['skipped']}", ms=ms)
        if data.get("dryRun"):
            return Result(p, True, score=None, grade="(dry)", ms=ms,
                          extra={"words": data.get("wordCount"),
                                 "was": data.get("previousGrade"),
                                 "outOf": data.get("outOf")})
        if data.get("needsInput") or data.get("status") == "needs_input":
            return Result(p, False, detail="no stored content to review", ms=ms)
        if data.get("blocked"):
            return Result(p, False, detail=f"blocked: {data['blocked']}", ms=ms)
        fb = data.get("feedback") or {}
        if data.get("success") and fb:
            # fb["score"] is the rubric PERCENT. The student is graded in the
            # item's own marks (scoreMarks/outOf, written by review_payload).
            # Print both — "19/100" on a 10-mark assignment reads as a score
            # out of a hundred and is the same /100 confusion the review card
            # had.
            return Result(p, True, score=fb.get("score"), grade=fb.get("grade", ""),
                          ms=ms, extra={"ai": fb.get("aiLikelihoodPercent"),
                                        "marks": fb.get("scoreMarks"),
                                        "outOf": fb.get("outOf")})
        if data.get("partialReview"):
            return Result(p, False, detail="partial: AI unavailable, saved", ms=ms)
        return Result(p, False, detail=f"unexpected response: {str(data)[:120]}", ms=ms)

    return Result(p, False, detail=f"gave up after {MAX_TRIES} tries — {last}",
                  ms=int((time.time() - started) * 1000))


# ── CLI ───────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description="Bulk-trigger AiRev reviews (dry run by default).")
    ap.add_argument("--type", choices=["assignment", "casestudy", "both"], default="assignment")
    ap.add_argument("--assignment-id", type=int)
    ap.add_argument("--case-study-id", type=int)
    ap.add_argument("--limit", type=int, default=10, help="max reviews (default 10)")
    ap.add_argument("--concurrency", type=int, default=3, help="parallel reviews (default 3)")
    ap.add_argument("--timeout", type=int, default=180, help="per-review seconds")
    ap.add_argument("--out", default="bulk_review_log.csv")
    ap.add_argument("--run", action="store_true", help="actually call the agent")
    ap.add_argument("--redo", action="store_true",
                    help="re-score submissions that ALREADY have a grade, "
                         "rewriting each row in place (assignments only). "
                         "Requires AIREV_ADMIN_KEY.")
    ap.add_argument("--bill-students", action="store_true",
                    help="charge each student's credits (default: staff run, no charge)")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    api_key = os.getenv("AIREV_API_KEY", "")
    agent_url = os.getenv("AIREV_URL", DEFAULT_AGENT_URL).rstrip("/")
    if not db_url:
        sys.exit("Set AIREV_DB_URL (mysql://user:pass@host:port/dbname)")
    if not api_key and args.run:
        sys.exit("Set AIREV_API_KEY (the lms tenant key) to use --run")
    if args.run and not os.getenv("AIREV_ADMIN_KEY") and not args.bill_students:
        sys.exit("Set AIREV_ADMIN_KEY (the Space's ADMIN_JOB_KEY) so this staff run "
                 "does NOT debit students' credits.\n"
                 "If you really intend to charge them, pass --bill-students.")

    if args.redo:
        # No regrade endpoint exists for case studies yet, and silently
        # falling back to the submit endpoint would insert duplicate rows —
        # the exact outcome --redo exists to avoid.
        unsupported = [t for t in (["assignment", "casestudy"]
                                   if args.type == "both" else [args.type])
                       if "regrade_endpoint" not in TYPES[t]]
        if unsupported:
            sys.exit(f"--redo is not supported for: {', '.join(unsupported)} "
                     f"(no in-place regrade endpoint). Run it with "
                     f"--type assignment.")
        if not os.getenv("AIREV_ADMIN_KEY"):
            sys.exit("--redo requires AIREV_ADMIN_KEY — the regrade endpoint is "
                     "staff-only and returns 403 without it.")

    types = ["assignment", "casestudy"] if args.type == "both" else [args.type]
    conn = connect(db_url)
    pending: list[Pending] = []
    try:
        for t in types:
            item_id = args.assignment_id if t == "assignment" else args.case_study_id
            pending.extend(fetch_pending(conn, t, item_id, redo=args.redo))
    finally:
        conn.close()

    empty = [p for p in pending if not p.has_content]
    ready = sorted((p for p in pending if p.has_content),
                   key=lambda p: p.submitted_at, reverse=True)

    mode = "STAFF RUN — students NOT billed" if os.getenv("AIREV_ADMIN_KEY") \
        else "students WILL be billed"
    print(f"\nAgent   : {agent_url}")
    print(f"Billing : {mode}")
    label = "ALREADY GRADED (re-score in place)" if args.redo else "unreviewed"
    print(f"Pending : {len(pending)} {label}  |  reviewable: {len(ready)}  |  "
          f"no content (skipped): {len(empty)}")
    if empty:
        print("  skipped (nothing stored to review):")
        for p in empty[:10]:
            print(f"    - {p.review_type} {p.item_id} student {p.student_id} — {p.title}")
        if len(empty) > 10:
            print(f"    ... and {len(empty) - 10} more")

    batch = ready[:args.limit]
    print(f"\n{'WOULD REVIEW' if not args.run else 'REVIEWING'} {len(batch)} "
          f"of {len(ready)} (limit {args.limit}, concurrency {args.concurrency}):")
    for p in batch:
        src = f"{p.notes_len} chars" + (f" + {p.file_name}" if p.file_name else "")
        if args.redo:
            src += f" | now {p.current_grade}"
        print(f"  [{p.review_type}] item {p.item_id} student {p.student_id} "
              f"| {src} | {p.title}")

    if not args.run:
        print(f"\nDRY RUN — nothing called. Re-run with --run to execute.\n")
        return

    results: list[Result] = []
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(review_one, p, agent_url, api_key, args.timeout,
                               args.redo, False): p
                   for p in batch}
        for i, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            results.append(res)
            p = res.pending
            if res.ok:
                ai = res.extra.get("ai")
                print(f"  [{i}/{len(batch)}] OK   item {p.item_id} student {p.student_id} "
                      f"-> {_fmt_score(res)} {res.grade}"
                      + (f" (est. {ai}% AI)" if ai is not None else "")
                      + f"  {res.ms}ms")
            else:
                print(f"  [{i}/{len(batch)}] FAIL item {p.item_id} student {p.student_id} "
                      f"-> {res.detail}")

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["type", "item_id", "student_id", "submission_id", "title",
                    "ok", "grade_before", "marks", "out_of", "percent", "grade",
                    "ai_percent", "detail", "ms"])
        for r in results:
            p = r.pending
            w.writerow([p.review_type, p.item_id, p.student_id, p.submission_id,
                        p.title, r.ok, p.current_grade,
                        r.extra.get("marks"), r.extra.get("outOf"),
                        r.score, r.grade, r.extra.get("ai"),
                        r.detail, r.ms])

    ok = sum(1 for r in results if r.ok)
    print(f"\nDone: {ok} reviewed, {len(results) - ok} failed, "
          f"{int(time.time() - started)}s total. Log: {args.out}")
    print(f"Remaining unreviewed after this run: {len(ready) - ok}\n")


if __name__ == "__main__":
    main()
