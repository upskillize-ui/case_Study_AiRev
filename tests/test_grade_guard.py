"""IF THE AGENT DID NOT READ IT, THE AGENT DOES NOT MARK IT.

Every wrong mark AiRev has produced shares one shape: something failed on our
side, and a number was written anyway. These tests hold the gate that refuses
that shape, wherever it comes from.
"""

import pytest

from app.services import grade_guard as gg


JUDGED = [{"criteria": "A dashboard is present", "percentage": 70},
          {"criteria": "It uses the given data set", "percentage": 40}]
UNJUDGED = [{"criteria": "A dashboard is present", "percentage": None},
            {"criteria": "It uses the given data set"}]
FAILED_READ = ("=== SUBMISSION MANIFEST === 1. LINK — https://x.notion.site/p "
               "— could not be read (the page asks visitors to sign in)")
CLEAN_READ = ("=== SUBMISSION MANIFEST === 1. TYPED TEXT — answer box — "
              "read (412 words)")


# ── a mark needs a judgement behind it ──────────────────────────────────────

def test_a_real_judgement_is_allowed_through():
    ok, why = gg.may_write_grade(criteria=JUDGED, manifest=CLEAN_READ,
                                 proposed_score=55, words_read=412)
    assert ok and why == ""


def test_no_criterion_was_judged_so_no_mark_may_exist():
    """The bare '0 out of 10 with no reasons' shape."""
    ok, why = gg.may_write_grade(criteria=UNJUDGED, manifest=CLEAN_READ,
                                 proposed_score=0, words_read=412)
    assert not ok and "no judgement" in why


def test_an_empty_criteria_table_is_refused():
    ok, _ = gg.may_write_grade(criteria=[], manifest=CLEAN_READ,
                               proposed_score=30, words_read=200)
    assert not ok


def test_none_criteria_is_refused_rather_than_crashing():
    ok, _ = gg.may_write_grade(criteria=None, proposed_score=30, words_read=200)
    assert not ok


def test_junk_rows_do_not_count_as_evidence():
    ok, _ = gg.may_write_grade(criteria=["not a dict", None],
                               proposed_score=30, words_read=200)
    assert not ok


# ── a mark needs something to have been read ────────────────────────────────

def test_nothing_read_means_nothing_to_mark():
    ok, why = gg.may_write_grade(criteria=JUDGED, manifest=CLEAN_READ,
                                 proposed_score=45, words_read=0)
    assert not ok and "nothing readable" in why


# ── a zero on unread work is our failure, not their grade ───────────────────

def test_a_zero_beside_a_failed_read_is_refused():
    """The 21 Day-04 rows: marks of 0.0-4.7 while the feedback said the page
    could not be read."""
    ok, why = gg.may_write_grade(criteria=JUDGED, manifest=FAILED_READ,
                                 proposed_score=0, words_read=60)
    assert not ok and "could not be read" in why


def test_a_zero_on_work_we_DID_read_is_a_judgement_and_stands():
    """Scoring integrity cuts both ways. A genuine zero must survive."""
    ok, _ = gg.may_write_grade(criteria=JUDGED, manifest=CLEAN_READ,
                               proposed_score=0, words_read=412)
    assert ok


def test_a_low_but_nonzero_mark_beside_a_failed_read_still_stands():
    """The guard refuses zeros, not judgement. A learner whose screenshot read
    fine while their link did not is still marked on the screenshot."""
    ok, _ = gg.may_write_grade(criteria=JUDGED, manifest=FAILED_READ,
                               proposed_score=25, words_read=300)
    assert ok


def test_an_unparseable_score_does_not_crash_the_gate():
    ok, _ = gg.may_write_grade(criteria=JUDGED, manifest=CLEAN_READ,
                               proposed_score="not a number", words_read=100)
    assert ok


# ── read_failed reads the manifest, not the mood ────────────────────────────

@pytest.mark.parametrize("manifest", [
    "1. LINK — could not be read (private)",
    "1. FILE — could not be retrieved (HTTP 403)",
    "1. LINK — not rendered (the browser was busy)",
])
def test_every_failure_wording_the_intake_writes_is_recognised(manifest):
    assert gg.read_failed(manifest) is True


def test_a_clean_manifest_is_not_a_failure():
    assert gg.read_failed(CLEAN_READ) is False
    assert gg.read_failed("") is False


# ── our outage must not become their grade ──────────────────────────────────

@pytest.mark.parametrize("err", [
    Exception("All AI models are currently unavailable. Last error: 529"),
    Exception("Model returned no structured result (model=claude-haiku-4-5)"),
    Exception("Model returned empty content (likely truncated inside <think>)"),
    TimeoutError("read operation timed out"),
    Exception("HTTP 429 rate limit exceeded"),
    Exception("503 Service Unavailable"),
    Exception("Your credit balance is too low"),
])
def test_an_outage_is_recognised_and_must_not_fall_through_to_a_weaker_marker(err):
    assert gg.is_transport_failure(err) is True


@pytest.mark.parametrize("err", [
    KeyError("criteria"),
    AttributeError("'str' object has no attribute 'get'"),
    ValueError("could not convert string to float"),
])
def test_a_shape_problem_is_not_an_outage_and_may_fall_back(err):
    assert gg.is_transport_failure(err) is False


# ── the gate is at the chokepoint, not scattered across routes ──────────────

def test_the_gate_sits_on_the_single_db_writer():
    src = open("app/services/assignment_db_service.py", encoding="utf-8").read()
    assert "grade_guard.may_write_grade" in src
    guard_at = src.index("may_write_grade")
    write_at = src.index("UPDATE assignment_submissions SET")
    assert guard_at < write_at, "the guard must run before the UPDATE"


def test_a_refusal_writes_an_explanation_instead_of_a_mark():
    src = open("app/services/assignment_db_service.py", encoding="utf-8").read()
    block = src[src.index("if not allowed:"):src.index("awarded = scaled_marks")]
    assert "mark_not_graded" in block and "return False" in block


def test_both_writer_shapes_are_visible_to_the_guard():
    from app.services.assignment_db_service import _criteria_rows
    assert _criteria_rows({"facultyView": {"rubricScores": JUDGED}}) == JUDGED
    assert _criteria_rows({"rubricScores": JUDGED}) == JUDGED
    assert _criteria_rows({}) == []


def test_the_route_refuses_to_fall_back_after_an_outage():
    src = open("app/routes/assignment_review.py", encoding="utf-8").read()
    guard_at = src.index("grade_guard.is_transport_failure")
    legacy_at = src.index("falling back to legacy")
    assert guard_at < legacy_at, "the outage check must precede the fallback"
