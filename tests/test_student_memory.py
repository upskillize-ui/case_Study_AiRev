# tests/test_student_memory.py
# Pure-function tests for person-memory — no DB, no AI.

from app.services.student_memory_service import (
    fold_entry, authorship_shift, render_for_prompt, MAX_ENTRIES,
)


def _entry(score, missing=None, ai=30):
    return {"scope": "case_study", "sid": 1, "score": score,
            "missing": missing or [], "ai": ai}


def test_fold_keeps_rolling_window():
    p = {}
    for i in range(MAX_ENTRIES + 5):
        p = fold_entry(p, _entry(50 + i))
    assert len(p["entries"]) == MAX_ENTRIES
    assert p["entries"][-1]["score"] == 50 + MAX_ENTRIES + 4  # newest kept


def test_recurring_weaknesses_surface():
    p = {}
    for _ in range(3):
        p = fold_entry(p, _entry(55, missing=["Drawing power linkage", "FOIR"]))
    p = fold_entry(p, _entry(60, missing=["FOIR"]))
    recurring = " ".join(p["aggregates"]["recurring"]).lower()
    assert "drawing power linkage (3x)" in recurring
    assert "foir (4x)" in recurring


def test_trajectory_detection():
    p = {}
    for s in (40, 45, 42, 60, 65, 70):
        p = fold_entry(p, _entry(s))
    assert p["aggregates"]["trend"] == "improving"
    p2 = {}
    for s in (80, 75, 78, 55, 50, 52):
        p2 = fold_entry(p2, _entry(s))
    assert p2["aggregates"]["trend"] == "declining"


def test_authorship_shift_needs_baseline_and_discontinuity():
    p = {}
    for _ in range(4):
        p = fold_entry(p, _entry(60, ai=25))       # human-styled baseline
    assert authorship_shift(p, 85) is True          # sudden ~AI text -> flag
    assert authorship_shift(p, 55) is False         # moderate rise -> no flag
    # consistently AI-assisted student: no discontinuity, no flag
    q = {}
    for _ in range(4):
        q = fold_entry(q, _entry(60, ai=70))
    assert authorship_shift(q, 85) is False
    # newcomers can never be flagged
    assert authorship_shift({}, 95) is False


def test_render_states_scoring_neutrality():
    p = {}
    for s in (50, 55, 60, 65):
        p = fold_entry(p, _entry(s, missing=["Evidence linkage"]))
    text = render_for_prompt(p)
    assert "must never influence any score" in text
    assert "improving" in text or "flat" in text
    assert render_for_prompt({}) == ""              # newcomers: no block at all


if __name__ == "__main__":
    import sys, inspect
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
