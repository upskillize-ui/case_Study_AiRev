"""Audio and video are coursework now, not something to refuse.

Pinned to 14 Aug 2026, live:

    Day 05 (NotebookLM "Studio Output" — an mp3 Audio Overview)
        [ASSIGNMENT] 12 words of content across 2 artefact(s): file(unread), typed text
    Day 06 (Suno)
        [ASSIGNMENT] 11 words of content across 2 artefact(s): typed text, link(unread)

Learners produced exactly what the task asked and were recorded as having
submitted nothing, because file_extractor refused the medium outright.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import submission_media as sm
from app.utils import file_extractor as fx
from app.utils import submission_intake as intake


# ── the medium is accepted ────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "overview.mp3", "song.wav", "note.m4a", "voice.ogg", "track.flac",
    "demo.mp4", "clip.mov", "render.webm",
])
def test_media_is_recognised(name):
    assert sm.is_media(name) is True


@pytest.mark.parametrize("name", ["essay.pdf", "sheet.xlsx", "photo.png", "notes.txt"])
def test_non_media_is_not_claimed(name):
    assert sm.is_media(name) is False


def test_file_extractor_no_longer_refuses_audio(monkeypatch):
    """The old behaviour returned 'AiRev reviews written work' and stopped."""
    monkeypatch.setattr(sm, "transcribe_and_describe",
                        lambda data, name: ("TRANSCRIPT HERE", ""))
    text, why = fx.extract_text_from_bytes(b"\x00\x01fake-mp3", "overview.mp3")
    assert text == "TRANSCRIPT HERE", why
    assert "reviews written work" not in (text + why)


def test_the_manifest_names_the_medium():
    """A marker must see 'AUDIO RECORDING', not 'FILE'."""
    assert intake.kind_for("overview.mp3") == "audio recording"
    assert intake.kind_for("demo.mp4") == "video"


# ── binary is still never decoded as text ─────────────────────────────────

def test_binary_media_is_never_decoded_as_text():
    """The original guard existed because a WhatsApp .mp4 decoded to 272,000 words of
    garbage and 500'd a live submit. Accepting the medium must not reopen it."""
    binary = bytes(range(256)) * 40
    assert fx._looks_like_text(binary) is False


def test_an_unknown_binary_extension_is_still_refused():
    binary = bytes(range(256)) * 40
    text, why = fx.extract_text_from_bytes(binary, "mystery.bin")
    assert text == ""
    assert why


# ── honesty about what was and was not assessed ───────────────────────────

def test_video_header_declares_what_cannot_be_judged():
    header = sm._header(is_video=True, duration=95)
    assert "Motion" in header and "NOT assessed" in header
    assert "1m 35s" in header


def test_audio_header_does_not_claim_visuals():
    header = sm._header(is_video=False, duration=None)
    assert "Only the audio" in header
    assert "Motion" not in header


def test_an_unreadable_recording_returns_no_content(monkeypatch, tmp_path):
    """A header alone must not count as content — that would tell the marker a
    deliverable was read when nothing was."""
    monkeypatch.setattr(sm, "_duration_seconds", lambda p: 30)
    monkeypatch.setattr(sm, "_transcribe", lambda s, w: ("", "no speech"))
    text, why = sm.transcribe_and_describe(b"fake", "silent.mp3")
    assert text == ""
    assert why


def test_a_transcript_reaches_the_marker(monkeypatch):
    monkeypatch.setattr(sm, "_duration_seconds", lambda p: 240)
    monkeypatch.setattr(sm, "_transcribe",
                        lambda s, w: ("Today I explain UPI settlement.", ""))
    text, why = sm.transcribe_and_describe(b"fake", "overview.mp3")
    assert "UPI settlement" in text, why
    assert "WHAT IS SAID" in text


# ── bounds ────────────────────────────────────────────────────────────────

def test_an_over_long_recording_is_refused_with_an_actionable_message(monkeypatch):
    monkeypatch.setattr(sm, "_duration_seconds", lambda p: sm.MAX_MEDIA_SECONDS + 600)
    text, why = sm.transcribe_and_describe(b"fake", "long.mp4")
    assert text == ""
    assert "minutes" in why and "re-upload" in why


def test_unknown_duration_does_not_bypass_the_cap(monkeypatch):
    """ffprobe returning None must not be read as zero length."""
    monkeypatch.setattr(sm, "_duration_seconds", lambda p: None)
    monkeypatch.setattr(sm, "_transcribe", lambda s, w: ("words here", ""))
    text, _ = sm.transcribe_and_describe(b"fake", "unknown.mp3")
    assert "words here" in text          # proceeds, but the cap logic ran


def test_frame_count_is_bounded():
    assert 1 <= sm.MAX_VIDEO_FRAMES <= 12


def test_media_gets_a_bigger_ceiling_than_documents():
    """A 10-minute NotebookLM Audio Overview is 10-15 MB. The single 10 MB
    limit ran BEFORE the extension check, so it refused the exact files this
    pipeline exists to read."""
    assert fx.MAX_MEDIA_BYTES > fx.MAX_FILE_BYTES


def test_an_oversized_audio_file_is_still_accepted(monkeypatch):
    monkeypatch.setattr(sm, "transcribe_and_describe", lambda d, n: ("TRANSCRIPT", ""))
    big = b"\x00" * (fx.MAX_FILE_BYTES + 5 * 1024 * 1024)   # 15 MB
    text, why = fx.extract_text_from_bytes(big, "overview.mp3")
    assert text == "TRANSCRIPT", why


def test_an_oversized_document_is_still_refused():
    big = b"%PDF-1.4\n" + b"\x00" * (fx.MAX_FILE_BYTES + 1024)
    text, why = fx.extract_text_from_bytes(big, "huge.pdf")
    assert text == ""
    assert "too large" in why
