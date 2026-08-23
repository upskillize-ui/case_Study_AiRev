"""NOTHING TECHNICAL REACHES A LEARNER.

Ranjana, 23 Aug: "students do not get confused or be in problem — whatever is
happening they should know in an understandable and polished version. Do not
tell backend or developer issue as msg them."

It was live. Verbatim, from the notices themselves:

    "...could not read any text from it (HTTP 403: forbidden)"
    "...could not be read (base64 decode failed: BinasciiError)"
    "...could not be rendered (TimeoutError)"

A learner reading that learns nothing except that something is broken and it
might be their fault.

This file is a SWEEP, not a set of examples: it calls every public notice with
the ugliest internal reasons the system can produce and fails if any developer
vocabulary survives into the text. A new notice added without care fails here.
"""

import re

import pytest

from app.services import student_notices as sn


# Every one of these has been seen in a real internal reason string.
UGLY_REASONS = [
    "HTTP 403: forbidden",
    "HTTP 404: not found",
    "base64 decode failed: BinasciiError",
    "the page could not be rendered (TimeoutError)",
    "extraction failed: UnicodeDecodeError",
    "ffmpeg: Invalid data found when processing input",
    "no audio track (CalledProcessError)",
    "TRANSCRIBE_API_KEY is not set on the Space",
    "playwright: net::ERR_CONNECTION_REFUSED",
    "pypdf: invalid pdf header",
    "500 Internal Server Error from api.anthropic.com",
    "the browser was busy with another page for longer than 120s",
    "OCR found nothing",
    "",
]

# Words a learner must never see. Each one is either a status code, an
# exception, a library, or a piece of our own plumbing.
BANNED = [
    "http ", "http:", "403", "404", "401", "500", "502", "503", "504",
    "error:", "exception", "traceback", "stack",
    "timeouterror", "binascii", "unicodedecode", "calledprocess",
    "ffmpeg", "ffprobe", "playwright", "pypdf", "whisper", "chromium",
    "api key", "api_key", "token", "endpoint", "server", "backend",
    "database", "sql", "null", "none type", "nonetype",
    "the space", "huggingface", "cloudinary", "render.com", "aiven",
    "base64", "utf-8", "json", "parse", "decode", "stderr", "stdout",
    "env var", "environment variable", "config",
]


def _every_message(reason: str) -> list:
    """One message from every notice a learner can receive."""
    return [
        sn.nothing_submitted(),
        sn.file_unreadable(reason, "my dashboard.pdf"),
        sn.too_little_content(4, reason, True),
        sn.link_never_opened("https://share.gemini.google/abc"),
        sn.link_opens_only_in_a_browser("https://claude.site/artifacts/x"),
        sn.link_missing_entirely(),
        sn.wrong_task("a music song link", "Day 07 : Gemini Canvas"),
        sn.link_is_not_the_work("a landing page", "Day 07"),
        sn.media_not_transcribed("audio"),
        sn.queued_for_review(),
        sn.still_being_reviewed(),
    ]


@pytest.mark.parametrize("reason", UGLY_REASONS)
@pytest.mark.parametrize("banned", BANNED)
def test_no_developer_word_survives_into_any_notice(reason, banned):
    for message in _every_message(reason):
        assert banned not in message.lower(), \
            f"'{banned}' reached a learner in: {message[:160]}"


# The shapes a leaked internal reason actually takes: an exception name, a
# bare status code, a module path. An assignment title in brackets — "(Day 07
# : Gemini Canvas)" — is not one of them, and flagging it would make this
# test noise instead of a guard.
# A dotted module path was in this list and it flagged the learner's OWN
# filename ("my dashboard.pdf"), which is exactly the thing we want to show
# them. A guard that cries wolf gets switched off, so it names only shapes
# that cannot be anything but ours.
_LEAK_SHAPE = re.compile(
    r"\b[A-Za-z]+(?:Error|Exception|Warning)\b"      # TimeoutError
    r"|\bHTTP\s*\d{3}\b|\b[45]\d{2}\b"              # HTTP 403, 500
    r"|::|0x[0-9a-f]{2,}"                            # net::ERR_..., 0x8f
    r"|\b[A-Z][A-Z_]{4,}\b")                         # TRANSCRIBE_API_KEY


@pytest.mark.parametrize("reason", UGLY_REASONS)
def test_no_notice_leaks_an_internal_reason(reason):
    """"(TimeoutError)" and "(HTTP 403: forbidden)" both arrived this way."""
    for message in _every_message(reason):
        # yourname.notion.site is instructions to the learner, not a leak.
        scrubbed = message.replace("yourname.notion.site", "")
        found = _LEAK_SHAPE.search(scrubbed)
        assert not found, \
            f"an internal reason reached a learner: '{found.group()}' " \
            f"in: {message[:160]}"


# ── the translator itself ───────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected_fragment", [
    ("HTTP 403: forbidden", "not allowed to open it from our side"),
    ("HTTP 404: not found", "no longer there"),
    ("ReadTimeout after 30s", "took too long"),
    ("the page could not be rendered (TimeoutError)", "took too long"),
    ("PdfReadError: file has not been decrypted", "password protected"),
    ("base64 decode failed: BinasciiError", "damaged"),
    ("the file was empty after decode", "empty"),
    ("no text layer and OCR found nothing", "no text could be read"),
    ("file exceeds the size ceiling", "larger than we can open"),
])
def test_each_technical_reason_becomes_something_a_learner_can_act_on(
        raw, expected_fragment):
    assert expected_fragment in sn.plain_reason(raw)


def test_an_unrecognised_reason_is_never_passed_through_raw():
    """Passing it through IS the bug. An honest plain sentence instead."""
    out = sn.plain_reason("KrakenError: flux capacitor desynchronised (0x8f)")
    assert out == "it could not be opened"
    assert "kraken" not in out.lower() and "0x8f" not in out


def test_an_empty_reason_produces_nothing_rather_than_a_dangling_phrase():
    assert sn.plain_reason("") == ""
    assert "—  " not in sn.file_unreadable("", "x.pdf")


def test_our_faults_are_named_as_ours():
    """A learner must never be left thinking they broke it."""
    for raw in ("HTTP 403: forbidden", "500 Internal Server Error",
                "the browser was busy"):
        assert "our" in sn.plain_reason(raw).lower() or \
               "we" in sn.plain_reason(raw).lower(), sn.plain_reason(raw)


# ── the live-feedback wording ───────────────────────────────────────────────

def test_the_submit_message_says_it_is_safe_to_close_the_page():
    """The whole point of the queue: nobody waits, nobody watches a spinner."""
    msg = sn.queued_for_review()
    assert "close this page" in msg.lower()
    assert "waiting for you" in msg.lower()


def test_the_waiting_message_reassures_rather_than_alarms():
    msg = sn.still_being_reviewed()
    assert "nothing is wrong" in msg.lower() and "nothing is lost" in msg.lower()


def test_both_live_messages_avoid_promising_an_exact_time():
    """"Instant" would be a promise the queue cannot keep on a busy evening."""
    for msg in (sn.queued_for_review(), sn.still_being_reviewed()):
        assert "instant" not in msg.lower()
        assert "seconds" not in msg.lower()
