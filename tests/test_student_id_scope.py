"""The student-id lookup must never return one learner's work to another.

Reproduces the exact production collision found on 13 Aug 2026 (students 827,
937 and 1178) and proves the strict mode closes it. No database: the SQL
expression's semantics are simulated against a real students table, so the test
runs anywhere and still fails if the expression changes meaning.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

# Real shape from production. Note the overlap that causes the bug: the value
# 937 is BOTH student 937's primary key AND student 827's user_id.
STUDENTS = {          # students.id -> students.user_id
    827: 937,
    937: 1178,
    1178: 1493,
}
# submissions: student_id column value -> which learner really owns the row
SUBMISSIONS = {827: 827, 937: 937, 1178: 1178}


def _load(strict: bool):
    """Import app.database with STRICT_STUDENT_ID set, fresh each time."""
    os.environ["STRICT_STUDENT_ID"] = "1" if strict else ""
    sys.modules.pop("app.database", None)
    return importlib.import_module("app.database")


def _matched_owners(expression: str, sid: int) -> set:
    """Evaluate the IN-list the expression builds, return the owning learners."""
    if "SELECT" not in expression:                       # strict: one exact id
        candidates = {sid}
    else:                                                # legacy: three hops
        candidates = {
            sid,
            STUDENTS.get(sid, -1),                       # id -> user_id
            next((s for s, u in STUDENTS.items() if u == sid), -1),  # user_id -> id
        }
    return {SUBMISSIONS[c] for c in candidates if c in SUBMISSIONS}


def test_legacy_lookup_leaks_across_learners():
    """The defect, pinned. Remove this only when the legacy path is deleted."""
    db = _load(strict=False)
    owners = _matched_owners(db.DUAL_ID_MATCH, 937)
    assert owners == {827, 937, 1178}, owners
    assert len(owners) == 3


def test_strict_lookup_returns_exactly_one_learner():
    db = _load(strict=True)
    for sid in STUDENTS:
        owners = _matched_owners(db.DUAL_ID_MATCH, sid)
        assert owners == {sid}, f"sid={sid} matched {owners}"


def test_no_row_is_visible_to_two_learners_under_strict():
    """The property that actually matters: every row has at most one viewer."""
    db = _load(strict=True)
    seen = {}
    for sid in STUDENTS:
        for owner in _matched_owners(db.DUAL_ID_MATCH, sid):
            seen.setdefault(owner, []).append(sid)
    for owner, viewers in seen.items():
        assert len(viewers) == 1, f"row of {owner} visible to {viewers}"


def test_param_arity_is_identical_in_both_modes():
    """Call sites pass (sid, sid, sid) — three placeholders, both modes, or
    every query in the codebase breaks."""
    assert _load(strict=False).DUAL_ID_MATCH.count("%s") == 3
    assert _load(strict=True).DUAL_ID_MATCH.count("%s") == 3


def test_default_is_legacy_behaviour():
    """Deploying must change nothing until the variable is set."""
    os.environ.pop("STRICT_STUDENT_ID", None)
    sys.modules.pop("app.database", None)
    db = importlib.import_module("app.database")
    assert db.STRICT_STUDENT_ID is False
    assert "SELECT" in db.DUAL_ID_MATCH


def test_flag_accepts_common_truthy_spellings():
    for value in ("1", "true", "TRUE", "yes", "on", " 1 "):
        os.environ["STRICT_STUDENT_ID"] = value
        sys.modules.pop("app.database", None)
        assert importlib.import_module("app.database").STRICT_STUDENT_ID is True, value
    for value in ("", "0", "false", "no", "off"):
        os.environ["STRICT_STUDENT_ID"] = value
        sys.modules.pop("app.database", None)
        assert importlib.import_module("app.database").STRICT_STUDENT_ID is False, value


# ─── id-space declaration ─────────────────────────────────────────────────
# canonical_student_id() maps users.id -> students.id with
# `WHERE user_id = given`, which never checks whether `given` is ALREADY a
# students.id. With overlapping ranges that silently returns another learner.

def test_students_space_is_never_remapped(monkeypatch):
    """The bulk-review case: a students.id must survive untouched."""
    db = _load(strict=False)
    called = {"n": 0}

    def _boom(*a, **k):                      # any DB hop here is the bug
        called["n"] += 1
        return [{"id": 827}]                 # what production would return for 937
    monkeypatch.setattr(db, "query", _boom)

    assert db.canonical_student_id(937, "students") == 937
    assert db.canonical_student_id(937, "STUDENTS") == 937
    assert db.canonical_student_id(937, " students ") == 937
    assert called["n"] == 0, "students-space id must not hit the mapping query"


def test_users_space_still_maps(monkeypatch):
    """Browser traffic keeps the existing behaviour."""
    db = _load(strict=False)
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"id": 669}])
    db._sid_cache.clear()
    assert db.canonical_student_id(774) == 669           # default = users
    db._sid_cache.clear()
    assert db.canonical_student_id(774, "users") == 669  # explicit


def test_cache_is_keyed_by_id_space(monkeypatch):
    """A users-space answer must never be served to a students-space lookup."""
    db = _load(strict=False)
    db._sid_cache.clear()
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"id": 827}])
    assert db.canonical_student_id(937, "users") == 827      # caches 937 -> 827
    assert db.canonical_student_id(937, "students") == 937   # must NOT reuse it
