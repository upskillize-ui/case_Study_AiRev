"""Pure-function tests for the link challenge handling. No browser, no network.

RUN:  python -m pytest tests/test_link_challenge.py -q
"""

from app.services.link_renderer import (
    interstitial_match, interstitial_reason, is_challenge,
    host_gap_ms, note_challenge, wait_needed, _challenges,
)


# --- which gate was it? ------------------------------------------------------

def test_cloudflare_is_reported_as_a_challenge():
    reason, phrase = interstitial_match("Just a moment...", "Verifying you are human")
    assert "human-check" in reason
    assert phrase in ("just a moment", "verifying you are human")
    assert is_challenge(reason)


def test_a_sign_in_wall_is_not_a_challenge():
    reason, _ = interstitial_match("Sign in - Google Accounts",
                                   "Use your Google Account. Forgot email?")
    assert reason
    assert not is_challenge(reason), "a private link must not be waited out"


def test_the_matched_phrase_is_returned_for_the_log():
    _, phrase = interstitial_match("", "Checking your browser before accessing")
    assert phrase == "checking your browser"


def test_real_work_matches_nothing():
    reason, phrase = interstitial_match(
        "My Data Science deck",
        "I built a five-slide deck covering data cleaning, feature "
        "engineering and a short evaluation of the model.")
    assert reason == "" and phrase == ""


def test_a_long_page_survives_a_stray_weak_phrase():
    body = ("sign in to continue " + "word " * 300)
    assert interstitial_reason("", body) == "", \
        "a long page mentioning sign-in is still the student's work"


def test_a_short_page_does_not():
    assert interstitial_reason("", "sign in to continue") != ""


def test_reason_wrapper_agrees_with_match():
    t, b = "Just a moment...", "verify you are human"
    assert interstitial_reason(t, b) == interstitial_match(t, b)[0]


# --- backing off a host that keeps challenging us ---------------------------

def test_no_challenges_leaves_the_base_gap_untouched():
    assert host_gap_ms(5000, 0, 60000) == 5000


def test_each_challenge_doubles_the_gap():
    assert host_gap_ms(5000, 1, 60000) == 10000
    assert host_gap_ms(5000, 2, 60000) == 20000
    assert host_gap_ms(5000, 3, 60000) == 40000


def test_the_gap_is_capped():
    assert host_gap_ms(5000, 10, 60000) == 60000


def test_a_disabled_gap_stays_disabled():
    assert host_gap_ms(0, 5, 60000) == 0


def test_the_ceiling_can_never_drop_below_the_base_gap():
    assert host_gap_ms(5000, 3, 1000) == 5000


def test_a_clean_render_forgets_the_host_s_history():
    _challenges.clear()
    assert note_challenge("gamma.app", True) == 1
    assert note_challenge("gamma.app", True) == 2
    assert note_challenge("gamma.app", False) == 0, \
        "one good render must not leave the host throttled for the rest of the day"


def test_an_unknown_host_is_ignored():
    assert note_challenge("", True) == 0


# --- the pause itself --------------------------------------------------------

def test_a_new_host_waits_for_nothing():
    assert wait_needed(0.0, 100.0, 5000) == 0.0


def test_a_recent_visit_waits_the_remainder():
    assert wait_needed(100.0, 102.0, 5000) == 3.0


def test_a_jumped_clock_can_never_park_a_review():
    assert wait_needed(1_000_000.0, 0.0, 5000) == 5.0
