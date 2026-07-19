# app/services/review_pipeline.py
# The evidence-gated scoring engine (brain spec, slice 1).
#
# Principle: the LLM makes JUDGMENTS (what does this text demonstrate?);
# this module makes DECISIONS (what score follows?) — deterministically,
# in pure Python, so generosity is structurally impossible:
#
#   - The schema forces evidence_quotes BEFORE any score per criterion —
#     judgment must be paid for with the student's own text.
#   - GATES are declarative config applied in code, not model mood:
#       no evidence for a criterion        -> criterion capped at 20
#       no case-specific grounding         -> application/evidence dims capped at 40
#       must-cover concept coverage < 50%  -> total capped at 69
#       factual errors                     -> fixed deductions by severity
#   - aggregate() computes the total. The model never does arithmetic, so
#     the score always reconciles with its own evidence.
#   - Low confidence or a triggered garbage flag escalates to the strong
#     model with extended thinking before anything is released.
#
# Output additions: howYouScored (the score arithmetic in student language)
# and languageReport (grammar/spelling/redundancy/clarity — advisory).

import json
import os
from typing import Optional

from app.services import ai_service
from app.services.knowledge_service import render_for_prompt
from app.services.feedback_service import ai_verdict
from app.prompts import AI_DETECTION_CALIBRATION

# ─── Gates — every threshold in ONE place ────────────────────────────────────
GATES = {
    "no_evidence_cap":        20,   # criterion % cap when zero evidence quotes
    "generic_answer_cap":     40,   # cap on application/evidence-type criteria when not case-specific
    "concept_total_cap":      69,   # total cap when must-cover coverage < concept_min_ratio
    "concept_min_ratio":      0.5,
    "major_error_deduction":  5,    # per major factual error (max 3 counted)
    "minor_error_deduction":  2,    # per minor factual error (max 3 counted)
    "low_confidence_escalate": True,
}

# Criterion names whose score demands case-specific grounding.
_SPECIFICITY_BOUND = ("evidence", "application", "analysis", "depth", "practical", "recommend")

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "is_garbage":     {"type": "boolean"},
        "garbage_reason": {"type": "string"},
        "criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    # Field order is deliberate: evidence precedes judgment
                    # precedes score — verdict-first rationalization is
                    # structurally discouraged.
                    "name":            {"type": "string"},
                    "evidence_quotes": {"type": "array", "items": {"type": "string"},
                                        "description": "Verbatim quotes from the student's answer that bear on this criterion. Empty if none exist."},
                    "case_specific":   {"type": "boolean",
                                        "description": "True only if the evidence engages this material's specificity markers (its actual facts/figures/names), not generic topic talk."},
                    "judgment":        {"type": "string",
                                        "description": "1-2 sentences judging ONLY what the evidence shows."},
                    "score_pct":       {"type": "integer", "minimum": 0, "maximum": 100},
                    "confidence":      {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["name", "evidence_quotes", "case_specific",
                             "judgment", "score_pct", "confidence"],
            },
        },
        "concepts_covered": {"type": "array", "items": {"type": "string"}},
        "concepts_missing": {"type": "array", "items": {"type": "string"}},
        "factual_errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "quote":    {"type": "string"},
                    "issue":    {"type": "string"},
                    "severity": {"type": "string", "enum": ["minor", "major"]},
                },
                "required": ["quote", "issue", "severity"],
            },
        },
        "strengths":         {"type": "array", "items": {"type": "string"}},
        "improvements":      {"type": "array", "items": {"type": "string"}},
        "detailed_feedback": {"type": "string"},
        "language_report": {
            "type": "object",
            "properties": {
                "grammar_issues":   {"type": "array", "items": {
                    "type": "object",
                    "properties": {"quote": {"type": "string"}, "fix": {"type": "string"}},
                    "required": ["quote", "fix"]}},
                "spelling_examples": {"type": "array", "items": {"type": "string"}},
                "redundancy_note":  {"type": "string"},
                "clarity_note":     {"type": "string"},
            },
            "required": ["grammar_issues", "spelling_examples", "redundancy_note", "clarity_note"],
        },
        "authorship": {
            "type": "object",
            "properties": {
                "ai_likelihood_percent": {"type": "integer", "minimum": 0, "maximum": 100},
                "reason":                {"type": "string"},
            },
            "required": ["ai_likelihood_percent", "reason"],
        },
    },
    "required": ["is_garbage", "garbage_reason", "criteria", "concepts_covered",
                 "concepts_missing", "factual_errors", "strengths", "improvements",
                 "detailed_feedback", "language_report", "authorship"],
}

_JUDGE_INSTRUCTIONS = """You are AiRev's examiner. Judge the student's answer against the AGENT KNOWLEDGE above — it is your only ground truth. Be exacting in judgement, constructive in wording.

NON-NEGOTIABLE METHOD:
1. For every rubric criterion, FIRST extract verbatim evidence_quotes from the student's answer. Judge ONLY from that evidence. No evidence = say so and score accordingly (the system caps it regardless).
2. case_specific=true ONLY if the evidence engages the specificity markers — this material's actual facts, figures, names, constraints. Fluent generic prose about the topic is case_specific=false.
3. Score each criterion strictly on what its NAME demands. Do not let fluency halo into substance scores.
4. concepts_covered/missing: check against the MUST concepts in the knowledge. Mentioning a term is not covering a concept — the student must USE it correctly.
5. factual_errors: quote the exact wrong claim. major = would mislead a practitioner; minor = imprecision.
6. Feedback: direct, specific, second person. Name the paragraph/line. Coaching wording — the scores carry the severity, the words carry the way forward.
7. language_report: up to 5 grammar issues with fixes, up to 5 misspellings, one redundancy note, one clarity note. Indian English is standard usage, never an error.
8. Authorship: estimate per the calibration. Advisory only — it must not influence any score."""


# ─── Pure functions: gates + aggregation (unit-tested, no I/O) ───────────────

def apply_gates(criteria: list, rubric_criteria: list, concepts_missing: list,
                concepts_covered: list, factual_errors: list,
                gates: dict = None) -> dict:
    """Apply deterministic caps. Returns per-criterion results + gate trace.
    `gates` allows bounded, DB-tuned overrides (consolidation service);
    defaults to the static GATES config. Pure function either way."""
    GATES_ACTIVE = {**GATES, **(gates or {})}
    gates_hit = []
    by_name = { (c.get("name") or "").lower(): c for c in criteria }
    results = []

    for rc in rubric_criteria:
        name, max_score = rc["name"], rc["maxScore"]
        judged = _match(by_name, name)
        pct = int(judged.get("score_pct", 0)) if judged else 0
        evidence = judged.get("evidence_quotes", []) if judged else []

        if not evidence and pct > GATES_ACTIVE["no_evidence_cap"]:
            gates_hit.append({"gate": "no_evidence", "criterion": name,
                              "from": pct, "to": GATES_ACTIVE["no_evidence_cap"]})
            pct = GATES_ACTIVE["no_evidence_cap"]

        if (judged and not judged.get("case_specific")
                and any(k in name.lower() for k in _SPECIFICITY_BOUND)
                and pct > GATES_ACTIVE["generic_answer_cap"]):
            gates_hit.append({"gate": "generic_answer", "criterion": name,
                              "from": pct, "to": GATES_ACTIVE["generic_answer_cap"]})
            pct = GATES_ACTIVE["generic_answer_cap"]

        pct = max(0, min(100, pct))
        results.append({
            "criteria":   name,
            "maxScore":   max_score,
            "percentage": pct,
            "score":      round((pct / 100) * max_score, 2),
            "status":     "good" if pct >= 70 else "average" if pct >= 40 else "needs_improvement",
            "evidence":   evidence[:3],
            "judgment":   (judged or {}).get("judgment", "No assessment available."),
        })

    total_cap = 100
    must_total = len(concepts_missing) + len(concepts_covered)
    if must_total > 0:
        ratio = len(concepts_covered) / must_total
        if ratio < GATES_ACTIVE["concept_min_ratio"]:
            total_cap = GATES_ACTIVE["concept_total_cap"]
            gates_hit.append({"gate": "concept_coverage", "criterion": "TOTAL",
                              "from": 100, "to": total_cap,
                              "detail": f"{len(concepts_covered)}/{must_total} concepts covered"})

    deduction = 0
    majors = [e for e in factual_errors if e.get("severity") == "major"][:3]
    minors = [e for e in factual_errors if e.get("severity") == "minor"][:3]
    deduction = len(majors) * GATES_ACTIVE["major_error_deduction"] + len(minors) * GATES_ACTIVE["minor_error_deduction"]
    if deduction:
        gates_hit.append({"gate": "factual_errors", "criterion": "TOTAL",
                          "from": 0, "to": -deduction,
                          "detail": f"{len(majors)} major, {len(minors)} minor"})

    return {"breakdown": results, "total_cap": total_cap,
            "error_deduction": deduction, "gates_hit": gates_hit}


def aggregate(gated: dict, word_count: int, word_limit_min: int,
              word_limit_max: int) -> dict:
    """Compute the final score. Pure arithmetic — no model involvement."""
    raw_total = sum(r["score"] for r in gated["breakdown"])

    word_penalty, word_note = 0, ""
    if word_count < word_limit_min:
        shortfall = 1 - (word_count / max(word_limit_min, 1))
        word_penalty = min(20, round(shortfall * 30))
        word_note = (f"Answer is {word_count} words (minimum {word_limit_min}) — "
                     f"{word_penalty} point penalty.")
    elif word_count > word_limit_max * 1.5:
        word_penalty = 5
        word_note = (f"Answer is {word_count} words (guide maximum {word_limit_max}) — "
                     f"5 point penalty.")

    total = raw_total - gated["error_deduction"] - word_penalty
    total = min(total, gated["total_cap"])
    total = max(0, min(100, round(total)))

    return {
        "totalScore":       total,
        "rawTotal":         round(raw_total),
        "rubricBreakdown":  gated["breakdown"],
        "wordCountPenalty": word_penalty,
        "wordCountNote":    word_note,
        "errorDeduction":   gated["error_deduction"],
        "totalCap":         gated["total_cap"],
        "gatesHit":         gated["gates_hit"],
    }


def needs_escalation(review: dict) -> bool:
    """Low-confidence criteria or garbage suspicion warrant the strong model."""
    if not GATES["low_confidence_escalate"]:
        return False
    if review.get("is_garbage"):
        return True
    low = sum(1 for c in review.get("criteria", []) if c.get("confidence") == "low")
    return low >= 2


def build_how_you_scored(scores: dict, pack: dict, concepts_missing: list) -> str:
    """The score arithmetic, in language a student can follow."""
    lines = [f"Your score: {scores['totalScore']}/100. Here is exactly where it came from."]
    for r in scores["rubricBreakdown"]:
        earned = f"{r['criteria']} {r['score']:g}/{r['maxScore']}"
        if r["evidence"]:
            earned += f' — credit came from your own words: "{_trim(r["evidence"][0])}"'
        else:
            earned += " — no part of your answer addressed this, so it earned minimal marks"
        lines.append(earned + f". {r['judgment']}")
    for g in scores["gatesHit"]:
        lines.append(_gate_explanation(g))
    if scores["wordCountNote"]:
        lines.append(scores["wordCountNote"])
    anchor = (pack.get("band_anchors", {}) or {}).get("outstanding")
    if anchor:
        lines.append(f"What a top answer looks like on this question: {anchor}")
    if concepts_missing:
        lines.append("Concepts your answer never engaged: " + ", ".join(concepts_missing[:6]) + ".")
    return "\n".join(lines)


def _gate_explanation(g: dict) -> str:
    if g["gate"] == "no_evidence":
        return (f"Cap applied — {g['criterion']}: your answer contained nothing this "
                f"criterion could credit, so it cannot score above {g['to']}%.")
    if g["gate"] == "generic_answer":
        return (f"Cap applied — {g['criterion']}: your answer discusses the topic in "
                f"general but never engages this case's actual facts and figures, "
                f"so it cannot score above {g['to']}%. Ground your points in the case material.")
    if g["gate"] == "concept_coverage":
        return (f"Total capped at {g['to']} — {g.get('detail','')}. More than half the "
                f"core concepts are absent; no answer missing that much can reach the top bands.")
    if g["gate"] == "factual_errors":
        return f"Deduction of {-g['to']} points for factual errors ({g.get('detail','')})."
    return ""


def _match(by_name: dict, rubric_name: str) -> Optional[dict]:
    n = rubric_name.lower().strip()
    if n in by_name:
        return by_name[n]
    for k, v in by_name.items():
        if n in k or k in n:
            return v
    n_tokens = set(n.split())
    for k, v in by_name.items():
        k_tokens = set(k.split())
        if k_tokens and len(n_tokens & k_tokens) / len(n_tokens | k_tokens) >= 0.5:
            return v
    return None


def _trim(quote: str, limit: int = 120) -> str:
    q = (quote or "").strip()
    return q if len(q) <= limit else q[:limit].rstrip() + "..."


# ─── Orchestration ───────────────────────────────────────────────────────────

def review_with_knowledge(scope_type: str, scope_id: int, raw_source: dict,
                          rubric: dict, student_answer: str, word_count: int,
                          word_limit_min: int, word_limit_max: int,
                          background_tasks=None, student_id: int = 0) -> Optional[dict]:
    """Shared entry for every review type: recall (or build) the knowledge
    pack, then run the gated pipeline. Returns the pipeline result, or None
    when no pack could be built (caller falls back to its legacy path).

    One function, four callers — case study, assignment, capstone, session —
    so the recall-then-review contract lives in exactly one place.
    """
    from app.services import knowledge_service  # local import avoids cycle

    known = knowledge_service.get_or_build(
        scope_type, scope_id, raw_source, background_tasks)
    if known is None:
        return None
    return run_review(
        scope_type=scope_type, scope_id=scope_id,
        pack=known["pack"], pack_version=known["version"],
        rubric=rubric, student_answer=student_answer, word_count=word_count,
        word_limit_min=word_limit_min, word_limit_max=word_limit_max,
        student_id=student_id,
    )


def run_review(scope_type: str, pack: dict, pack_version: int,
               rubric: dict, student_answer: str, word_count: int,
               word_limit_min: int, word_limit_max: int,
               scope_id: int = 0, student_id: int = 0) -> dict:
    """Full pipeline for one submission. Raises on AI failure — the route
    owns the fallback to the legacy path."""
    rubric_criteria = rubric.get("criteria", []) or []
    criteria_list = "\n".join(f"- \"{c['name']}\" (max {c['maxScore']} points)"
                              for c in rubric_criteria)

    # What the agent learned in its sleep: calibration notes + verified
    # anchors for this scope, plus bounded gate overrides. All optional —
    # a scope the agent hasn't slept on reviews exactly as before.
    sleep_context, gate_overrides = "", {}
    if scope_id:
        try:
            from app.services import consolidation_service
            sleep_context = consolidation_service.review_context(scope_type, scope_id)
            tuned = consolidation_service.get_config_float(
                "generic_answer_cap", GATES["generic_answer_cap"])
            if tuned != GATES["generic_answer_cap"]:
                gate_overrides["generic_answer_cap"] = int(tuned)
        except Exception as ce:
            print(f"⚠️ sleep context unavailable: {ce}")

    static_block = (  # cacheable prefix — identical for every student on this item
        f"{render_for_prompt(pack)}\n\n"
        + (f"{sleep_context}\n\n" if sleep_context else "")
        + f"=== RUBRIC CRITERIA (judge each BY NAME) ===\n{criteria_list}\n\n"
        f"{AI_DETECTION_CALIBRATION}\n\n{_JUDGE_INSTRUCTIONS}"
    )

    # Person-memory: continuity context for feedback wording. Deliberately in
    # the per-student (non-cached) block, and structurally harmless to
    # scoring — evidence gates + Python aggregation mean history cannot buy
    # or cost marks.
    continuity = ""
    if student_id:
        try:
            from app.services import student_memory_service
            continuity = student_memory_service.render_for_prompt(
                student_memory_service.get_profile(student_id))
        except Exception as se:
            print(f"⚠️ student memory unavailable: {se}")

    student_block = ((continuity + "\n\n") if continuity else "") \
        + ai_service.frame_student_text(student_answer)

    review = ai_service.call_structured(
        blocks=[{"text": static_block, "cache": True},
                {"text": student_block, "cache": False}],
        schema=REVIEW_SCHEMA, tier="default", max_tokens=3500,
    )
    scoring_path = "haiku-single"

    if needs_escalation(review):
        print("ℹ️  Escalating to strong model (low confidence / garbage suspicion)")
        review = ai_service.call_structured(
            blocks=[{"text": static_block, "cache": True},
                    {"text": student_block, "cache": False}],
            schema=REVIEW_SCHEMA, tier="strong", max_tokens=3500,
            thinking_budget=int(os.getenv("THINKING_BUDGET", "2000")),
        )
        scoring_path = "sonnet-thinking-escalated"

    if review.get("is_garbage"):
        return _garbage_result(review, rubric_criteria, pack_version, scoring_path)

    gated = apply_gates(review["criteria"], rubric_criteria,
                        review["concepts_missing"], review["concepts_covered"],
                        review["factual_errors"], gates=gate_overrides)
    scores = aggregate(gated, word_count, word_limit_min, word_limit_max)

    ai_pct = max(0, min(100, int(review["authorship"]["ai_likelihood_percent"])))
    return {
        "scores": scores,
        "howYouScored": build_how_you_scored(scores, pack, review["concepts_missing"]),
        "languageReport": review["language_report"],
        "strengths": review["strengths"],
        "improvements": review["improvements"],
        "detailedFeedback": review["detailed_feedback"],
        "conceptsCovered": review["concepts_covered"],
        "conceptsMissing": review["concepts_missing"],
        "factualErrors": review["factual_errors"],
        "authorship": {
            "aiLikelihoodPercent": ai_pct,
            "humanLikelihoodPercent": 100 - ai_pct,
            "aiDetectionReason": review["authorship"]["reason"],
            "aiVerdict": ai_verdict(ai_pct),
        },
        "isGarbage": False,
        "garbageWarning": "",
        "decisions": {"packVersion": pack_version, "scoringPath": scoring_path,
                      "gatesHit": scores["gatesHit"]},
    }


def _garbage_result(review: dict, rubric_criteria: list, pack_version: int,
                    scoring_path: str) -> dict:
    breakdown = [{"criteria": c["name"], "maxScore": c["maxScore"], "score": 0,
                  "percentage": 0, "status": "needs_improvement",
                  "evidence": [], "judgment": "Not a genuine attempt."}
                 for c in rubric_criteria]
    reason = review.get("garbage_reason", "").strip()
    return {
        "scores": {"totalScore": 0, "rawTotal": 0, "rubricBreakdown": breakdown,
                   "wordCountPenalty": 0, "wordCountNote": "", "errorDeduction": 0,
                   "totalCap": 0, "gatesHit": [{"gate": "garbage", "criterion": "TOTAL",
                                                "from": 0, "to": 0, "detail": reason}]},
        "howYouScored": ("Your score: 0/100. The submission did not read as a genuine "
                         "attempt" + (f" — {reason}" if reason else "") +
                         ". Re-read the material and submit a real analysis."),
        "languageReport": review.get("language_report", {}),
        "strengths": [], "improvements":
            ["Re-read the case material and attempt a genuine analysis."],
        "detailedFeedback": "Submission flagged as non-genuine. " + reason,
        "conceptsCovered": [], "conceptsMissing": [], "factualErrors": [],
        "authorship": {"aiLikelihoodPercent": 50, "humanLikelihoodPercent": 50,
                       "aiDetectionReason": "Not assessed for non-genuine submission.",
                       "aiVerdict": "uncertain"},
        "isGarbage": True,
        "garbageWarning": ("Your submission did not appear to be a genuine attempt. "
                           + (f"Reason: {reason}. " if reason else "")
                           + "Please submit a thoughtful response."),
        "decisions": {"packVersion": pack_version, "scoringPath": scoring_path,
                      "gatesHit": ["garbage"]},
    }
