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
from dataclasses import dataclass, field
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
# ...but clearing LINK_MIN_WORDS is not the same as carrying the work. A
# published-site shell can server-render its nav, its title, a footer and a
# cookie notice — sixty words, none of them the learner's. Day 04 (Notion
# portfolio pages, published as websites) is exactly that shape, and scoring
# that chrome would be the marker reviewing the platform instead of the
# student. Below this many words the agent OPENS the page in its browser and
# keeps the render only if it comes back richer. A genuinely content-bearing
# fetch is untouched and costs no browser.
LINK_RENDER_PREFER_WORDS = int(os.getenv("LINK_RENDER_PREFER_WORDS", "120"))

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
    # THE WORK ITSELF, when it is something you look at (23 Aug 2026).
    #
    # Everything used to be flattened to text before judging: a poster became
    # OCR'd words, a Lovable site became its headings, a mind map became a
    # list of labels. The marker read a description and scored the
    # description. Carrying the picture alongside the text lets it judge what
    # OCR throws away — layout, finish, whether the placeholder text is still
    # in there.
    image_b64: str = ""
    media_type: str = ""
    # A video yields several frames. The first is the artefact's picture; the
    # rest ride along here so images_for_judge can offer the whole sequence
    # when there is room for it.
    extra_images: list = field(default_factory=list)

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
    art = _finish_file(label, text, why, had_bytes=bool(file_data))
    return _with_picture(art, file_data, file_url, file_name)


def _with_picture(art: Artefact, file_data=None, file_url=None,
                  file_name: str = "") -> Artefact:
    """Attach the image itself when this upload is one, so the marker can LOOK.

    Never raises and never changes the text: a picture the marker cannot be
    shown simply is not attached, and the OCR that was already extracted
    carries the review exactly as before.
    """
    try:
        from app.utils.file_extractor import (IMAGE_EXTS, image_for_judge,
                                              upload_bytes)
        name = file_name or file_url or ""
        ext = ("." + name.rsplit(".", 1)[-1].lower().split("?")[0]) if "." in name else ""
        # Gate on the extension BEFORE reading: a 50 MB PDF must never be
        # re-fetched just to discover it is not a picture.
        from app.services.submission_media import MEDIA_EXTS, frames_for
        if ext not in IMAGE_EXTS and ext not in MEDIA_EXTS:
            return art
        data = upload_bytes(file_data, file_url)
        if not data:
            return art
        if ext in MEDIA_EXTS:
            # A video was already sampled into frames while its audio was
            # transcribed. Carry the first of those, so the marker LOOKS at
            # the learner's video instead of reading a description of it.
            frames = frames_for(data)
            if frames:
                art.image_b64 = frames[0]["image"]
                art.media_type = frames[0]["media_type"]
                art.extra_images = frames[1:]
            return art
        b64, media_type = image_for_judge(data, ext)
        if b64:
            art.image_b64, art.media_type = b64, media_type
    except Exception as e:
        logger.warning("could not attach picture for %s: %s", art.label, e)
    return art


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


def from_links_in(text: str, limit: int = MAX_LINKS,
                  task_text: str = "") -> List[Artefact]:
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
        picture = ""
        body, why = fetch_link(url)
        if not body or _is_preview_only(body) or _is_thin_body(body):
            # The plain fetch saw nothing, only link-preview metadata, or so
            # little that it cannot be the deliverable — the page builds
            # itself in a browser. If the agent's own browser is switched on,
            # open the link the way a visitor would and read what actually
            # renders. Failure falls through to the honest confirmed-but-
            # unread record, exactly as before.
            from app.services import link_renderer
            if link_renderer.enabled():
                # On a build-a-thing day, USE the page: scroll it, press its
                # safe controls, keep its JavaScript errors. On a reading day
                # that spends seconds for nothing, so it stays off.
                rendered_text, render_why, shot, notes = \
                    link_renderer.read_rendered_page(
                        url, walk=link_renderer.task_wants_a_walkthrough(task_text))
                if rendered_text and _render_is_richer(rendered_text, body):
                    body, why = rendered_text, ""
                elif not body:
                    why = render_why or why
                # Kept even when the page's own text carried the review: what
                # a built page SAYS and what it LOOKS LIKE are different
                # questions, and a website day turns on the second one.
                if shot and body:
                    picture = shot
                if notes and body:
                    # Stated as OUR observation, never as the learner's words,
                    # so the marker cannot mistake it for their writing.
                    body = f"{body}\n\n{notes}"
        if body:
            out.append(Artefact(kind="link", label=url, text=body,
                                confirmed=True, image_b64=picture,
                                media_type="image/jpeg" if picture else ""))
        else:
            # The link itself is still evidence the learner published SOMETHING;
            # we simply could not read it from here. Both facts go on the record.
            out.append(Artefact(kind="link", label=url, note=why, confirmed=True))
    return out


def _is_thin_body(body: str) -> bool:
    """Too little text to be the work itself — open it in a browser. Pure."""
    return len((body or "").split()) < LINK_RENDER_PREFER_WORDS


def _render_is_richer(rendered: str, body: str) -> bool:
    """Keep a render only when it beats what the plain fetch already had.

    A render that comes back thinner than the fetch is a failed render
    dressed as a success; the fetch keeps the page. Pure.
    """
    if not (body or "").strip() or _is_preview_only(body):
        return True
    return len(rendered.split()) > len(body.split())


def _is_preview_only(body: str) -> bool:
    """Is this fetch_link result just link-preview metadata, not content?
    That marker is describe_from_metadata's own header — when the renderer is
    available, a real render beats a two-line preview. Pure."""
    return (body or "").lstrip().startswith("[This link is a published page")


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


# Meta tags that describe the PLATFORM, not the learner's work. Verified live
# 21 Aug: every claude.ai/claude.site artifact page serves the identical
# "Title: Claude Artifact" boilerplate with no artifact-specific content —
# counting that as "read" would hand the judge 20 generic words per learner
# and pin the whole cohort at the no-evidence cap (the Day 06 Suno failure,
# one level up). Boilerplate is a CONFIRMED-but-unread deliverable instead,
# which routes the row to the honest ask-for-more path, never to a 2/10.
_PLATFORM_BOILERPLATE_TITLES = {
    "claude.ai": "claude artifact",
    "claude.site": "claude artifact",
}


def _is_platform_boilerplate(url: str, meta: str) -> bool:
    """True when the metadata names the platform, not the learner's work."""
    host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    generic = _PLATFORM_BOILERPLATE_TITLES.get(host)
    if not generic:
        return False
    m = re.search(r"^Title:\s*(.+)$", meta or "", re.M)
    return bool(m) and m.group(1).strip().lower() == generic


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
        if meta and not _is_platform_boilerplate(url, meta):
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


MANIFEST_HEADER = "=== SUBMISSION MANIFEST ==="
# Whitespace-TOLERANT. The literal "\n=== ITEM " failed the moment a stored row
# went through clean_text(), which collapses newline runs to spaces — the
# marker stopped matching, split_manifest returned the whole blob as manifest
# with EMPTY content, and the route then reported "no readable content" for a
# row that plainly had some. Match the text, not the surrounding whitespace.
_ITEM_RE = re.compile(r"===\s*ITEM\s+\d+\s*:", re.IGNORECASE)


def split_manifest(text: str) -> Tuple[str, str]:
    """Separate the provenance manifest from the learner's actual content.

    Needed because the two must be delivered to the marker DIFFERENTLY. The
    manifest is OUR statement about what arrived; the content is untrusted
    learner text. Routes join them for storage, and stored rows are re-read on
    re-review, so the split has to work on text that came back out of the
    database as well as text we just built.

    Returns ("", text) when there is no manifest — a typed-only submission.
    """
    body = text or ""
    if not body.lstrip().startswith(MANIFEST_HEADER):
        return "", body
    match = _ITEM_RE.search(body)
    if not match:
        return body.strip(), ""
    return body[:match.start()].strip(), body[match.start():].strip()


def from_stored_submission(notes: str):
    """Reuse a row whose notes are ALREADY assembled intake output.

    Returns (manifest, content), or None when these notes are raw learner text
    that still needs assembling.

    WHY. Routes store `manifest + content` in `notes`. A re-review that reads
    that row back and ALSO re-extracts the attachment produces:

        new manifest  ("2 items: an IMAGE and TYPED TEXT")
          ITEM 1 IMAGE       <- the OCR, extracted a second time
          ITEM 2 TYPED TEXT  <- the ENTIRE previous assembly, old manifest and
                                old ITEM headers included, presented as words
                                the learner typed

    The marker then reads our own provenance instructions as the submission,
    and the same image twice. Observed live on 14 Aug: re-running assignment 14
    moved student 1126 from 6.8/10 to 1.2/10 on identical input, and every
    other learner down with them.

    Already-assembled notes are complete. Reuse them; extract nothing.
    """
    manifest, content = split_manifest(notes or "")
    if not manifest:
        return None
    return manifest, content


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
        MANIFEST_HEADER + "\n"
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


# ---------------------------------------------------------------------------
# Is there anything here a marker could honestly judge?
#
# Day 06 of "30 Days 30 AI Tools" asks for a song made in Suno. Most learners
# submitted exactly what was asked: a suno.com link and nothing else. The
# regrade path handed that URL to the marker as if it were the learner's prose,
# so there was nothing to quote, the no-evidence cap pinned every criterion at
# 20%, and a cohort that DID the work was told it scored 2/10.
#
# A URL is not an answer and it is not a failure either. It is a deliverable we
# have not opened. Grading it as prose asserts something we did not check —
# which is the fabrication this project forbids, pointed the other way.
# ---------------------------------------------------------------------------

# Below this, the text is a caption or a bare link, not an answer.
MIN_GRADABLE_WORDS = 12


def substantive_words(content: str) -> int:
    """Word count with URLs and bare filenames removed.

    'https://suno.com/song/6f2a-91bb my song' is two words of answer, not six.
    """
    text = re.sub(r"https?://\S+|www\.\S+", " ", content or "")
    text = re.sub(r"\b\S+\.(?:mp3|mp4|wav|m4a|png|jpe?g|pdf|docx?|pptx?|xlsx?)\b",
                  " ", text, flags=re.I)
    text = _ITEM_RE.sub(" ", text)
    return len([w for w in text.split() if any(ch.isalnum() for ch in w)])


# Tasks whose DELIVERABLE is the published thing itself: a Notion site, a
# Claude artifact, a Gamma deck, a Lovable app. On these, a typed paragraph
# is a description OF the work, not the work — so a link that will not open
# means the submission was never seen, however much the learner typed.
#
# Ranjana's ruling, 22 Aug, on 21 Day-04 rows that were marked 0.0-4.7 while
# their own feedback said the page could not be read: "No grade, ask them to
# publish." A mark on work nobody could open measures our reach, not their
# effort — and unlike a low mark, a withheld one can still become a real
# score the same evening.
_PUBLISHED_DELIVERABLE = re.compile(
    # "share your notebook link", "share the published link", "share it and
    # send the link" — the words between the verb and the noun vary, and Day
    # 05 (22 Aug) slipped through a pattern that demanded them adjacent:
    # 100 students were filed as "we will fix it" when their notebook was
    # simply private. Allow up to a short clause between share and link.
    r"\bshare[^.\n]{0,40}\blinks?\b|\bsend[^.\n]{0,30}\blinks?\b|"
    r"publish(?:ed|ing)?\b|\bpublic link\b|\bas a website\b|"
    r"\bartifacts?\b|\bartefacts?\b|\bnotion\b|\bnotebooklm\b|"
    r"\bnotebook\b|\bgamma\b|\blovable\b|\bdeploy(?:ed)?\b|"
    r"\blive (?:page|site|link|url)\b|"
    # THE TOOL NAME IS THE TELL, when the task text never mentions links.
    #
    # Day 07 (assignment 24, 23 Aug) reads only "Create a Dashboard from a
    # data set using Gemini Canvas". No "share", no "publish", no "link" — so
    # this pattern returned False, both publish rules stayed dead, and three
    # learners were marked 0.0/10 on links nobody could use. The task never
    # asked for a link; the TOOL only hands you one.
    #
    # So the course's own tools are named here. Each of these produces a
    # shareable artifact as its normal output, which is what the learner will
    # paste. Kept to whole words and multi-word names so ordinary prose ("the
    # canvas of Indian fintech") cannot trip it.
    r"\bgemini canvas\b|\bcanva\b|\bsuno\b|\bmidjourney\b|"
    r"\bheygen\b|\brunway\b|\bdescript\b|\bjulius\b|"
    r"\bpower bi\b|\bcustom gpts?\b|\bclaude artifacts?\b|"
    r"\bchatgpt agent\b|\bmanus\b|\bn8n\b|\bzapier\b|\blindy\b",
    re.I)


def link_is_the_deliverable(task_text: str) -> bool:
    """Does this task ask for a published page or artifact? Pure.

    Deliberately narrow: on an essay day a pasted link is a citation, and
    withholding a grade there would punish a learner for a footnote.
    """
    return bool(_PUBLISHED_DELIVERABLE.search(task_text or ""))


def only_unreadable_links(artefacts) -> bool:
    """The learner submitted link(s), none opened, and no file opened either.

    True means nothing of the actual deliverable reached the marker. Typed
    text is not consulted here on purpose — whether it rescues the row is
    the caller's policy decision, not this function's. Pure.
    """
    links = [a for a in artefacts if a.kind == "link"]
    if not links or any(a.readable for a in links):
        return False
    return not any(a.readable for a in artefacts if a.kind != "link")


def link_deliverable_unseen(artefacts) -> bool:
    """The published page was the work, and we never saw it. Pure.

    only_unreadable_links() lets ANY readable artefact rescue the row,
    including the learner's own typed caption. On a publish-this task that is
    wrong, and Day 07 showed why: student 220 pasted a Gemini link we could
    not open and typed 6,622 characters describing their dashboard. The link
    was refused, the caption remained, the rule did not fire — and they were
    marked 0.0/10 on a description of work nobody had seen.

    A CAPTION IS NOT THE DELIVERABLE. A screenshot is, a PDF export is, an
    uploaded file is — those are the work in another form, and they still
    rescue the row. Prose about the work is not the work.

    True when: links were submitted, none opened, and nothing the learner
    PRODUCED was readable either.
    """
    links = [a for a in artefacts if a.kind == "link"]
    if not links or any(a.readable for a in links):
        return False
    # Everything except typed prose counts as the work in another form.
    return not any(a.readable for a in artefacts
                   if a.kind not in ("link", "typed text"))


def deliverable_is_only_links(artefacts) -> bool:
    """Everything the learner PRODUCED is a link. Pure.

    Typed prose does not count as a produced deliverable — the same reasoning
    as link_deliverable_unseen(). True when at least one link was submitted
    and no file, image or document came with it.
    """
    produced = [a for a in artefacts if a.kind not in ("typed text",)]
    return bool(produced) and all(a.kind == "link" for a in produced)


# How many pictures one review may carry. Each is real money and real
# latency; four covers a page, two screenshots and an upload, which is more
# than almost any submission holds.
MAX_JUDGE_IMAGES = int(os.getenv("MAX_JUDGE_IMAGES", "4"))


def images_for_judge(artefacts, limit: int = MAX_JUDGE_IMAGES) -> list:
    """The learner's work as pictures, ready for the marker. Pure.

    Ordered as the learner's own uploads first, then anything we rendered on
    their behalf: their file is the deliverable, our screenshot is a proxy
    for it, and when the cap bites the proxy is what should fall off.
    """
    own = [a for a in artefacts if a.image_b64 and a.kind != "link"]
    ours = [a for a in artefacts if a.image_b64 and a.kind == "link"]
    out = []
    for a in own + ours:
        out.append({"image": a.image_b64,
                    "media_type": a.media_type or "image/png"})
        out.extend(a.extra_images or [])
    return out[:max(0, limit)]


def unreadable_deliverable(manifest: str) -> bool:
    """Does the manifest record an artefact that exists but could not be read?"""
    return "could not be read" in (manifest or "")


def records_failed_read(manifest: str) -> bool:
    """Does this stored manifest carry a FAILED read — an item that could not
    be read or could not be retrieved when the row was first assembled?

    Why it matters: the regrade path reuses stored assemblies whole (see
    from_stored_submission — correct, that stopped the 1126 double-manifest
    bug). But a stored assembly that RECORDS a failure is a snapshot of a bad
    day: the 21 Aug probe found ~110 'unreadable' files that existed and
    served bytes on demand — transient fetch errors and the media-type bug,
    frozen into notes and replayed by every regrade since. A failure on
    record is a reason to read the source again, not a result to reuse.
    """
    m = manifest or ""
    return "could not be read" in m or "could not be retrieved" in m


_TYPED_BLOCK_RE = re.compile(
    r"===\s*ITEM\s+\d+\s*:\s*TYPED TEXT[^\n]*===\s*\n(.*?)(?=\n===\s*ITEM\s+\d+\s*:|\Z)",
    re.IGNORECASE | re.DOTALL)


def typed_text_from(assembled_content: str) -> str:
    """Recover the learner's own typing from an assembled content body.

    Used when a stored assembly is being DISCARDED for re-extraction: feeding
    the whole assembly back through from_typed would nest manifest inside
    manifest — the exact defect that took student 1126 from 6.8 to 1.2 — so
    only the TYPED TEXT blocks come forward.
    """
    return "\n\n".join(
        m.strip() for m in _TYPED_BLOCK_RE.findall(assembled_content or "")
    ).strip()


def is_unassessable(manifest: str, content: str) -> bool:
    """True when the only thing submitted is a deliverable we could not read.

    Deliberately narrow. A thin TYPED answer is assessable and scores what it
    earns — that judgement is the marker's job. This catches only the case
    where the substance sits inside a file or behind a link that never opened,
    so any score would be about our reach, not the learner's work.
    """
    if substantive_words(content) >= MIN_GRADABLE_WORDS:
        return False
    return unreadable_deliverable(manifest) or bool(find_urls(content))
