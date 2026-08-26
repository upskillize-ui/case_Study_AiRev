"""RUBRIC_VERSION 7 — optional steps and preparation research never carry marks.

Live failure (Day 12, Replit Agent): the brief said research on Perplexity/
ChatGPT was optional ideation, yet it became an equal-weight requirement and
working published apps scored 0.3-2.5/10 for missing notes nobody asked for.
These tests lock the rule text so a regression is caught before deploy.
"""
import app.services.rubric_service as rs
import app.services.review_pipeline as rp


def test_extraction_rules_ban_optional_requirements():
    rules = rs._INSTRUCTIONS.lower()
    assert "optional is never a requirement" in rules
    assert "not mandatory" in rules
    assert "preparation steps are not deliverables" in rules
    # The rule must name the concrete failure mode it prevents.
    assert "research" in rules and "means, not a thing to submit" in rules


def test_judge_rules_deliverable_is_the_mark():
    rules = rp._JUDGE_INSTRUCTIONS.lower()
    assert "the deliverable is the mark" in rules
    assert "statement of intent" in rules
    assert "process narrative" in rules


def test_rubric_version_bumped_so_cached_rubrics_invalidate():
    # source_hash folds RUBRIC_VERSION into the cache key, so bumping the
    # version is what re-derives every assignment's criteria after this fix.
    assert rs.RUBRIC_VERSION >= 7
    task = {"title": "t", "description": "d", "questions": [], "maxScore": 100}
    assert rs.source_hash(task) != ""


# ── Core-dominant weighting (user policy, 26 Aug 2026) ──────────────────────
# The built thing carries the marks: core 70 / supporting 30, fixed arithmetic.

def _req(name, role=None, ev=True):
    r = {"name": name, "what_earns_it": "x", "evidenceable": ev}
    if role:
        r["role"] = role
    return r


def test_core_gets_70_supporting_splits_30():
    out = rs.requirements_to_criteria([
        _req("Published app link", "core"),
        _req("Research screenshot", "supporting"),
        _req("3 lines on the idea", "supporting"),
        _req("2 app screenshots", "supporting"),
    ])
    assert out[0]["name"] == "Published app link" and out[0]["maxScore"] == 70
    assert sum(c["maxScore"] for c in out) == 100
    # Core carries the large majority; quality decides how much is earned.
    assert out[0]["maxScore"] == 70


def test_two_core_items_share_the_70():
    out = rs.requirements_to_criteria([
        _req("The app", "core"), _req("The pitch page", "core"),
        _req("Notes", "supporting"),
    ])
    assert [c["maxScore"] for c in out] == [35, 35, 30]


def test_no_roles_keeps_equal_weighting():
    out = rs.requirements_to_criteria([_req("A"), _req("B")])
    assert [c["maxScore"] for c in out] == [50, 50]


def test_all_core_keeps_equal_weighting():
    out = rs.requirements_to_criteria([_req("A", "core"), _req("B", "core")])
    assert [c["maxScore"] for c in out] == [50, 50]


def test_unevidenceable_core_still_dropped():
    out = rs.requirements_to_criteria([
        _req("Built app", "core"),
        _req("Share on WhatsApp", "supporting", ev=False),
    ])
    assert [c["name"] for c in out] == ["Built app"]
    assert out[0]["maxScore"] == 100


def test_core_survives_the_six_criterion_clip():
    reqs = [_req("The build", "core")] + [
        _req(f"Note {i}", "supporting") for i in range(7)]
    out = rs.requirements_to_criteria(reqs)
    assert out[0]["name"] == "The build"      # core is never the one clipped
    assert len(out) <= 6
    assert sum(c["maxScore"] for c in out) == 100


def test_judge_rules_simple_english_and_quality_scale():
    rules = rp._JUDGE_INSTRUCTIONS
    # No guaranteed pass: relevance earns ~40% base, quality earns the rest.
    assert "never an automatic pass" in rules
    assert "40%" in rules and "QUALITY" in rules
    assert "copy-paste" in rules
    assert "SIMPLE ENGLISH" in rules
    assert "role" in rs._INSTRUCTIONS.lower() or "core" in rs._INSTRUCTIONS


# ── Faculty-stated marks override everything (user policy, 26 Aug 2026) ─────
# When the brief publishes its own grading table, those numbers ARE the
# weights — e.g. the Canva-resume day: Design 2, Sections 2, Content 2,
# Canva AI 2, Creativity 1, Made-in-Canva 1 (total 10 -> scaled to 100).

def test_brief_marks_table_is_used_exactly():
    out = rs.requirements_to_criteria([
        dict(_req("Design & presentation"), brief_marks=2),
        dict(_req("All required sections"), brief_marks=2),
        dict(_req("Content quality"), brief_marks=2),
        dict(_req("Use of Canva AI"), brief_marks=2),
        dict(_req("Creativity & detail"), brief_marks=1),
        dict(_req("Created in Canva"), brief_marks=1),
    ])
    assert [c["maxScore"] for c in out] == [20, 20, 20, 20, 10, 10]
    assert sum(c["maxScore"] for c in out) == 100


def test_brief_marks_beat_core_supporting():
    out = rs.requirements_to_criteria([
        dict(_req("The resume", "core"), brief_marks=6),
        dict(_req("Photo included", "supporting"), brief_marks=4),
    ])
    assert [c["maxScore"] for c in out] == [60, 40]   # table wins, not 70/30


def test_item_the_table_missed_gets_the_average():
    out = rs.requirements_to_criteria([
        dict(_req("A"), brief_marks=4),
        dict(_req("B"), brief_marks=2),
        _req("C"),                                    # brief stated no marks
    ])
    # C gets avg(4,2)=3 -> raw 4:2:3 -> ~44/22/33 after normalise
    assert sum(c["maxScore"] for c in out) == 100
    assert out[0]["maxScore"] > out[2]["maxScore"] > out[1]["maxScore"]


def test_no_brief_marks_falls_back_to_core_supporting():
    out = rs.requirements_to_criteria([
        _req("The build", "core"), _req("Notes", "supporting")])
    assert [c["maxScore"] for c in out] == [70, 30]


def test_quality_bands_are_system_wide_judge_rules():
    """User policy 26 Aug: 90-100 rare/exceptional, 80-90 really good,
    60-70 complete-but-ordinary — for every task and course, and always
    for visible quality symptoms, never for AI use itself."""
    rules = rp._JUDGE_INSTRUCTIONS
    assert "QUALITY BANDS" in rules and "EVERY COURSE" in rules
    assert "90-100 is reserved" in rules
    assert "completeness alone never buys the top bands" in rules
    assert "never for AI use itself" in rules


def test_missing_role_never_deletes_a_requirement():
    """A malformed model reply (role absent on one item) must count that item
    as supporting — before this guard it fell in neither bucket and silently
    vanished from the criteria, redistributing its marks."""
    out = rs.requirements_to_criteria([
        _req("The app", "core"), _req("Notes", "supporting"),
        _req("Screenshots"),                       # role missing
    ])
    assert [c["name"] for c in out] == ["The app", "Notes", "Screenshots"]
    assert out[0]["maxScore"] == 70
    assert sum(c["maxScore"] for c in out) == 100
