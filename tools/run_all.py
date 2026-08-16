#!/usr/bin/env python3
"""
run_all.py — review the whole 30 Days 30 AI Tools course in ONE command.

Start it and walk away. It repairs, reviews, logs, and can be re-run safely
after an interruption without redoing finished work.

WHY THIS EXISTS. The work was being driven one assignment at a time, which
meant sitting at a terminal for four hours issuing eight commands and watching
for failures. That is not a person's job.

WHAT IT DOES, in order:

  1. Repair any nested rows (tools/repair_nested_notes.py logic) — a re-review
     of a corrupted row just grades the corruption.
  2. For each assignment, smallest first: bulk_review.py --redo. Always --redo,
     which rewrites rows IN PLACE. The student submit path INSERTs a new row per
     call and took assignment 14 from 49 rows to 95.
  3. Timestamped log per assignment, so no run erases the evidence of the last.
  4. A resume file, so an interrupted run picks up where it stopped.

ORDER MATTERS FOR COST. One assignment per invocation means the rubric and
knowledge pack — identical for every learner on that assignment — stay in the
prompt cache. Interleaving assignments misses the cache on every call.

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname
    set AIREV_API_KEY=<lms tenant key>
    set AIREV_ADMIN_KEY=<ADMIN_JOB_KEY>

    python tools/run_all.py                  # DRY RUN — shows the plan
    python tools/run_all.py --run            # do it
    python tools/run_all.py --run            # again: resumes, skips finished
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "..", "run_all_state.json")

# Smallest first: a fault shows up after 86 reviews, not 426.
COURSE_ASSIGNMENTS = [23, 22, 21, 14, 19, 20, 18, 17]

REQUIRED_ENV = ("AIREV_DB_URL", "AIREV_API_KEY", "AIREV_ADMIN_KEY")


def load_state() -> dict:
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"done": [], "started": None}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except Exception as e:
        print(f"  ! could not write resume file: {e}")


def plan(assignments: list, done: list) -> list:
    """Which assignments still need running. Pure — the resume contract."""
    return [a for a in assignments if a not in set(done)]


def log_name(assignment_id: int, stamp: str) -> str:
    """Timestamped, so a later run cannot erase an earlier one's evidence.
    bulk_review.py opens its log with "w"; that is how the 13 Aug batch record
    was lost."""
    return f"bulk_review_{assignment_id}_{stamp}.csv"


def build_command(assignment_id: int, log: str, concurrency: int, limit: int) -> list:
    return [sys.executable, os.path.join(HERE, "bulk_review.py"),
            "--redo", "--assignment-id", str(assignment_id),
            "--limit", str(limit), "--concurrency", str(concurrency),
            "--out", log, "--run"]


def run_step(label: str, cmd: list, dry: bool) -> bool:
    print(f"\n{'=' * 70}\n{label}\n{'=' * 70}")
    if dry:
        print("  (dry run) " + " ".join(cmd))
        return True
    started = time.time()
    result = subprocess.run(cmd)
    mins = (time.time() - started) / 60
    ok = result.returncode == 0
    print(f"  -> {'done' if ok else 'FAILED'} in {mins:.1f} min")
    return ok


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Review the whole course in one command (dry run unless --run).")
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--assignments", default="",
                    help="comma-separated ids; default is the whole course")
    ap.add_argument("--skip-repair", action="store_true")
    ap.add_argument("--restart", action="store_true", help="ignore the resume file")
    args = ap.parse_args()

    missing = [v for v in REQUIRED_ENV if not os.getenv(v)]
    if missing:
        sys.exit("Set these first:\n  " + "\n  ".join(
            f"set {v}=..." for v in missing))

    ids = ([int(x) for x in args.assignments.split(",") if x.strip()]
           if args.assignments else COURSE_ASSIGNMENTS)

    state = {"done": [], "started": None} if args.restart else load_state()
    todo = plan(ids, state["done"])
    stamp = datetime.now().strftime("%Y%m%d_%H%M")

    print(f"\nAssignments in scope : {ids}")
    if state["done"]:
        print(f"Already finished     : {state['done']} (resuming)")
    print(f"To run               : {todo}")
    print(f"Concurrency          : {args.concurrency}")
    print(f"Logs                 : bulk_review_<id>_{stamp}.csv")

    if not todo:
        print("\nNothing left to do. Use --restart to run them all again.\n")
        return

    if not args.run:
        print("\nDRY RUN — nothing called. Re-run with --run.\n")
        for a in todo:
            print("  " + " ".join(build_command(a, log_name(a, stamp),
                                                args.concurrency, args.limit)))
        return

    state["started"] = state["started"] or datetime.now().isoformat()
    save_state(state)

    if not args.skip_repair:
        # A re-review of a nested row just grades the corruption, so this runs
        # BEFORE anything else and covers every assignment in scope.
        for a in todo:
            run_step(f"REPAIR nested rows — assignment {a}",
                     [sys.executable, os.path.join(HERE, "repair_nested_notes.py"),
                      "--assignment-id", str(a), "--run"],
                     dry=False)

    failures = []
    for a in todo:
        ok = run_step(f"REVIEW assignment {a}",
                      build_command(a, log_name(a, stamp), args.concurrency, args.limit),
                      dry=False)
        if ok:
            state["done"].append(a)
            save_state(state)          # after EACH one, so a crash resumes here
        else:
            failures.append(a)
            print(f"  ! assignment {a} failed — continuing with the rest. "
                  f"Re-run this script to retry it.")

    print(f"\n{'=' * 70}")
    print(f"Finished {len(state['done'])} of {len(ids)} assignments.")
    if failures:
        print(f"Failed: {failures} — re-run this script; finished work is skipped.")
    print(f"Logs: bulk_review_<id>_{stamp}.csv")
    print(f"Resume file: {os.path.relpath(STATE_FILE)}\n")


if __name__ == "__main__":
    main()
