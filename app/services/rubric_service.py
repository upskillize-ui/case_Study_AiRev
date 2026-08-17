# app/services/rubric_service.py
# ---------------------------------------------------------------------------
# ADAPTIVE RUBRICS — the agent reads the task and decides what "good" means
# for THAT task, instead of grading everything against one fixed template.
#
# Why this exists (live findings, 12 Aug 2026):
#   - Every assignment fell back to Accuracy/Completeness/Reasoning/Presentation
#     (30/25/25/20). Applied to "Generate an AI image of your 5-year self and
#     list 5 steps", those criteria describe none of what the task asks, so
#     genuine work scored 0.
#   - wordLimitMin was hardcoded to 100 words for every assignment, so an
#     image-first task with a short caption took an automatic length penalty.
#
# Design:
#   - Derived ONCE per assignment from its own text, cached by content hash,
#     rebuilt automatically when faculty edit the task. Reviews therefore cost
#     nothing extra: N students on one assignment share one derivation.
#   - The agent derives independently — it does not read the faculty rubric
#     column (product decision, 12 Aug 2026: scoring is the agent's own
#     judgement of the task as written).
#   - Criteria always sum to 100. That is the scoring engine's native unit
#     (aggregate() clamps 0-100); the assignment's real marks are applied when
#     the grade is persisted, so a 10-mark task stores 7.0/10, not 70.
#   - Every failure falls back to the generic rubric. A derivation problem must
#     never block a student's review.
# ---------------------------------------------------------------------------

import hashlib
import json
import re
from typing import Optional

from app.database import tquery, texecute
from app.services import ai_service

_TABLE = "derived_rubrics"
# Bump when the derivation RULES change: it is part of the cache key, so every
# stored rubric re-derives under the new rules.
#   v2: criteria must be verifiable from the submission — "Link shared in
#       WhatsApp group" scored 0 for everyone because the agent cannot see
#       WhatsApp.
#   v3: v2 was a PROMPT rule and the model still produced "Submission uploaded
#       to LMS" (weight 15, scored 0/15 for a student whose work the reviewer
#       was holding). Prompts advise; strip_offplatform() enforces.
#   v4: HOW the work was made is not visible in the work. Day 06 (Suno) derived
#       "ChatGPT lyrics with music style line" (20) and "Suno Custom mode used"
#       (15) — 35 of 100 marks for facts a finished song cannot carry. Every
#       learner scored 0 and 15% on those, so the cohort ceiling was 6.5/10
#       before anyone was judged on the song itself. The task does name those
#       steps; the rubric's job is to measure what the SUBMISSION can show.
RUBRIC_VERSION = 4
# Per-tenant: one tenant's CREATE TABLE must never suppress another's.
_tables_ready: set = set()

# Used only when derivation is impossible. Deliberately generic — its presence
# in a review is a signal that the agent could not read the task.
FALLBACK_CRITERIA = [
    {"name": "Task completion", "maxScore": 35},
    {"name": "Accuracy",        "maxScore": 25},
    {"name": "Reasoning",       "maxScore": 25},
    {"name": "Communication",   "maxScore": 15},
]
FALLBACK_WORDS = (30, 1500)

RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "deliverables": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every distinct thing the task explicitly asks the student to produce or submit. Quote the task's own wording where possible.",
        },
        "criteria": {
            "type": "array",
            "minItems": 3,
            "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Short criterion name naming what is judged, in the task's own terms. MUST be judgeable from the submitted text/files alone — never something requiring access to WhatsApp, a live URL, attendance, or any system outside this submission."},
                    "maxScore": {"type": "integer", "minimum": 5, "maximum": 60,
                                 "description": "Weight out of 100. All criteria must total exactly 100."},
                    "what_earns_it": {"type": "string",
                                      "description": "One sentence: what the SUBMITTED TEXT OR FILES must contain to earn full marks here. If you cannot state that without needing to see something outside the submission, this criterion is invalid."},
                },
                "required": ["name", "maxScore", "what_earns_it"],
            },
        },
        "word_min": {"type": "integer", "minimum": 0, "maximum": 2000,
                     "description": "Fair MINIMUM word count for the written part. Use a low number (0-40) when the deliverable is an image, artifact, link or file and text is only a caption."},
        "word_max": {"type": "integer", "minimum": 50, "maximum": 20000},
        "submission_kind": {
            "type": "string",
            "enum": ["written", "image", "artifact_or_link", "file_or_workbook", "mixed"],
            "description": "The primary form of the deliverable.",
        },
    },
    "required": ["deliverables", "criteria", "word_min", "word_max", "submission_kind"],
}

_INSTRUCTIONS = """You are designing the marking rubric for ONE specific piece of coursework.

Read the task below and derive criteria that measure WHAT THIS TASK ACTUALLY ASKS FOR — nothing else.

RULES:
1. Start from the deliverables. If the task says "generate an image and list 5 steps", then submitting the image and listing five steps ARE the criteria. Do not import generic academic criteria the task never asked for.
2. If the deliverable is an image, a published artifact, a link or a file, the student's written text is a caption, not an essay. Set word_min low (0-40) and do NOT create criteria that demand extended prose.
3. Weight by what the task emphasises. maxScore values must total EXACTLY 100.
4. Name criteria in the task's own language, so a student reading the name knows what was judged.
5. Be demanding but fair: full marks must mean the task was genuinely done, not that words were written.
6. Never invent a requirement the task does not state.
7. VERIFIABILITY IS MANDATORY. You will judge ONLY the text and files the student uploads to this platform. You cannot open links, visit published pages, see a WhatsApp group, check attendance, or view anything outside the submission. NEVER create a criterion you could not evidence from the submission itself — an unverifiable criterion scores 0 for everyone and fails students who did the work.
   - "Link shared in WhatsApp group"        -> NOT allowed (you cannot see WhatsApp)
   - "Artifact is live and publicly hosted" -> NOT allowed (you cannot open the link)
   - "A published link is provided"          -> allowed (visible in the submission)
   - "The write-up explains what was built"  -> allowed (visible in the submission)
   Where the task requires off-platform actions, judge the evidence of them that appears IN the submission, and weight the rest onto what you can actually read.
8. HOW THE WORK WAS MADE IS NOT VISIBLE IN THE WORK. A finished artifact does not record which AI wrote its first draft, which settings were toggled, or which steps came in which order. Tasks routinely PRESCRIBE a method ("use ChatGPT for the lyrics, turn on Custom mode, then generate") — that is instruction to the learner, not something the deliverable can evidence. Never make a criterion out of it.
   - "ChatGPT was used to write the lyrics" -> NOT allowed (a song carries no authorship signature)
   - "Custom mode was enabled in the tool"  -> NOT allowed (a setting leaves no trace in the output)
   - "The workflow steps were followed in order" -> NOT allowed (unless the learner submits the record of them)
   - "The lyrics are original and on the assigned theme" -> allowed (readable in the submission)
   - "A style/genre direction is stated"     -> allowed IF the submission is asked to contain it
   Judge the OUTPUT the method was supposed to produce, and put the method's weight there. A rubric where the cohort cannot reach full marks however well they did the task is a broken rubric."""


def _ensure_table(tenant) -> None:
    key = getattr(tenant, "id", str(tenant))
    if key in _tables_ready:
        return
    texecute(tenant, """
        CREATE TABLE IF NOT EXISTS derived_rubrics (
            scope_type   VARCHAR(32)  NOT NULL,
            scope_id     INT          NOT NULL,
            source_hash  CHAR(32)     NOT NULL,
            payload      LONGTEXT     NOT NULL,
            created_at   DATETIME     DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (scope_type, scope_id)
        )
    """)
    _tables_ready.add(key)


def source_hash(task: dict) -> str:
    """Content fingerprint — editing the task invalidates its rubric."""
    blob = json.dumps({
        "title": task.get("title", ""),
        "description": task.get("description", ""),
        "questions": task.get("questions", []),
        "marks": task.get("maxScore", 100),
        "rules": RUBRIC_VERSION,
    }, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.md5(blob.encode()).hexdigest()


def _as_int(v) -> int:
    """Tolerant int: the model sometimes emits "33.3" or a Decimal."""
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Off-platform criteria — the class of rubric bug that fails honest students.
#
# AiRev receives exactly one thing: the text and files of the submission. It
# cannot see the LMS, a WhatsApp group, an attendance register, or a live URL.
# A criterion asking about any of those scores 0 for EVERY student, every time,
# and silently lowers the ceiling of the whole cohort.
#
# The v2 prompt rule asks the model not to write these. It still wrote
# "Submission uploaded to LMS" (Day 03, 12 Aug 2026). So this filter enforces
# it deterministically.
#
# SCOPE: the criterion NAME only, never what_earns_it. The rationale text is
# ordinary business prose and is full of innocent collisions — "the post opens
# with a hook", "interprets the attendance data", "describes how the site is
# hosted". Scanning it dropped nine legitimate criteria in review. A criterion
# name is short and purposeful; that is the honest signal.
#
# Each pattern requires an ACT (upload/submit/share/attend) plus its OBJECT, so
# "Portal design critique", "Landing page copy" and "Attendance analytics"
# survive while "Submission uploaded to LMS" does not.
# ---------------------------------------------------------------------------
_OFFPLATFORM_PATTERNS = [
    # Getting the work into a system: "Submission uploaded to LMS"
    re.compile(r"\b(upload|uploaded|uploading|submit|submitted|submission)\b"
               r".{0,25}\b(lms|portal|classroom|google\s+drive|google\s+form|dropbox)\b", re.I),
    # A criterion whose entire point is that the file arrived
    re.compile(r"^\s*(the\s+)?(submission|file|document|work|assignment|answer|deliverable)\s+"
               r"(is\s+|was\s+|has\s+been\s+)?(uploaded|submitted|shared|attached|received)\s*$", re.I),
    # Deadline compliance — the DB knows this, the reviewer does not
    re.compile(r"\b(on[-\s]time|timely|punctual|late)\b.{0,15}\b(submi\w+|upload\w*|delivery)\b", re.I),
    re.compile(r"\bsubmi\w+\b.{0,20}\b(on time|before the deadline|by the deadline|"
               r"within the deadline|by the due date)\b", re.I),
    re.compile(r"\bdeadline\s+(met|compliance|adherence)\b", re.I),
    # Posting somewhere the reviewer cannot read — requires the ACT, not the noun
    re.compile(r"\b(shared|share|posted|posting|published|circulated|forwarded)\b"
               r".{0,25}\b(whatsapp|telegram|slack|discord|the\s+group|group\s+chat|"
               r"community|forum|batch)\b", re.I),
    # Attendance as a fact to be checked, not as data to be analysed
    re.compile(r"\battendance\s+(is\s+|was\s+)?(marked|recorded|taken|maintained|met)\b", re.I),
    re.compile(r"\battended\b.{0,20}\b(session|class|webinar|lecture|live)\b", re.I),
    # "Link is live", "site is publicly hosted" — the reviewer cannot open it
    re.compile(r"\b(link|url|site|page|app|artifact|deployment)\b.{0,20}"
               r"\b(is\s+live|live\s+and|publicly\s+(accessible|hosted|available)|"
               r"accessible\s+online|reachable)\b", re.I),
    # A SETTING INSIDE THE TOOL. "Suno Custom mode used" (Day 06, 16 Aug) —
    # a toggle leaves no trace in the finished song, so it scored 15% for the
    # entire cohort. Requires the word "mode"/"setting" AND a state word, so
    # "Mode of address", "Setting and atmosphere" and "Custom illustration"
    # all survive.
    re.compile(r"\b(mode|setting|toggle|option|feature)\b\s*"
               r"(was\s+|is\s+|been\s+)?(used|enabled|activated|turned\s+on|"
               r"switched\s+on|selected)\b", re.I),
    re.compile(r"\b(used|enabled|activated|turned\s+on|selected)\s+"
               r"(the\s+)?\S*\s*\b(mode|setting|toggle)\b", re.I),
]

# Never strip a rubric to nothing. One real criterion is still a fair rubric;
# zero is not. (An earlier guard demanded two survivors, which quietly restored
# the exact bug on a 3-criterion rubric where two were off-platform.)
_MIN_CRITERIA_AFTER_STRIP = 1


def is_offplatform(criterion: dict) -> bool:
    """True when a criterion judges something outside the submission itself.

    Reads the NAME only — see the scope note above.
    """
    name = str((criterion or {}).get("name") or "")
    return any(p.search(name) for p in _OFFPLATFORM_PATTERNS)


def strip_offplatform(criteria: list) -> tuple:
    """Drop criteria the reviewer cannot evidence. Returns (kept, dropped).

    Weights are NOT rebalanced here — normalise() already stretches whatever
    survives back to exactly 100, so a dropped criterion costs the student
    nothing instead of costing them its full weight.
    """
    kept, dropped = [], []
    for c in (criteria or []):
        if not isinstance(c, dict):
            continue
        (dropped if is_offplatform(c) else kept).append(c)
    if len(kept) < _MIN_CRITERIA_AFTER_STRIP:
        return [c for c in (criteria or []) if isinstance(c, dict)], []
    return kept, dropped


def normalise(criteria: list) -> list:
    """Force criteria weights to total exactly 100 without losing proportions.

    The model is asked for 100 and usually complies; when it doesn't, silently
    rescaling is safer than rejecting a good rubric over arithmetic. Any
    rounding drift lands on the largest criterion so the total is exact.
    """
    clean = [c for c in (criteria or [])
             if isinstance(c, dict) and c.get("name")
             and _as_int(c.get("maxScore")) > 0][:6]   # schema says <=6; enforce it
    if not clean:
        return [dict(c) for c in FALLBACK_CRITERIA]

    total = sum(_as_int(c["maxScore"]) for c in clean)
    out = []
    for c in clean:
        out.append({
            "name": str(c["name"])[:60],
            "maxScore": max(1, round(_as_int(c["maxScore"]) * 100 / total)),
            "weight": 0.0,
            "whatEarnsIt": str(c.get("what_earns_it", ""))[:300],
        })
    drift = 100 - sum(c["maxScore"] for c in out)
    if drift:
        biggest = max(out, key=lambda c: c["maxScore"])
        biggest["maxScore"] = max(1, biggest["maxScore"] + drift)
    for c in out:
        c["weight"] = c["maxScore"] / 100
    return out


def _fallback(reason: str) -> dict:
    print(f"⚠️  rubric derivation unavailable ({reason}) — using generic rubric")
    return {
        "criteria": [dict(c, weight=c["maxScore"] / 100) for c in FALLBACK_CRITERIA],
        "wordMin": FALLBACK_WORDS[0],
        "wordMax": FALLBACK_WORDS[1],
        "deliverables": [],
        "submissionKind": "mixed",
        "derived": False,
    }


def _load(tenant, scope_type: str, scope_id: int, fresh_hash: str) -> Optional[dict]:
    _ensure_table(tenant)
    rows = tquery(
        tenant,
        f"SELECT payload, source_hash FROM {_TABLE} "
        f"WHERE scope_type=%s AND scope_id=%s LIMIT 1", (scope_type, scope_id))
    if not rows or rows[0]["source_hash"] != fresh_hash:
        return None
    try:
        return json.loads(rows[0]["payload"])
    except Exception:
        return None


def _store(tenant, scope_type: str, scope_id: int, fresh_hash: str, payload: dict) -> None:
    try:
        _ensure_table(tenant)
        texecute(
            tenant,
            f"REPLACE INTO {_TABLE} (scope_type, scope_id, source_hash, payload) "
            f"VALUES (%s, %s, %s, %s)",
            (scope_type, scope_id, fresh_hash,
             json.dumps(payload, ensure_ascii=False)))
    except Exception as e:
        print(f"⚠️  could not cache derived rubric: {e}")


def derive(task: dict) -> dict:
    """Ask the model to design a rubric for this task. Raises on AI failure."""
    questions = task.get("questions") or []
    q_text = ""
    if questions:
        q_text = "\n\nQUESTIONS ASKED:\n" + "\n".join(
            f"- {q if isinstance(q, str) else q.get('question', '')}" for q in questions)

    prompt = (
        f"{_INSTRUCTIONS}\n\n"
        f"=== TASK AS THE STUDENTS SEE IT ===\n"
        f"TITLE: {task.get('title', '') or '(untitled)'}\n"
        f"TOTAL MARKS: {task.get('maxScore', 100)}\n"
        f"BRIEF: {task.get('description', '') or '(no description provided)'}"
        f"{q_text}"
    )
    result = ai_service.call_structured(
        blocks=[{"text": prompt, "cache": False}],
        schema=RUBRIC_SCHEMA, tier="default", max_tokens=1500,
    )
    kept, dropped = strip_offplatform(result.get("criteria"))
    if dropped:
        # Visible in the Space log: a silently-narrowed rubric must be auditable.
        print("⚠️  rubric: dropped off-platform criteria "
              f"{[c.get('name') for c in dropped]} — weights rebalanced onto "
              "what the reviewer can actually read")
    return {
        "criteria": normalise(kept),
        "wordMin": max(0, min(2000, int(result.get("word_min", 30) or 0))),
        "wordMax": max(50, min(20000, int(result.get("word_max", 1500) or 1500))),
        "deliverables": [str(d)[:200] for d in (result.get("deliverables") or [])][:8],
        "submissionKind": result.get("submission_kind", "mixed"),
        "derived": True,
    }


def get_or_derive(tenant, scope_type: str, scope_id: int, task: dict) -> dict:
    """Cached adaptive rubric for one task. Never raises.

    Returns {criteria, wordMin, wordMax, deliverables, submissionKind, derived}.
    `derived=False` means the generic fallback is in use — visible in the
    review log so an undiagnosed template rubric can't hide.
    """
    try:
        fresh = source_hash(task)
    except Exception as e:
        return _fallback(f"hash failed: {e}")

    try:
        cached = _load(tenant, scope_type, scope_id, fresh)
        if cached:
            # The pattern list is NOT part of source_hash, so a rubric cached
            # under this RUBRIC_VERSION can predate a newly-added pattern.
            # Re-filtering on read makes the invariant hold either way.
            kept, dropped = strip_offplatform(cached.get("criteria"))
            if dropped:
                print("⚠️  cached rubric contained off-platform criteria "
                      f"{[c.get('name') for c in dropped]} — filtered on read")
                cached["criteria"] = normalise(kept)
            return cached
    except Exception as e:
        print(f"⚠️  rubric cache read failed: {e}")

    try:
        payload = derive(task)
    except Exception as e:
        return _fallback(str(e)[:120])

    names = ", ".join(c["name"] for c in payload["criteria"])
    print(f"🎯 Rubric derived for {scope_type} {scope_id} "
          f"({payload['submissionKind']}, words {payload['wordMin']}-{payload['wordMax']}): {names}")
    _store(tenant, scope_type, scope_id, fresh, payload)
    return payload
