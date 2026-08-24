"""A site's bot check is not a private link.

24 Aug 2026, verified live: two published gamma.app decks, both returning
Cloudflare's human-check to the agent's browser. Gamma sits behind bot
protection that refuses datacentre IPs — the Space's browser cannot read a
Gamma deck, and no amount of waiting or reloading changes that.

Day 09's own brief told 76 learners to submit "a Gamma share link only (no
PDFs or file uploads)". They followed it exactly and their work became
unreadable BY INSTRUCTION.

So the notice must not say "make your link public". Their link IS public.
Telling a learner to publish something already published is how they conclude
the system is broken and stop trying.
"""

import pytest

from app.services import student_notices as sn


# ── the host is recognised ──────────────────────────────────────────────────

@pytest.mark.parametrize("url,tool", [
    ("https://gamma.app/docs/Data-Science-cyo98fbnfn6ql58", "Gamma"),
    ("https://www.canva.com/design/abc/view", "Canva"),
    ("https://www.figma.com/file/abc/deck", "Figma"),
])
def test_a_bot_protected_host_is_named(url, tool):
    name, how = sn.bot_protected_host(url)
    assert name == tool and how


def test_an_ordinary_host_is_not_flagged():
    assert sn.bot_protected_host("https://x.notion.site/page") == ("", "")
    assert sn.bot_protected_host("") == ("", "")
    assert sn.bot_protected_host(None) == ("", "")


# ── what the learner is told ────────────────────────────────────────────────

def test_the_learner_is_told_it_is_not_their_fault():
    msg = sn.link_blocked_by_the_site("https://gamma.app/docs/abc")
    assert "not something you did wrong" in msg
    assert "your link is fine" in msg


def test_it_never_tells_them_to_publish_an_already_published_page():
    """The exact wrong message. Their link IS public."""
    msg = sn.link_blocked_by_the_site("https://gamma.app/docs/abc")
    for wrong in ("publish", "make it public", "anyone with the link",
                  "sign in", "private"):
        assert wrong not in msg.lower(), wrong


def test_it_asks_for_the_export_that_tool_actually_offers():
    assert "Share -> Export -> PDF" in sn.link_blocked_by_the_site(
        "https://gamma.app/docs/abc")
    assert "Share -> Download -> PDF" in sn.link_blocked_by_the_site(
        "https://canva.com/design/abc")


def test_an_unknown_blocked_host_still_gets_useful_advice():
    msg = sn.link_blocked_by_the_site("https://unknown-tool.example/x")
    assert "PDF export or a few screenshots" in msg
    assert "not something you did wrong" in msg


def test_no_mark_is_claimed():
    assert sn.NO_MARK in sn.link_blocked_by_the_site("https://gamma.app/docs/a")


def test_the_notice_is_registered_for_the_sweep():
    assert sn.NOTICES["link_blocked"] is sn.link_blocked_by_the_site


# ── the route picks the right one of the two ────────────────────────────────

def test_the_route_distinguishes_a_bot_check_from_a_browser_only_page():
    src = open("app/routes/assignment_review.py", encoding="utf-8").read()
    block = src[src.index('blocked = any("human-check"'):]
    block = block[:block.index("processingTimeMs")]
    assert "link_blocked_by_the_site(first_link) if blocked" in block
    assert "link_opens_only_in_a_browser(first_link)" in block


def test_the_renderers_wording_is_what_the_route_looks_for():
    """These two strings must agree or the branch never fires — the Day 07
    lesson, where a rule shipped and matched nothing."""
    from app.services import link_renderer as lr
    why = lr.interstitial_reason("Just a moment...",
                                 "Just a moment... verifying you are human")
    assert "human-check" in why
    src = open("app/routes/assignment_review.py", encoding="utf-8").read()
    assert '"human-check" in (a.note or "").lower()' in src
