"""The language of the work is not the work (04 Sep 2026, owner's ruling).

A learner did the assignment in Marathi. The marker ruled it "a Marathi
essay, not the English reflection asked for" and the row went to the
wrong-task bucket — a month's wait ending in "attach the right work".
Any language counts. Pinned:
  1. A ruling that identifies the work by its language is voided.
  2. The zero the marker wrote behind that ruling is re-judged, once.
  3. A "format miss" that names only a language takes no deduction.
  4. A format miss that names a container AND a language still does.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import review_pipeline as rp
from tests.test_silent_ruling import _run, _scored

TASK = "Day 03: Perplexity and Grok — AI Transformation in India"


def test_a_ruling_that_names_the_language_is_voided():
    for what in ("A Marathi-language essay on AI in Indian banking, not the English reflection asked for",
                 "A reflection written in Hindi rather than English",
                 "Content typed in Devanagari script",
                 "A Gujarati translation of the learner's notes, not in English"):
        assert "LANGUAGE" in rp.wrong_task_void_reason(what, TASK), what


def test_a_ruling_about_different_work_still_stands():
    for what in ("An investment analysis slide deck for a startup pitch",
                 "A study guide summarising the Eat That Frog book",
                 # Different work in another language is still different work.
                 "A Hindi film review, not a data science deck",
                 "A Tamil Nadu tourism brochure, not a fintech analysis"):
        assert "LANGUAGE" not in rp.wrong_task_void_reason(what, TASK), what


def test_a_language_ruling_with_a_zero_behind_it_is_rejudged(monkeypatch):
    out, calls = _run(monkeypatch, [
        _scored(0, declare=True, what="A Marathi essay on the 5-year plan, not in English"),
        _scored(64)])
    assert len(calls) == 2
    assert "LANGUAGE" in calls[1][-1]["text"]
    assert out["wrongTask"]["declared"] is False
    assert out["scores"]["totalScore"] == 64
    assert "rejudged-without-ruling" in out["decisions"]["scoringPath"]


def test_a_language_only_format_miss_takes_no_deduction(monkeypatch):
    answer = _scored(70)
    answer["format_miss"] = {"is_format_miss": True, "asked": "English",
                             "arrived": "Written in Marathi"}
    out, calls = _run(monkeypatch, [answer])
    assert len(calls) == 1
    assert out["scores"]["totalScore"] == 70
    assert not [g for g in out["scores"]["gatesHit"] if g["gate"] == "format_miss"]


def test_a_container_miss_in_another_language_still_deducts(monkeypatch):
    answer = _scored(70)
    answer["format_miss"] = {"is_format_miss": True, "asked": "Gamma link",
                             "arrived": "PDF of the deck, written in Hindi"}
    out, _ = _run(monkeypatch, [answer])
    assert out["scores"]["totalScore"] == 70 - rp.FORMAT_MISS_PENALTY


def test_the_prompt_carries_the_rule():
    assert "THE LANGUAGE IS NOT THE WORK" in rp._JUDGE_INSTRUCTIONS
