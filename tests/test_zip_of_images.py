"""A zip of images IS a deliverable, not an attachment.

Student 312, Day 09 (24 Aug 2026): eight slides exported as PNGs and zipped —

    1_Data-Science.png ... 8_Advantages-and-Challenges.png

— and the stored row read "zip contained no readable files; skipped: ...
(unsupported inside zip (.png))". Their entire deck was inside. They were
marked 0.6/10 for submitting nothing.

A zip of images is how people hand over a presentation. Refusing it told a
learner who did the work that they had not done it.
"""

import io
import zipfile

import pytest

from app.utils import file_extractor as fx


def _png(colour="navy", size=(60, 40)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return buf.getvalue()


def _zip(files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, payload in files.items():
            zf.writestr(name, payload)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _fake_vision(monkeypatch):
    """Vision is the thing under test's dependency, not the thing under test."""
    seen = []

    def fake_ocr(images, kind):
        seen.append(len(images))
        return f"slide text ({len(images)} image)", ""

    monkeypatch.setattr(fx, "_ocr_with_claude", fake_ocr)
    return seen


# ── the deck comes through ──────────────────────────────────────────────────

def test_a_zip_of_slide_pngs_is_read():
    data = _zip({f"{i}_slide.png": _png() for i in range(1, 4)})
    text, why = fx.extract_text_from_bytes(data, "Data-Science.zip")
    assert why == ""
    assert "1_slide.png" in text and "3_slide.png" in text


def test_student_312s_exact_archive_is_no_longer_empty():
    names = ["1_Data-Science.png", "2_What-is-Data-Science.png",
             "3_Why-is-Data-Science-Important.png", "4_The-Data-Science-Process.png",
             "5_Tools-Used-in-Data-Science.png", "6_Applications-of-Data-Science.png",
             "7_Data-Science-in-Business.png", "8_Advantages-and-Challenges.png"]
    text, why = fx.extract_text_from_bytes(_zip({n: _png() for n in names}),
                                           "Data-Science (1).zip")
    assert why == "", why
    assert "no readable files" not in (text + why)
    for name in names:
        assert name in text


def test_jpegs_and_the_other_ordinary_formats_work_too():
    for ext in (".jpg", ".jpeg", ".webp"):
        data = _zip({f"slide{ext}": _png()})
        text, why = fx.extract_text_from_bytes(data, "deck.zip")
        assert why == "", f"{ext}: {why}"


# ── bounded, and honest about what it left out ─────────────────────────────

def test_only_a_bounded_number_of_images_is_read(monkeypatch):
    monkeypatch.setattr(fx, "ZIP_MAX_IMAGES", 3)
    data = _zip({f"{i}_slide.png": _png() for i in range(1, 9)})
    text, why = fx.extract_text_from_bytes(data, "deck.zip")
    assert "3_slide.png" in text
    assert "8_slide.png" not in text.split("[Files in the zip")[0]


def test_what_was_skipped_is_named_not_silently_dropped(monkeypatch):
    """A silent truncation reads as 'we saw everything' when we did not."""
    monkeypatch.setattr(fx, "ZIP_MAX_IMAGES", 2)
    text, _why = fx.extract_text_from_bytes(
        _zip({f"{i}_slide.png": _png() for i in range(1, 6)}), "deck.zip")
    assert "could not be read" in text
    assert "image limit" in text


def test_a_mixed_zip_reads_the_documents_AND_the_images():
    data = _zip({"notes.txt": b"my written notes about data science",
                 "slide.png": _png()})
    text, why = fx.extract_text_from_bytes(data, "work.zip")
    assert why == ""
    assert "my written notes" in text and "slide.png" in text


def test_a_zip_with_nothing_readable_still_says_so_honestly():
    data = _zip({"a.exe": b"MZ\x90\x00", "b.bin": b"\x00\x01\x02"})
    text, why = fx.extract_text_from_bytes(data, "junk.zip")
    assert text == "" and "no readable files" in why


def test_nested_archives_are_still_refused():
    """Unchanged: a zip inside a zip is a bomb vector, not a deliverable."""
    inner = _zip({"a.png": _png()})
    text, _why = fx.extract_text_from_bytes(_zip({"inner.zip": inner,
                                                 "s.png": _png()}), "outer.zip")
    assert "inner.zip" in text          # named as skipped
    assert "s.png" in text              # the real image still read


def test_the_bomb_guard_is_untouched():
    src = open("app/utils/file_extractor.py", encoding="utf-8").read()
    body = src[src.index("def _extract_zip("):src.index("def _extract_inner(")]
    assert "ZIP_MAX_TOTAL" in body and "ZIP_MAX_FILES" in body
