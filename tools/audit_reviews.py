#!/usr/bin/env python3
"""
audit_reviews.py — how many learners are currently holding a review the agent
should never have written, across ALL four review types.

STRICTLY READ-ONLY. This script issues SELECT statements only. It never
UPDATEs, DELETEs, or commits. Run it on a live production database without
hesitation; run it before and after any remediation to prove what changed.

Why it exists: bulk_review.py and clear_unfair_zeros.py each report only the
rows THEY touched (and default to --limit 10). After a scoring regression you
need the opposite view — the whole cohort, every type, counted once.

It reports per TYPE and per ITEM. The per-item table is the one to read
before triggering any correction run — it names each assignment (Day 01, Day
02 ...), how many learners submitted it, and what state their reviews are in.
Triggering a run without that count is guessing at the blast radius.

It answers four questions per review type:

  1. AGENT ZEROS      agent-written reviews sitting at exactly 0
  2. AGENT LOW        agent-written reviews at or below --low-pct of the item's
                      own total marks (default 40%) — the band that produces
                      "I submitted a correct answer but it shows fail"
  3. CLEARED PENDING  submissions whose grade was wiped (grade IS NULL) but
                      which were never re-reviewed — learners left with nothing
  4. HUMAN GRADED     rows carrying a faculty grade, reported so you can see
                      the blast radius of anything that overwrites them

Reuses the connection helper and the agent/auto-zero fingerprints from
clear_unfair_zeros.py — one definition of "the agent wrote this", not two.

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname

    python tools/audit_reviews.py                    # all types, summary + CSV
    python tools/audit_reviews.py --type assignment
    python tools/audit_reviews.py --low-pct 50 --out audit_aug13.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from dataclasses import dataclass
from typing import Optional

try:
    import pymysql  # noqa: F401  (imported for the clear error message below)
except ImportError:
    sys.exit("Missing dep. Run:  pip install pymysql")

# ONE definition of "an agent wrote this review" — imported, never re-declared.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from clear_unfair_zeros import AGENT_MARKERS, AUTO_ZERO_MARKERS, connect  # noqa: E402

# AGENT_MARKERS ('"reviewedBy": "ai"') is written ONLY by the assignment path
# (assignment_db_service.update_assignment_submission_with_ai_results). The
# case-study path (db_service.update_submission_with_ai_results) has never
# written it, so marker-only detection silently classifies EVERY agent-written
# case-study review as faculty-graded — the exact rows this audit exists to
# count. Fall back to fields only the agent emits. Verified against the
# deployed payload at a62157f; a human grade entered in the LMS carries none.
#
# Same blind spot means clear_unfair_zeros.py cannot currently clear a
# case-study zero at all — it protects them as "human-graded".
STRUCTURAL_AGENT_MARKERS = ('"aiLikelihoodPercent"', '"scoreEmoji"',
                            '"plagiarismFlag"', '"rubricScores"',
                            '"detailedFeedback"')


def wrote_by_agent(feedback: str) -> bool:
    """True when AiRev produced this review, by marker or by payload shape."""
    return (any(m in feedback for m in AGENT_MARKERS)
            or any(m in feedback for m in STRUCTURAL_AGENT_MARKERS))


@dataclass(frozen=True)
class TypeSpec:
    """Where one review type keeps its score, its feedback, and its body.

    The four types genuinely differ (sessions score 0-100 in `score`,
    capstones are one row with no submissions table). A spec per type beats an
    if/elif ladder and makes adding a fifth type a one-entry change.
    """
    kind: str
    table: str
    grade_col: str
    feedback_col: str
    body_cols: tuple[str, ...]
    item_table: Optional[str] = None
    fk: Optional[str] = None
    marks_col: Optional[str] = None      # total marks, on the item table
    percent_scale: bool = False          # score already 0-100, ignore marks_col


SPECS: dict[str, TypeSpec] = {
    "assignment": TypeSpec(
        kind="assignment", table="assignment_submissions",
        grade_col="grade", feedback_col="feedback",
        body_cols=("notes", "file_name"),
        item_table="assignments", fk="assignment_id", marks_col="total_marks"),
    "casestudy": TypeSpec(
        kind="casestudy", table="case_study_submissions",
        grade_col="grade", feedback_col="feedback",
        body_cols=("notes", "file_name"),
        item_table="case_studies", fk="case_study_id", marks_col="total_marks"),
    "capstone": TypeSpec(
        kind="capstone", table="capstones",
        grade_col="grade", feedback_col="feedback",
        body_cols=("file_url",), marks_col="total_marks"),
    "session": TypeSpec(
        kind="session", table="industry_session_submissions",
        grade_col="score", feedback_col="feedback_json",
        body_cols=("insight_text", "file_url"),
        item_table="industry_sessions", fk="session_id", percent_scale=True),
}


# ---------- schema probing (never SELECT a column you have not confirmed) ----

def table_exists(conn, table: str) -> bool:
    """EXISTS probe — a missing table is a data condition, not a crash."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = %s) AS present",
            (table,))
        row = cur.fetchone() or {}
    return bool(_val(row, "present"))


def existing_columns(conn, table: str) -> set:
    """Column names actually present. information_schema key casing varies by
    server config, so read both TABLE_NAME and table_name forms."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s", (table,))
        rows = cur.fetchall() or []
    return {str(_val(r, "column_name")) for r in rows}


def _val(row: dict, key: str):
    """Fetch a row value tolerating upper/lower-case keys."""
    if key in row:
        return row[key]
    return row.get(key.upper(), row.get(key.lower()))


# ---------- fetching -------------------------------------------------------

def build_query(spec: TypeSpec, cols: set, item_cols: set = frozenset()) -> str:
    """SELECT only columns confirmed to exist. Read-only by construction."""
    body = [c for c in spec.body_cols if c in cols]
    body_expr = (" , ".join(f"s.{c}" for c in body)
                 if body else "NULL")
    select = [
        "s.id AS submission_id",
        f"s.{spec.grade_col} AS grade",
        f"CAST(s.{spec.feedback_col} AS CHAR) AS feedback",
        "s.student_id",
        "s.submitted_at" if "submitted_at" in cols else "NULL AS submitted_at",
        "s.status" if "status" in cols else "NULL AS status",
        f"COALESCE(CONCAT_WS('', {body_expr}), '') AS body",
    ]
    if spec.item_table and spec.fk:
        select += [f"s.{spec.fk} AS item_id", "COALESCE(i.title, '') AS title"]
        select.append(f"i.{spec.marks_col} AS max_marks"
                      if spec.marks_col else "NULL AS max_marks")
        # The cohort. Upskillize runs TWO batches (junior and senior college)
        # that often share a task title, so two assignment rows with the same
        # name are usually two batches, not a duplicate. Without this column
        # the report cannot tell those apart, and a reader concludes the data
        # is dirty when it is correct. Probed, never assumed.
        select.append("i.course_id AS batch" if "course_id" in item_cols
                      else "NULL AS batch")
        join = f" JOIN {spec.item_table} i ON i.id = s.{spec.fk}"
    else:
        select += ["s.id AS item_id", "COALESCE(s.title, '') AS title"]
        select.append("s.course_id AS batch" if "course_id" in cols
                      else "NULL AS batch")
        select.append(f"s.{spec.marks_col} AS max_marks"
                      if spec.marks_col and spec.marks_col in cols
                      else "NULL AS max_marks")
        join = ""
    return f"SELECT {', '.join(select)} FROM {spec.table} s{join}"


def fetch(conn, spec: TypeSpec) -> list[dict]:
    """All rows for one type, or [] when the type is absent from this tenant."""
    if not table_exists(conn, spec.table):
        print(f"  [{spec.kind}] table '{spec.table}' not present — skipped")
        return []
    cols = existing_columns(conn, spec.table)
    item_cols = (existing_columns(conn, spec.item_table)
                 if spec.item_table and table_exists(conn, spec.item_table)
                 else set())
    with conn.cursor() as cur:
        cur.execute(build_query(spec, cols, item_cols))
        rows = list(cur.fetchall() or [])
    for r in rows:
        r["kind"] = spec.kind
        r["percent_scale"] = spec.percent_scale
    return rows


# ---------- classification (pure) -------------------------------------------

def classify(row: dict, low_pct: float) -> str:
    """One bucket per submission. Pure: no I/O, trivially testable."""
    feedback = row.get("feedback") or ""
    by_agent = wrote_by_agent(feedback)
    grade = row.get("grade")
    has_body = bool((row.get("body") or "").strip())

    if grade is None:
        return "cleared_pending" if has_body else "unreviewed"
    if not by_agent:
        return "human_graded"

    grade = float(grade)
    if grade == 0:
        return "agent_zero"

    ceiling = 100.0 if row.get("percent_scale") else _max_marks(row)
    if ceiling and grade <= (low_pct / 100.0) * ceiling:
        return "agent_low"
    return "agent_ok"


def _max_marks(row: dict) -> float:
    """The item's own total. 0 means unknown — never guess 100."""
    try:
        return float(row.get("max_marks") or 0)
    except (TypeError, ValueError):
        return 0.0


def is_auto_zero(row: dict) -> bool:
    """True when the length rule, not a model, produced this score."""
    return any(m in (row.get("feedback") or "") for m in AUTO_ZERO_MARKERS)


# ---------- reporting -------------------------------------------------------

BUCKETS = ("agent_zero", "agent_low", "cleared_pending",
           "agent_ok", "human_graded", "unreviewed")


def summarise(rows: list[dict]) -> dict:
    """Counts and distinct-student counts per bucket, per kind."""
    out: dict = {}
    for r in rows:
        key = (r["kind"], r["bucket"])
        entry = out.setdefault(key, {"n": 0, "students": set()})
        entry["n"] += 1
        entry["students"].add(r.get("student_id"))
    return out


def print_summary(summary: dict, low_pct: float) -> None:
    kinds = sorted({k for k, _ in summary})
    print(f"\n{'type':<12}{'bucket':<18}{'rows':>7}{'students':>10}")
    print("-" * 47)
    for kind in kinds:
        for bucket in BUCKETS:
            entry = summary.get((kind, bucket))
            if not entry:
                continue
            print(f"{kind:<12}{bucket:<18}{entry['n']:>7}"
                  f"{len(entry['students']):>10}")
    hurt = sum(e["n"] for (_, b), e in summary.items()
               if b in ("agent_zero", "agent_low", "cleared_pending"))
    students = set()
    for (_, b), e in summary.items():
        if b in ("agent_zero", "agent_low", "cleared_pending"):
            students |= e["students"]
    print("-" * 47)
    print(f"LEARNERS CURRENTLY AFFECTED: {len(students)} "
          f"across {hurt} submissions "
          f"(zeros + <={low_pct:g}% + cleared-never-rereviewed)")


def summarise_by_item(rows: list[dict]) -> list[dict]:
    """One line per assignment / case study / session — the view you need
    BEFORE triggering anything.

    The per-type summary answers "how bad is it"; it cannot answer "which day,
    how many learners, in what state", which is the question that decides what
    a correction run should target. Counting the population first is not
    optional: a run triggered without it is a run whose blast radius nobody
    knows.

    Dicts keyed by item, one pass — no nested scan over rows per item.
    """
    by_item: dict = {}
    for r in rows:
        key = (r["kind"], r.get("item_id"))
        e = by_item.setdefault(key, {
            "kind": r["kind"], "item_id": r.get("item_id"),
            "batch": r.get("batch"),
            "title": (r.get("title") or "")[:40],
            "max_marks": _max_marks(r),
            "submissions": 0, "students": set(),
            **{b: 0 for b in BUCKETS}})
        e["submissions"] += 1
        e["students"].add(r.get("student_id"))
        e[r["bucket"]] += 1
    out = sorted(by_item.values(),
                 key=lambda e: (e["kind"], _sort_key(e["item_id"])))
    _flag_shared_titles(out)
    for e in out:
        e["students"] = len(e["students"])
    return out


def _flag_shared_titles(items: list[dict]) -> None:
    """Mark items whose title appears more than once in the same type.

    Two rows with one title are normally the SAME task issued to the junior and
    the senior batch — legitimate, and both need reviewing. It only becomes a
    problem when one learner appears under both, so the flag invites the check
    rather than declaring a duplicate.
    """
    seen: dict = {}
    for e in items:
        seen.setdefault((e["kind"], e["title"].strip().lower()), []).append(e)
    for group in seen.values():
        if len(group) > 1:
            ids = ", ".join(str(g["item_id"]) for g in group)
            for g in group:
                g["shared_title_with"] = ids


def _sort_key(item_id):
    """Numeric ids sort numerically; anything else falls back to text, so a
    non-integer id cannot crash the report."""
    try:
        return (0, int(item_id), "")
    except (TypeError, ValueError):
        return (1, 0, str(item_id))


def print_by_item(items: list[dict], low_pct: float) -> None:
    print(f"\n{'PER ITEM — count the population before triggering anything':<96}")
    print(f"{'type':<11}{'id':>5}{'batch':>7}  {'title':<40}{'subs':>6}{'stud':>6}"
          f"{'zero':>6}{'low':>6}{'ok':>5}{'human':>6}{'pend':>6}{'new':>5}")
    print("-" * 112)
    for e in items:
        print(f"{e['kind']:<11}{str(e['item_id']):>5}"
              f"{str(e['batch'] if e['batch'] is not None else '-'):>7}  {e['title']:<40}"
              f"{e['submissions']:>6}{e['students']:>6}"
              f"{e['agent_zero']:>6}{e['agent_low']:>6}{e['agent_ok']:>5}"
              f"{e['human_graded']:>6}{e['cleared_pending']:>6}"
              f"{e['unreviewed']:>5}")
    print("-" * 112)
    print(f"  zero = agent scored 0   low = agent scored <={low_pct:g}% of total   "
          f"pend = grade cleared, never re-reviewed   new = never reviewed")
    shared = [e for e in items if e.get("shared_title_with")]
    if shared:
        print("\n  Same title on more than one item — usually the two batches "
              "(junior / senior), not a duplicate:")
        for ids in sorted({e["shared_title_with"] for e in shared}):
            match = next(e for e in shared if e["shared_title_with"] == ids)
            print(f"    ids {ids} — {match['title']}")
        print("    Confirm with: are any learners present under BOTH ids?")

    needs = [e for e in items if e["agent_zero"] or e["agent_low"]
             or e["cleared_pending"]]
    if needs:
        print("\n  Items with learners to correct, worst first:")
        for e in sorted(needs, key=lambda e: -(e["agent_zero"] + e["agent_low"]
                                               + e["cleared_pending"]))[:10]:
            n = e["agent_zero"] + e["agent_low"] + e["cleared_pending"]
            print(f"    {e['kind']} {e['item_id']}: {n} of {e['submissions']} "
                  f"submissions — {e['title']}")


def write_item_csv(items: list[dict], path: str) -> None:
    fields = ("kind", "batch", "item_id", "title", "max_marks", "submissions",
              "students") + BUCKETS
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for e in items:
            w.writerow(e)
    print(f"Per-item summary written to {path}")


def write_csv(rows: list[dict], path: str) -> None:
    fields = ("kind", "bucket", "batch", "submission_id", "item_id", "title",
              "student_id", "grade", "max_marks", "auto_zero", "status",
              "submitted_at", "body_chars")
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({**r, "body_chars": len((r.get("body") or ""))})
    print(f"\nDetail written to {path}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Read-only audit of AiRev review outcomes (writes nothing).")
    ap.add_argument("--type", choices=list(SPECS) + ["all"], default="all")
    ap.add_argument("--low-pct", type=float, default=40.0,
                    help="treat scores at or below this %% of total marks as low")
    ap.add_argument("--out", default="review_audit.csv",
                    help="detail CSV; a <name>_by_item.csv summary is written "
                         "alongside it")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:\n"
                 "  set AIREV_DB_URL=mysql://user:pass@host:port/dbname")

    specs = list(SPECS.values()) if args.type == "all" else [SPECS[args.type]]
    conn = connect(db_url)
    try:
        rows: list[dict] = []
        for spec in specs:
            fetched = fetch(conn, spec)
            print(f"  [{spec.kind}] {len(fetched)} submission rows read")
            rows.extend(fetched)
    finally:
        conn.close()

    for r in rows:
        r["bucket"] = classify(r, args.low_pct)
        r["auto_zero"] = is_auto_zero(r)

    items = summarise_by_item(rows)
    print_summary(summarise(rows), args.low_pct)
    print_by_item(items, args.low_pct)
    write_csv(rows, args.out)
    write_item_csv(items, args.out.replace(".csv", "") + "_by_item.csv")
    print("\nNothing was modified. This script only reads.")


if __name__ == "__main__":
    main()
