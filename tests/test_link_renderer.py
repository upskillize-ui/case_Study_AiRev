"""The agent's own browser — flag-gated, SSRF-guarded, honest on failure.

The browser itself is not exercised here (CI has no display and the sandbox
proxy resets browser traffic); everything AROUND it is: the flag, the guard
order, the vision-spend decision, the text composition, and the intake
wiring with its fallback. The real browser gets its canary on the Space via
POST /api/review/jobs/render-check with Ranjana's two live student links.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import link_renderer as lr
from app.utils import submission_intake as intake


ART = "https://claude.ai/public/artifacts/78cff8f0-32a7-4213-930c-120df82487b8"


def _rendered(text="", shot="x" * 100, title="EMI Quiz"):
    return lr.Rendered(title=title, text=text, screenshot_b64=shot,
                       final_url=ART)


# ── the flag: shipping this must change nothing ───────────────────────────

def test_off_means_inert(monkeypatch):
    monkeypatch.delenv("LINK_RENDER_ENABLED", raising=False)
    assert lr.enabled() is False
    rendered, why = lr.render_link(ART)
    assert rendered is None and "switched off" in why


def test_intake_never_touches_the_renderer_while_off(monkeypatch):
    monkeypatch.delenv("LINK_RENDER_ENABLED", raising=False)
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: pytest.fail("browser started while off"))
    monkeypatch.setattr(intake, "fetch_link", lambda url: ("", "browser-only"))
    arts = intake.from_links_in(ART)
    assert len(arts) == 1 and not arts[0].readable and arts[0].confirmed


# ── the guard runs before any browser exists ──────────────────────────────

def test_a_private_url_is_refused_without_launching(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(lr, "check_public_url",
                        lambda url: (False, "private address refused"))
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: pytest.fail("browser launched for a private URL"))
    rendered, why = lr.render_link("http://169.254.169.254/")
    assert rendered is None and "refused" in why


def test_in_page_request_blocking_is_pure_and_exact():
    assert lr._blocked_host("localhost") is True
    assert lr._blocked_host("127.0.0.1") is True
    assert lr._blocked_host("169.254.169.254") is True
    assert lr._blocked_host("10.2.3.4") is True
    assert lr._blocked_host("db.internal") is True
    assert lr._blocked_host("printer.local") is True
    assert lr._blocked_host("claude.ai") is False
    assert lr._blocked_host("cdn.example.com") is False
    assert lr._blocked_host("8.8.8.8") is False


# ── vision spend: screenshots are read only when the text is thin ─────────

def test_a_visual_page_sends_its_screenshot_to_vision():
    assert lr.needs_vision(_rendered(text="Play  Score: 0")) is True


def test_a_text_rich_page_costs_no_vision_call():
    doc = _rendered(text="word " * 200)
    assert lr.needs_vision(doc) is False


def test_read_rendered_link_ocrs_the_game_and_not_the_doc(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(lr, "check_public_url", lambda url: (True, ""))
    calls = []
    import app.utils.file_extractor as fe
    monkeypatch.setattr(fe, "_ocr_with_claude",
                        lambda images, kind: (calls.append(kind)
                                              or ("A quiz with three questions "
                                                  "about EMI calculation", "")))
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: (_rendered(text="Start Quiz"), ""))
    text, why = lr.read_rendered_link(ART)
    assert why == "" and "EMI Quiz" in text and "three questions" in text
    assert len(calls) == 1

    calls.clear()
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: (_rendered(text="content " * 150), ""))
    text, why = lr.read_rendered_link(ART)
    assert why == "" and calls == []      # rich text -> no OCR money spent


def test_provenance_is_stated_in_the_composed_text():
    text = lr.compose_submission_text(_rendered(text="Q1. What is EMI?"),
                                      ocr_text="A blue quiz interface")
    assert text.startswith("[PUBLISHED PAGE OPENED IN A BROWSER — title: EMI Quiz]")
    assert "Q1. What is EMI?" in text
    assert "[WHAT THE PAGE LOOKS LIKE ON SCREEN]" in text


def test_an_empty_render_reports_honestly(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(lr, "check_public_url", lambda url: (True, ""))
    monkeypatch.setattr(lr, "_render_raw",
                        lambda url: (lr.Rendered("", "", "", ART), ""))
    text, why = lr.read_rendered_link(ART)
    assert text == "" and "empty" in why


# ── the intake wiring: render beats preview, failure falls back ───────────

def test_a_rendered_link_becomes_a_readable_artefact(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(intake, "fetch_link", lambda url: ("", "browser-only"))
    monkeypatch.setattr(lr, "read_rendered_link",
                        lambda url: ("[PUBLISHED PAGE OPENED IN A BROWSER]\n"
                                     "My EMI quiz with three questions.", ""))
    arts = intake.from_links_in(ART)
    assert len(arts) == 1 and arts[0].readable
    assert "EMI quiz" in arts[0].text


def test_the_render_replaces_a_preview_only_body(monkeypatch):
    """fetch_link's metadata fallback ('[This link is a published page...')
    is two lines of preview; a real render must win over it."""
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(intake, "fetch_link",
                        lambda url: ("[This link is a published page whose "
                                     "content is rendered in the browser...]"
                                     "\nTitle: Claude Artifact", ""))
    monkeypatch.setattr(lr, "read_rendered_link",
                        lambda url: ("[PUBLISHED PAGE OPENED IN A BROWSER]\n"
                                     "Full rendered quiz content here.", ""))
    arts = intake.from_links_in(ART)
    assert "Full rendered quiz content" in arts[0].text


def test_render_failure_keeps_the_old_honest_fallback(monkeypatch):
    monkeypatch.setenv("LINK_RENDER_ENABLED", "1")
    monkeypatch.setattr(intake, "fetch_link", lambda url: ("", ""))
    monkeypatch.setattr(lr, "read_rendered_link",
                        lambda url: ("", "the page could not be rendered (TimeoutError)"))
    arts = intake.from_links_in(ART)
    assert not arts[0].readable and arts[0].confirmed
    assert "rendered" in arts[0].note


def test_preview_marker_detection_is_pure():
    assert intake._is_preview_only("[This link is a published page …]") is True
    assert intake._is_preview_only("Real content of a real page") is False
    assert intake._is_preview_only("") is False
