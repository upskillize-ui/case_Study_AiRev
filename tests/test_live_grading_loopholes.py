"""The three live-grading loopholes found on 28 Aug 2026.

Each one capped a student who had done the work, for a reason the card never
showed. These tests exist so none of them can come back quietly.

  L1  concept_total_cap (69) applied to ASSIGNMENTS, sourced from knowledge
      pack concepts rather than the task's own words — a second standard,
      applied to the whole score. Ranjana's ruling: off for assignments.
  L2  no_evidence_cap (20) fired on image / audio / video submissions, where
      the work was fully READ but cannot be QUOTED verbatim.
  L3  the word-count penalty (up to 20 marks) still reached MIXED tasks, so a
      published deliverable with a one-line caption was fined for its length.
"""
import pytest

from app.services.review_pipeline import (
    GATES, CONCEPT_CAP_SCOPES, concept_cap_for, has_nontext_evidence,
    apply_gates, aggregate,
)
from app.routes.assignment_review import scoring_knobs


def _crit(name, pct, quotes=(), case_specific=True):
    return {"name": name, "score_pct": pct, "evidence_quotes": list(quotes),
            "case_specific": case_specific, "judgment": ".", "confidence": "high"}


def _rubric(*names):
    share = round(100 / len(names))
    return [{"name": n, "maxScore": share} for n in names]


# ── L1 ─────────────────────────────────────────────────────────────────────
def test_concept_cap_is_off_for_assignments():
    assert concept_cap_for("assignment", 69) == 100


@pytest.mark.parametrize("scope", sorted(CONCEPT_CAP_SCOPES))
def test_concept_cap_survives_where_the_pack_is_the_syllabus(scope):
    assert concept_cap_for(scope, 69) == 69


def test_assignment_with_poor_concept_coverage_can_still_reach_full_marks():
    """The Day-14 shape: every criterion done well, most 'concepts' unmet."""
    rubric = _rubric("Three screens", "Consistent design")
    judged = [_crit("Three screens", 100, ["login, dashboard, confirmation"]),
              _crit("Consistent design", 100, ["dark blue and white throughout"])]
    gated = apply_gates(judged, rubric,
                        concepts_missing=["a", "b", "c"], concepts_covered=["d"],
                        gates={"concept_total_cap":
                               concept_cap_for("assignment",
                                               GATES["concept_total_cap"])},
                        factual_errors=[])
    assert gated["total_cap"] == 100
    assert aggregate(gated, 60, 0, 999999)["totalScore"] == 100


def test_case_study_with_poor_coverage_is_still_capped():
    rubric = _rubric("Problem framing", "Recommendation")
    judged = [_crit("Problem framing", 100, ["q"]),
              _crit("Recommendation", 100, ["q"])]
    gated = apply_gates(judged, rubric,
                        concepts_missing=["a", "b", "c"], concepts_covered=["d"],
                        gates={"concept_total_cap":
                               concept_cap_for("case_study",
                                               GATES["concept_total_cap"])},
                        factual_errors=[])
    assert gated["total_cap"] == 69
    assert aggregate(gated, 600, 0, 999999)["totalScore"] == 69


# ── L2 ─────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("manifest,expected", [
    ("=== ITEM 1: IMAGE (shot.png) ===\nVISUAL: three screens", True),
    ("=== ITEM 1: AUDIO RECORDING (song.m4a) ===\ntranscript", True),
    ("=== ITEM 1: VIDEO (clip.mp4) ===\ntranscript", True),
    ("===  item  2 :  image  (x.png) ===", True),
    ("=== ITEM 1: DOCUMENT (essay.pdf) ===\nreal quotable prose", False),
    ("=== ITEM 1: TYPED TEXT ===\nI built an app", False),
    ("", False),
])
def test_has_nontext_evidence_reads_the_manifest(manifest, expected):
    assert has_nontext_evidence(manifest) is expected


def test_attached_images_alone_prove_nontext_evidence():
    assert has_nontext_evidence("", images=[{"b64": "..."}]) is True


def test_image_submission_is_not_pinned_at_the_no_evidence_cap():
    """Day 10 / Day 15 shape: nothing quotable, work fully read and excellent."""
    rubric = _rubric("Image created", "Prompt quality")
    judged = [_crit("Image created", 100), _crit("Prompt quality", 90)]
    gated = apply_gates(judged, rubric, [], [], [], nontext_evidence=True)
    assert [r["score"] for r in gated["breakdown"]] == [50, 45]
    assert not any(g["gate"] == "no_evidence" for g in gated["gates_hit"])


def test_typed_prose_with_no_quotes_is_still_capped():
    """The gate must keep doing its job where quoting IS possible."""
    rubric = _rubric("Image created", "Prompt quality")
    judged = [_crit("Image created", 100), _crit("Prompt quality", 90)]
    gated = apply_gates(judged, rubric, [], [], [], nontext_evidence=False)
    assert all(g["to"] == GATES["no_evidence_cap"]
               for g in gated["gates_hit"] if g["gate"] == "no_evidence")
    assert [r["score"] for r in gated["breakdown"]] == [10, 10]


# ── L3 ─────────────────────────────────────────────────────────────────────
def _adaptive(kind):
    return {"submissionKind": kind, "wordMin": 150, "wordMax": 500}


def test_mixed_task_with_a_real_deliverable_waives_the_length_limits():
    overrides, wmin, wmax = scoring_knobs(_adaptive("mixed"), has_deliverable=True)
    assert (wmin, wmax) == (0, 999999)
    assert overrides == {"generic_answer_cap": 100}


def test_mixed_task_with_nothing_attached_keeps_its_limits():
    assert scoring_knobs(_adaptive("mixed"), has_deliverable=False) == (
        {}, 150, 500)


def test_written_task_stays_strict_even_when_a_file_arrives():
    """An essay uploaded as a .docx is still an essay."""
    assert scoring_knobs(_adaptive("written"), has_deliverable=True) == (
        {}, 150, 500)


def test_link_only_build_submission_loses_no_marks_for_length():
    """Three screens published, captioned in one line — 14(c): the deliverable
    is the mark."""
    _, wmin, wmax = scoring_knobs(_adaptive("mixed"), has_deliverable=True)
    rubric = _rubric("Screens designed", "Flow is logical")
    judged = [_crit("Screens designed", 100, ["login, dashboard, confirm"]),
              _crit("Flow is logical", 100, ["login, dashboard, confirm"])]
    gated = apply_gates(judged, rubric, [], [], [])
    scores = aggregate(gated, word_count=6,
                       word_limit_min=wmin, word_limit_max=wmax)
    assert scores["wordCountPenalty"] == 0
    assert scores["totalScore"] == 100


# ── Authorship visibility (28 Aug, Ranjana) ────────────────────────────────
# "remove ai and human factor bcz we are teaching ai tools and assignments
# also on that" — on a course ABOUT AI tools the estimate reads as an
# accusation for doing what was taught. Suppressed at the payload so the value
# is never stored and no future screen or export can render it.
from app.services import review_payload


def test_the_ai_tools_course_carries_no_authorship_estimate():
    assert review_payload.authorship_visible(55) is False


def test_other_courses_keep_it():
    assert review_payload.authorship_visible(46) is True
    assert review_payload.authorship_visible(47) is True


def test_an_unknown_course_behaves_as_before():
    """Never silently strip a signal because a caller forgot to pass the id."""
    assert review_payload.authorship_visible(None) is True
    assert review_payload.authorship_visible("not-a-number") is True


def _result():
    return {"totalScore": 70, "grade": "B", "aiLikelihoodPercent": 72,
            "humanLikelihoodPercent": 28, "aiDetectionReason": "generic phrasing",
            "aiVerdict": "likely-ai"}


def test_hidden_authorship_is_null_not_missing():
    """The card guards on `!= null`, so null removes the line cleanly; the KEY
    must stay so "is this an agent review?" checks still recognise the row."""
    fb = review_payload.build(_result(), 10, 7.0, show_authorship=False)
    for key in ("aiLikelihoodPercent", "humanLikelihoodPercent"):
        assert key in fb and fb[key] is None
    assert fb["aiVerdict"] == "" and fb["aiDetectionReason"] == ""


def test_authorship_still_travels_where_it_is_wanted():
    fb = review_payload.build(_result(), 10, 7.0, show_authorship=True)
    assert fb["aiLikelihoodPercent"] == 72
    assert fb["humanLikelihoodPercent"] == 28
    assert fb["aiVerdict"] == "likely-ai"


def test_hiding_authorship_never_touches_the_score():
    shown = review_payload.build(_result(), 10, 7.0, show_authorship=True)
    hidden = review_payload.build(_result(), 10, 7.0, show_authorship=False)
    assert shown["totalScore"] == hidden["totalScore"] == 70
    assert shown["scoreMarks"] == hidden["scoreMarks"] == 7.0
