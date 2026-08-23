"""An Audio Overview must survive the download, not just the extractor.

Day 05 (22 Aug): every .m4a and .mp4 came back "no stored content to
review" — students 405, 275, 466, 160, 317, 286, 526, 1347. Those files
are NotebookLM's Audio and Video Overviews, which is precisely what the
assignment asked them to create. The students who did the task best were
the only ones we could not read.

Cause: MAX_MEDIA_BYTES (80 MB) was consulted by the EXTRACTOR, after the
bytes had arrived. The DOWNLOADER capped everything at MAX_FILE_BYTES
(10 MB), so a 15 MB audio overview was refused before the media ceiling
ever ran. The larger limit was decorative.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.utils.file_extractor import (MAX_FILE_BYTES, MAX_MEDIA_BYTES,
                                      size_ceiling_for)


def test_audio_and_video_get_the_media_ceiling():
    for name in ("How_the_INA_ended_British_rule.m4a",
                 "Barter_System_to_Money.mp4",
                 "VID-20260813-WA0006.mp4",
                 "Commerce_Speaks_the_Real_Language_of_Money.m4a"):
        assert size_ceiling_for(name) == MAX_MEDIA_BYTES, name


def test_documents_and_images_keep_the_tighter_one():
    """A 60 MB 'PDF' is not a coursework document."""
    for name in ("Day05_Assignment.pdf", "NotebookLM Mind Map.png",
                 "report.docx", "deck.pptx"):
        assert size_ceiling_for(name) == MAX_FILE_BYTES, name


def test_the_media_ceiling_is_actually_larger():
    assert MAX_MEDIA_BYTES > MAX_FILE_BYTES


def test_a_query_string_does_not_hide_the_extension():
    assert size_ceiling_for("clip.mp4?v=2&token=abc") == MAX_MEDIA_BYTES


def test_an_unknown_or_missing_name_stays_conservative():
    for name in ("", "noextension", None):
        assert size_ceiling_for(name) == MAX_FILE_BYTES, repr(name)


def test_the_downloader_uses_the_per_file_ceiling_not_the_default():
    """The whole bug: the fetch loop must not hardcode MAX_FILE_BYTES."""
    import inspect
    from app.utils import file_extractor as fe
    src = inspect.getsource(fe._get_following_redirects)
    assert "MAX_FILE_BYTES" not in src, \
        "the fetch loop is capping every file at the document limit again"
    assert "ceiling" in src
    assert "ceiling" in inspect.signature(fe._download_file).parameters


def test_the_reader_passes_the_ceiling_down():
    import inspect
    from app.utils import file_extractor as fe
    src = inspect.getsource(fe.extract_text_from_url)
    assert "size_ceiling_for" in src, "the caller still uses the default"
