# tests/test_ungraded_leak.py
# ---------------------------------------------------------------------------
# WHY THE "TO BE GRADED" LIST ONLY EVER GREW.
#
# A row entered it on submit and left it only when a review was written. Three
# things stopped reviews being written — a guard refusal, a file we could not
# open, a model that never answered — and none of the three was ever retried:
#
#   - a refusal stamped `notGraded`, and the sweeper's WHERE clause skipped
#     `notGraded` rows unconditionally, for ever;
#   - the regrade route's two "row untouched" returns left no marker at all,
#     so the row was re-selected every night, never resolved, and the student
#     was never told anything.
#
# The fix makes the skip CONDITIONAL on the marking rules being unchanged, and
# resolves unreadable rows by telling the learner. These tests pin both, plus
# the requirement-list stability rule that keeps one standard per assignment.
#
# Pure + fake DB. No network, no model, no real database.
# ---------------------------------------------------------------------------

import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from _stubs import import_with_stubs

sweeper = import_with_stubs("app.services.sweeper_service")
rubric_service = import_with_stubs("app.services.rubric_service")

FAILURES = []


def check(name, cond, detail=""):
    if cond:
        print(f"  PASS  {name}")
    else:
        print(f"  FAIL  {name} — {detail}")
        FAILURES.append(name)


class FakeDB:
    """Captures the query and returns rows, so the WHERE clause is testable."""

    def __init__(self, rows):
        self.rows, self.sql, self.params = rows, "", ()

    def __call__(self, tenant, sql, params=()):
        self.sql, self.params = sql, params
        return self.rows


def _sweep_with(rows):
    db = FakeDB(rows)
    original = sweeper.tquery
    sweeper.tquery = db
    try:
        picked = sweeper.find_unreviewed("lms")
    finally:
        sweeper.tquery = original
    return db, picked


def test_refusals_are_re_offered():
    print("\nthe permanent skip is now conditional")
    db, _ = _sweep_with([])
    check("the notGraded skip has an escape arm",
          "OR COALESCE(s.feedback, '') NOT LIKE %s" in db.sql, db.sql)
    check("the escape is the rules version",
          db.params and db.params[0] ==
          f'%"rulesVersion": {rubric_service.RUBRIC_VERSION}%', db.params)
    check("the LIKE pattern is a value, not query text — single %",
          "%%" not in (db.params[0] if db.params else "%%"), db.params)


def test_stamp_shape_matches_what_is_written():
    """The pattern must match the JSON mark_not_graded actually stores."""
    print("\nthe stamp the sweeper looks for is the stamp we write")
    stored = json.dumps({"notGraded": True, "reviewedBy": "airev",
                         "message": "…",
                         "rulesVersion": rubric_service.RUBRIC_VERSION,
                         "notGradedAt": "2026-09-02T00:00:00+00:00"},
                        ensure_ascii=False)
    needle = f'"rulesVersion": {rubric_service.RUBRIC_VERSION}'
    check("current-version refusal is matched (so it is SKIPPED)",
          needle in stored, stored[:90])

    old = json.dumps({"notGraded": True, "rulesVersion":
                      rubric_service.RUBRIC_VERSION - 1})
    check("an older refusal is NOT matched (so it is RE-OFFERED)",
          needle not in old, old)

    unstamped = json.dumps({"notGraded": True, "reviewedBy": "airev"})
    check("every refusal written before today is re-offered exactly once",
          needle not in unstamped, unstamped)


def test_course_filter_params_stay_in_order():
    print("\nthe rules-version param does not displace the course filter")
    db = FakeDB([])
    original = sweeper.tquery
    sweeper.tquery = db
    try:
        sweeper.find_unreviewed("lms", course_ids=[55, 61])
    finally:
        sweeper.tquery = original
    check("stamp first, then course ids",
          list(db.params) == [f'%"rulesVersion": {rubric_service.RUBRIC_VERSION}%',
                              55, 61], db.params)
    check("placeholders match params",
          db.sql.count("%s") == len(db.params),
          f"{db.sql.count('%s')} placeholders, {len(db.params)} params")


def test_latest_attempt_only():
    print("\none row per learner per assignment, oldest waiter first")
    rows = [{"id": 9, "assignment_id": 1, "student_id": 7, "submitted_at": "c"},
            {"id": 3, "assignment_id": 1, "student_id": 7, "submitted_at": "b"},
            {"id": 5, "assignment_id": 2, "student_id": 7, "submitted_at": "a"}]
    _, picked = _sweep_with(rows)
    check("one per (assignment, student)", len(picked) == 2, picked)
    check("oldest id served first", [p["id"] for p in picked] == [5, 9],
          [p["id"] for p in picked])


def test_one_requirement_list_per_task():
    print("\none standard per assignment, or no mark")
    fb = rubric_service._fallback("derivation down")
    check("a generic rubric is not gradeable", fb["gradeable"] is False)
    check("it says why", "derivation down" in fb["reason"], fb["reason"])
    check("four generic lines is what drift looked like",
          len(fb["criteria"]) == 4)

    a = [{"name": "Publish the app"}, {"name": "Explain the prompt"}]
    b = [{"name": "Explain the prompt"}, {"name": "Publish the app"}]
    c = [{"name": "Publish the app"}]
    check("same list, same fingerprint whatever the order",
          rubric_service.requirements_fingerprint(a) ==
          rubric_service.requirements_fingerprint(b))
    check("a different list is a different fingerprint",
          rubric_service.requirements_fingerprint(a) !=
          rubric_service.requirements_fingerprint(c))


def test_cache_read_failure_never_derives_a_private_list():
    print("\na DB hiccup must not invent a list for one learner")
    original = rubric_service._load

    def boom(*_a, **_k):
        raise RuntimeError("connection reset")

    rubric_service._load = boom
    try:
        out = rubric_service.get_or_derive(
            "lms", "assignment", 26, {"title": "t", "description": "d"})
    finally:
        rubric_service._load = original
    check("refused, not derived", out["gradeable"] is False, out.get("reason"))
    check("the reason names the cause",
          "cache read failed" in out["reason"], out["reason"])


def test_unstored_derivation_is_not_gradeable():
    print("\na list nobody else will see is not a standard")
    o_load, o_derive, o_store = (rubric_service._load, rubric_service.derive,
                                 rubric_service._store)
    rubric_service._load = lambda *a, **k: None
    rubric_service.derive = lambda task: {
        "criteria": [{"name": "Publish the app", "maxScore": 100}],
        "wordMin": 0, "wordMax": 500, "deliverables": [],
        "submissionKind": "artifact_or_link", "derived": True}
    rubric_service._store = lambda *a, **k: False
    try:
        out = rubric_service.get_or_derive(
            "lms", "assignment", 26, {"title": "t", "description": "d"})
    finally:
        (rubric_service._load, rubric_service.derive,
         rubric_service._store) = o_load, o_derive, o_store
    check("not gradeable when it did not persist", out["gradeable"] is False,
          out.get("reason"))

    rubric_service._load = lambda *a, **k: None
    rubric_service.derive = lambda task: {
        "criteria": [{"name": "Publish the app", "maxScore": 100}],
        "wordMin": 0, "wordMax": 500, "deliverables": [],
        "submissionKind": "artifact_or_link", "derived": True}
    rubric_service._store = lambda *a, **k: True
    try:
        out = rubric_service.get_or_derive(
            "lms", "assignment", 26, {"title": "t", "description": "d"})
    finally:
        (rubric_service._load, rubric_service.derive,
         rubric_service._store) = o_load, o_derive, o_store
    check("gradeable once stored", out["gradeable"] is True)
    check("and fingerprinted", len(out["fingerprint"]) == 12, out.get("fingerprint"))


if __name__ == "__main__":
    for fn in (test_refusals_are_re_offered, test_stamp_shape_matches_what_is_written,
               test_course_filter_params_stay_in_order, test_latest_attempt_only,
               test_one_requirement_list_per_task,
               test_cache_read_failure_never_derives_a_private_list,
               test_unstored_derivation_is_not_gradeable):
        fn()
    print(f"\n{'FAILED: ' + ', '.join(FAILURES) if FAILURES else 'ALL PASS'}")
    sys.exit(1 if FAILURES else 0)
