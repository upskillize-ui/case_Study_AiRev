"""A published page nobody could open is not graded — Ranjana's ruling, 22 Aug.

Day 04 (assignment 21): 21 rows carried marks of 0.0-4.7 while their own
feedback said the Notion page could not be read. Student 204 got 2.5/10
beside the sentence "The Notion page you submitted is not readable through
automated access." The learner typed a description, so there was text to
judge — but the deliverable, the published page, was never seen.

Her decision: no grade, ask them to publish. A withheld mark can still
become a real score the same evening; a recorded 2.5 cannot.

The narrowness is the whole point. On an essay day a pasted link is a
citation, and withholding a grade there would punish a footnote.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.utils import submission_intake as intake
from app.utils.submission_intake import Artefact

DAY04 = ("Day 04: Notion : Create a 30 Days 30 AI Tools Portfolio Page. "
         "Create a new page in Notion, write down your learnings and upload "
         "your assignments, publish it as a website.")
ESSAY = ("Day 03: Perplexity and Grok - India's Fintech Market. Research the "
         "Indian fintech market and write up what you found.")


def _link(readable):
    return Artefact(kind="link", label="https://x.notion.site/p", confirmed=True,
                    text="the page" if readable else "",
                    note="" if readable else "private")


def _file(readable):
    return Artefact(kind="image", label="shot.png", confirmed=True,
                    text="a screenshot of the portfolio" if readable else "")


# ── which tasks the rule applies to ───────────────────────────────────────

def test_a_publish_this_task_is_recognised():
    assert intake.link_is_the_deliverable(DAY04) is True
    assert intake.link_is_the_deliverable("Day 02 - Claude: Artifact Creation "
                                          "and Publishing") is True
    assert intake.link_is_the_deliverable("Build it in Lovable and share the "
                                          "live link") is True


def test_an_essay_task_is_not_touched():
    """A link in an essay is a citation. Withholding a grade there would
    punish a learner for a footnote."""
    assert intake.link_is_the_deliverable(ESSAY) is False
    assert intake.link_is_the_deliverable("") is False


# ── which submissions the rule applies to ─────────────────────────────────

def test_a_link_that_never_opened_and_nothing_else_counts():
    assert intake.only_unreadable_links([_link(False)]) is True


def test_a_link_that_opened_is_not_a_problem():
    assert intake.only_unreadable_links([_link(True)]) is False


def test_a_readable_screenshot_beside_the_dead_link_rescues_the_row():
    """The learner ALSO uploaded proof. That is the work — grade it."""
    assert intake.only_unreadable_links([_link(False), _file(True)]) is False


def test_an_unreadable_file_beside_the_dead_link_does_not_rescue_it():
    assert intake.only_unreadable_links([_link(False), _file(False)]) is True


def test_a_submission_with_no_link_at_all_is_never_caught():
    assert intake.only_unreadable_links([_file(True)]) is False
    assert intake.only_unreadable_links([]) is False


def test_one_dead_link_among_several_is_not_enough():
    """If any link opened, the work was seen."""
    assert intake.only_unreadable_links([_link(False), _link(True)]) is False


# ── the two together: exactly student 204's row ───────────────────────────

def test_student_204s_row_is_now_withheld_not_marked():
    typed_a_description = [_link(False)]
    assert (intake.link_is_the_deliverable(DAY04)
            and intake.only_unreadable_links(typed_a_description))


def test_the_same_row_on_an_essay_day_is_still_graded():
    assert not (intake.link_is_the_deliverable(ESSAY)
                and intake.only_unreadable_links([_link(False)]))


def test_both_routes_carry_the_rule_and_name_the_fix():
    import inspect
    from app.routes import assignment_review as ar
    src = inspect.getsource(ar)
    assert src.count("link_is_the_deliverable") == 2, \
        "submit AND regrade must both refuse; one path alone leaves the hole"
    assert "unreadable_published_link" in src
    assert "Share, then Publish" in src, "the note must tell them the fix"
