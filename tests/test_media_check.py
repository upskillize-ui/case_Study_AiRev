"""The diagnostic that ends the guessing about the 8 unread Audio Overviews.

Eight Day-05 learners submitted recordings that were never read. The pipeline
exists, ffmpeg is in the image, the dispatch is wired — and a week went by
without anyone establishing WHICH link in the chain was missing at runtime.
A tool that answers that in one command is worth more than another theory.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import media_check


def test_the_wiring_check_passes_on_a_correctly_wired_tree():
    """If this ever fails, media dispatch has been broken by a refactor."""
    assert media_check.check_wiring() is True


def test_a_missing_key_is_reported_as_a_failure(monkeypatch, capsys):
    monkeypatch.delenv("TRANSCRIBE_API_KEY", raising=False)
    assert media_check.check_key() is False
    assert "unset" in capsys.readouterr().out


def test_a_present_key_passes_and_is_never_printed(monkeypatch, capsys):
    """Ranjana, standing instruction: never echo a secret."""
    monkeypatch.setenv("TRANSCRIBE_API_KEY", "sk-super-secret-value-123456")
    assert media_check.check_key() is True
    out = capsys.readouterr().out
    assert "sk-super-secret" not in out
    assert "characters" in out, "report the length, never the value"


def test_ffmpeg_presence_is_checked_not_assumed(capsys):
    media_check.check_tools()
    out = capsys.readouterr().out
    assert "ffmpeg installed" in out and "ffprobe installed" in out


def test_a_missing_file_fails_honestly(capsys):
    assert media_check.read_one("/nonexistent/overview.m4a") is False
    assert "no such file" in capsys.readouterr().out


def test_the_tool_says_the_space_must_be_checked_separately(capsys):
    """The key is per-environment. A green run on her laptop proves nothing
    about the Space, and that misreading is exactly how a week was lost."""
    import pytest
    with pytest.raises(SystemExit):
        media_check.main()
    out = capsys.readouterr().out
    assert "Space" in out and "proves nothing" in out


def test_the_tool_writes_nothing():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "media_check.py"), encoding="utf-8").read()
    for forbidden in ("execute(", "UPDATE ", "INSERT ", "DELETE ", "open(",):
        if forbidden == "open(":
            assert '"rb"' in src, "the only open() may be reading a local file"
            continue
        assert forbidden not in src, f"a read-only tool must not {forbidden}"
