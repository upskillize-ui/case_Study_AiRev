"""Every "we could not mark this" message a learner can receive.

Ranjana's rule (19 Aug 2026): "if it student side fault show them what is the
issue so they can re-submit or next time don't repeat same issue." These tests
hold the module to that: say what happened, say what to do IN THEIR OWN TOOL,
and say where their mark stands.
"""

import pytest

from app.services import student_notices as sn


ALL_NOTICES = [
    sn.nothing_submitted(),
    sn.file_unreadable("password protected", "report.pdf"),
    sn.too_little_content(4, "", False),
    sn.link_never_opened("https://x.notion.site/p"),
    sn.link_opens_only_in_a_browser("https://claude.site/artifacts/abc"),
    sn.link_missing_entirely(),
    sn.wrong_task("a spreadsheet of loan data", "Day 07 : Gemini Canvas"),
    sn.media_not_transcribed("audio"),
]


# ── the shape every notice must have ────────────────────────────────────────

@pytest.mark.parametrize("msg", ALL_NOTICES)
def test_every_notice_says_where_the_mark_stands(msg):
    """A learner must never have to guess whether they were scored."""
    assert sn.NO_MARK in msg or "not been counted" in msg


@pytest.mark.parametrize("msg", ALL_NOTICES)
def test_every_notice_tells_them_what_to_do_next(msg):
    lowered = msg.lower()
    assert any(verb in lowered for verb in
               ("submit again", "submit and", "then submit", "attach", "paste",
                "copy", "add ")), msg


@pytest.mark.parametrize("msg", ALL_NOTICES)
def test_no_notice_blames_the_learner_for_our_reach(msg):
    for punitive in ("you failed", "your fault", "invalid", "rejected",
                     "you did not bother", "penalty"):
        assert punitive not in msg.lower(), msg


@pytest.mark.parametrize("msg", ALL_NOTICES)
def test_no_emojis_anywhere(msg):
    """Visual rule: Lucide-style icons, never emojis."""
    assert all(ord(ch) < 0x2190 for ch in msg), msg


# ── the steps must match the tool the learner actually used ────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://ranjana.notion.site/day4",            "Publish"),
    ("https://www.notion.so/ranjana/day4",          "Publish"),
    ("https://docs.google.com/document/d/abc/edit", "Anyone with the link"),
    ("https://drive.google.com/file/d/abc/view",    "Anyone with the link"),
    ("https://share.gemini.google/yUtJ3c5THlPu",    "Gemini"),
    ("https://notebooklm.google.com/notebook/abc",  "NotebookLM"),
    ("https://claude.site/artifacts/abc",           "screenshot"),
    ("https://gamma.app/docs/abc",                  "Gamma"),
    ("https://www.canva.com/design/abc/view",       "Canva"),
])
def test_the_instructions_name_the_learners_own_tool(url, expected):
    assert expected in sn.publish_steps(url)


def test_an_unknown_host_gets_the_incognito_test():
    """Teaches the underlying idea rather than one vendor's menu."""
    steps = sn.publish_steps("https://some-tool-we-have-never-seen.io/p/1")
    assert "incognito" in steps and "anyone with the link" in steps


def test_a_missing_url_still_produces_usable_advice():
    assert sn.publish_steps("") == sn.publish_steps(None) != ""


def test_a_gemini_learner_is_never_sent_to_notions_menu():
    """The live fault: one Notion-specific sentence went to every learner."""
    msg = sn.link_never_opened("https://share.gemini.google/abc")
    assert "Notion" not in msg


# ── the individual notices ──────────────────────────────────────────────────

def test_file_unreadable_names_the_file_and_the_reason():
    msg = sn.file_unreadable("password protected", "dashboard.pdf")
    assert "dashboard.pdf" in msg and "password protected" in msg


def test_file_unreadable_survives_a_missing_reason():
    assert "()" not in sn.file_unreadable("", "")


def test_too_little_content_counts_correctly_and_reads_naturally():
    assert "1 word of text" in sn.too_little_content(1)
    assert "4 words of text" in sn.too_little_content(4)


def test_too_little_content_distinguishes_no_file_from_an_unread_file():
    assert "no file was attached" in sn.too_little_content(4, "", False)
    assert "no readable text" in sn.too_little_content(4, "", True)
    # 23 Aug: the raw reason no longer reaches the learner — "corrupt" becomes
    # a sentence they can act on. See test_no_developer_language.py.
    damaged = sn.too_little_content(4, "corrupt", True)
    assert "could not be read" in damaged and "damaged" in damaged
    assert "(corrupt)" not in damaged


def test_wrong_task_names_both_what_arrived_and_what_was_asked():
    msg = sn.wrong_task("a spreadsheet of loan data", "Day 07 : Gemini Canvas")
    assert "a spreadsheet of loan data" in msg and "Day 07 : Gemini Canvas" in msg


def test_wrong_task_reassures_that_the_attempt_is_not_held_against_them():
    msg = sn.wrong_task("", "Day 07")
    assert "not been counted against you" in msg
    assert "work for a different task" in msg      # sane default


def test_link_missing_entirely_asks_for_the_actual_address():
    msg = sn.link_missing_entirely()
    assert "https://" in msg and "link submission" in msg


def test_media_notice_names_the_medium():
    assert "video file" in sn.media_not_transcribed("video")
    assert "audio file" in sn.media_not_transcribed("audio")


# ── the registry stays honest ───────────────────────────────────────────────

def test_every_registered_reason_resolves_to_a_callable():
    for reason, fn in sn.NOTICES.items():
        assert callable(fn), reason


def test_the_registry_covers_every_public_notice():
    """Every notice a learner can receive is registered — so the sweep in
    test_no_developer_language.py cannot miss one that was added quietly.

    plain_reason, publish_steps and format_fix are excluded on purpose: they
    build PART of a message, they are never sent alone.
    """
    helpers = {"publish_steps", "plain_reason", "bot_protected_host", "urlparse",
               "format_fix"}
    public = {n for n in dir(sn)
              if not n.startswith("_") and callable(getattr(sn, n))
              and n not in helpers}
    assert public == set(f.__name__ for f in sn.NOTICES.values())
