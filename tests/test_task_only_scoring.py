"""Task-only scoring (v6).

The fault this file locks down: for five versions the model was asked to
DESIGN a rubric, so it kept adding requirements the brief never stated and
weighting them however it liked. Day 05 (NotebookLM) derived "sources
uploaded" and "iterative review" — 45 marks between them, neither in the task
— and the whole cohort lost them before a word was read.

From v6 the model may only LIST what the task asks for. Weighting is
arithmetic, done in Python, and equal.
"""

from app.services import rubric_service as rs
from app.services import review_pipeline as rp


# ── The model no longer weights the task ────────────────────────────────────

def test_every_requirement_carries_the_same_weight():
    out = rs.requirements_to_criteria([
        {"name": "A dashboard is present", "what_earns_it": "x", "evidenceable": True},
        {"name": "It uses the given data set", "what_earns_it": "y", "evidenceable": True},
    ])
    assert [c["maxScore"] for c in out] == [50, 50]


def test_weights_still_total_exactly_100_when_they_do_not_divide():
    out = rs.requirements_to_criteria([
        {"name": f"R{i}", "what_earns_it": "x", "evidenceable": True}
        for i in range(3)
    ])
    assert sum(c["maxScore"] for c in out) == 100


def test_a_model_supplied_weight_is_ignored():
    """Even if the model smuggles maxScore in, it must not survive."""
    out = rs.requirements_to_criteria([
        {"name": "A", "maxScore": 90, "what_earns_it": "x", "evidenceable": True},
        {"name": "B", "maxScore": 10, "what_earns_it": "y", "evidenceable": True},
    ])
    assert [c["maxScore"] for c in out] == [50, 50]


# ── Unprovable requirements are excluded, not failed ────────────────────────

def test_unevidenceable_requirements_are_dropped_and_cost_the_learner_nothing():
    """The Day 06 Suno shape: 'Custom mode was used' cannot be seen in a song."""
    out = rs.requirements_to_criteria([
        {"name": "A song is submitted", "what_earns_it": "x", "evidenceable": True},
        {"name": "Suno Custom mode was used", "what_earns_it": "y", "evidenceable": False},
    ])
    assert [c["name"] for c in out] == ["A song is submitted"]
    assert out[0]["maxScore"] == 100          # the survivor is stretched back up


def test_a_wholly_unevidenceable_task_keeps_its_requirements():
    """Scoring nothing is worse than scoring the task as written."""
    out = rs.requirements_to_criteria([
        {"name": "Posted in the WhatsApp group", "what_earns_it": "x", "evidenceable": False},
    ])
    assert [c["name"] for c in out] == ["Posted in the WhatsApp group"]


def test_missing_evidenceable_flag_is_treated_as_markable():
    out = rs.requirements_to_criteria([{"name": "A", "what_earns_it": "x"}])
    assert [c["name"] for c in out] == ["A"]


def test_empty_requirements_fall_back_rather_than_scoring_nothing():
    out = rs.requirements_to_criteria([])
    assert len(out) == len(rs.FALLBACK_CRITERIA)


def test_junk_entries_are_ignored():
    out = rs.requirements_to_criteria([
        {"name": "Real", "what_earns_it": "x", "evidenceable": True},
        {"name": "   "}, None, "not a dict",
    ])
    assert [c["name"] for c in out] == ["Real"]


def test_task_wording_reaches_the_criterion():
    out = rs.requirements_to_criteria([
        {"name": "A dashboard is present",
         "what_earns_it": "The submission contains a dashboard built from the data set.",
         "evidenceable": True}])
    assert out[0]["whatEarnsIt"].startswith("The submission contains a dashboard")


# ── The derivation prompt forbids invention ─────────────────────────────────

def test_the_prompt_forbids_adding_requirements_the_task_never_stated():
    rules = rs._INSTRUCTIONS.lower()
    assert "never add a requirement the task does not state" in rules
    for invented in ("depth of analysis", "structure and clarity",
                     "critical reasoning", "iterative refinement"):
        assert invented in rules      # named explicitly as forbidden examples


def test_the_prompt_forbids_the_model_from_weighting():
    assert "do not assign weights" in rs._INSTRUCTIONS.lower()


def test_the_schema_gives_the_model_no_weight_field():
    props = rs.REQUIREMENTS_SCHEMA["properties"]["requirements"]["items"]["properties"]
    assert "maxScore" not in props and "weight" not in props
    # "role" is a classification (core vs supporting), not a number — the
    # 70/30 arithmetic stays in Python, so the model still sets no weights.
    # "brief_marks" is a COPY of marks the faculty printed in the brief's own
    # grading table — copying is not deciding, so the no-invented-weights
    # rule stands.
    assert set(props) == {"name", "what_earns_it", "evidenceable", "role",
                          "brief_marks"}
    assert set(props["role"]["enum"]) == {"core", "supporting"}
    assert "brief_marks" not in rs.REQUIREMENTS_SCHEMA[
        "properties"]["requirements"]["items"]["required"]


def test_the_version_bumped_so_cached_invented_rubrics_re_derive():
    assert rs.RUBRIC_VERSION >= 6


# ── The judge is told the requirement list is the whole standard ────────────

def test_the_judge_may_not_score_against_anything_outside_the_requirements():
    j = rp._JUDGE_INSTRUCTIONS.lower()
    assert "the criteria list is the entire standard" in j
    assert "nothing else" in j


def test_the_judge_receives_the_tasks_own_wording_not_just_a_label():
    src = open("app/services/review_pipeline.py", encoding="utf-8").read()
    assert "fully done when:" in src


# ── A7: packaging is not the work (28 Aug 2026) ────────────────────────────
# The Day-14 Figma card told a learner, as a shortfall on a DESIGN task:
# "Confirm the export and submission steps: export all three screens as PNG or
# JPG at high quality, upload to the LMS, and share via WhatsApp."
#
# _OFFPLATFORM_PATTERNS drops pure logistics, but it needs an ACT plus an
# OBJECT ("uploaded to LMS"), so "export as PNG at high quality" survived as a
# real criterion — and nothing demoted it, so the 70/30 split could hand it
# CORE weight beside the design itself.

import pytest

from app.services.rubric_service import requirements_to_criteria


def _share(criteria, exact_name):
    """The marks held by the criterion with EXACTLY this name.

    Matching on a fragment silently matched the wrong row here: "Screens
    exported as PNG" and "Three screens designed in Figma" share the word
    "screens", so a fragment lookup returned the design's own weight and the
    assertion compared 70 against itself.
    """
    for c in criteria:
        if c["name"].strip().lower() == exact_name.strip().lower():
            return c["maxScore"]
    raise AssertionError(f"no criterion named {exact_name!r} in "
                         f"{[c['name'] for c in criteria]}")


PACKAGING_NAMES = [
    "Screens exported as PNG or JPG at high quality",
    "Files saved as PDF",
    "Export quality is high",
    "Correct file format used",
    "File naming convention followed",
]


@pytest.mark.parametrize("packaging", PACKAGING_NAMES)
def test_packaging_never_outweighs_the_thing_that_was_built(packaging):
    reqs = [
        {"name": "Three screens designed in Figma", "what_earns_it": "...",
         "evidenceable": True, "role": "core"},
        {"name": packaging, "what_earns_it": "...",
         "evidenceable": True, "role": "core"},
    ]
    out = requirements_to_criteria(reqs)
    built = _share(out, "Three screens designed in Figma")
    pack = _share(out, packaging)
    assert built > pack, (
        f"packaging ({pack}) is worth as much as the design ({built}): {out}")
    assert built >= 65, f"the built thing must hold core weight: {out}"


def test_the_design_still_wins_when_it_is_the_only_core_item():
    reqs = [
        {"name": "A working 3-screen prototype", "what_earns_it": "...",
         "evidenceable": True, "role": "core"},
        {"name": "Screens exported as PNG", "what_earns_it": "...",
         "evidenceable": True, "role": "core"},
        {"name": "2 screenshots of your work", "what_earns_it": "...",
         "evidenceable": True, "role": "core"},
    ]
    out = requirements_to_criteria(reqs)
    assert _share(out, "A working 3-screen prototype") == 70


# ── The deliverable can never be "unprovable" (28 Aug 2026) ────────────────
# Rule 5 asks the model to drop criteria a finished submission cannot show.
# It applied that to the DELIVERABLE whenever the criterion also named the
# tool that made it:
#
#   Day 07  "Dashboard from a data set using Gemini Canvas"  100 marks
#   Day 09  "Gamma presentation on Data Science"              34 marks
#
# Day 07's was the ONLY criterion, so audit_rubrics reported a ceiling of
# 0.0/10: 195 learners mathematically unable to score, 77 already holding a
# mark from it. Prompts advise, code enforces.

from app.services.rubric_service import (
    force_visible_deliverables, requirements_to_criteria,
)


def _req(name, evidenceable=False, role="core"):
    return {"name": name, "what_earns_it": "", "evidenceable": evidenceable,
            "role": role}


@pytest.mark.parametrize("name", [
    "Dashboard from a data set using Gemini Canvas",
    "Gamma presentation on Data Science",
    "Song created with Suno",
    "A 60-90 second video made with ElevenLabs",
    "Portfolio website built in Lovable",
    "Speaker Report Card image",
])
def test_a_thing_the_learner_produced_is_always_scoreable(name):
    assert force_visible_deliverables([_req(name)])[0]["evidenceable"] is True


@pytest.mark.parametrize("name", [
    "Use Gemini Canvas to build it",
    "ChatGPT assessment included",
    "Yoodli practice session completed",
    "Share the link in the WhatsApp group",
    "Attendance marked for the session",
])
def test_a_claim_about_method_stays_unprovable(name):
    """These name no produced thing, so nothing in the submission can show
    them. Un-dropping these would fail every learner on a promise instead."""
    assert force_visible_deliverables([_req(name)])[0]["evidenceable"] is False


def test_the_input_is_never_mutated():
    reqs = [_req("Dashboard built with Gemini Canvas")]
    force_visible_deliverables(reqs)
    assert reqs[0]["evidenceable"] is False, "caller's list was mutated"


def test_day_07_can_now_be_passed():
    """The whole task was one criterion, and it was excluded from scoring."""
    criteria = requirements_to_criteria(
        [_req("Dashboard from a data set using Gemini Canvas")])
    assert sum(c["maxScore"] for c in criteria) == 100
    assert "Dashboard" in criteria[0]["name"]


def test_a_logistics_line_naming_the_artefact_is_still_dropped():
    """force_visible_deliverables runs BEFORE strip_offplatform, which is what
    keeps 'share the dashboard link on WhatsApp' out of scoring."""
    from app.services.rubric_service import strip_offplatform
    kept, dropped = strip_offplatform(
        [{"name": "Share the dashboard link in the WhatsApp group",
          "maxScore": 50, "whatEarnsIt": ""},
         {"name": "Dashboard from a data set", "maxScore": 50,
          "whatEarnsIt": ""}])
    assert [c["name"] for c in kept] == ["Dashboard from a data set"]
    assert len(dropped) == 1


def test_the_version_bump_invalidates_the_cached_zero_ceiling_rubrics():
    """The broken rubrics are CACHED under the v7 source hash. Without a bump
    the code fix changes nothing for Day 07."""
    from app.services.rubric_service import RUBRIC_VERSION
    assert RUBRIC_VERSION >= 8


# ── strip_offplatform's 25-character gap (28 Aug 2026) ─────────────────────
# The off-platform patterns need an ACT and an OBJECT within N characters.
# N was 25, and "Share the dashboard link in the WhatsApp group" is 26 — so a
# pure logistics line survived as a scored criterion by one character.
#
# It mattered more after force_visible_deliverables: that line names a
# dashboard, so it is now forced evidenceable, and strip_offplatform is the
# only thing standing between it and 50 marks for something nobody can see.

from app.services.rubric_service import strip_offplatform


def _c(name):
    return {"name": name, "maxScore": 50, "whatEarnsIt": ""}


@pytest.mark.parametrize("logistics", [
    "Share the dashboard link in the WhatsApp group",
    "Submission uploaded to the LMS portal for review",
    "Post your finished presentation in the batch group chat",
])
def test_logistics_are_dropped_however_wordy(logistics):
    kept, dropped = strip_offplatform([_c(logistics), _c("Dashboard created")])
    assert [c["name"] for c in kept] == ["Dashboard created"]
    assert len(dropped) == 1


@pytest.mark.parametrize("deliverable", [
    "Write a blog post for the community website",
    "LinkedIn post about your learning",
    "Portfolio page published as a website",
    "Blog post published",
])
def test_a_deliverable_is_never_mistaken_for_logistics(deliverable):
    """Widening the gap and adding an imperative "Post" must not start
    eating real work. Publishing a page IS the deliverable on Day 04."""
    kept, dropped = strip_offplatform([_c(deliverable)])
    assert [c["name"] for c in kept] == [deliverable], f"dropped: {dropped}"
