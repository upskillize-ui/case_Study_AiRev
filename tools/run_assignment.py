"""One command grades one assignment — the Space does the work, this watches.

    set AIREV_API_KEY=...paste the lms tenant key here...
    set AIREV_ADMIN_KEY=...paste the admin job key here...
    python tools\\run_assignment.py --assignment-id 18
    python tools\\run_assignment.py --assignment-id 18 --below 7

Replaces the 19-window sweep17 loop: this sends ONE request, the Space
queues every latest-attempt submission and reviews them itself (job state
lives in the Space's database, so a deploy or a closed laptop no longer
kills the run — it resumes on restart). This script only polls progress and
prints it; closing it changes nothing. Run it again any time to re-attach.

Needs REVIEW_JOBS_ENABLED=1 set on the Space (one-time, in Space settings).
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

DEFAULT_AGENT_URL = "https://upskill25-airev-agent.hf.space"
POLL_SECONDS = 20


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one assignment's reviews on the Space.")
    ap.add_argument("--assignment-id", type=int, required=True)
    ap.add_argument("--below", type=float, metavar="MARKS",
                    help="also re-review graded rows under this mark "
                         "(rows at/above it keep their marks and cost nothing); "
                         "omit to review ungraded rows only")
    ap.add_argument("--watch", type=int, metavar="JOB_ID",
                    help="re-attach to an already-running job instead of starting one")
    args = ap.parse_args()

    agent_url = os.getenv("AIREV_URL", DEFAULT_AGENT_URL).rstrip("/")
    api_key = os.getenv("AIREV_API_KEY", "")
    admin_key = os.getenv("AIREV_ADMIN_KEY", "")
    if not api_key:
        sys.exit("Set AIREV_API_KEY (the lms tenant key) first")
    headers = {"Content-Type": "application/json", "x-api-key": api_key,
               "x-admin-key": admin_key}

    with httpx.Client(timeout=60) as client:
        if args.watch:
            job_id = args.watch
        else:
            if not admin_key:
                sys.exit("Set AIREV_ADMIN_KEY to start a job")
            body = {"assignmentId": args.assignment_id}
            if args.below is not None:
                body["below"] = args.below
            r = client.post(f"{agent_url}/api/review/jobs", json=body,
                            headers=headers)
            if r.status_code == 409:
                sys.exit(f"Refused: {r.json().get('detail', r.text)}")
            r.raise_for_status()
            out = r.json()
            if not out.get("queued"):
                print(out.get("detail", "Nothing to review."))
                return
            job_id = out["jobId"]
            print(f"Job {job_id} started — {out['queued']} submission(s) queued "
                  f"({out['note']}). Safe to close this window; reviews run on "
                  f"the Space.")

        # Watch. Ctrl+C stops the WATCHING only, never the job.
        last = ""
        while True:
            try:
                r = client.get(f"{agent_url}/api/review/jobs/{job_id}",
                               headers=headers)
                r.raise_for_status()
                p = r.json()
            except Exception as e:
                print(f"  (poll failed: {e} — job unaffected, retrying)")
                time.sleep(POLL_SECONDS)
                continue
            line = (f"  {p['percent']:3d}%  done {p['done']}  "
                    f"skipped {p['skipped']}  failed {p['failed']}  "
                    f"pending {p['pending']}  [{p['state']}]")
            if line != last:
                print(line)
                last = line
            if p["state"] in ("finished", "aborted"):
                print(f"\nJob {job_id} {p['state']}: {p['note']}")
                if p["attention"]:
                    print("Rows needing attention:")
                    for row in p["attention"]:
                        print(f"  submission {row['submissionId']}: "
                              f"{row['state']} — {row['detail']}")
                return
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
