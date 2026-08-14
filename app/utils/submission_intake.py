# app/utils/submission_intake.py
# ---------------------------------------------------------------------------
# One place that turns WHATEVER a learner submitted into reviewable text.
#
# Learners do not submit a single tidy essay. Across the four review types they
# submit, in every combination: typed text, a pasted link (a Claude artifact, a
# published Perplexity page, a Google Doc, a Canva design), a photograph of
# handwritten notes, an AI-generated image, a PDF, a Word file, a spreadsheet,
# a deck, a notebook, a zip. Every one of those is a legitimate solution and
# every one must reach the marker.
#
# WHY THIS MODULE EXISTS — two defects it closes:
#
#   1. THE ATTACHMENT WAS INVISIBLE. Each route built its answer text by
#      concatenating EXTRACTED text and threw the provenance away. When a task
#      said "generate an AI image of your five-year future self" and the
#      learner did exactly that, the marker received a bare caption with no
#      indication that an image existed — and scored the image criterion
#      (40% of that rubric) at zero. Roughly half of the 13 Aug batch failed
#      this way, for work that was actually done.
#
#   2. LINKS WERE NEVER OPENED. A pasted artifact URL reached the marker as a
#      literal string. The learner's whole deliverable sat one HTTP GET away
#      and was graded as "no evidence".
#
# So intake records ARTEFACTS, not strings: what arrived, of what kind, what
# was read from it, and — when nothing could be read — why. render() emits a
# factual manifest plus the content. The manifest states what exists; it never
# states what it is worth. Quality is judged from the content alone.
#
# SAFETY. Link fetching takes a URL from untrusted learner input, so it is a
# server-side request forgery vector by construction. _safe_target() resolves
# the host and refuses loopback, private, link-local (including the cloud
# metadata endpoint), reserved and multicast addresses; only http/https on
# ports 80/443 are allowed; redirects are followed MANUALLY so every hop is
# re-validated rather than trusted. Never replace that with
# follow_redirects=True.
# ---------------------------------------------------------------------------

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from app.utils.url_guard import check_public_url
from app.utils.file_extractor import (
    MAX_TEXT_CHARS,
    extract_text_from_bytes,
    extract_upload,
)

logger = logging.getLogger(__name__)

# ---------- config ---------------------------------------------------------

MAX_LINKS = int(os.getenv("MAX_SUBMISSION_LINKS", "3"))
LINK_TIMEOUT = float(os.getenv("LINK_FETCH_TIMEOUT", "12"))
# Link fetching happens INSIDE the submit request, so the worst case is the
# learner's wait. MAX_LINKS * LINK_TIMEOUT is the theoretical ceiling; this
# budget is the real one — once it is spent, remaining links are recorded as
# unfetched rather than holding the submission open.
LINK_TOTAL_BUDGET = float(os.getenv("LINK_TOTAL_BUDGET", "25"))
LINK_MAX_BYTES = int(os.getenv("LINK_MAX_BYTES", str(8 * 1024 * 1024)))
LINK_MAX_REDIRECTS = 4
LINK_MAX_CHARS = int(os.getenv("LINK_MAX_CHARS", "20000"))
# A fetched page yielding less than this is not real content — it is a
# JavaScript shell. Say so plainly instead of feeding the marker boilerplate.
LINK_MIN_WORDS = 25

_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)}]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?’”'\")]}>"

# Extension → the word a human would use for it. Drives the manifest only.
_KIND_BY_EXT = {
    **{e: "image" for e in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp")},
    **{e: "document" for e in (".pdf", ".doc", ".docx", ".rtf", ".txt", ".md", ".odt")},
    **{e: "spreadsheet" for e in (".xlsx", ".xlsm", ".xltx", ".csv", ".tsv", ".ods")},
    **{e: "slide deck" for e in (".pptx", ".potx", ".ppt", ".odp")},
    **{e: "notebook" for e in (".ipynb",)},
    **{e: "archive" for e in (".zip",)},
    **{e: "audio recording" for e in (".mp3", ".wav", ".m4a", ".aac", ".ogg",
                                      ".opus", ".wma", ".flac")},
    **{e: "video" for e in (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v",
                            ".3gp", ".wmv", ".flv")},
}


def kind_for(name: str) -> str:
    """Human-facing category for a filename. Unknown extensions are 'file' —
    honest, and never claims a type we did not verify."""
    lowered = (name or "").lower().split("?")[0]
    ext = "." + lowered.rsplit(".", 1)[-1] if "." in lowered else ""
    return _KIND_BY_EXT.get(ext, "file")


# ---------- the record -----------------------------------------------------


@dataclass
class Artefact:
    """One thing the learner submitted.

    kind      — 'typed text', 'image', 'document', 'link' ...
    label     — filename or URL as the learner supplied it
    text      — what was read from it ('' when nothing could be read)
    note      — why nothing could be read; shown to the learner, not the marker
    confirmed — we hold proof this artefact exists (bytes fetched, or the
                learner typed it). False when a download failed: the row may
                reference a file we could not verify, and the marker must not
                be told a deliverable exists on that basis.
    """

    kind: str
    label: str
    text: str = ""
    note: str = ""
    confirmed: bool = True

    @property
    def readable(self) -> bool:
        return bool((self.text or "").strip())

    @property
    def is_deliverable(self) -> bool:
        """A produced artefact — anything the learner MADE rather than typed."""
        return self.kind != "typed text" and self.confirmed


# ---------- builders -------------------------------------------------------


def from_typed(text: str) -> Artefact:
    return Artefact(kind="typed text", label="answer box", text=text or "")


def from_upload(file_data: Optional[str], file_url: Optional[str],
                file_name: str = "") -> Artefact:
    """The attachment on THIS request (inline bytes or a URL)."""
    label = file_name or file_url or "uploaded file"
    text, why = extract_upload(file_data, file_url, file_name)
    return _finish_file(label, text, why, had_bytes=bool(file_data))


def from_stored_file(file_url: str, file_name: str = "") -> Artefact:
    """A file already on the submission row (Coursework writes file_path)."""
    from app.utils.file_extractor import extract_text_from_url

    label = file_name or file_url or "stored file"
    text, why = extract_text_from_url(file_url, file_name)
    return _finish_file(label, text, why, had_bytes=False)


def _finish_file(label: str, text: str, why: str, had_bytes: bool) -> Artefact:
    """Decide whether an unreadable attachment still counts as EXISTING.

    The distinction is the whole point. 'We opened your image and it carries no
    words' proves a deliverable exists — the learner produced it. 'We could not
    download your file' proves nothing, and inventing a deliverable from it
    would be exactly the fabrication this project forbids.
    """
    kind = kind_for(label)
    if text:
        return Artefact(kind=kind, label=label, text=text, confirmed=True)

    reason = (why or "").lower()
    unreachable = ("download" in reason or "not found" in reason
                   or "http " in reason or "no file" in reason)
    return Artefact(kind=kind, label=label, note=why or "nothing readable found",
                    confirmed=had_bytes or not unreachable)


def from_links_in(text: str, limit: int = MAX_LINKS) -> List[Artefact]:
    """Open every URL the learner pasted and read what is behind it.

    Published artifacts, hosted dashboards and shared docs are the deliverable
    for a growing share of these tasks. Leaving them unopened graded the URL
    string, not the work.
    """
    import time as _time

    out: List[Artefact] = []
    deadline = _time.monotonic() + LINK_TOTAL_BUDGET
    for url in find_urls(text)[:limit]:
        if _time.monotonic() >= deadline:
            out.append(Artefact(kind="link", label=url, confirmed=True,
                                note="not opened — link-reading time budget spent"))
            continue
        body, why = fetch_link(url)
        if body:
            out.append(Artefact(kind="link", label=url, text=body, confirmed=True))
        else:
            # The link itself is still evidence the learner published SOMETHING;
            # we simply could not read it from here. Both facts go on the record.
            out.append(Artefact(kind="link", label=url, note=why, confirmed=True))
    return out


def find_urls(text: str) -> List[str]:
    """Distinct URLs in submission order, trailing sentence punctuation removed."""
    seen, urls = set(), []
    for raw in _URL_RE.findall(text or ""):
        url = raw.rstrip(_TRAILING_PUNCT)
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


# ---------- link fetching (SSRF-guarded) -----------------------------------


def _safe_target(url: str) -> Tuple[bool, str]:
    """Reject anything that could reach our own infrastructure.

    Learner-supplied URLs are untrusted input hitting an HTTP client that runs
    inside the Space, so a naive fetch of http://169.254.169.254/ or
    http://127.0.0.1:8000/ would turn the reviewer into a proxy for our
    internal network. Every hostname is resolved and EVERY resolved address
    must be public.
    """
    # Delegated to url_guard so link-fetching and file-fetching cannot drift
    # apart again — that divergence is what left `fileUrl` unguarded while this
    # function was busy protecting URLs typed into the answer box.
    #
    # url_guard also wraps the .port access: urlparse raises ValueError LAZILY
    # from that attribute, so "http://a:99999999/" pasted into an answer used
    # to escape as an unhandled 500 and deny the whole submit flow.
    return check_public_url(url)


def fetch_link(url: str) -> Tuple[str, str]:
    """Fetch one link and return (readable_text, reason_if_empty).

    Redirects are followed by hand so that every hop is re-validated — a
    permitted public URL that 302s to 169.254.169.254 is the classic bypass of
    a one-shot check.
    """
    current = url
    with httpx.Client(timeout=LINK_TIMEOUT, follow_redirects=False,
                      headers={"User-Agent": "AiRev/3.1 (coursework review)"}) as client:
        for _ in range(LINK_MAX_REDIRECTS):
            ok, why = _safe_target(current)
            if not ok:
                return "", why
            try:
                r = client.get(current)
            except Exception as e:
                return "", f"could not open the link ({type(e).__name__})"

            if r.status_code in (301, 302, 303, 307, 308):
                nxt = r.headers.get("location")
                if not nxt:
                    return "", f"link redirected with no destination (HTTP {r.status_code})"
                current = httpx.URL(current).join(nxt).__str__()
                continue
            if r.status_code != 200:
                return "", f"link returned HTTP {r.status_code}"

            content = r.content[:LINK_MAX_BYTES]
            ctype = (r.headers.get("content-type") or "").split(";")[0].strip().lower()
            return _read_response(content, ctype, current)

    return "", "link redirected too many times"


_META_RE = re.compile(
    r"<meta\s+[^>]*?(?:property|name)\s*=\s*[\"\']([^\"\']+)[\"\'][^>]*?"
    r"content\s*=\s*[\"\']([^\"\']*)[\"\']", re.I | re.S)
_META_RE_REVERSED = re.compile(
    r"<meta\s+[^>]*?content\s*=\s*[\"\']([^\"\']*)[\"\'][^>]*?"
    r"(?:property|name)\s*=\s*[\"\']([^\"\']+)[\"\']", re.I | re.S)
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)

# Which tags carry the substance, and what to call them for the marker.
_META_FIELDS = [
    ("og:title", "Title"), ("twitter:title", "Title"),
    ("og:description", "Description"), ("twitter:description", "Description"),
    ("description", "Description"),
    ("og:site_name", "Published on"), ("og:type", "Kind"),
    ("music:musician", "Credited artist"), ("author", "Author"),
    ("og:audio", "Audio"), ("og:video", "Video"),
]


def describe_from_metadata(html: str) -> str:
    """What a link preview would show — title, description, platform, author.

    Server-rendered by every platform that wants its links to preview nicely,
    which is exactly the client-rendered platforms whose body text we cannot
    read. Returns "" when there is nothing worth reporting.

    This is DESCRIPTION, not judgement: it states what the published artefact
    says about itself. The marker still decides whether that meets the task.
    """
    import html as _html

    found = {}
    for pattern, key_first in ((_META_RE, True), (_META_RE_REVERSED, False)):
        for a, b in pattern.findall(html or ""):
            key, value = (a, b) if key_first else (b, a)
            key = key.strip().lower()
            value = _html.unescape(value or "").strip()
            if value and key not in found:
                found[key] = value

    lines, seen_labels = [], set()
    for key, label in _META_FIELDS:
        value = found.get(key)
        # First tag wins per label, so og:title beats twitter:title without
        # printing both.
        if value and label not in seen_labels:
            seen_labels.add(label)
            lines.append(f"{label}: {value}")

    if not lines:
        m = _TITLE_RE.search(html or "")
        if m:
            title = _html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
            if title:
                lines.append(f"Title: {title}")

    if not lines:
        return ""
    return ("[This link is a published page whose content is rendered in the "
            "browser, so its body text could not be read from the server. What "
            "the page states about itself:]\n" + "\n".join(lines))


def _read_response(content: bytes, ctype: str, url: str) -> Tuple[str, str]:
    """Turn a fetched body into reviewable text, whatever it turned out to be."""
    if not content:
        return "", "the link returned an empty page"

    if ctype in ("text/html", "application/xhtml+xml") or not ctype:
        html = content.decode("utf-8", errors="ignore")
        text = html_to_text(html)
        if len(text.split()) >= LINK_MIN_WORDS:
            return text[:LINK_MAX_CHARS], ""

        # Single-page apps — Suno, Claude artifacts, Canva, Gamma, most hosted
        # dashboards — render client-side, so the body is a loader, not the
        # work. Passing that loader off as the submission would be dishonest.
        #
        # But the page is not empty: link previews exist, so these platforms
        # server-render Open Graph tags with the real title, description and
        # author. That IS publishable evidence of what the learner made, and
        # discarding it told the marker "nothing here" about a learner who had
        # published exactly what the task asked for. Observed live 14 Aug on
        # Day 06 (Suno): "11 words of content, link(unread)".
        meta = describe_from_metadata(html)
        if meta:
            return meta, ""
        return "", ("the page loads its content in the browser, so its text "
                    "could not be read server-side")

    # Anything else — a PDF, an image, a deck behind a direct link — goes
    # through the same extractor every upload uses. One code path, one set of
    # format rules, no second implementation to drift.
    name = urlparse(url).path.rsplit("/", 1)[-1] or "linked file"
    if "." not in name and ctype:
        name += "." + ctype.rsplit("/", 1)[-1]
    text, why = extract_text_from_bytes(content, name)
    return (text[:LINK_MAX_CHARS], "") if text else ("", why or "nothing readable at that link")


_TAG_STRIP = re.compile(
    r"<(script|style|noscript|template|svg)\b[^>]*>.*?</\1>", re.I | re.S)
_BLOCK_END = re.compile(r"</(p|div|li|h[1-6]|tr|section|article|br)\s*>|<br\s*/?>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")


def html_to_text(html: str) -> str:
    """Readable text from a page. Deliberately dependency-free: adding a parser
    to run on learner-supplied HTML buys formatting we do not need and a parser
    CVE surface we do not want."""
    import html as _html

    text = _TAG_STRIP.sub(" ", html)
    text = _BLOCK_END.sub("\n", text)
    text = _ANY_TAG.sub(" ", text)
    text = _html.unescape(text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n\s*", "\n\n", text)
    return text.strip()


# ---------- rendering ------------------------------------------------------

_MANIFEST_RULE = (
    "This list is the record of WHAT was submitted, not of its quality. An item "
    "listed here WAS submitted; an item not listed WAS NOT. Where the task asks "
    "the learner to PRODUCE something — an image, a link, a published artifact, "
    "a document, a workbook — settle that requirement against this list: never "
    "report a deliverable as missing when it appears here, and never credit one "
    "that does not. Judge quality, depth and correctness only from the content "
    "below, and only against this task's own criteria."
)


def render(artefacts: List[Artefact]) -> Tuple[str, str]:
    """Return (manifest, content).

    Kept separate on purpose: the manifest is provenance and must never inflate
    the word count the length gates run on. Callers count words on `content`
    and send `manifest + content` to the marker.
    """
    usable = [a for a in artefacts if a.readable]

    # Nothing to declare. When a learner only typed an answer, a manifest adds
    # tokens and tells the marker nothing it cannot see, so the answer travels
    # exactly as before — this change must not alter plain written submissions.
    if all(a.kind == "typed text" for a in artefacts):
        return "", "\n\n".join(a.text.strip() for a in usable).strip()

    lines: List[str] = []
    for i, a in enumerate(artefacts, 1):
        head = f"  {i}. {a.kind.upper()} — {_short(a.label)}"
        if a.readable:
            words = len(a.text.split())
            lines.append(f"{head} — read ({words} words of content, item {i} below)")
        elif a.confirmed:
            lines.append(f"{head} — submitted, but its content could not be read "
                         f"({a.note or 'no readable text'}). It exists; its "
                         f"substance is unknown.")
        else:
            lines.append(f"{head} — referenced but could not be retrieved "
                         f"({a.note or 'unavailable'}), so it is NOT evidence "
                         f"that anything was submitted.")

    manifest = (
        "=== SUBMISSION MANIFEST ===\n"
        f"The learner submitted {len(artefacts)} item(s):\n"
        + "\n".join(lines)
        + "\n\n" + _MANIFEST_RULE + "\n"
    )

    # Numbered by POSITION, not by list.index() — two artefacts can compare
    # equal (a dataclass with the same fields), and index() would then label
    # both with the first one's number and mis-key them against the manifest.
    blocks = [f"=== ITEM {i}: {a.kind.upper()} ({_short(a.label)}) ===\n{a.text.strip()}"
              for i, a in enumerate(artefacts, 1) if a.readable]
    return manifest, _fit(manifest, "\n\n".join(blocks).strip())


def _fit(manifest: str, content: str) -> str:
    """Keep manifest + content inside the ceiling the storage column proved it
    can take. file_extractor caps each artefact at MAX_TEXT_CHARS, but intake
    can now assemble SEVERAL — a workbook plus two links plus a deck — and the
    sum is what gets written to the notes column. Exceeding it is not a slow
    degradation: it is DataError 1406, HTTP 500, and the submission lost, which
    is exactly what happened on 13 Aug. Truncation is announced, because silent
    truncation reads to the marker as a thin answer and costs the learner
    marks."""
    room = MAX_TEXT_CHARS - len(manifest)
    if len(content) <= room:
        return content
    return (content[:max(room - 200, 0)]
            + "\n\n[Note: this learner submitted more material than fits in one "
              "review; the text above is the first portion of it.]")


def _short(label: str, limit: int = 120) -> str:
    label = (label or "").strip()
    return label if len(label) <= limit else label[:limit - 1] + "…"


def first_error(artefacts: List[Artefact]) -> str:
    """The message shown to a learner when nothing could be read — name the
    file that failed and why, never a generic 'no answer found'."""
    for a in artefacts:
        if not a.readable and a.note:
            return f"{_short(a.label, 60)}: {a.note}"
    return ""


def has_deliverable(artefacts: List[Artefact]) -> bool:
    """True when the learner demonstrably produced something beyond typing.

    This is what releases the short-answer gate: an image-first or link-first
    task is complete at 12 words of caption, and failing it on length was
    failing the learner for following the instructions.
    """
    return any(a.is_deliverable for a in artefacts)
