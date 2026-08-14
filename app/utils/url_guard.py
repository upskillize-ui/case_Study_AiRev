# app/utils/url_guard.py
# ---------------------------------------------------------------------------
# ONE gate for every server-side fetch of a learner-supplied URL.
#
# WHY THIS MODULE EXISTS. The service had two download paths with two different
# security postures:
#
#   submission_intake.fetch_link()  — resolved the host, refused private and
#                                     link-local addresses, re-validated every
#                                     redirect hop.
#   file_extractor._download_file() — no scheme check, no host check, no
#                                     address check, follow_redirects=True.
#
# The second one is the path a learner's `fileUrl` actually reaches. So the
# hardened guard protected URLs typed into the answer box while the request
# field named "fileUrl" walked straight through:
#
#   POST /api/review/submit-assignment
#   {"fileName": "x.txt", "fileUrl": "http://169.254.169.254/latest/meta-data/"}
#
# fileName drives extension dispatch, so ".txt" makes the extractor decode the
# response as plain text; the result is stored in assignment_submissions.notes
# and handed back by GET /assignment-submission/{id}. Full server-side request
# forgery WITH the response returned to the attacker — cloud metadata, the LMS
# backend's own internal routes, anything the Space can reach.
#
# A guard that lives next to one caller is not a guard. It lives here, and both
# callers import it. Adding a third download path means importing this too.
#
# Relative paths are the other half. resolve_lms_url() turns "/x" into
# LMS_FILE_BASE_URL + "/x", which made AiRev a confused deputy for the LMS
# backend: "/api/admin/users" is a legal value for fileUrl. Relative paths are
# now allowlisted by prefix and normalised, so a learner can reference an
# upload and nothing else.
# ---------------------------------------------------------------------------

from __future__ import annotations

import ipaddress
import os
import posixpath
import socket
from typing import Tuple
from urllib.parse import quote, unquote, urlparse, urlunparse

# Where LMS-relative upload paths resolve.
LMS_FILE_BASE_URL = os.getenv(
    "LMS_FILE_BASE_URL", "https://upskillize-lms-backend.onrender.com")

# The ONLY path prefixes a learner-supplied relative URL may address. Anything
# else on the LMS backend — /api, /admin, /internal — is out of reach by
# construction rather than by hoping nobody tries.
ALLOWED_RELATIVE_PREFIXES = tuple(
    p.strip() for p in os.getenv(
        "LMS_FILE_PATH_PREFIXES",
        "/uploads/,/upload/,/files/,/file/,/media/,/static/,/public/,/storage/,/attachments/"
    ).split(",") if p.strip()
)

ALLOWED_SCHEMES = ("http", "https")
ALLOWED_PORTS = (None, 80, 443)


def _classify(ip_text: str) -> Tuple[bool, str]:
    """True when this address is safe to connect to (i.e. genuinely public)."""
    try:
        ip = ipaddress.ip_address(ip_text)
    except ValueError:
        return False, "link host resolved to an unusable address"
    # IPv4-mapped IPv6 (::ffff:127.0.0.1) is classified correctly by the
    # ipaddress module on CPython — verified — so no manual unwrapping.
    if (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
        return False, "link points to a non-public address"
    return True, ""


def check_public_url(url: str) -> Tuple[bool, str]:
    """Reject anything that could reach our own infrastructure.

    Every hostname is resolved and EVERY resolved address must be public — a
    host with one public and one private A record is refused, not raced.

    KNOWN LIMIT: this is a check-then-connect, so a hostname whose DNS answer
    changes between the check and the connect (DNS rebinding) can still slip
    through. Closing that needs connecting to the validated address with an
    explicit Host header, or an egress firewall. Documented rather than
    silently assumed away.
    """
    try:
        p = urlparse(url)
        scheme, hostname, port = p.scheme, p.hostname, p.port
    except ValueError:
        # urlparse raises on a malformed port ("http://a:99999999/"). It is
        # raised LAZILY by .port, so this must wrap the attribute access too —
        # it previously escaped as an unhandled 500 on any submit whose answer
        # text contained such a string.
        return False, "malformed URL"

    if scheme not in ALLOWED_SCHEMES:
        return False, f"unsupported link type ({scheme or 'no scheme'})"
    if not hostname:
        return False, "link has no host"
    if port not in ALLOWED_PORTS:
        return False, f"unsupported port ({port})"

    try:
        infos = socket.getaddrinfo(
            hostname, port or (443 if scheme == "https" else 80),
            proto=socket.IPPROTO_TCP)
    except Exception:
        return False, "link host could not be resolved"

    for info in infos:
        ok, why = _classify(info[4][0])
        if not ok:
            return False, why
    return True, ""


def resolve_lms_url(file_url: str) -> Tuple[str, str]:
    """Turn a stored/​submitted file reference into a URL that is safe to fetch.

    Returns (url, reason). url is "" when the reference must be refused.

    Absolute URLs are address-checked. Relative paths are normalised first —
    so "/uploads/../api/admin/users" collapses to "/api/admin/users" and is
    then refused by the prefix allowlist, rather than being passed to the LMS
    backend as written.
    """
    if not file_url:
        return "", "no file_url provided"
    ref = file_url.strip()

    if ref.startswith("/"):
        raw_path = ref.split("?", 1)[0].split("#", 1)[0]

        # DECODE before normalising. posixpath.normpath leaves "%2e%2e" intact
        # — it is not ".." to Python — but the origin server percent-decodes it
        # and walks the parent anyway, so "/uploads/%2e%2e/api/admin" passed the
        # allowlist and still reached the admin route. Decode repeatedly, since
        # "%252e" decodes to "%2e" and then to ".".
        decoded = raw_path
        for _ in range(3):
            once = unquote(decoded)
            if once == decoded:
                break
            decoded = once
        else:
            return "", "unsupported file path"      # still changing — refuse

        if "\\" in decoded or "\x00" in decoded:
            return "", "unsupported file path"

        clean = posixpath.normpath(decoded)
        if not clean.startswith("/") or clean.startswith("//"):
            return "", "unsupported file path"
        if not clean.startswith(ALLOWED_RELATIVE_PREFIXES):
            return "", "that file path is not an upload location"

        base = LMS_FILE_BASE_URL.rstrip("/")
        # Re-encode from the DECODED, NORMALISED path so nothing we just
        # neutralised can be smuggled back in. "/" is safe; everything else
        # that needs escaping gets escaped.
        safe_path = quote(clean, safe="/-._~")
        query = ref.split("?", 1)[1] if "?" in ref else ""
        return f"{base}{safe_path}" + (f"?{query}" if query else ""), ""

    ok, why = check_public_url(ref)
    if not ok:
        return "", why

    # Rebuild from parsed parts so stray control characters or embedded
    # newlines cannot survive into the request line.
    p = urlparse(ref)
    return urlunparse((p.scheme, p.netloc, p.path, p.params, p.query, "")), ""
