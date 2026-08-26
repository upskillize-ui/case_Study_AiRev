"""Grading jargon can never reach a student card — deterministic scrub.

Live 26 Aug: "The rubric asks for 3 lines" appeared in Shelke Disha's
feedback despite the prompt ban. Prompts request; this code guarantees.
"""
from app.services.review_pipeline import simple_english


def test_the_live_failure_sentence_is_fixed():
    out = simple_english("The rubric asks for 3 lines connecting your research finding to why you built this app.")
    assert out == "The task asks for 3 lines connecting your research finding to why you built this app."


def test_all_banned_words_are_replaced():
    src = ("Your Rubric score on this criterion: the deliverable lacks a "
           "debugging narrative per the submission manifest criteria.")
    out = simple_english(src)
    low = out.lower()
    for banned in ("rubric", "criterion", "criteria", "deliverable",
                   "manifest", "narrative"):
        assert banned not in low, out


def test_normal_text_is_untouched():
    s = "Your app works well. The map, compass and treasure markers are cohesive."
    assert simple_english(s) == s
