"""Does live auto-review actually work? Run this BEFORE any student relies on it.

Ranjana, 24 Aug: "today onwards live submission will work right? test first".

The right instinct. Tomorrow every submission takes this path, and the first
time it runs must not be the first time it is tried. This walks the exact
route a learner's Submit will take — enqueue, worker, mark — and says at each
step whether it worked and what would have gone wrong.

It NEVER submits anything on a learner's behalf. It queues ONE existing
submission for review, which is the same thing the daily batch does.

    set AIREV_URL=https://upskill25-airev-agent.hf.space
    set AIREV_API_KEY=...
    set AIREV_ADMIN_KEY=...
    python tools/live_check.py --assignment-id 24 --student-id 1217
"""
from __future__ import annotations

import argparse
import os
import sys
import time

try:
    import httpx
except ImportError:
    sys.exit("Missing dep. Run:  pip install httpx")

AGENT = os.getenv("AIREV_URL", "https://upskill25-airev-agent.hf.space").rstrip("/")
POLL_SECONDS = 5
GIVE_UP_AFTER = 300


def step(n: int, what: str) -> None:
    print(f"\n[{n}] {what}")


def verdict(ok: bool, message: str) -> None:
    print(f"    {'PASS' if ok else 'FAIL'}  {message}")


def describe_progress(p: dict) -> str:
    return (f"{p.get('done', 0)} done, {p.get('skipped', 0)} skipped, "
            f"{p.get('failed', 0)} failed, {p.get('pending', 0)} still queued")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Exercise the live auto-review path end to end (read-mostly: "
                    "it re-scores one existing submission, exactly as the daily "
                    "batch does; it never submits work for anyone).")
    ap.add_argument("--assignment-id", type=int, required=True)
    ap.add_argument("--student-id", type=int, required=True,
                    help="a student who has ALREADY submitted this assignment")
    args = ap.parse_args()

    api_key = os.getenv("AIREV_API_KEY", "")
    admin_key = os.getenv("AIREV_ADMIN_KEY", "")
    if not (api_key and admin_key):
        sys.exit("Set AIREV_API_KEY and AIREV_ADMIN_KEY first.")

    headers = {"Content-Type": "application/json", "x-api-key": api_key,
               "x-admin-key": admin_key}
    print(f"Agent: {AGENT}")
    print(f"Testing: assignment {args.assignment_id}, student {args.student_id}")
    print("Nothing is submitted on anyone's behalf. One existing row is re-scored.")

    with httpx.Client(timeout=60) as client:
        # 1 ── is the queue even switched on?
        step(1, "Is the review queue enabled on the Space?")
        try:
            r = client.get(f"{AGENT}/api/review/jobs", headers=headers)
        except Exception as e:
            verdict(False, f"could not reach the agent ({type(e).__name__})")
            sys.exit(1)
        if r.status_code == 503 or "not enabled" in r.text.lower():
            verdict(False, "the queue is OFF. Set REVIEW_JOBS_ENABLED=1 on the "
                           "Space, wait for it to rebuild, and run this again.")
            sys.exit(1)
        if r.status_code == 403:
            verdict(False, "the admin key was refused. Check AIREV_ADMIN_KEY "
                           "matches ADMIN_JOB_KEY on the Space.")
            sys.exit(1)
        if r.status_code != 200:
            verdict(False, f"unexpected answer: HTTP {r.status_code} "
                           f"{r.text[:120]}")
            sys.exit(1)
        verdict(True, "the queue is on and accepting staff calls")

        # 2 ── enqueue, and time it: the learner waits on THIS
        step(2, "Enqueue one submission — this is what a learner's Submit does")
        started = time.time()
        try:
            r = client.post(f"{AGENT}/api/review/jobs/enqueue", headers=headers,
                            json={"assignmentId": args.assignment_id,
                                  "studentId": args.student_id})
        except Exception as e:
            verdict(False, f"the enqueue call failed ({type(e).__name__})")
            sys.exit(1)
        took_ms = int((time.time() - started) * 1000)

        if r.status_code == 404:
            verdict(False, f"no submission found for student {args.student_id} "
                           f"on assignment {args.assignment_id} — pick a student "
                           f"who has submitted it")
            sys.exit(1)
        if r.status_code != 200:
            verdict(False, f"HTTP {r.status_code}: {r.text[:160]}")
            sys.exit(1)

        data = r.json()
        job_id = data.get("jobId")
        verdict(True, f"queued in {took_ms} ms — {data.get('detail', '')}")
        if took_ms > 5000:
            print("    NOTE: over 5 seconds. A learner should never wait this "
                  "long to be told their work is in — check the Space's load.")

        # 3 ── does the worker actually drain it?
        step(3, "Wait for the worker to review it (the learner is NOT waiting "
                "for this — they can close the page)")
        deadline = time.time() + GIVE_UP_AFTER
        last = ""
        while time.time() < deadline:
            time.sleep(POLL_SECONDS)
            try:
                s = client.get(f"{AGENT}/api/review/jobs/{job_id}",
                               headers=headers)
                if s.status_code != 200:
                    continue
                body = s.json()
            except Exception:
                continue
            progress = body.get("progress") or {}
            line = describe_progress(progress)
            if line != last:
                print(f"    {int(time.time() - started):>3}s  {line}")
                last = line
            if progress.get("pending", 1) == 0:
                elapsed = int(time.time() - started)
                verdict(True, f"the queue drained in {elapsed}s")
                print(f"\nRESULT: live auto-review WORKS on this Space.")
                print(f"  A learner presses Submit, waits ~{took_ms} ms, and "
                      f"closes the page.")
                print(f"  Their feedback appears about {elapsed} seconds later.")
                print("\nStill to switch on for real submissions:")
                print("  LMS:   AIREV_AUTO_REVIEW=1")
                print("  LMS:   AIREV_AUTO_REVIEW_COURSES=<this course's id>")
                print("  LMS:   deploy the student.js that calls /jobs/enqueue")
                return

        verdict(False, f"still not finished after {GIVE_UP_AFTER}s — the worker "
                       f"may be stuck. Check the Space log for '[JOB {job_id}]'.")
        sys.exit(1)


if __name__ == "__main__":
    main()
