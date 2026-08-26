"""Guards for the six forecast risks of the next live run (26 Aug):
Total-row phantom criterion, core mis-tag, grade floor, new jargon."""
import app.services.rubric_service as rs
import app.services.review_pipeline as rp


def _req(name, role=None, marks=None, ev=True):
    r = {"name": name, "what_earns_it": "x", "evidenceable": ev}
    if role: r["role"] = role
    if marks: r["brief_marks"] = marks
    return r


def test_total_row_never_becomes_a_criterion():
    out = rs.requirements_to_criteria([
        _req("Design & presentation", marks=2),
        _req("Use of Canva AI", marks=2),
        _req("Total", marks=10),          # table arithmetic, not an item
    ])
    names = [c["name"].lower() for c in out]
    assert "total" not in names
    assert [c["maxScore"] for c in out] == [50, 50]


def test_overall_and_grand_total_also_dropped():
    out = rs.requirements_to_criteria([
        _req("The app", "core"), _req("Notes", "supporting"),
        _req("Grand Total"), _req("Overall marks"),
    ])
    names = " ".join(c["name"].lower() for c in out)
    assert "total" not in names and "overall" not in names


def test_screenshots_can_never_be_core():
    out = rs.requirements_to_criteria([
        _req("2 screenshots of your app", "core"),     # mis-tagged by model
        _req("Published app link", "core"),
        _req("Research screenshot", "supporting"),
    ])
    by = {c["name"]: c["maxScore"] for c in out}
    assert by["Published app link"] == 70          # the real build keeps 70
    assert by["2 screenshots of your app"] == 15   # demoted to supporting


def test_all_core_mis_tags_fall_back_to_equal():
    out = rs.requirements_to_criteria([
        _req("Research screenshot", "core"),
        _req("What went wrong story", "core"),
    ])
    assert [c["maxScore"] for c in out] == [50, 50]


def test_regrade_has_a_grade_floor_by_default():
    src = open("app/routes/assignment_review.py", encoding="utf-8").read()
    assert "allowLower: bool = False" in src
    assert '"skipped": "kept_higher_previous_grade"' in src
    # the floor runs BEFORE persistence
    assert (src.index("kept_higher_previous_grade")
            < src.index('"reReviewed"] = True'))


def test_new_jargon_forms_are_scrubbed():
    out = rp.simple_english(
        "The marking scheme and the grading criteria say the grader wants more.")
    low = out.lower()
    assert "marking scheme" not in low and "grading criteria" not in low
    assert "the grader" not in low
