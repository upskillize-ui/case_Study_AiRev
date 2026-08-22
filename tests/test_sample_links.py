"""sample_links: find the assignment by name, hand back real student URLs.

Pure parts only — the two SELECTs are read-only and shaped like the ones
list_assignments already proves. What matters here is that the URLs handed
to render_check are real, distinct, and spread across the cohort rather
than three consecutive rows from whoever submitted first.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from sample_links import pick_spread, urls_in


def test_trailing_punctuation_never_becomes_part_of_the_url():
    assert urls_in("my page https://x.notion.site/abc.") == \
        ["https://x.notion.site/abc"]
    assert urls_in("see (https://x.notion.site/abc)") == \
        ["https://x.notion.site/abc"]


def test_the_same_link_twice_is_one_link():
    text = "https://a.site/x and again https://a.site/x"
    assert urls_in(text) == ["https://a.site/x"]


def test_a_submission_with_no_link_yields_nothing():
    assert urls_in("I have attached my file.") == []
    assert urls_in("") == []


def test_the_sample_is_spread_across_the_cohort_not_the_first_three():
    rows = [{"student_id": i, "notes": f"https://n.site/{i}"} for i in range(30)]
    picked = pick_spread(rows, 3)
    assert [s for s, _ in picked] == [0, 10, 20]     # spread, not 0,1,2


def test_students_who_pasted_no_link_are_skipped():
    rows = [{"student_id": 1, "notes": "file attached"},
            {"student_id": 2, "notes": "https://n.site/two"}]
    assert pick_spread(rows, 2) == [(2, "https://n.site/two")]


def test_asking_for_more_than_exist_returns_what_exists():
    rows = [{"student_id": 9, "notes": "https://n.site/nine"}]
    assert len(pick_spread(rows, 5)) == 1


def test_an_empty_assignment_never_crashes():
    assert pick_spread([], 3) == []
