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
