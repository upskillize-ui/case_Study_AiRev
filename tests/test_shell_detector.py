"""Day 07 (assignment 24, 23 Aug 2026): the audit reported 17 students as
READABLE at exactly 120 words each. Seventeen distinct share.gemini.google
links cannot serve identical pages — what came back was Google's signed-out
app shell, and every one of those learners was about to be marked on it.

Two guards, because either alone is insufficient:
  - the phrase list catches shells we have already met;
  - the duplicate check catches the ones we have not, including whichever
    tool the syllabus adopts next.
"""

from app.services import link_renderer as lr


GEMINI_SHELL = ("Gemini - direct access to Google AI. Sign in. Google apps. "
                "About Privacy Terms Help Send feedback")
REAL_WORK = ("Quarterly revenue dashboard: Q1 4.2 crore, Q2 5.1 crore, Q3 "
             "6.8 crore. The chart shows steady growth driven by the retail "
             "lending book, with fee income flat across all three quarters.")


# ── the phrase list ─────────────────────────────────────────────────────────

def test_geminis_signed_out_shell_is_refused():
    why = lr.interstitial_reason("Gemini", GEMINI_SHELL)
    assert why and "signed in" in why


def test_the_gemini_reason_tells_the_student_what_is_wrong_with_the_link():
    why = lr.interstitial_reason("Gemini", GEMINI_SHELL)
    assert "share link" in why.lower()


def test_real_work_is_untouched():
    assert lr.interstitial_reason("My dashboard", REAL_WORK) == ""


def test_the_shell_is_decisive_even_on_a_long_page():
    """It is a STRONG phrase: 120 words of chrome is still chrome, and so is
    500. The weak list would have let this through above 200 words."""
    padded = GEMINI_SHELL + " lorem ipsum" * 300
    assert lr.interstitial_reason("Gemini", padded) != ""


# ── the duplicate-shell guard (pure) ────────────────────────────────────────

def test_the_same_page_from_a_different_url_is_a_shell():
    seen = {lr.page_signature(REAL_WORK): "https://a.example/1"}
    why = lr.duplicate_shell_reason(seen, "https://b.example/2", REAL_WORK)
    assert why and "another student" in why


def test_the_same_page_from_the_SAME_url_is_not_a_duplicate():
    """A re-review of one learner's link must not be refused for matching
    itself — that would make every second pass over a row fail."""
    url = "https://a.example/1"
    seen = {lr.page_signature(REAL_WORK): url}
    assert lr.duplicate_shell_reason(seen, url, REAL_WORK) == ""


def test_an_unseen_page_passes():
    assert lr.duplicate_shell_reason({}, "https://a.example/1", REAL_WORK) == ""


def test_whitespace_and_quote_differences_do_not_hide_a_shell():
    """_flatten() straightens the text first, so a stray newline cannot make
    one copy of the shell look unique."""
    seen = {lr.page_signature(GEMINI_SHELL): "https://a.example/1"}
    noisy = GEMINI_SHELL.replace(". ", ".\n   ")
    assert lr.duplicate_shell_reason(seen, "https://b.example/2", noisy) != ""


def test_a_very_short_page_is_left_to_the_empty_page_rule():
    """Below the floor, "the page rendered empty" is the honest reason — two
    blank pages matching each other proves nothing."""
    seen = {lr.page_signature("hi there"): "https://a.example/1"}
    assert lr.duplicate_shell_reason(seen, "https://b.example/2", "hi there") == ""


# ── the stateful wrapper ────────────────────────────────────────────────────

def test_the_first_link_passes_and_the_second_identical_one_is_refused():
    lr.forget_pages()
    assert lr.note_page("https://share.gemini.google/aaa", GEMINI_SHELL) == ""
    assert lr.note_page("https://share.gemini.google/bbb", GEMINI_SHELL) != ""
    assert lr.note_page("https://share.gemini.google/ccc", GEMINI_SHELL) != ""


def test_distinct_real_pages_all_pass():
    lr.forget_pages()
    for i in range(5):
        assert lr.note_page(f"https://x.example/{i}", f"{REAL_WORK} item {i}") == ""


def test_forget_pages_resets_between_sweeps():
    lr.forget_pages()
    lr.note_page("https://a.example/1", REAL_WORK)
    lr.forget_pages()
    assert lr.note_page("https://b.example/2", REAL_WORK) == ""


def test_the_renderer_consults_the_shell_check_before_spending_on_vision():
    src = open("app/services/link_renderer.py", encoding="utf-8").read()
    shell_at = src.index("shell = note_page(url, rendered.text)")
    vision_at = src.index("if needs_vision(rendered):")
    assert shell_at < vision_at, "a shell must never reach the OCR bill"
