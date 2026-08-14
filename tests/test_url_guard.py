"""Every server-side fetch of a learner-supplied URL goes through one gate.

Pinned to the audit of 14 Aug 2026, which found that `fileUrl` — a plain string
in the submit request body — reached an UNGUARDED downloader while the hardened
guard sat next to it protecting only URLs typed into the answer box:

    POST /api/review/submit-assignment
    {"fileName": "x.txt",
     "fileUrl": "http://169.254.169.254/latest/meta-data/"}

fileName drove extension dispatch, so the response decoded as plain text,
landed in assignment_submissions.notes, and came back out of
GET /assignment-submission/{id}. Server-side request forgery with the response
handed to the attacker.

These tests exist so the two paths cannot drift apart again.
"""
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.utils import url_guard
from app.utils.url_guard import check_public_url, resolve_lms_url


def _resolves_to(monkeypatch, addr):
    monkeypatch.setattr(
        url_guard.socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (addr, 443))])


# ── the attack that was live ──────────────────────────────────────────────

def test_the_metadata_endpoint_is_refused():
    ok, why = check_public_url("http://169.254.169.254/latest/meta-data/")
    assert ok is False and "non-public" in why


@pytest.mark.parametrize("url", [
    "http://127.0.0.1:8000/admin",
    "http://localhost/health",
    "http://[::1]/",
    "http://0.0.0.0/",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
    "http://example.com:22/",
])
def test_unsafe_targets_are_refused(url):
    ok, why = check_public_url(url)
    assert ok is False, f"{url} allowed ({why})"


@pytest.mark.parametrize("addr", [
    "10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.169.254",
    "127.0.0.1", "0.0.0.0", "224.0.0.1",
])
def test_every_private_range_is_refused(monkeypatch, addr):
    """A public hostname pointing at a private address must not pass."""
    _resolves_to(monkeypatch, addr)
    ok, _ = check_public_url("https://evil.example.com/x")
    assert ok is False, f"{addr} allowed"


def test_ipv6_mapped_ipv4_loopback_is_refused(monkeypatch):
    monkeypatch.setattr(
        url_guard.socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET6, socket.SOCK_STREAM, 6, "",
                          ("::ffff:127.0.0.1", 443, 0, 0))])
    ok, _ = check_public_url("https://evil.example.com/")
    assert ok is False


def test_a_host_with_one_bad_address_is_refused(monkeypatch):
    """Split-horizon DNS: one public A record, one private. Refuse, don't race."""
    monkeypatch.setattr(
        url_guard.socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
                         (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))])
    ok, _ = check_public_url("https://evil.example.com/")
    assert ok is False


def test_a_genuinely_public_host_is_allowed(monkeypatch):
    _resolves_to(monkeypatch, "93.184.216.34")
    ok, why = check_public_url("https://example.com/artifact")
    assert ok is True, why


# ── the crash ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://a:99999999/x",     # urlparse raises ValueError LAZILY from .port
    "http://a:abc/x",
    "http://[/x",
])
def test_a_malformed_port_is_refused_not_raised(url):
    """This escaped as an unhandled 500: any answer text containing such a
    string denied the whole submit flow."""
    ok, why = check_public_url(url)
    assert ok is False and why


# ── the confused deputy ───────────────────────────────────────────────────

def test_an_upload_path_still_resolves():
    url, why = resolve_lms_url("/uploads/report.pdf")
    assert why == "" and url.endswith("/uploads/report.pdf")
    assert url.startswith("http")


def test_a_signed_query_string_survives():
    url, why = resolve_lms_url("/uploads/a.pdf?sig=abc123")
    assert why == "" and url.endswith("/uploads/a.pdf?sig=abc123")


@pytest.mark.parametrize("path", [
    "/api/admin/users",
    "/api/users",
    "/admin",
    "/internal/config",
    "/",
])
def test_non_upload_paths_on_the_lms_backend_are_refused(path):
    """resolve_lms_url made AiRev a confused deputy for the LMS backend: any
    path starting with '/' was fetched from the backend with AiRev's egress
    identity and returned to the student."""
    url, why = resolve_lms_url(path)
    assert url == "", f"{path} was allowed"
    assert why


@pytest.mark.parametrize("path", [
    "/uploads/../api/admin/users",
    "/uploads/../../etc/passwd",
    "/uploads/%2e%2e/api/admin",
    "//evil.example.com/x",
])
def test_traversal_cannot_escape_the_allowlist(path):
    """Normalisation happens BEFORE prefix matching, or the allowlist is
    decorative."""
    url, why = resolve_lms_url(path)
    assert url == "", f"{path} resolved to {url}"
    assert why


def test_an_absolute_internal_url_is_refused():
    url, why = resolve_lms_url("http://169.254.169.254/latest/meta-data/")
    assert url == "" and "non-public" in why


def test_empty_input_is_refused():
    url, why = resolve_lms_url("")
    assert url == "" and why


# ── the two paths must stay unified ───────────────────────────────────────

def test_file_extractor_refuses_before_any_network_call(monkeypatch):
    """extract_text_from_url is the fileUrl path. It must refuse an internal
    target WITHOUT reaching the downloader."""
    from app.utils import file_extractor as fx
    called = {"n": 0}
    monkeypatch.setattr(fx, "_download_file",
                        lambda *a, **k: (called.__setitem__("n", called["n"] + 1), (None, "x"))[1])
    text, why = fx.extract_text_from_url(
        "http://169.254.169.254/latest/meta-data/", "x.txt")
    assert text == ""
    assert called["n"] == 0, "the downloader was reached for an internal URL"


def test_submission_intake_uses_the_same_guard():
    """One implementation, not two — the divergence IS the vulnerability."""
    from app.utils import submission_intake as intake
    assert intake._safe_target("http://127.0.0.1/") == check_public_url("http://127.0.0.1/")


@pytest.mark.parametrize("path", [
    "/uploads/%2e%2e/api/admin",
    "/uploads/%252e%252e/api/admin",
    "/uploads/%2e%2e%2fapi%2fadmin",
    "/uploads/..%2f..%2fadmin",
    "/uploads/\\..\\admin",
])
def test_encoded_traversal_cannot_escape_either(path):
    """normpath does not treat %2e%2e as '..' — but the origin server does.
    Decode first, then normalise, then match."""
    url, why = resolve_lms_url(path)
    assert url == "", f"{path} resolved to {url}"
    assert why


def test_a_null_byte_is_refused():
    url, _ = resolve_lms_url("/uploads/a.pdf\x00.jpg")
    assert url == ""
