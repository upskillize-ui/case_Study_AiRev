"""Scoring math is pure and must be tested — repo standard."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services.rubric_service import (
    normalise, source_hash, _as_int, FALLBACK_CRITERIA,
    is_offplatform, strip_offplatform,
)


def test_weights_always_total_100():
    import random
    random.seed(7)
    for _ in range(2000):
        n = random.randint(1, 8)
        crit = [{"name": f"C{i}", "maxScore": random.randint(1, 900)} for i in range(n)]
        assert sum(c["maxScore"] for c in normalise(crit)) == 100


def test_more_than_six_criteria_are_capped():
    crit = [{"name": f"C{i}", "maxScore": 10} for i in range(60)]
    out = normalise(crit)
    assert len(out) <= 6
    assert sum(c["maxScore"] for c in out) == 100


def test_malformed_input_falls_back():
    for bad in (None, [], [{"name": "", "maxScore": 5}], [{"maxScore": 5}],
                [{"name": "A", "maxScore": 0}], [{"name": "A", "maxScore": "x"}]):
        out = normalise(bad)
        assert sum(c["maxScore"] for c in out) == 100
        assert len(out) == len(FALLBACK_CRITERIA)


def test_float_string_weights_are_tolerated():
    out = normalise([{"name": "A", "maxScore": "33.3"}, {"name": "B", "maxScore": "66.7"}])
    assert sum(c["maxScore"] for c in out) == 100


def test_weight_matches_maxscore():
    out = normalise([{"name": "A", "maxScore": 60}, {"name": "B", "maxScore": 40}])
    assert all(abs(c["weight"] - c["maxScore"] / 100) < 1e-9 for c in out)


def test_hash_is_stable_and_edit_sensitive():
    t = {"title": "T", "description": "D", "questions": [], "maxScore": 10}
    assert source_hash(t) == source_hash(dict(t))
    assert source_hash(t) != source_hash({**t, "description": "D2"})


def test_hash_survives_decimal_marks():
    import decimal
    source_hash({"title": "T", "description": "D", "questions": [],
                 "maxScore": decimal.Decimal("10.00")})


def test_as_int_is_total():
    assert _as_int("33.3") == 33 and _as_int(None) == 0 and _as_int("x") == 0


# ── Off-platform criteria ──────────────────────────────────────────────────
# Live bug, Day 03 (12 Aug 2026): "Submission uploaded to LMS" carried weight
# 15 and scored 0/15 for a student whose work the reviewer was reading. These
# tests exist so that class of criterion can never reach a student again.

OFFPLATFORM = [
    "Submission uploaded to LMS",
    "Timely submission",
    "Deadline met",
    "Shared in the batch group",
    "Work uploaded to the portal",
    "Link shared in WhatsApp group",
    "Posted in the community group",
    "On-time submission",
    "Submitted before the deadline",
    "Attendance marked",
    "Attended the live session",
    "Artifact is live and publicly hosted",
    "The file was submitted",
]

LEGITIMATE = [
    "Submission quality",
    "Group discussion synthesis",
    "Post-implementation review",
    "Community impact analysis",
    "Deliverable completeness",
    "Research on India's fintech regulatory rules",
    "Generated image provided",
    "A published link is provided",
    "Clarity of the write-up",
    "Structure & clarity",
    "Evidence use",
    "Uploaded diagram supports the argument",
    "Recommendation strength",
]


def test_offplatform_criteria_are_detected():
    for name in OFFPLATFORM:
        assert is_offplatform({"name": name}), name


def test_legitimate_criteria_survive():
    for name in LEGITIMATE:
        assert not is_offplatform({"name": name}), name


def test_rationale_text_is_never_scanned():
    """The filter reads the criterion NAME only.

    what_earns_it is ordinary business prose: "the post opens with a hook",
    "interprets the attendance data", "describes how the site is hosted". An
    earlier version scanned it and dropped nine legitimate criteria.
    """
    assert not is_offplatform({
        "name": "Sharing strategy",
        "what_earns_it": "The student shared the post in the WhatsApp group.",
    })


# Criteria a business/AI course legitimately produces, each of which an
# over-eager filter has already been observed to swallow.
NEAR_MISS = [
    ("Quality of the LinkedIn post",
     "The post is written for a LinkedIn audience and opens with a hook."),
    ("Evidence of tool use",
     "The uploaded screenshot shows the AI platform output."),
    ("Regulatory awareness",
     "The answer notes the timely submission of STR reports to FIU-IND."),
    ("Compliance timeline",
     "Names the filings that must be submitted before the due date."),
    ("Attendance analytics",
     "The analysis interprets the attendance data in the HR dataset."),
    ("Stakeholder segmentation",
     "Identifies the shared goals of the customer group."),
    ("Landing page copy",
     "The landing page opens with a clear value proposition."),
    ("Portal design critique",
     "Critiques the upload flow of the bank's customer portal."),
    ("Deployment write-up",
     "Describes how the site is hosted and why that stack was chosen."),
]


def test_near_miss_criteria_are_not_dropped():
    for name, earns in NEAR_MISS:
        assert not is_offplatform({"name": name, "what_earns_it": earns}), name


def test_small_rubric_still_drops_offplatform_criteria():
    """A 3-criterion rubric with 2 bad ones must still be cleaned.

    The first guard demanded 2 survivors, which silently restored the original
    bug for exactly this shape.
    """
    kept, dropped = strip_offplatform([
        {"name": "Research quality",           "maxScore": 50},
        {"name": "Submission uploaded to LMS", "maxScore": 25},
        {"name": "On-time submission",         "maxScore": 25},
    ])
    assert [c["name"] for c in kept] == ["Research quality"]
    assert len(dropped) == 2
    assert sum(c["maxScore"] for c in normalise(kept)) == 100


def test_strip_then_normalise_rebalances_to_100():
    criteria = [
        {"name": "Research on fintech rules",  "maxScore": 35},
        {"name": "Identification of players",  "maxScore": 30},
        {"name": "Generated image provided",   "maxScore": 20},
        {"name": "Submission uploaded to LMS", "maxScore": 15},
    ]
    kept, dropped = strip_offplatform(criteria)
    assert [c["name"] for c in dropped] == ["Submission uploaded to LMS"]
    out = normalise(kept)
    assert sum(c["maxScore"] for c in out) == 100
    assert len(out) == 3


def test_strip_never_empties_the_rubric():
    """A rubric that is entirely off-platform is left alone: the generic
    fallback describes no task at all, which is worse than an imperfect one."""
    criteria = [{"name": "Submission uploaded to LMS", "maxScore": 50},
                {"name": "On-time submission",         "maxScore": 50}]
    kept, dropped = strip_offplatform(criteria)
    assert kept == criteria and dropped == []


def test_strip_tolerates_junk_entries():
    kept, _ = strip_offplatform([None, "x", {"name": "Evidence use", "maxScore": 50},
                                 {"name": "Reasoning", "maxScore": 50}])
    assert len(kept) == 2


# ── Stored-review completeness ─────────────────────────────────────────────
# Personalised Feedback (feedbackPoints) and The Bottom Line (hardTruth) were
# produced by the reviewer but never written to the DB, so they disappeared as
# soon as the student left the page. These tests pin the stored shape.

def test_stored_payload_carries_every_rendered_field():
    from app.services import review_payload
    result = {
        "totalScore": 37, "grade": "D", "summary": "s",
        "rubricScores": [{"criteria": "A", "maxScore": 35, "score": 16.8}],
        "strengths": ["s1"], "improvements": ["i1"],
        "feedbackPoints": ["p1", "p2"], "detailedFeedback": "d",
        "hardTruth": "the bottom line", "missingConcepts": ["m"],
        "encouragement": "e", "nextAction": "n",
    }
    p = review_payload.build(result, max_marks=10, score_marks=3.7)
    for field in ("feedbackPoints", "hardTruth", "detailedFeedback", "summary",
                  "strengths", "improvements", "missingConcepts",
                  "encouragement", "nextAction"):
        assert p[field], f"{field} missing from stored review"
    # Policy change 18 Aug: the rubric table is FACULTY-facing now — re-homed
    # under facultyView, never shown on the student card, never dropped.
    assert "rubricScores" not in p
    # 23 Aug: the rubric is gone. What faculty read is the list of things the
    # TASK asked for, in the task's own words.
    assert p["facultyView"]["requirements"], "task detail lost, not re-homed"
    assert p["outOf"] == 10 and p["scoreMarks"] == 3.7
    assert p["scorePercent"] == 37
    assert p["reviewedBy"] == "ai"


def test_stored_payload_marks_human_grades_distinctly():
    from app.services import review_payload
    p = review_payload.build({"totalScore": 8}, 10, 8, reviewed_by="faculty")
    assert p["reviewedBy"] == "faculty"


def test_scale_rubric_expresses_rows_in_task_marks():
    from app.services.scoring_service import scale_rubric
    rows = [{"criteria": "R", "maxScore": 35, "score": 16.8, "percentage": 48}]
    out = scale_rubric(rows, 10)[0]
    assert out["maxScoreMarks"] == 3.5 and out["scoreMarks"] == 1.68
    # originals preserved: old consumers must keep working
    assert out["maxScore"] == 35 and out["score"] == 16.8


def test_summary_has_no_grade_letter():
    from app.services.scoring_service import build_summary
    line = build_summary(3.7, 10, 4, 15)
    assert "(D)" not in line and "3.7 out of 10" in line
    assert "key points" in line and "core concepts engaged" not in line


def test_scale_rubric_survives_junk_scores():
    """A non-numeric score must not 500 the request AFTER the AI spend."""
    from app.services.scoring_service import scale_rubric
    out = scale_rubric([{"criteria": "A", "maxScore": "n/a", "score": None}], 10)
    assert out[0]["maxScoreMarks"] == 0 and out[0]["scoreMarks"] == 0


def test_penalty_is_carried_so_the_card_adds_up():
    """Rubric rows and the headline are both in marks now; an invisible
    word-count deduction would make the card fail its own arithmetic."""
    from app.services import review_payload
    p = review_payload.build({"totalScore": 54, "penaltyPercent": 20}, max_marks=10)
    assert p["penaltyPercent"] == 20 and p["penaltyMarks"] == 2.0


def test_case_study_marks_are_not_assumed_to_be_100():
    from app.services import review_payload
    p = review_payload.build({"totalScore": 78}, max_marks=20, score_marks=15.6)
    assert p["outOf"] == 20 and p["scoreMarks"] == 15.6


def test_summary_uses_the_real_marks_on_the_legacy_path():
    """feedback_service hardcoded 100, printing "37 out of 100" beside a
    headline of 3.7/10."""
    from app.services import feedback_service
    fb = feedback_service.generate_feedback(
        {"totalScore": 37, "grade": "D", "rubricBreakdown": []},
        {"conceptsCovered": ["a"], "conceptsMissing": ["b"]},
        120, 100, 500, max_marks=10)
    assert "3.7 out of 10" in fb["studentFeedback"]["summary"]
    assert "out of 100" not in fb["studentFeedback"]["summary"]
