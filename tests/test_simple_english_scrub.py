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


# ── E3: a cut sentence must LOOK cut (28 Aug 2026) ─────────────────────────
# The old last-resort branch cut at a word boundary and appended a full stop,
# so a truncated point read as a finished one. Live on the Day-14 cards:
#   "...but the description doesn't mention how information is prioritized
#    within each screen—which elements are sized larger, which are."
#   "...with specific details like transaction IDs and named users that."
# A student cannot tell those were cut. They read as a broken product.
#
# Ranjana's ruling: length is governed by the PROMPT, not a clamp — so this
# ceiling should rarely be reached. When it is, be honest about it.

from app.services.review_pipeline import _tidy


def test_a_complete_sentence_that_fits_is_untouched():
    text = "Your login screen is clear. Add a password toggle."
    assert _tidy(text, 200) == text


def test_a_clean_sentence_end_needs_no_ellipsis():
    """"flow.…" looks like a typo. A fragment that already ended in a full
    stop gives the reader a whole sentence and needs no cut mark."""
    text = ("Your three screens follow a real banking flow. " + "x" * 300)
    out = _tidy(text, 120)
    assert out.startswith("Your three screens follow a real banking flow.")
    assert ".…" not in out and not out.endswith("…")


def test_a_cut_never_fabricates_a_full_stop():
    one_long_sentence = (
        "The dark blue and white palette is consistent but the description "
        "does not mention how information is prioritised within each screen "
        "which elements are sized larger and which are smaller overall")
    out = _tidy(one_long_sentence, 120)
    assert not out.endswith("."), f"reads as a finished sentence: {out!r}"
    assert out.endswith("…")


def test_a_cut_ends_at_a_clause_boundary_where_one_exists():
    text = ("Your transaction details are realistic and banking specific, "
            "and the naming of the payee shows real attention to the domain "
            "which is exactly what this task was asking you to demonstrate")
    out = _tidy(text, 70)
    assert out.endswith("…")
    assert "," not in out[-3:], f"cut left a dangling comma: {out!r}"


def test_the_ellipsis_never_follows_stray_punctuation():
    for text in ["word " * 40, "clause, " * 30, "part; " * 30, "a — b — " * 20]:
        out = _tidy(text, 60)
        assert not any(out.endswith(bad + "…") for bad in (",", ";", ":", "-", "—")), out


def test_empty_and_short_inputs_are_safe():
    assert _tidy("", 100) == ""
    assert _tidy(None, 100) == ""
    assert _tidy("Short.", 100) == "Short."
