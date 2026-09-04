"""The home reader (04 Sep 2026).

Our browser leaves from a datacenter IP and 174 links (claude.ai, gamma.app,
Notion, some Lovable apps) show it a Cloudflare human-check instead of the
work. A script on the owner's PC opens each page from a home connection and
posts what it saw; the review then proceeds as if our own browser had
rendered the page. Pinned:
  1. A seeded url is used instead of rendering — and goes through the SAME
     gate checks, so a seed that is a sign-in wall is refused.
  2. Our own remembered refusal never outranks a fresh seed.
  3. The seed endpoint stores nothing for a gate page, and re-offers the
     row (refusal cleared, ledger relabelled, intake cache dropped) for a
     real page; a graded row is never touched.
  4. The blocked-links list uses intake's own link rules.
"""
import os
import sys

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

import pytest

from app.services import link_renderer as lr
from app.services import link_seed_service as seeds
from app.routes import link_seeds as route

URL = "https://gamma.app/docs/Data-Science-xyz123"
PAGE = ("Data Science in Banking. Fraud detection uses anomaly models on "
        "transaction streams. Credit scoring blends bureau data with behaviour. ") * 12


class _Tenant:
    id = "lms"
    database_url = "mysql://x"


@pytest.fixture(autouse=True)
def _clean():
    lr.forget_links(); lr.forget_pages(); lr.forget_screenshots()
    yield
    lr.forget_links(); lr.forget_pages(); lr.forget_screenshots()


def _seed_lookup(monkeypatch, seed):
    monkeypatch.setattr(seeds, "lookup", lambda tenant, url: seed)
    monkeypatch.setattr("app.database.get_current_tenant", lambda: _Tenant())


def _no_render(monkeypatch):
    def boom(url, walk=False):
        raise AssertionError("render_link must not be called for a seeded url")
    monkeypatch.setattr(lr, "render_link", boom)


# ─── 1. the seeded page is read like a rendered one ────────────────────────

def test_a_seeded_page_is_used_instead_of_rendering(monkeypatch):
    _seed_lookup(monkeypatch, seeds.Seed(URL, "Data Science", PAGE, "", "home-reader"))
    _no_render(monkeypatch)
    text, why = lr.read_rendered_link(URL)
    assert why == "" and "Fraud detection" in text


def test_a_seeded_screenshot_reaches_the_marker(monkeypatch):
    _seed_lookup(monkeypatch, seeds.Seed(URL, "Data Science", PAGE, "/9j/AAAA", "home-reader"))
    _no_render(monkeypatch)
    text, why, shot, _notes = lr.read_rendered_page(URL)
    assert why == "" and shot == "/9j/AAAA"


def test_a_seed_that_is_a_sign_in_wall_is_refused_like_any_page(monkeypatch):
    _seed_lookup(monkeypatch, seeds.Seed(
        URL, "Sign in - Google Accounts",
        "Use your Google Account. Email or phone. Forgot email?", "", "home-reader"))
    _no_render(monkeypatch)
    text, why = lr.read_rendered_link(URL)
    assert text == "" and "sign-in" in why


def test_an_unseeded_url_still_renders(monkeypatch):
    _seed_lookup(monkeypatch, None)
    called = []
    monkeypatch.setattr(lr, "render_link", lambda url, walk=False: (called.append(url), (None, "the browser timed out"))[1])
    text, why = lr.read_rendered_link(URL)
    assert called == [URL] and why == "the browser timed out"


def test_a_broken_seed_table_never_stops_a_review(monkeypatch):
    def boom(tenant, url):
        raise RuntimeError("table gone")
    monkeypatch.setattr(seeds, "lookup", boom)
    monkeypatch.setattr("app.database.get_current_tenant", lambda: _Tenant())
    monkeypatch.setattr(lr, "render_link", lambda url, walk=False: (None, "the browser timed out"))
    assert lr.read_rendered_link(URL) == ("", "the browser timed out")


# ─── 2. our remembered refusal does not outrank the seed ───────────────────

def test_forget_link_drops_one_remembered_verdict():
    lr._remember(URL, "", "the page was still showing a human-check (Cloudflare) when the browser gave up")
    lr._remember("https://other.example/x", "kept", "")
    lr.forget_link(URL)
    assert lr.cache_lookup(lr._link_cache, URL, 0) is None
    assert lr.cache_lookup(lr._link_cache, "https://other.example/x", 0) == ("kept", "")


# ─── 3. the seed endpoint ──────────────────────────────────────────────────

def _post(monkeypatch, body: dict, grade=None, admin_ok=True):
    stored, reoffered, forgotten = [], [], []
    monkeypatch.setattr(route, "_require_staff", lambda key: None if admin_ok else (_ for _ in ()).throw(RuntimeError("staff")))
    monkeypatch.setattr(seeds, "store", lambda tenant, seed: stored.append(seed))
    monkeypatch.setattr(seeds, "tquery", lambda tenant, sql, params=(): [{"grade": grade}])
    executed = []
    monkeypatch.setattr(seeds, "texecute", lambda tenant, sql, params=(): executed.append((sql, params)))
    monkeypatch.setattr("app.services.intake_cache.forget", lambda tenant, sid: forgotten.append(sid))
    monkeypatch.setattr(lr, "forget_link", lambda url: None)
    res = route.seed_link(route.SeedBody(**body), tenant=_Tenant(), x_admin_key="k")
    return res, stored, executed, forgotten


def test_a_real_page_is_stored_and_the_row_reoffered(monkeypatch):
    res, stored, executed, forgotten = _post(monkeypatch, {
        "submissionId": 5378, "url": URL, "title": "Data Science", "text": PAGE})
    assert res["accepted"] is True and res["row"] == "reoffered"
    assert stored and stored[0].url == URL
    sqls = " ".join(s for s, _ in executed)
    assert "SET grade = NULL, feedback = NULL, status = 'submitted'" in sqls
    assert "NOT LIKE 'our outage %%'" in sqls
    assert executed[-1][1][0].startswith("our outage — seeded by home-reader")
    assert forgotten == [5378]


def test_a_gate_page_stores_nothing_and_touches_no_row(monkeypatch):
    res, stored, executed, forgotten = _post(monkeypatch, {
        "submissionId": 5378, "url": URL, "title": "Just a moment...",
        "text": "Verifying you are human. This may take a few seconds."})
    assert res["accepted"] is False and "human-check" in res["why"]
    assert not stored and not executed and not forgotten


def test_a_sign_in_wall_seed_tells_the_learner_with_that_sites_steps(monkeypatch):
    """Run 3 (04 Sep): 36 private Gamma decks were refused correctly and
    left carrying "site blocked our reader". The learner's fault is stamped
    on the row now, with the share steps for that site."""
    told = []
    monkeypatch.setattr("app.services.assignment_db_service.mark_not_graded",
                        lambda tenant, sid, msg, **kw: told.append((sid, msg)))
    res, stored, executed, forgotten = _post(monkeypatch, {
        "submissionId": 5272, "url": "https://gamma.app/docs/x-abc",
        "title": "Sign in - Google Accounts",
        "text": "Use your Google Account. Email or phone. Forgot email?"})
    assert res["accepted"] is False and res["row"] == "told"
    assert not stored and not executed and not forgotten
    assert told[0][0] == 5272
    assert "asks whoever visits it to sign in" in told[0][1]
    assert "In Gamma open Share" in told[0][1]


def test_a_sign_in_wall_seed_never_touches_a_graded_row(monkeypatch):
    told = []
    monkeypatch.setattr("app.services.assignment_db_service.mark_not_graded",
                        lambda tenant, sid, msg, **kw: told.append(sid))
    res, *_ = _post(monkeypatch, {
        "submissionId": 5272, "url": "https://gamma.app/docs/x-abc",
        "title": "Sign in - Google Accounts",
        "text": "Use your Google Account. Email or phone. Forgot email?"}, grade=6.5)
    assert res["accepted"] is False and res["row"] == "graded" and not told


def test_a_graded_row_is_never_reset_by_a_seed(monkeypatch):
    res, stored, executed, forgotten = _post(monkeypatch, {
        "submissionId": 5378, "url": URL, "title": "Data Science", "text": PAGE}, grade=6.5)
    assert res["accepted"] is True and res["row"] == "graded"
    assert stored and not executed and not forgotten


def test_an_empty_seed_is_a_400(monkeypatch):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _post(monkeypatch, {"submissionId": 1, "url": URL, "title": "", "text": ""})
    assert e.value.status_code == 400


def test_validate_is_pure():
    assert seeds.validate("ftp://x", "", "t", "") != ""
    assert seeds.validate(URL, "", "", "") != ""
    assert seeds.validate(URL, "", "t", "not base64!!") != ""
    assert seeds.validate(URL, "", "", "/9j/AAAA") == ""
    assert seeds.validate(URL, "", "t", "") == ""


# ─── 4. the blocked-links list ─────────────────────────────────────────────

def test_links_of_follows_intakes_own_rules():
    notes = "My deck: https://gamma.app/docs/abc. Also https://gamma.app/docs/abc"
    assert route.links_of(notes, "", "") == ["https://gamma.app/docs/abc"]
    # A stored 'file' that is really a page (the LMS link field) is included.
    assert route.links_of("", "https://claude.ai/public/artifacts/f1f6", "Link submission") == [
        "https://claude.ai/public/artifacts/f1f6"]
    # A real upload is not a page to open.
    assert route.links_of("", "https://res.cloudinary.com/d/raw/upload/v1/x.pdf", "x.pdf") == []


def test_blocked_rows_keeps_the_latest_attempt_per_learner(monkeypatch):
    rows = [
        {"id": 20, "assignment_id": 33, "student_id": 7, "notes": "https://gamma.app/docs/new",
         "file_path": "", "file_name": "", "submitted_at": 2, "title": "Day 09 : Gamma"},
        {"id": 10, "assignment_id": 33, "student_id": 7, "notes": "https://gamma.app/docs/old",
         "file_path": "", "file_name": "", "submitted_at": 1, "title": "Day 09 : Gamma"},
        {"id": 15, "assignment_id": 20, "student_id": 9, "notes": "",
         "file_path": "https://claude.ai/public/artifacts/z", "file_name": "Link submission",
         "submitted_at": 1, "title": "Day 02"},
    ]
    seen = {}
    def fake_q(tenant, sql, params=()):
        seen["sql"] = sql; seen["params"] = params
        return rows
    monkeypatch.setattr(route, "tquery", fake_q)
    monkeypatch.setattr(route, "_require_staff", lambda key: None)
    res = route.blocked_links(course_id=55, limit=400, tenant=_Tenant(), x_admin_key="k")
    assert [l["submissionId"] for l in res["links"]] == [15, 20]
    assert res["links"][0]["host"] == "claude.ai"
    assert "lets us read it" in seen["params"][0] and seen["params"][-1] == 55
    assert "%%notGraded%%" in seen["sql"]


# ─── 5. the courier script ─────────────────────────────────────────────────

def test_the_courier_recognises_a_challenge_title():
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import home_reader as hr
    assert hr.is_challenge("Just a moment...")
    assert hr.is_challenge("Attention Required! | Cloudflare")
    assert not hr.is_challenge("Data Science - Gamma")


def test_the_courier_reports_a_400_as_a_refusal_not_a_crash(monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import home_reader as hr

    class R:
        status_code = 400
        def json(self): return {"detail": "a seed needs page text or a screenshot"}
        def raise_for_status(self): raise AssertionError("must not raise on 400")
    monkeypatch.setattr(hr.requests, "post", lambda *a, **k: R())
    res = hr.post_seed(1, URL, "", "", "")
    assert res == {"accepted": False, "why": "a seed needs page text or a screenshot"}


# ─── 6. two image shares are not one shell ─────────────────────────────────

SIDEBAR = ("ChatGPT New chat Images Library Scheduled Plugins Projects Codex More "
           "Pinned Recents upskillize Free Claim offer This is a copy of a shared "
           "ChatGPT chat. Content created using ChatGPT. Report conversation Message ChatGPT ") * 2


def _share_page(poster_seed, quality=70, cursor=False) -> str:
    """A full-page JPEG the home reader would post for a ChatGPT share:
    identical app chrome (sidebar, header) on every page, and — when
    `poster_seed` is given — one 600 x 600 picture in the middle. Base64."""
    import base64
    import random
    from io import BytesIO
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1366, 1100), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 260, 1100], fill=(23, 23, 23))
    for y in range(20, 1100, 34):
        d.rectangle([16, y, 240, y + 14], fill=(60, 60, 60))
    d.rectangle([260, 0, 1366, 56], fill=(245, 245, 245))
    if cursor:
        d.rectangle([900, 700, 912, 716], fill="black")
    if poster_seed is not None:
        rnd = random.Random(poster_seed)
        for _ in range(12):
            x, y = 560 + rnd.randint(0, 540), 160 + rnd.randint(0, 540)
            d.rectangle([x, y, min(1160, x + rnd.randint(60, 300)),
                         min(760, y + rnd.randint(60, 300))],
                        fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)))
    buf = BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode("ascii")


POSTER_A = _share_page(1)
POSTER_B = _share_page(2)
LOGIN_1 = _share_page(None, quality=70)
LOGIN_2 = _share_page(None, quality=45, cursor=True)   # re-encoded, a cursor


def test_two_image_shares_with_the_same_sidebar_are_two_pages():
    """Owner's question, 04 Sep: 'students submitted a ChatGPT link with the
    real image of the assignment — can the agent read it?' Two such links
    carry identical text (the app's sidebar, ~97 words) and different
    pictures. Job 911 refused 11 of them as 'the same page as another
    student's link'."""
    assert lr.note_page("https://chatgpt.com/s/m_1", SIDEBAR, POSTER_A) == ""
    assert lr.note_page("https://chatgpt.com/s/m_2", SIDEBAR, POSTER_B) == ""


def test_the_same_picture_behind_two_links_is_still_a_shell():
    assert lr.note_page("https://chatgpt.com/s/m_1", SIDEBAR, LOGIN_1) == ""
    assert "same page" in lr.note_page("https://chatgpt.com/s/m_2", SIDEBAR, LOGIN_2)


def test_a_text_only_shell_is_still_a_shell():
    """Day 07: seventeen signed-out Gemini shells, 120 identical words each,
    no picture kept. Still one shell."""
    shell = "Sign in to continue to Gemini. " * 30
    assert lr.note_page("https://share.gemini/a", shell, "") == ""
    assert "same page" in lr.note_page("https://share.gemini/b", shell, "")


def test_picture_look_is_pure_and_tolerant():
    assert lr.picture_look("") is None and lr.picture_look("not-an-image") is None
    a, b = lr.picture_look(POSTER_A), lr.picture_look(POSTER_B)
    assert a is not None and b is not None and lr.looks_differ(a, b)
    # The same shell captured twice — different JPEG quality, a cursor.
    assert not lr.looks_differ(lr.picture_look(LOGIN_1), lr.picture_look(LOGIN_2))
    # The same poster captured twice is one page.
    assert not lr.looks_differ(a, lr.picture_look(_share_page(1, quality=45)))
    assert not lr.looks_differ(a, None)


def test_a_thin_seeded_image_share_reaches_vision(monkeypatch):
    _seed_lookup(monkeypatch, seeds.Seed("https://chatgpt.com/s/m_9", "ChatGPT - Banking plan",
                                         SIDEBAR[:300], POSTER_A, "home-reader"))
    _no_render(monkeypatch)
    seen = {}
    def fake_ocr(images, kind=""):
        seen["images"] = images
        return ("BANKING COURSE 5 YEARS PLAN. Year 1 Foundation and Basics: understand "
                "banking structure, learn basic operations. Year 2 Learn and Explore. "
                "Year 3 Prepare and Practice. Year 4 Perform and Grow. Year 5 Specialize."), ""
    monkeypatch.setattr("app.utils.file_extractor._ocr_with_claude", fake_ocr)
    text, why = lr.read_rendered_link("https://chatgpt.com/s/m_9")
    assert why == "" and "Year 3 Prepare" in text
    assert seen["images"] == [("image/jpeg", POSTER_A)]


# ─── 7. a link our reader was refused on is not graded on its caption ──────

def test_a_blocked_link_with_a_caption_is_reader_blocked_not_graded():
    from app.utils import submission_intake as intake
    art = [intake.Artefact(kind="typed text", label="answer box", text="Here is my Gamma deck on data science, please review it thank you sir."),
           intake.Artefact(kind="link", label="https://gamma.app/docs/x", note=lr.GAVE_UP_MESSAGE, confirmed=True)]
    assert intake.link_carries_the_work(art, art[0].text) is True


def test_a_blocked_link_beside_a_real_answer_or_file_is_graded():
    from app.utils import submission_intake as intake
    essay = "word " * 200
    art = [intake.Artefact(kind="typed text", label="answer box", text=essay),
           intake.Artefact(kind="link", label="https://gamma.app/docs/x", note=lr.GAVE_UP_MESSAGE, confirmed=True)]
    assert intake.link_carries_the_work(art, essay) is False
    with_file = [intake.Artefact(kind="typed text", label="answer box", text="see deck"),
                 intake.Artefact(kind="document", label="deck.pdf", text="Slide 1 " * 50, confirmed=True),
                 intake.Artefact(kind="link", label="https://gamma.app/docs/x", note=lr.GAVE_UP_MESSAGE, confirmed=True)]
    assert intake.link_carries_the_work(with_file, "see deck") is False
    opened = [intake.Artefact(kind="typed text", label="answer box", text="see deck"),
              intake.Artefact(kind="link", label="https://gamma.app/docs/x", text="Slide 1 " * 50, confirmed=True)]
    assert intake.link_carries_the_work(opened, "see deck") is False
