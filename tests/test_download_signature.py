"""EVERY FILE DOWNLOAD WAS FAILING. Found 24 Aug 2026, by file_check.

    download failed: _get_following_redirects() takes 2 positional arguments
    but 3 were given

The per-file size ceiling was added to the CALLER and used throughout the
FUNCTION BODY, and never added to the function's SIGNATURE. So every call
raised TypeError, _download_file caught it as a generic failure, retried four
times with backoff — nine seconds of sleeping per file — and reported
"download failed".

Every screenshot, PDF, deck and recording a learner uploaded by URL has been
failing since. On Day 07 that was roughly fifty students told their work could
not be read. It was readable. It was never fetched.

Two tests here, and the second is the one that matters: a signature test would
have caught this, and no test in 1,700 was looking at the join between the
downloader and its retry loop.
"""

import inspect

import pytest

from app.utils import file_extractor as fx


def test_the_downloader_accepts_the_ceiling_its_caller_passes():
    """The exact defect: caller passes three arguments, function took two."""
    params = inspect.signature(fx._get_following_redirects).parameters
    assert list(params) == ["client", "url", "ceiling"]


def test_the_ceiling_is_required_not_defaulted():
    """A default would have hidden this same mismatch for another week, and
    would let a caller silently cap an Audio Overview at the document limit."""
    params = inspect.signature(fx._get_following_redirects).parameters
    assert params["ceiling"].default is inspect.Parameter.empty


def test_the_body_uses_the_ceiling_it_now_receives():
    """It always did — that was the half of the edit that landed."""
    src = inspect.getsource(fx._get_following_redirects)
    assert "> ceiling" in src and "ceiling // 1024" in src


def test_the_caller_and_the_function_actually_agree():
    """The test that would have caught it: bind the real call to the real
    signature and let Python check them against each other."""
    src = inspect.getsource(fx._download_file)
    assert "_get_following_redirects(client, file_url," in src
    sig = inspect.signature(fx._get_following_redirects)
    sig.bind(object(), "https://example.com/a.pdf", 1024)      # raises if wrong


def test_a_media_caller_may_raise_the_ceiling_above_the_default():
    """The whole reason the parameter exists: an Audio Overview is legitimately
    tens of megabytes, and capping it at the document ceiling is what made
    MAX_MEDIA_BYTES decorative."""
    sig = inspect.signature(fx._get_following_redirects)
    sig.bind(object(), "https://example.com/a.m4a", fx.MAX_FILE_BYTES * 8)
    assert fx.size_ceiling_for("overview.m4a") > fx.size_ceiling_for("notes.pdf")


def test_a_download_no_longer_dies_before_it_starts(monkeypatch):
    """End to end through _download_file with a fake client: the TypeError
    used to happen before a single byte was requested."""
    calls = []

    class _FakeStream:
        status_code = 200
        headers = {"content-length": "11"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def iter_bytes(self):
            yield b"hello world"

    class _FakeClient:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url):
            calls.append(url)
            return _FakeStream()

    monkeypatch.setattr(fx.httpx, "Client", lambda **kw: _FakeClient())
    monkeypatch.setattr(fx, "check_public_url", lambda url: (True, ""))

    data, why = fx._download_file("https://example.com/dashboard.png")
    assert data == b"hello world", why
    assert why == ""
    assert len(calls) == 1, "it should not have retried at all"
