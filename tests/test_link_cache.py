"""Open each page once per run, not once per student.

Day 05 was swept three times; every sweep re-opened every link, including the
dead ones. The browser is the slowest thing in the system and the one most
likely to be rate limited, so an avoided visit buys both time and a smaller
chance of being blocked on the visits that matter.
"""

from app.services import link_renderer as lr


URL = "https://x.notion.site/portfolio"
TEXT = "My 30 Days 30 AI Tools portfolio, with notes on each day."
WHY_PRIVATE = "this link is private — it opens a sign-in page instead of the work"


def test_a_remembered_page_comes_back():
    cache = {}
    lr.cache_store(cache, URL, 1000.0, TEXT, "")
    assert lr.cache_lookup(cache, URL, 1000.5) == (TEXT, "")


def test_a_failure_is_remembered_too():
    """The whole point: a dead link must not be re-opened for every student
    who pasted it."""
    cache = {}
    lr.cache_store(cache, URL, 1000.0, "", WHY_PRIVATE)
    assert lr.cache_lookup(cache, URL, 1001.0) == ("", WHY_PRIVATE)


def test_an_unknown_url_is_a_miss():
    assert lr.cache_lookup({}, URL, 1000.0) is None


def test_an_entry_expires():
    """A learner who publishes their page and resubmits must not be told
    about yesterday's sign-in wall."""
    cache = {}
    lr.cache_store(cache, URL, 1000.0, "", WHY_PRIVATE)
    assert lr.cache_lookup(cache, URL, 1000.0 + 3601, ttl=3600) is None


def test_an_entry_just_inside_the_window_survives():
    cache = {}
    lr.cache_store(cache, URL, 1000.0, TEXT, "")
    assert lr.cache_lookup(cache, URL, 1000.0 + 3599, ttl=3600) is not None


def test_a_zero_ttl_turns_the_cache_off():
    """One honest switch, not a second flag to forget about."""
    cache = {}
    lr.cache_store(cache, URL, 1000.0, TEXT, "")
    assert lr.cache_lookup(cache, URL, 1000.1, ttl=0) is None
    assert lr.cache_lookup(cache, URL, 1000.1, ttl=-1) is None


def test_different_urls_do_not_share_an_entry():
    cache = {}
    lr.cache_store(cache, URL, 1000.0, TEXT, "")
    assert lr.cache_lookup(cache, "https://y.notion.site/other", 1000.1) is None


def test_a_later_store_replaces_the_earlier_verdict():
    """The learner fixed their sharing setting. The newer read wins."""
    cache = {}
    lr.cache_store(cache, URL, 1000.0, "", WHY_PRIVATE)
    lr.cache_store(cache, URL, 2000.0, TEXT, "")
    assert lr.cache_lookup(cache, URL, 2000.1) == (TEXT, "")


def test_forget_links_clears_the_live_cache():
    lr.cache_store(lr._link_cache, URL, 1e9, TEXT, "")
    lr.forget_links()
    assert lr._link_cache == {}


# ── every exit path caches, or the cache is a lie ───────────────────────────

def test_no_exit_path_from_read_rendered_link_forgets_to_cache():
    src = open("app/services/link_renderer.py", encoding="utf-8").read()
    body = src[src.index("def read_rendered_link("):]
    body = body[:body.index("\ndef ", 1)] if "\ndef " in body[1:] else body
    returns = [line.strip() for line in body.splitlines()
               if line.strip().startswith("return ")]
    # The first return is the cache hit itself; every other must go through
    # _remember, which is the only place that writes the entry.
    # Three exits legitimately do not cache: the cache hit itself, and the
    # our-side failure that must never become the page's permanent verdict.
    allowed = {"return hit", 'return "", why'}
    uncached = [r for r in returns if "_remember" not in r and r not in allowed]
    assert not uncached, f"these exits never cache: {uncached}"


def test_the_only_uncached_exit_is_guarded_by_our_failure():
    """That bare exit is allowed ONLY because our_failure() stands in front of
    it. If the guard ever moves, this test fails and the exemption above must
    be re-earned."""
    src = open("app/services/link_renderer.py", encoding="utf-8").read()
    body = src[src.index("def read_rendered_link("):]
    guard = body.index("if our_failure(why):")
    bare = body.index('return "", why')
    assert guard < bare < guard + 200


# ── our bad minute must not become the page's permanent verdict ─────────────

def test_our_own_failures_are_never_cached():
    """Caching "the browser was busy" would hand our timeout to every later
    student who pasted the same link, for a full hour."""
    for why in ("the browser was busy with another page for longer than 120s "
                "— not rendered",
                "the render timed out",
                "the browser could not start"):
        assert lr.our_failure(why) is True, why


def test_verdicts_about_the_page_are_cached():
    for why in ("this link is private — it opens a sign-in page",
                "the page no longer exists at that address",
                "the page rendered empty"):
        assert lr.our_failure(why) is False, why
