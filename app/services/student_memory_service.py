# app/services/student_memory_service.py
# PERSON-MEMORY (brain spec, slice 5): the agent remembers each student
# across submissions, like a professor with thirty students — at three
# thousand. A rolling profile per student:
#
#   - last 12 review outcomes (scope, score, concepts missed, authorship est.)
#   - recurring weaknesses (a concept missed across multiple reviews)
#   - trajectory (recent scores vs earlier — improving / flat / declining)
#   - authorship history for stylometry trending
#
# Folding is PURE CODE — no AI cost per review. The profile is injected into
# the review prompt for FEEDBACK CONTINUITY ONLY: the instruction states, and
# the scoring architecture enforces (evidence gates + Python aggregation),
# that history never influences a score. A student's past cannot buy or cost
# them marks; it only makes the coaching personal:
# "Recommendation strength is still your weak spot — third review in a row."
#
# Stylometry trending: a student whose work read mostly-human for several
# submissions and suddenly reads ~AI-written gets an advisory
# 'authorship_shift' flag to the exception queue. Never a penalty — a human
# looks. This catches what single-text detection cannot.

import json
from typing import Optional

from app.database import query, execute

_TABLE = "student_memory"
_table_ready = False

MAX_ENTRIES = 12
SHIFT_MIN_HISTORY = 3     # need this many prior reviews to judge a shift
SHIFT_BASELINE_MAX = 40   # median past AI% at or below this = human-styled baseline
SHIFT_CURRENT_MIN = 80    # current AI% at or above this = discontinuity


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    execute("""
        CREATE TABLE IF NOT EXISTS student_memory (
            student_id   INT PRIMARY KEY,
            profile_json LONGTEXT NOT NULL,
            updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )
    """)
    _table_ready = True


# ─── Pure functions (unit-tested, no I/O) ────────────────────────────────────

def fold_entry(profile: dict, entry: dict) -> dict:
    """Fold one review outcome into the profile. Pure — returns new profile."""
    entries = list(profile.get("entries", []))
    entries.append({
        "scope":   entry.get("scope", ""),
        "sid":     entry.get("sid", 0),
        "score":   entry.get("score"),
        "missing": [str(m)[:80] for m in (entry.get("missing") or [])[:4]],
        "ai":      entry.get("ai"),
    })
    entries = entries[-MAX_ENTRIES:]
    return {"entries": entries, "aggregates": _aggregate(entries)}


def _aggregate(entries: list) -> dict:
    scores = [e["score"] for e in entries if isinstance(e.get("score"), (int, float))]
    ais    = [e["ai"] for e in entries if isinstance(e.get("ai"), (int, float))]

    freq: dict[str, int] = {}
    for e in entries:
        for m in e.get("missing", []):
            key = m.strip().lower()
            if key:
                freq[key] = freq.get(key, 0) + 1
    recurring = sorted(((c, n) for c, n in freq.items() if n >= 2),
                       key=lambda x: -x[1])[:4]

    trend = "flat"
    if len(scores) >= 4:
        half = len(scores) // 2
        earlier, recent = scores[:half], scores[half:]
        delta = (sum(recent) / len(recent)) - (sum(earlier) / len(earlier))
        trend = "improving" if delta >= 5 else "declining" if delta <= -5 else "flat"

    return {
        "n": len(entries),
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "trend": trend,
        "recurring": [f"{c} ({n}x)" for c, n in recurring],
        "ai_median": sorted(ais)[len(ais) // 2] if ais else None,
    }


def authorship_shift(profile: dict, current_ai_pct: int) -> bool:
    """True when a human-styled baseline suddenly reads ~AI-written.
    Advisory signal only — routed to the exception queue, never a penalty."""
    ais = [e["ai"] for e in profile.get("entries", [])
           if isinstance(e.get("ai"), (int, float))]
    if len(ais) < SHIFT_MIN_HISTORY:
        return False
    median = sorted(ais)[len(ais) // 2]
    return median <= SHIFT_BASELINE_MAX and current_ai_pct >= SHIFT_CURRENT_MIN


def render_for_prompt(profile: dict) -> str:
    """Student-continuity block for the review prompt. Empty for newcomers."""
    agg = profile.get("aggregates") or {}
    if not agg.get("n"):
        return ""
    lines = [
        "=== STUDENT CONTINUITY (for feedback WORDING only — this section "
        "must never influence any score; score purely on the current answer) ===",
        f"History: {agg['n']} prior reviews, average {agg.get('avg_score')}, "
        f"trajectory {agg.get('trend')}.",
    ]
    if agg.get("recurring"):
        lines.append("Recurring weaknesses across their reviews: "
                     + "; ".join(agg["recurring"])
                     + ". If the same gap appears again, say so plainly "
                       "('third review in a row') and escalate the coaching.")
    lines.append("If they have visibly improved on a past weakness, "
                 "acknowledge it in one clause — earned recognition, not flattery.")
    return "\n".join(lines)


# ─── DB API ──────────────────────────────────────────────────────────────────

def get_profile(student_id: int) -> dict:
    try:
        _ensure_table()
        rows = query(f"SELECT profile_json FROM {_TABLE} WHERE student_id=%s LIMIT 1",
                     (student_id,))
        if rows:
            return json.loads(rows[0]["profile_json"])
    except Exception as e:
        print(f"⚠️ student memory read failed (review proceeds): {e}")
    return {}


def fold_review(student_id: int, scope_type: str, scope_id: int,
                score: Optional[float], missing: list, ai_pct: Optional[int]) -> None:
    """Fold an outcome into the student's profile. Never raises — memory
    failures must not break a review."""
    try:
        _ensure_table()
        profile = get_profile(student_id)
        updated = fold_entry(profile, {
            "scope": scope_type, "sid": scope_id, "score": score,
            "missing": missing, "ai": ai_pct,
        })
        execute(
            f"REPLACE INTO {_TABLE} (student_id, profile_json) VALUES (%s, %s)",
            (student_id, json.dumps(updated, ensure_ascii=False)))
    except Exception as e:
        print(f"⚠️ student memory fold failed (review unaffected): {e}")
