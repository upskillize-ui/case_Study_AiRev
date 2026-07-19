# app/services/consolidation_service.py
# The agent's SLEEP (brain spec, slice 4): while students rest, the agent
# consolidates the day's experience into durable memory. Runs nightly per
# tenant, plus on demand via the key-protected admin endpoint.
#
# What one night's sleep does, per active question/scope:
#   1. COHORT STATS   — score distribution (n, mean, spread, quartiles);
#                       drift detection (a question averaging 80+ is being
#                       over-credited; one averaging 25 may be mis-calibrated
#                       or genuinely hard — both get a note, never a silent
#                       curve. Individual scores are NEVER bent to fit.)
#   2. MISCONCEPTIONS — what this cohort actually misses, mined from real
#                       reviews, written back as calibration notes that
#                       future review prompts carry.
#   3. ANCHORS        — candidate answers re-examined by the STRONG model
#                       through three lenses (rubric-strict / evidence /
#                       comparative). Tight consensus -> anonymized anchor
#                       exemplar; future reviews see "what a verified 72
#                       looks like" instead of guessing.
#   4. GATE TUNING    — bounded, evidence-backed adjustment of scoring gates
#                       stored in agent_config. Hard bounds live in CODE, so
#                       the agent can tune its strictness but never rewrite
#                       its principles.
#
# Cost discipline: strong-model calls are capped per night
# (CONSOLIDATION_MAX_AI_CALLS, default 60) and logged. An empty night is a
# no-op that costs nothing.

import json
import os
import statistics
from typing import Optional

from app.database import query, execute, set_current_tenant
from app.tenants import TENANTS, configured_tenant_ids
from app.services import ai_service

_tables_ready = False

MIN_SAMPLE = 8              # a scope needs this many reviewed answers to consolidate
MAX_SCOPES_PER_NIGHT = 10
ANCHOR_CANDIDATES = 3       # per scope per night (high / mid / low band)
CONSENSUS_LENSES = ("rubric-strict", "evidence-anchored", "comparative")
CONSENSUS_MAX_SPREAD = 15   # lens disagreement above this rejects the anchor
ANCHOR_EXCERPT_CHARS = 1500

# Self-tuning bounds — the agent may move a knob only inside these rails.
GATE_BOUNDS = {
    "generic_answer_cap": (30, 50),
    "concept_min_ratio":  (0.4, 0.6),
}

# Where reviewed submissions live, per scope. Capstones are excluded from
# v1 consolidation: few rows, file-based content — stated, not hidden.
SOURCES = {
    "case_study": {
        "table": "case_study_submissions", "scope_col": "case_study_id",
        "text_col": "notes", "score_col": "grade", "feedback_col": "feedback",
        "where": "status = 'reviewed'",
    },
    "assignment": {
        "table": "assignment_submissions", "scope_col": "assignment_id",
        "text_col": "notes", "score_col": "grade", "feedback_col": "feedback",
        "where": "status = 'graded'",
    },
    "industry_session": {
        "table": "industry_session_submissions", "scope_col": "session_id",
        "text_col": "insight_text", "score_col": "score", "feedback_col": "feedback_json",
        "where": "has_feedback = 1",
    },
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verified_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "reason":         {"type": "string"},
    },
    "required": ["verified_score", "reason"],
}


def _ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return
    execute("""
        CREATE TABLE IF NOT EXISTS anchor_exemplars (
            id             INT AUTO_INCREMENT PRIMARY KEY,
            scope_type     VARCHAR(24) NOT NULL,
            scope_id       INT NOT NULL,
            verified_score INT NOT NULL,
            spread         INT NOT NULL,
            excerpt        TEXT NOT NULL,
            reasons        TEXT,
            active         TINYINT DEFAULT 1,
            created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_scope (scope_type, scope_id, active)
        )""")
    execute("""
        CREATE TABLE IF NOT EXISTS calibration_notes (
            id         INT AUTO_INCREMENT PRIMARY KEY,
            scope_type VARCHAR(24) NOT NULL,
            scope_id   INT NOT NULL,
            note       TEXT NOT NULL,
            evidence   TEXT,
            active     TINYINT DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_scope (scope_type, scope_id, active)
        )""")
    execute("""
        CREATE TABLE IF NOT EXISTS cohort_stats (
            scope_type VARCHAR(24) NOT NULL,
            scope_id   INT NOT NULL,
            n          INT, mean_score DECIMAL(5,2), std_score DECIMAL(5,2),
            p25 DECIMAL(5,2), p50 DECIMAL(5,2), p75 DECIMAL(5,2),
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            PRIMARY KEY (scope_type, scope_id)
        )""")
    execute("""
        CREATE TABLE IF NOT EXISTS agent_config (
            k VARCHAR(64) PRIMARY KEY,
            v VARCHAR(64) NOT NULL,
            evidence TEXT,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )""")
    _tables_ready = True


# ─── Entry points ────────────────────────────────────────────────────────────

def run_all_tenants() -> dict:
    """The nightly job body. Loops every configured tenant."""
    summary = {}
    for tid in configured_tenant_ids():
        try:
            set_current_tenant(TENANTS[tid])
            global _tables_ready
            _tables_ready = False           # tables are per-tenant DBs
            summary[tid] = consolidate_tenant()
        except Exception as e:
            print(f"❌ Consolidation failed for tenant {tid}: {e}")
            summary[tid] = {"error": str(e)[:300]}
    print(f"🌙 Consolidation complete: {json.dumps(summary, default=str)[:500]}")
    return summary


def consolidate_tenant() -> dict:
    _ensure_tables()
    budget = int(os.getenv("CONSOLIDATION_MAX_AI_CALLS", "60"))
    spent = 0
    done = {"scopes": 0, "anchors": 0, "notes": 0, "ai_calls": 0}

    for scope_type, src in SOURCES.items():
        try:
            scopes = _active_scopes(src)
        except Exception as e:
            print(f"ℹ️  {scope_type}: skip ({e})")
            continue
        for scope_id, n in scopes[:MAX_SCOPES_PER_NIGHT]:
            rows = _reviewed_rows(src, scope_id)
            if len(rows) < MIN_SAMPLE:
                continue
            stats = _update_stats(scope_type, scope_id, rows)
            done["notes"] += _mine_misconceptions(scope_type, scope_id, rows)
            done["notes"] += _drift_note(scope_type, scope_id, stats)
            if spent < budget and not _has_anchors(scope_type, scope_id):
                added, calls = _build_anchors(scope_type, scope_id, rows,
                                              budget - spent)
                done["anchors"] += added
                spent += calls
            done["scopes"] += 1

    done["ai_calls"] = spent
    _tune_gates()
    return done


# ─── Data gathering ──────────────────────────────────────────────────────────

def _active_scopes(src: dict) -> list:
    rows = query(
        f"SELECT {src['scope_col']} AS sid, COUNT(*) AS n FROM {src['table']} "
        f"WHERE {src['where']} GROUP BY {src['scope_col']} "
        f"HAVING n >= %s ORDER BY n DESC", (MIN_SAMPLE,))
    return [(r["sid"], r["n"]) for r in rows]


def _reviewed_rows(src: dict, scope_id: int) -> list:
    rows = query(
        f"SELECT {src['text_col']} AS text, {src['score_col']} AS score, "
        f"{src['feedback_col']} AS feedback FROM {src['table']} "
        f"WHERE {src['scope_col']} = %s AND {src['where']} "
        f"ORDER BY id DESC LIMIT 200", (scope_id,))
    out = []
    for r in rows:
        try:
            score = float(r["score"]) if r["score"] is not None else None
        except (TypeError, ValueError):
            score = None
        if score is None or not (r["text"] or "").strip():
            continue
        fb = {}
        if r.get("feedback"):
            try:
                fb = json.loads(r["feedback"]) if isinstance(r["feedback"], str) else r["feedback"]
            except (TypeError, ValueError):
                fb = {}
        out.append({"text": r["text"], "score": score, "feedback": fb})
    return out


# ─── 1. Cohort stats + drift ─────────────────────────────────────────────────

def _update_stats(scope_type: str, scope_id: int, rows: list) -> dict:
    scores = sorted(r["score"] for r in rows)
    n = len(scores)
    stats = {
        "n": n,
        "mean": round(statistics.fmean(scores), 2),
        "std": round(statistics.pstdev(scores), 2) if n > 1 else 0.0,
        "p25": scores[n // 4], "p50": scores[n // 2], "p75": scores[(3 * n) // 4],
    }
    execute(
        "REPLACE INTO cohort_stats (scope_type, scope_id, n, mean_score, "
        "std_score, p25, p50, p75) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (scope_type, scope_id, stats["n"], stats["mean"], stats["std"],
         stats["p25"], stats["p50"], stats["p75"]))
    return stats


def _drift_note(scope_type: str, scope_id: int, stats: dict) -> int:
    """Distribution anomalies become calibration notes — never bent scores."""
    note = None
    if stats["mean"] >= 78 and stats["n"] >= 20:
        note = (f"Cohort mean is {stats['mean']} over {stats['n']} answers — "
                f"likely over-crediting. Demand sharper evidence before the top bands.")
    elif stats["mean"] <= 28 and stats["n"] >= 20:
        note = (f"Cohort mean is {stats['mean']} over {stats['n']} answers — "
                f"verify the question and pack are fair; credit partial understanding "
                f"where the rubric allows it.")
    if not note:
        return 0
    return _write_note(scope_type, scope_id, note,
                       f"stats: {json.dumps(stats)}")


# ─── 2. Misconception mining ─────────────────────────────────────────────────

def _mine_misconceptions(scope_type: str, scope_id: int, rows: list) -> int:
    """Aggregate what the cohort misses — pure counting, no AI cost."""
    freq: dict[str, int] = {}
    for r in rows:
        missing = (r["feedback"].get("missingConcepts")
                   or r["feedback"].get("critical_gaps") or [])
        for concept in missing:
            key = str(concept).strip()[:120]
            if key:
                freq[key] = freq.get(key, 0) + 1
    n = len(rows)
    common = [(c, k) for c, k in freq.items() if k / n >= 0.4]
    if not common:
        return 0
    common.sort(key=lambda x: -x[1])
    tops = "; ".join(f"{c} ({k}/{n} missed)" for c, k in common[:4])
    note = (f"This cohort commonly misses: {tops}. Probe for these explicitly "
            f"and target feedback at them.")
    return _write_note(scope_type, scope_id, note, "misconception mining")


def _write_note(scope_type: str, scope_id: int, note: str, evidence: str) -> int:
    """Insert if not already active-duplicate. Returns rows written."""
    existing = query(
        "SELECT id FROM calibration_notes WHERE scope_type=%s AND scope_id=%s "
        "AND active=1 AND note=%s LIMIT 1", (scope_type, scope_id, note))
    if existing:
        return 0
    # One active auto-note of each kind per scope: retire older ones.
    execute(
        "UPDATE calibration_notes SET active=0 WHERE scope_type=%s AND scope_id=%s "
        "AND active=1 AND evidence LIKE %s",
        (scope_type, scope_id, evidence.split(":")[0] + "%"))
    execute(
        "INSERT INTO calibration_notes (scope_type, scope_id, note, evidence) "
        "VALUES (%s,%s,%s,%s)", (scope_type, scope_id, note, evidence[:1000]))
    return 1


# ─── 3. Consensus anchors ────────────────────────────────────────────────────

def _has_anchors(scope_type: str, scope_id: int) -> bool:
    rows = query(
        "SELECT COUNT(*) AS c FROM anchor_exemplars "
        "WHERE scope_type=%s AND scope_id=%s AND active=1",
        (scope_type, scope_id))
    return bool(rows and rows[0]["c"] >= 3)


def _pick_candidates(rows: list) -> list:
    """High / mid / low band representatives, screened for authorship."""
    def ai_pct(r):
        return (r["feedback"].get("aiLikelihoodPercent")
                or r["feedback"].get("authorship", {}).get("aiLikelihoodPercent") or 50)
    eligible = [r for r in rows if ai_pct(r) < 60 and len(r["text"]) > 200]
    if len(eligible) < 3:
        return []
    ranked = sorted(eligible, key=lambda r: -r["score"])
    return [ranked[0], ranked[len(ranked) // 2], ranked[-1]][:ANCHOR_CANDIDATES]


def _build_anchors(scope_type: str, scope_id: int, rows: list,
                   remaining_budget: int) -> tuple[int, int]:
    candidates = _pick_candidates(rows)
    calls_needed = len(candidates) * len(CONSENSUS_LENSES)
    if not candidates or calls_needed > remaining_budget:
        return 0, 0
    added, calls = 0, 0
    for cand in candidates:
        verdicts = []
        for lens in CONSENSUS_LENSES:
            try:
                v = ai_service.call_structured(
                    blocks=[{"text": _verify_prompt(lens, cand["text"]), "cache": False}],
                    schema=VERIFY_SCHEMA, tier="strong", max_tokens=600)
                verdicts.append(v)
                calls += 1
            except Exception as e:
                print(f"⚠️ consensus lens '{lens}' failed: {e}")
        if len(verdicts) < len(CONSENSUS_LENSES):
            continue
        scores = sorted(v["verified_score"] for v in verdicts)
        spread = scores[-1] - scores[0]
        if spread > CONSENSUS_MAX_SPREAD:
            print(f"ℹ️  anchor rejected ({scope_type}:{scope_id}): spread {spread}")
            continue
        median = scores[len(scores) // 2]
        execute(
            "INSERT INTO anchor_exemplars (scope_type, scope_id, verified_score, "
            "spread, excerpt, reasons) VALUES (%s,%s,%s,%s,%s,%s)",
            (scope_type, scope_id, median, spread,
             cand["text"][:ANCHOR_EXCERPT_CHARS],
             json.dumps([v["reason"] for v in verdicts])[:1500]))
        added += 1
        print(f"⚓ Anchor promoted: {scope_type}:{scope_id} score={median} spread={spread}")
    return added, calls


def _verify_prompt(lens: str, text: str) -> str:
    lens_line = {
        "rubric-strict":    "Judge ONLY against rigorous rubric standards — award nothing not demonstrated.",
        "evidence-anchored": "Judge ONLY what is substantiated by concrete evidence, examples and specifics in the text.",
        "comparative":      "Judge how this compares to what a genuinely strong practitioner answer would contain.",
    }[lens]
    return (f"You are an independent examiner re-scoring a student answer 0-100.\n"
            f"LENS: {lens_line}\nBe exacting; no grade inflation.\n\n"
            f"{ai_service.frame_student_text(text)}")


# ─── 4. Bounded gate tuning ──────────────────────────────────────────────────

def _tune_gates() -> None:
    """Adjust gates ONLY with broad statistical evidence, ONLY within bounds.
    Current policy: cohort-wide mean over many scopes drives generic_answer_cap
    one point at a time. Principles never self-modify."""
    rows = query("SELECT mean_score, n FROM cohort_stats WHERE n >= 20")
    if len(rows) < 3:
        return
    overall = statistics.fmean(float(r["mean_score"]) for r in rows)
    current = get_config_float("generic_answer_cap", 40.0)
    lo, hi = GATE_BOUNDS["generic_answer_cap"]
    target = current
    if overall >= 75:
        target = max(lo, current - 1)   # cohort-wide inflation -> tighten
    elif overall <= 35:
        target = min(hi, current + 1)   # cohort-wide harshness -> loosen
    if target != current:
        execute(
            "REPLACE INTO agent_config (k, v, evidence) VALUES (%s,%s,%s)",
            ("generic_answer_cap", str(int(target)),
             f"overall mean {overall:.1f} across {len(rows)} scopes"))
        print(f"🎛️ Gate tuned: generic_answer_cap {current} -> {target} "
              f"(bounds {lo}-{hi})")


def get_config_float(key: str, default: float) -> float:
    try:
        rows = query("SELECT v FROM agent_config WHERE k=%s LIMIT 1", (key,))
        if rows:
            val = float(rows[0]["v"])
            lo, hi = GATE_BOUNDS.get(key, (val, val))
            return max(lo, min(hi, val))   # bounds enforced in CODE, always
    except Exception:
        pass
    return default


# ─── Wake-side: what reviews recall from last night ──────────────────────────

def review_context(scope_type: str, scope_id: int) -> str:
    """Calibration notes + anchor exemplars for injection into the review
    prompt. Empty string when the agent hasn't slept on this scope yet."""
    try:
        _ensure_tables()
        parts = []
        notes = query(
            "SELECT note FROM calibration_notes WHERE scope_type=%s AND "
            "scope_id=%s AND active=1 ORDER BY id DESC LIMIT 3",
            (scope_type, scope_id))
        if notes:
            parts.append("=== CALIBRATION NOTES (learned from this cohort) ===\n"
                         + "\n".join(f"- {n['note']}" for n in notes))
        anchors = query(
            "SELECT verified_score, excerpt FROM anchor_exemplars "
            "WHERE scope_type=%s AND scope_id=%s AND active=1 "
            "ORDER BY verified_score DESC LIMIT 2",
            (scope_type, scope_id))
        if anchors:
            blocks = [f"[VERIFIED {a['verified_score']}/100 ANSWER — excerpt]\n"
                      f"{a['excerpt'][:800]}" for a in anchors]
            parts.append("=== ANCHOR EXEMPLARS (consensus-verified real answers "
                         "— compare, don't guess) ===\n" + "\n\n".join(blocks))
        return "\n\n".join(parts)
    except Exception as e:
        print(f"⚠️ review_context unavailable: {e}")
        return ""
