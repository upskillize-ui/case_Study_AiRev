"""Pictures pasted into Word and PowerPoint files are now READ, the same way
pictures inside PDFs already were.

Day 01 evidence: student 1315 submitted a .docx and scored 0.0 — the typed
text was nearly nothing and the plan image pasted inside was never looked at.
The PDF path had already solved this exact shape ('thin text plus embedded
images means OCR as well'); Office files are ZIPs with a media folder, so the
same rule now covers them.

These tests build the archives by hand — no python-docx needed, because what
is under test is the media-folder read and the thin-text decision, both pure.
"""
import io
import os
import sys
import zipfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.utils import file_extractor as fe


def _png(size: int) -> bytes:
    """A blob that passes the extension check and the size floor. Native PNGs
    are sent to vision untouched, so magic bytes plus padding is enough."""
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)


def _zip_with(entries: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, blob in entries.items():
            z.writestr(name, blob)
    return buf.getvalue()


def test_the_media_folder_is_found_and_icons_are_skipped():
    data = _zip_with({
        "word/media/image1.png": _png(50_000),      # the deliverable
        "word/media/icon.png":   _png(2_000),       # bullet glyph — skip
        "word/document.xml":     b"<xml/>",
    })
    images = fe._embedded_media_images(data, "word/media/")
    assert len(images) == 1
    media_type, b64 = images[0]
    assert media_type == "image/png" and b64


def test_a_broken_archive_returns_empty_never_raises():
    assert fe._embedded_media_images(b"not a zip at all", "word/media/") == []


def test_thin_text_with_pictures_gets_both_read(monkeypatch):
    """Student 1315's shape: a title line plus the pasted plan image."""
    calls = []
    monkeypatch.setattr(fe, "_ocr_with_claude",
                        lambda images, kind: (calls.append((len(images), kind))
                                              or ("FIVE STEPS: 1..5", "")))
    data = _zip_with({"word/media/image1.png": _png(50_000)})
    text, why = fe._with_embedded_pictures(
        "My 5 Year Plan", data, "word/media/",
        kind="pictures pasted into a Word document", label="DOCUMENT")
    assert why == ""
    assert "My 5 Year Plan" in text
    assert "PICTURES IN THIS DOCUMENT" in text and "FIVE STEPS" in text
    assert calls == [(1, "pictures pasted into a Word document")]


def test_a_real_write_up_is_never_rebilled_for_decoration(monkeypatch):
    """Same conservatism as PDFs: 150+ words of typed text means the images
    are decoration and no OCR money is spent."""
    monkeypatch.setattr(fe, "_ocr_with_claude",
                        lambda *a, **k: pytest.fail("OCR called on a full write-up"))
    data = _zip_with({"word/media/image1.png": _png(50_000)})
    essay = "word " * 200
    text, why = fe._with_embedded_pictures(
        essay, data, "word/media/", kind="k", label="DOCUMENT")
    assert why == "" and text == essay


def test_an_image_only_docx_returns_the_ocr_alone(monkeypatch):
    monkeypatch.setattr(fe, "_ocr_with_claude",
                        lambda images, kind: ("TEXT FROM THE PICTURE", ""))
    data = _zip_with({"ppt/media/image1.png": _png(50_000)})
    text, why = fe._with_embedded_pictures(
        "", data, "ppt/media/", kind="k", label="PRESENTATION")
    assert why == "" and text == "TEXT FROM THE PICTURE"


def test_no_pictures_and_no_text_keeps_the_old_empty_message():
    text, why = fe._with_embedded_pictures(
        "", _zip_with({"word/document.xml": b"<xml/>"}), "word/media/",
        kind="k", label="DOCUMENT")
    assert text == "" and "parsed but empty" in why
