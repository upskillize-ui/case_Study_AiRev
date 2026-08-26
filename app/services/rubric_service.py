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
#   v5: rule 9 (criteria must be INDEPENDENT). Assignments 17 and 23 had
#       already re-derived under v4, so without this bump they would keep the
#       overlapping criteria that cost student 1021 sixty marks for one flaw —
#       the fix would ship and change nothing for the two assignments it was
#       written for. The version is part of the cache key: change the RULES,
#       change the number, or the rules do not reach the cohort.
#   v6: THE RUBRIC IS NO LONGER INVENTED. v1-v5 asked the model to design
#       criteria AND weight them; it kept adding requirements the brief
#       never stated (Day 05 NotebookLM: "sources uploaded" + "iterative
#       review", 45 marks, neither in the task) and every learner lost
#       them before being read. From v6 the model may only LIST what the
#       task asks for, in the task's own words; weighting is arithmetic
#       (requirements_to_criteria) and unprovable requirements are
#       excluded from scoring instead of failed by the whole cohort.
RUBRIC_VERSION = 7
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

# ---------------------------------------------------------------------------
# THE LENGTH TRAP.
#
# aggregate() takes up to 20 marks off an answer shorter than wordMin. Rule 2
# of the derivation prompt tells the model to set word_min to 0-40 when the
# deliverable is an image, a link or a file, because the written part is a
# caption.
#
# Day 06 came back submission_kind="artifact_or_link" with word_min=100 anyway.
# A learner who submitted the song and a 40-word caption — exactly what was
# asked — lost 18 marks for it, on top of the 35 that were already unreachable.
#
# Prompts advise; code enforces. Same lesson as strip_offplatform.
# ---------------------------------------------------------------------------
CAPTION_WORD_MIN = 40
CAPTION_KINDS = {"image", "artifact_or_link", "file_or_workbook"}


def cap_word_min(word_min: int, submission_kind: str) -> int:
    """Clamp the length minimum when the deliverable is not prose. Pure.

    Leaves written and mixed tasks alone: an essay may fairly demand length.
    """
    value = max(0, min(2000, int(word_min or 0)))
    if submission_kind in CAPTION_KINDS:
        return min(value, CAPTION_WORD_MIN)
    return value

REQUIREMENTS_SCHEMA = {
    "type": "object",
    "properties": {
        "deliverables": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Every distinct thing the task explicitly asks the student to produce or submit. Quote the task's own wording where possible.",
        },
        "requirements": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "description": "What THIS TASK asks for, taken from the task's own words. Not qualities you think good work has — only what the task states.",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "The requirement in the TASK'S OWN WORDS, shortened to a label a student would recognise from reading the brief. Never a generic academic quality the task does not name."},
                    "what_earns_it": {"type": "string",
                                      "description": "One sentence: what the submitted text or files must contain for this requirement to be fully done. Must be answerable from the submission alone."},
                    "evidenceable": {"type": "boolean",
                                     "description": "TRUE if a marker holding only the student's text and files could tell whether this was done. FALSE for anything the finished work cannot show: which tool or model made it, which settings were toggled, the order steps were taken in, posting to WhatsApp or a group, attendance, or whether a link is live."},
                    "role": {"type": "string", "enum": ["core", "supporting"],
                             "description": "core = the main thing the task tells the learner to BUILD or CREATE (the app, the website, the deck, the written piece itself). supporting = evidence and notes AROUND that main thing: research screenshots, process write-ups, extra screenshots of the built thing, 'what went wrong' stories. Most tasks have exactly ONE core item."},
                    "brief_marks": {"type": "integer", "minimum": 0, "maximum": 100,
                                    "description": "ONLY when the task text ITSELF states marks for this item (a grading table like 'Design & Presentation - 2 marks'): copy that number exactly. Omit when the brief states no marks for it. Never invent a number."},
                },
                "required": ["name", "what_earns_it", "evidenceable", "role"],
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
    "required": ["deliverables", "requirements", "word_min", "word_max", "submission_kind"],
}

# Kept as an alias: external callers and older code refer to RUBRIC_SCHEMA.
RUBRIC_SCHEMA = REQUIREMENTS_SCHEMA

_INSTRUCTIONS = """You are reading ONE piece of coursework and listing WHAT IT ASKS FOR. You are not designing a rubric and you are not deciding what good work looks like in general.

Your only source is the task text below. Read it and write down what it tells the student to do.

RULES:
1. EVERY REQUIREMENT COMES FROM THE TASK'S OWN WORDS. If the task says "create a dashboard from a data set using Gemini Canvas", the requirements are the dashboard and the data set. Use the task's vocabulary so a student who read the brief recognises every line.
2. NEVER ADD A REQUIREMENT THE TASK DOES NOT STATE. No "depth of analysis", no "structure and clarity", no "critical reasoning", no "sources cited", no "iterative refinement" — unless the task asks for it in those terms. A requirement the brief never mentioned fails students for a rule they were never given, and that is the single worst thing this system can do.
2b. OPTIONAL IS NEVER A REQUIREMENT. Anything the brief marks as optional — "optional", "not mandatory", "you may", "if you wish", "if you want", "can also", or an equivalent — carries NO marks: leave it out of requirements entirely. It may be mentioned in feedback as coaching, never scored. Likewise, PREPARATION STEPS ARE NOT DELIVERABLES: when the brief tells the learner to research, brainstorm, or gather ideas (on Perplexity, ChatGPT, or anywhere) AS A WAY TO PRODUCE the deliverable, that step is a means, not a thing to submit — do not require a research write-up, notes, or process narrative unless the brief explicitly asks for one to be SUBMITTED. (Live failure this rule exists to prevent: a brief said "research ideas, build an app with Replit Agent, publish and share the link" — research was optional ideation, yet it became an equal-weight requirement, and learners who submitted exactly what was asked, a working published app, lost a full share of the marks for notes nobody asked them to hand in.)
3. A SHORT TASK HAS FEW REQUIREMENTS. One sentence asking for one thing yields ONE requirement. Do not pad to look thorough. Two honest requirements beat six invented ones.
4. DO NOT ASSIGN WEIGHTS. You list what was asked; the system does the weighting. This is deliberate — you are not permitted to decide that one part of the brief is worth more than another. ONE EXCEPTION: when the task text ITSELF assigns marks to items (a grading table such as "Design & Presentation - 2 marks, Use of Canva AI - 2 marks"), copy each stated number into brief_marks exactly — the faculty's own numbers ARE the weights and they override everything. Copying is not deciding; inventing a number the brief does not state is forbidden.
5. MARK evidenceable=false FOR ANYTHING THE FINISHED WORK CANNOT SHOW. A finished artifact carries no record of which AI made it, which mode was enabled, or in what order the steps were taken. Nor can the marker see WhatsApp, attendance, the LMS, or open a live URL. Those requirements are real instructions to the learner but unmarkable evidence, so they are excluded from scoring rather than failed by everyone.
   - "Use Gemini Canvas to build it"      -> evidenceable=false (a dashboard does not name its maker)
   - "A dashboard is present"             -> evidenceable=true
   - "Share the link in the WhatsApp group" -> evidenceable=false
   - "A published link is provided"       -> evidenceable=true (visible in the submission)
6. REQUIREMENTS MUST BE INDEPENDENT. Each names a DIFFERENT thing the task asked for. Never split one thing into two lines — a single shortcoming must never be chargeable twice.
7. HOW WELL each requirement was done is judged later, by a different step, on a 0-100 scale. Your job is only to say WHAT was asked. So write requirements that can be done well or badly, and do not smuggle a quality bar into the wording unless the task states one.
8. MARK EACH REQUIREMENT'S ROLE. role="core" is the main thing the task tells the learner to BUILD or CREATE — the app, the website, the presentation, the written piece itself. role="supporting" is everything asked AROUND it: research screenshots, "3 lines about your idea", extra screenshots of the built thing, "what went wrong and how you fixed it" stories. Most tasks have exactly ONE core item. The system gives core the large majority of the marks, because a learner who built the real thing must never fail on paperwork around it — and paperwork alone must never pass a learner who built nothing.

Also report word_min/word_max for the written part and the primary form of the deliverable. When the deliverable is an image, file, link or artifact, the written part is a caption: set word_min low (0-40)."""



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


# ---------------------------------------------------------------------------
# EQUAL WEIGHTING — why the model is not allowed to weight the task.
#
# Under v1-v5 the model both invented criteria AND decided their weights. On
# Day 05 (NotebookLM) it wrote "sources uploaded" and "iterative review" —
# neither in the brief — and gave them 45 marks between them. Every learner in
# the cohort lost those 45 marks before a word of their work was read.
#
# From v6 the model may only LIST what the task asked for. The split is
# arithmetic, done here: every requirement the marker can actually evidence
# carries the same weight. If faculty want a part of the task to count for
# more, they say so in the brief by asking for more of it.
# ---------------------------------------------------------------------------

def requirements_to_criteria(requirements: list) -> list:
    """Task requirements -> equally weighted criteria totalling 100. Pure.

    Requirements the marker cannot evidence are dropped, not failed: they are
    genuine instructions to the learner that the finished work cannot prove.
    Dropping them costs the learner nothing, because normalise() stretches the
    survivors back to 100.

    Falls back to keeping everything when nothing is evidenceable — a task
    whose every line is unprovable is a task-wording problem, and marking it
    against its own words is still fairer than the generic template.
    """
    clean = [r for r in (requirements or [])
             if isinstance(r, dict) and str(r.get("name") or "").strip()]
    if not clean:
        return [dict(c) for c in FALLBACK_CRITERIA]

    keepable = [r for r in clean if r.get("evidenceable", True)]
    dropped = [r for r in clean if not r.get("evidenceable", True)]
    if not keepable:
        print("⚠️  requirements: nothing in this task is evidenceable from a "
              "submission — keeping all of them rather than scoring nothing")
        keepable, dropped = clean, []
    if dropped:
        print("ℹ️  requirements: excluded from scoring (a finished submission "
              f"cannot show these) {[str(r.get('name'))[:50] for r in dropped]}")

    # FACULTY MARKS FIRST: when the brief itself publishes a marks table,
    # those numbers are the weights — they override core/supporting and
    # equal split alike. An item the table missed gets the table's average
    # so it is neither free nor fatal. normalise() rescales to 100.
    stated = [_as_int(r.get("brief_marks")) for r in keepable
              if _as_int(r.get("brief_marks")) > 0]
    if stated:
        avg = max(1, round(sum(stated) / len(stated)))
        return normalise([{"name": r["name"],
                           "maxScore": _as_int(r.get("brief_marks")) or avg,
                           "what_earns_it": r.get("what_earns_it", "")}
                          for r in keepable])

    # CORE-DOMINANT weighting (fixed arithmetic, never the model's choice):
    # when the task has both a core deliverable and supporting items, core
    # items share 70 and supporting items share 30 — a learner who built the
    # real thing lands above 60% before any paperwork is counted, and the
    # paperwork alone can never pass a learner who built nothing. When roles
    # are missing or uniform, weighting stays equal (the pre-v7 behaviour).
    core = [r for r in keepable if r.get("role") == "core"]
    # Anything not explicitly core counts as supporting — a missing or
    # misspelled role must never make a requirement vanish from scoring.
    supporting = [r for r in keepable if r not in core]
    if core and supporting:
        # Core first: normalise() keeps at most 6 criteria, so if the brief
        # lists more, the supporting tail is what gets clipped — never core.
        raw = ([{"name": r["name"], "maxScore": round(7000 / len(core)),
                 "what_earns_it": r.get("what_earns_it", "")} for r in core]
               + [{"name": r["name"], "maxScore": round(3000 / len(supporting)),
                   "what_earns_it": r.get("what_earns_it", "")}
                  for r in supporting])
        return normalise(raw)

    # Equal weights in, normalise() splits the 100 and absorbs the rounding.
    return normalise([{"name": r["name"], "maxScore": 100,
                       "what_earns_it": r.get("what_earns_it", "")}
                      for r in keepable])


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
    """Read the task and list what it asks for. Raises on AI failure.

    The model no longer designs a rubric — it extracts requirements in the
    task's own words. Weighting is done here, equally, by
    requirements_to_criteria(); see the note above it for why.
    """
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
        schema=REQUIREMENTS_SCHEMA, tier="default", max_tokens=1500,
    )
    criteria = requirements_to_criteria(result.get("requirements"))
    # Second line of defence: the deterministic off-platform filter still runs.
    # evidenceable=false is the model's judgement and it can miss one.
    kept, dropped = strip_offplatform(criteria)
    if dropped:
        print("⚠️  requirements: dropped off-platform "
              f"{[c.get('name') for c in dropped]} — weights rebalanced onto "
              "what the reviewer can actually read")
    kind = result.get("submission_kind", "mixed")
    asked_word_min = int(result.get("word_min", 30) or 0)
    word_min = cap_word_min(asked_word_min, kind)
    if word_min != asked_word_min:
        print(f"⚠️  rubric: wordMin {asked_word_min} -> {word_min} for a {kind} "
              f"deliverable — the written part is a caption, and the length "
              f"penalty would have cost marks for doing as asked")
    return {
        "criteria": normalise(kept),
        "wordMin": word_min,
        "wordMax": max(50, min(20000, int(result.get("word_max", 1500) or 1500))),
        "deliverables": [str(d)[:200] for d in (result.get("deliverables") or [])][:8],
        "submissionKind": kind,
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
