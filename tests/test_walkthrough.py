"""PHASE 2: the agent USES the page instead of photographing its front door.

A Lovable site, a Claude artifact game, any built app — the deliverable is a
thing that DOES something, and until now it was judged from one screenshot of
whatever sat above the fold. These tests hold the two rules that make walking
a stranger's published page safe and affordable:

  do no harm  — never press a control that could change the learner's world
  stay bounded — the browser handles one page at a time and a cohort sweep is
                 already the slowest part of the day
"""

import pytest

from app.services import link_renderer as lr


# ── do no harm ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("label", [
    "Start", "Play", "About", "Next", "Get Started", "Contact", "View demo",
    "Learn more", "Menu",
])
def test_a_harmless_control_may_be_pressed(label):
    assert lr.worth_clicking(label) is True


@pytest.mark.parametrize("label", [
    "Delete", "Delete account", "Remove item", "Clear all", "Reset",
    "Sign out", "Log out", "Buy now", "Pay", "Checkout", "Subscribe",
    "Upgrade", "Submit", "Send message", "Post", "Publish", "Share",
    "Download CSV", "Export", "Save changes", "Upload", "Invite", "Confirm",
])
def test_a_destructive_control_is_never_pressed(label):
    """We are a visitor on a stranger's page. Pressing Start tells us the app
    responds; pressing Delete costs the learner their work."""
    assert lr.worth_clicking(label) is False


def test_case_does_not_smuggle_a_destructive_control_through():
    for label in ("DELETE", "delete", "DeLeTe Account", "LOG OUT"):
        assert lr.worth_clicking(label) is False


def test_a_destructive_word_anywhere_in_the_label_is_enough():
    assert lr.worth_clicking("Click here to delete your project") is False


def test_an_empty_or_absurd_label_is_skipped():
    assert lr.worth_clicking("") is False
    assert lr.worth_clicking("   ") is False
    assert lr.worth_clicking(None) is False
    assert lr.worth_clicking("x" * 41) is False, \
        "a 41-character 'button' is a paragraph, not a control"


# ── which days get walked ───────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _walkthrough_on(monkeypatch):
    """The feature ships OFF. These tests are about WHICH days it walks once
    somebody has switched it on."""
    monkeypatch.setenv("WALKTHROUGH_ENABLED", "1")


def test_the_walkthrough_ships_switched_off(monkeypatch):
    """New code that presses buttons on learners' published pages must not
    start running on the night it happens to ship beside an urgent fix."""
    monkeypatch.delenv("WALKTHROUGH_ENABLED", raising=False)
    assert lr.walkthrough_enabled() is False
    assert lr.task_wants_a_walkthrough(
        "Day 11 : Lovable - build a website") is False


@pytest.mark.parametrize("task", [
    "Day 11 : Lovable - build a website for your career",
    "Build a web app that tracks your expenses",
    "Create a game in Claude artifacts",
    "Day 07 : Gemini Canvas Create a Dashboard from a data set",
    "Deploy your landing page",
])
def test_a_built_thing_is_walked(task):
    assert lr.task_wants_a_walkthrough(task) is True


@pytest.mark.parametrize("task", [
    "Day 03: Research the Indian fintech market and write it up",
    "Day 06: Suno - create a song about your career",
    "Write a 500 word reflection on the session",
    "",
])
def test_a_reading_or_listening_day_is_not_walked(task):
    """Scrolling and clicking a shared doc tells you nothing its text has not
    already said, and costs seconds the browser does not have."""
    assert lr.task_wants_a_walkthrough(task) is False


# ── what the marker is told ─────────────────────────────────────────────────

class _R:
    def __init__(self, steps=None, errors=None):
        self.steps = steps or []
        self.console_errors = errors or []


def test_the_notes_report_what_happened_not_whether_it_was_good():
    notes = lr.walkthrough_notes(_R(
        steps=["the page is about 3 screens tall",
               'pressed "Start" and the page changed']))
    assert "3 screens tall" in notes and 'pressed "Start"' in notes
    for verdict in ("good", "poor", "well designed", "should"):
        assert verdict not in notes.lower()


def test_javascript_errors_are_reported():
    notes = lr.walkthrough_notes(_R(steps=["x"],
                                    errors=["TypeError: x is not a function"]))
    assert "JAVASCRIPT ERROR" in notes and "TypeError" in notes


def test_a_clean_page_is_said_to_be_clean():
    """Silence is ambiguous. 'No errors' is evidence; nothing is not."""
    assert "no JavaScript errors" in lr.walkthrough_notes(_R(steps=["x"]))


def test_only_the_first_few_errors_are_shown():
    notes = lr.walkthrough_notes(_R(steps=["x"], errors=[f"E{i}" for i in range(9)]))
    assert "9 JAVASCRIPT ERROR" in notes
    assert notes.count("- E") == 3


def test_a_page_that_was_not_walked_produces_nothing():
    assert lr.walkthrough_notes(_R()) == ""
    assert lr.walkthrough_notes(None) == ""


# ── bounded by construction ─────────────────────────────────────────────────

def test_the_walkthrough_is_off_unless_asked_for():
    """Most days do not submit apps. Walking every link would spend the
    browser budget on shared docs."""
    src = open("app/services/link_renderer.py", encoding="utf-8").read()
    assert "def _render_raw(url: str, walk: bool = False)" in src
    assert "def render_link(url: str, walk: bool = False)" in src


def test_every_step_of_the_walkthrough_is_bounded():
    src = open("app/services/link_renderer.py", encoding="utf-8").read()
    body = src[src.index("def _walk_the_page("):src.index("def render_link(")]
    assert "deadline = time.monotonic() + WALKTHROUGH_BUDGET_S" in body
    assert body.count("time.monotonic() > deadline") >= 3, \
        "a budget checked once is a budget checked never"
    assert "WALKTHROUGH_MAX_SHOTS" in body and "WALKTHROUGH_MAX_CLICKS" in body


def test_a_broken_walkthrough_cannot_take_down_a_working_review():
    """The single screenshot the caller already holds is the floor."""
    src = open("app/services/link_renderer.py", encoding="utf-8").read()
    body = src[src.index("def _walk_the_page("):src.index("def render_link(")]
    assert body.count("except Exception") >= 3


def test_console_errors_are_collected_even_without_a_walkthrough():
    """It costs nothing and it is the most objective quality signal a built
    page emits."""
    src = open("app/services/link_renderer.py", encoding="utf-8").read()
    body = src[src.index("def _render_raw("):src.index("def _walk_the_page(")]
    listener = body.index('page.on("console"')
    walk_flag = body.index("if walk:")
    assert listener < walk_flag, "errors must be captured before the walk gate"


# ── the walkthrough reaches the marker, on the right days only ─────────────

def test_intake_asks_for_a_walkthrough_only_when_the_task_wants_one():
    src = open("app/utils/submission_intake.py", encoding="utf-8").read()
    body = src[src.index("def from_links_in("):src.index("def _is_thin_body")]
    assert "walk=link_renderer.task_wants_a_walkthrough(task_text)" in body


def test_both_assignment_paths_pass_the_task_text():
    """Without it every day looks like a reading day and Lovable is never
    walked — the fix would ship and change nothing, which is exactly what
    happened to the Day 07 publish rule."""
    src = open("app/routes/assignment_review.py", encoding="utf-8").read()
    assert src.count("task_text=f\"{assignment.get('title', '')} \"") == 2


def test_the_notes_are_marked_as_OUR_observation_not_the_learners_words():
    src = open("app/utils/submission_intake.py", encoding="utf-8").read()
    assert "WHAT HAPPENED WHEN THE PAGE WAS USED" in \
        open("app/services/link_renderer.py", encoding="utf-8").read()
    body = src[src.index("def from_links_in("):src.index("def _is_thin_body")]
    assert 'body = f"{body}\\n\\n{notes}"' in body


def test_the_notes_store_is_bounded_and_cleared_with_the_screenshots():
    from app.services import link_renderer as lr
    lr.forget_screenshots()
    for i in range(lr._SCREENSHOT_KEEP + 4):
        lr._keep_notes(f"https://x/{i}", "walked")
    assert len(lr._last_notes) <= lr._SCREENSHOT_KEEP
    lr.forget_screenshots()
    assert lr._last_notes == {}


def test_a_refused_page_hands_back_no_walkthrough_either():
    """A sign-in wall must never reach the marker as 'the learner's app'."""
    src = open("app/services/link_renderer.py", encoding="utf-8").read()
    body = src[src.index("def read_rendered_page("):]
    body = body[:body.index("\n_last_screenshot")] if "\n_last_screenshot" in body else body[:2000]
    assert 'return text, why, "", ""' in body
