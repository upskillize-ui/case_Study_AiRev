# tests/test_stored_link.py
# ---------------------------------------------------------------------------
# THE LINK THAT WAS NEVER OPENED AS A LINK (03 Sep 2026).
#
# The submit form has a link field. The LMS stores what a learner pastes there
# in file_path, with file_name "Link submission" — and from_stored_file handed
# it to the file DOWNLOADER. A Canva design, a Drive view page, a HeyGen share:
# the downloader asked for bytes, got HTML or a 403, and refused. The browser
# renderer that exists for exactly such pages was never tried, because only
# links pasted into the NOTES box reached from_links_in.
#
# 303 submissions on one course were refused that way. These pin the routing:
# a stored web page takes the link path; a stored upload still downloads.
#
# Pure. No network: from_links_in and the downloader are both stubbed.
# ---------------------------------------------------------------------------

import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.utils import submission_intake as intake
from app.utils.submission_intake import Artefact


# --- is_web_page: the routing decision, pure ---------------------------------

@pytest.mark.parametrize("url,name", [
    ("https://www.canva.com/design/DAF12/view",            "Link submission"),
    ("https://drive.google.com/file/d/1abc/view?usp=sharing", "Link submission"),
    ("https://app.heygen.com/share/abc123",                 "Link submission"),
    ("https://suno.com/song/9f8e",                          ""),
    ("https://notion.so/My-Page-1234",                      ""),
    ("https://example.com/portfolio",                       ""),
])
def test_a_page_on_the_web_is_a_page(url, name):
    assert intake.is_web_page(url, name) is True


@pytest.mark.parametrize("url,name", [
    ("https://res.cloudinary.com/dirgd2vmv/image/upload/v1/day17.mp4", ""),
    ("https://res.cloudinary.com/dirgd2vmv/raw/upload/v1/essay",       ""),  # no ext, still ours
    ("https://res.cloudinary.com/dirgd2vmv/raw/upload/v1/x",           "Link submission"),
    ("/uploads/submissions/essay.pdf",                                  "essay.pdf"),
    ("uploads/day04.png",                                               "day04.png"),
    ("https://example.com/files/report.pdf",                            ""),
    ("https://cdn.example.com/video.mp4?token=1",                       ""),
    ("",                                                                ""),
    (None,                                                              None),
])
def test_an_upload_is_still_an_upload(url, name):
    assert intake.is_web_page(url, name) is False


def test_the_lms_marker_wins_even_with_an_extension_in_the_path():
    # A share page whose path happens to end in something extension-like.
    assert intake.is_web_page("https://site.com/share/abc.view", "Link submission") is True


# --- from_stored_file: takes the link path for a page, the file path otherwise

def test_a_stored_page_is_read_as_a_link(monkeypatch):
    seen = {}

    def fake_links(text, limit=intake.MAX_LINKS, task_text=""):
        seen["text"], seen["limit"] = text, limit
        return [Artefact(kind="link", label=text, text="The rendered page says hello.",
                         confirmed=True)]

    def never(*a, **k):
        raise AssertionError("the downloader must not be called for a web page")

    monkeypatch.setattr(intake, "from_links_in", fake_links)
    monkeypatch.setattr("app.utils.file_extractor.extract_text_from_url", never)

    art = intake.from_stored_file("https://www.canva.com/design/DAF12/view", "Link submission")
    assert seen["text"] == "https://www.canva.com/design/DAF12/view"
    assert seen["limit"] == 1
    assert art.kind == "link"
    assert art.text == "The rendered page says hello."
    assert art.confirmed is True


def test_a_stored_upload_still_downloads(monkeypatch):
    def never(*a, **k):
        raise AssertionError("a real upload must not take the link path")
    monkeypatch.setattr(intake, "from_links_in", never)
    monkeypatch.setattr("app.utils.file_extractor.extract_text_from_url",
                        lambda url, name: ("Essay text here.", ""))

    art = intake.from_stored_file("https://res.cloudinary.com/dirgd2vmv/raw/upload/essay.pdf", "essay.pdf")
    assert art.kind == "document"
    assert art.text == "Essay text here."


def test_a_page_the_link_path_cannot_open_is_still_recorded_honestly(monkeypatch):
    # The link path found nothing. The stored-file path must NOT then invent a
    # download attempt on top — the link path's own honest record stands.
    monkeypatch.setattr(intake, "from_links_in",
        lambda text, limit=intake.MAX_LINKS, task_text="":
            [Artefact(kind="link", label=text, note="link returned HTTP 403", confirmed=True)])
    monkeypatch.setattr("app.utils.file_extractor.extract_text_from_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no download for a page")))

    art = intake.from_stored_file("https://www.canva.com/design/x/view", "Link submission")
    assert art.text == ""
    assert "403" in art.note
    # The refusal wording still classifies as a link refusal downstream.
    assert art.kind == "link"


def test_an_empty_link_result_falls_back_to_the_downloader(monkeypatch):
    # Defensive: if the link path returns nothing at all (budget spent, guard
    # refused), the old behaviour is still there rather than a silent None.
    monkeypatch.setattr(intake, "from_links_in",
                        lambda text, limit=intake.MAX_LINKS, task_text="": [])
    monkeypatch.setattr("app.utils.file_extractor.extract_text_from_url",
                        lambda url, name: ("", "download HTTP 403"))
    art = intake.from_stored_file("https://www.canva.com/design/x/view", "Link submission")
    assert art is not None
    assert "403" in art.note
