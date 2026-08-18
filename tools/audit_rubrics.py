#!/usr/bin/env python3
"""
audit_rubrics.py — can a learner who DID the task actually reach full marks?

STRICTLY READ-ONLY. SELECT statements only. No AI calls, so it costs nothing
and answers in a second.

WHY THIS EXISTS. Every low-score investigation so far has run the same way:
review 95 learners, look at the wreckage, work backwards. That is expensive and
slow, and it asks the question too late. The cheaper question comes first —

    of the 100 marks on this assignment, how many can a learner earn from
    what they actually submit?

Day 06 answered 65. Thirty-five marks sat behind "ChatGPT lyrics with music
style line" and "Suno Custom mode used" — facts a finished song cannot carry.
Nobody could reach them, however well they did the task, and the whole cohort's
ceiling was 6.5/10 before the song was judged at all. That was visible in the
rubric row the entire time, for free, before a single review ran.

WHAT IT FLAGS

  UNREACHABLE  the criterion asks about something outside the submission —
               which tool drafted it, which setting was on, whether it was
               posted to WhatsApp, whether a link is live. `strip_offplatform`
               removes the shapes it can match; this reports those AND the
               near-misses it cannot, so a human can judge the rest.

  LENGTH TRAP  wordMin is high on a task whose deliverable is an image, a link
               or a file. The written part is a caption, and the length penalty
               takes up to 20 marks off learners who did exactly what was asked.

  FALLBACK     the generic rubric is in use, which means derivation failed and
               the assignment is being marked against criteria it never had.

    set AIREV_DB_URL=mysql://user:pass@host:port/dbname

    python tools/audit_rubrics.py                      # every cached rubric
    python tools/audit_rubrics.py --assignment-id 14   # just Day 01
    python tools/audit_rubrics.py --course             # the 30-day course
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.parse import unquote, urlparse

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    sys.exit("Missing dependency. Run:  pip install pymysql")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from app.services.rubric_service import (  # noqa: E402
    FALLBACK_CRITERIA, RUBRIC_VERSION, is_offplatform,
)

RULE = "─" * 78

# The same eight the course runs on, smallest first.
COURSE_ASSIGNMENTS = [23, 22, 21, 14, 19, 20, 18, 17]

# A written answer can be as long as it likes. A caption cannot, and a length
# minimum above this on an artefact task is a penalty on doing as you were told.
CAPTION_WORD_MIN = 40
ARTEFACT_KINDS = {"image", "artifact_or_link", "file_or_workbook"}


# ---------------------------------------------------------------------------
# Near-misses: shapes strip_offplatform deliberately does NOT match.
#
# A criterion naming the tool that MADE the work is usually the deliverable
# itself ("Song created with Suno") and must survive. A criterion asserting
# which tool AUTHORED it ("ChatGPT wrote the lyrics") cannot be evidenced by
# the artefact. The line between them is too fine for a regex to enforce
# safely — this file's own history records an over-broad pattern dropping nine
# legitimate criteria — so these are REPORTED for a human, never stripped.
# ---------------------------------------------------------------------------

_AUTHORING_TOOLS = (
    r"chatgpt|gpt-?4|claude|gemini|copilot|perplexity|notebooklm|midjourney|"
    r"suno|elevenlabs|runway|heygen|descript|canva|gamma|julius|manus|cursor|"
    r"lovable|n8n|zapier|grammarly|teal|lindy"
)
_AUTHORSHIP_VERBS = r"used|wrote|written|drafted|generated|created|produced|assisted|prompted"

_SUSPECT_PATTERNS = [
    # A criterion that NAMES an AI tool is nearly always about provenance —
    # "ChatGPT lyrics with music style line" has no verb in it at all, so
    # requiring one missed the very case this was written for. A finished
    # artefact does not record which model produced it, so naming the tool in a
    # criterion is a claim we cannot check. Reported, never stripped: where the
    # tool IS the deliverable ("Song created with Suno") a human should decide,
    # and a regex cannot tell those two apart.
    (re.compile(rf"\b({_AUTHORING_TOOLS})\b", re.I),
     "names an AI tool — a finished artefact carries no record of what made it"),
    (re.compile(r"\b(steps?|process|workflow|method|procedure)\b.{0,30}"
                r"\b(followed|order|sequence|correctly)\b", re.I),
     "judges the METHOD followed, not the output it produced"),
    (re.compile(r"\b(original|originality|plagiaris\w+|own\s+work)\b", re.I),
     "asserts originality, which cannot be established from the submission alone"),
]


def suspect_reason(name: str) -> str:
    """Why this criterion name looks unearnable. '' when it looks fine. Pure."""
    for pattern, why in _SUSPECT_PATTERNS:
        if pattern.search(name or ""):
            return why
    return ""


# ---------------------------------------------------------------------------
# OVERLAPPING CRITERIA — one weakness billed twice.
#
# Day 01's derived rubric, 17 Aug:
#
#     5 concrete steps to achieve the future self listed        40
#     Steps are specific and credible to the student's context  20
#
# Student 1021 listed five numbered steps and named a real qualification, but
# the steps were generic — so the marker deducted for genericness on BOTH, and
# one flaw cost 60 of 100 marks. The first criterion asks whether five steps
# EXIST; they did.
#
# Reported, never acted on automatically. Two criteria sharing a subject is
# often perfectly correct ("image is present" / "steps are listed" both concern
# the future self), and only a person can say whether the split is fair.
# ---------------------------------------------------------------------------

_QUALITY_WORDS = {
    "specific", "credible", "concrete", "detailed", "clear", "quality",
    "effective", "thorough", "relevant", "accurate", "depth", "strong",
    "realistic", "actionable", "coherent", "compelling", "well",
}
_STOP = {
    "the", "and", "are", "for", "with", "that", "this", "from", "into", "its",
    "their", "student", "students", "learner", "submission", "answer", "work",
    "context", "own", "year", "years", "five", "listed", "list",
}


def _subject_words(name: str) -> set:
    return {w for w in re.findall(r"[a-z]+", (name or "").lower())
            if len(w) >= 4 and w not in _STOP and w not in _QUALITY_WORDS}


def overlapping_pairs(criteria: list) -> list:
    """Pairs where one criterion grades the QUALITY of what another grades the
    PRESENCE of. Returns [(name_a, name_b, shared_word)]. Pure.

    BOTH sides must carry a quality word. That is the whole discriminator, and
    it separates the two cases exactly:

        "5 CONCRETE steps listed" + "steps are SPECIFIC and CREDIBLE"
            -> both grade quality. Vague steps lose marks twice. FLAG.

        "Five steps are listed" + "steps are SPECIFIC to the learner"
            -> the first grades presence, the second quality. Correct split,
               and each is earnable on its own. NOT flagged.

    A shared word running through most criteria is the assignment's topic
    rather than a duplicated measure, so it is ignored.
    """
    items = [c for c in (criteria or []) if (c or {}).get("name")]
    words = [_subject_words(c["name"]) for c in items]
    everywhere = {w for w in set().union(*words) if sum(w in s for s in words) > 2} \
        if words else set()

    out = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            shared = (words[i] & words[j]) - everywhere
            if not shared:
                continue
            both_grade_quality = all(
                set(re.findall(r"[a-z]+", items[k]["name"].lower())) & _QUALITY_WORDS
                for k in (i, j))
            if both_grade_quality:
                out.append((items[i]["name"], items[j]["name"], sorted(shared)[0]))
    return out


def classify(criterion: dict) -> tuple:
    """(verdict, reason) for one criterion. Pure — no I/O, so it is testable.

    verdict is one of: "ok", "stripped", "suspect".
    """
    name = str((criterion or {}).get("name") or "")
    if is_offplatform({"name": name}):
        return "stripped", "outside the submission — strip_offplatform removes this"
    why = suspect_reason(name)
    return ("suspect", why) if why else ("ok", "")


def reachable_marks(criteria: list) -> int:
    """Marks a learner can actually earn from what they submit. Pure."""
    return sum(int(c.get("maxScore") or 0) for c in (criteria or [])
               if classify(c)[0] == "ok")


def length_trap(word_min: int, kind: str) -> str:
    """Is the length minimum a penalty on an artefact-first task? Pure."""
    if kind in ARTEFACT_KINDS and int(word_min or 0) > CAPTION_WORD_MIN:
        shortfall = 1 - (CAPTION_WORD_MIN / max(int(word_min), 1))
        return (f"wordMin={word_min} on a {kind} task — a {CAPTION_WORD_MIN}-word "
                f"caption takes a {min(20, round(shortfall * 30))}-mark penalty")
    return ""


def is_fallback(criteria: list) -> bool:
    """Derivation failed and the generic rubric is being used. Pure."""
    names = {str(c.get("name") or "").lower() for c in (criteria or [])}
    return names == {c["name"].lower() for c in FALLBACK_CRITERIA}


# ---------- database (read-only) -------------------------------------------

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


def fetch_rubrics(conn, scope_ids=None) -> list:
    # created_at matters: RUBRIC_VERSION is folded into the cache key, so a row
    # derived under an older version no longer matches and will be REPLACEd on
    # the next review. Auditing it tells you about a rubric that is already on
    # its way out — worth knowing before you act on the result.
    sql = ("SELECT scope_type, scope_id, payload, created_at FROM derived_rubrics "
           "WHERE scope_type = 'assignment'")
    params = []
    if scope_ids:
        sql += " AND scope_id IN (" + ",".join(["%s"] * len(scope_ids)) + ")"
        params = list(scope_ids)
    sql += " ORDER BY scope_id"
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        return list(cur.fetchall() or [])


def fetch_titles(conn, scope_ids) -> dict:
    if not scope_ids:
        return {}
    placeholders = ",".join(["%s"] * len(scope_ids))
    with conn.cursor() as cur:
        cur.execute(f"SELECT id, title FROM assignments WHERE id IN ({placeholders})",
                    tuple(scope_ids))
        return {r["id"]: r["title"] for r in (cur.fetchall() or [])}


# ---------- reporting -------------------------------------------------------

def report(scope_id: int, title: str, payload: dict, created_at=None) -> int:
    """Print one assignment's audit. Returns its reachable ceiling."""
    criteria = payload.get("criteria") or []
    kind = payload.get("submissionKind", "?")
    word_min = payload.get("wordMin", 0)
    reachable = reachable_marks(criteria)

    print(f"\n{RULE}\nassignment {scope_id}  ·  {title or '(title not found)'}")
    print(f"kind: {kind}   wordMin: {word_min}   wordMax: {payload.get('wordMax', '?')}"
          f"   criteria: {len(criteria)}")
    if created_at:
        print(f"derived: {created_at}   (rules v{RUBRIC_VERSION} in this build)")
        print("  NOTE: if that timestamp predates your v4 deploy, this rubric is")
        print("        stale — it re-derives on the NEXT review of this item, so")
        print("        run one review first, then audit again.")
    print(RULE)

    if is_fallback(criteria):
        print("  !! FALLBACK RUBRIC — derivation failed. This assignment is being")
        print("     marked against generic criteria it was never written for.")

    for c in criteria:
        verdict, why = classify(c)
        mark = {"ok": "   ", "stripped": ">> ", "suspect": " ? "}[verdict]
        print(f"  {mark}{str(c.get('name'))[:52]:54} {c.get('maxScore'):>3}")
        if why:
            print(f"      {why}")
        earns = str(c.get("whatEarnsIt") or "")[:150]
        if earns:
            print(f"      earns it: {earns}")

    for a, b, word in overlapping_pairs(criteria):
        print(f"\n  !! OVERLAP — both of these turn on '{word}':")
        print(f"       {a}")
        print(f"       {b}")
        print(f"     One shortcoming can be charged against both, so a learner "
              f"loses\n     twice for it. Check the first measures PRESENCE only.")

    trap = length_trap(word_min, kind)
    if trap:
        print(f"\n  !! LENGTH TRAP — {trap}")

    if reachable < 100:
        print(f"\n  CEILING: a learner who did this task perfectly can reach "
              f"{reachable}/100 = {reachable / 10:.1f}/10")
        print(f"           {100 - reachable} mark(s) sit behind things the "
              f"submission cannot show.")
    else:
        print("\n  CEILING: 100/100 — every mark is earnable from the submission.")
    return reachable


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Audit derived rubrics for unreachable marks. Read-only.")
    ap.add_argument("--assignment-id", type=int, action="append",
                    help="repeatable; default is every cached assignment rubric")
    ap.add_argument("--course", action="store_true",
                    help="the eight assignments of the 30-day course")
    args = ap.parse_args()

    db_url = os.getenv("AIREV_DB_URL", "")
    if not db_url:
        sys.exit("Set AIREV_DB_URL first:\n"
                 "  cmd:  set AIREV_DB_URL=mysql://user:pass@host:port/db")

    ids = COURSE_ASSIGNMENTS if args.course else (args.assignment_id or None)

    conn = connect(db_url)
    try:
        rows = fetch_rubrics(conn, ids)
        if not rows:
            print("\nNo cached rubrics found for those assignments.")
            print(f"RUBRIC_VERSION is {RUBRIC_VERSION} — a rubric derived under an "
                  f"older version is not cached under this key and will rebuild on\n"
                  f"the next review. Run one review, then run this again.\n")
            return
        titles = fetch_titles(conn, [r["scope_id"] for r in rows])

        ceilings = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except Exception as e:
                print(f"\nassignment {r['scope_id']}: unreadable payload ({e})")
                continue
            ceilings.append((r["scope_id"], report(r["scope_id"],
                                                   titles.get(r["scope_id"], ""),
                                                   payload, r.get("created_at"))))

        print(f"\n{RULE}\nSUMMARY — highest mark a perfect submission can reach\n{RULE}")
        for scope_id, ceiling in sorted(ceilings, key=lambda x: x[1]):
            flag = "  <-- capped" if ceiling < 100 else ""
            print(f"  assignment {scope_id:<5} {ceiling / 10:>4.1f}/10{flag}")
        capped = [c for c in ceilings if c[1] < 100]
        if capped:
            print(f"\n{len(capped)} of {len(ceilings)} assignments cannot be passed "
                  f"perfectly. Fix the rubric before re-running those reviews:")
            print("  edit the task text so the deliverable carries the evidence, or")
            print("  let the criterion re-derive (RUBRIC_VERSION bump) and re-audit.")
        print("\nNothing was modified. This script only reads.\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
