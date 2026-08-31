# app/utils/file_extractor.py
# ---------------------------------------------------------------------------
# Upgraded extractor for AiRev / Agent@4
#
# Supported student-solution formats:
#   1. Direct typing (handled in route, not here)
#   2. PDF — text-based       -> pypdf
#   3. PDF — scanned/photo    -> auto-rasterize -> Claude vision OCR
#   4. DOCX                   -> python-docx
#   5. DOC  (legacy)          -> friendly "save as .docx" message
#   6. TXT / MD               -> UTF-8 decode
#   7. RTF                    -> control-word strip
#   8. Images (handwritten/photographed notes):
#         JPG, JPEG, PNG, WEBP, HEIC, HEIF -> Claude vision OCR
#
# Cost guards:
#   - MAX_OCR_PAGES env (default 5) caps PDF rasterization
#   - MAX_FILE_BYTES env (default 10 MB) rejects oversized uploads
#   - HEIC support is conditional on pillow-heif being installed
# ---------------------------------------------------------------------------

import io
import os
import re
import base64
import logging
from typing import Tuple, List

import httpx

logger = logging.getLogger(__name__)

# ---------- config ---------------------------------------------------------

MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(10 * 1024 * 1024)))   # 10 MB
# Audio and video are legitimately bigger than documents. Applied only to the
# extensions in MEDIA_EXTS, after the type is known.
MAX_MEDIA_BYTES = int(os.getenv("MAX_MEDIA_BYTES", str(80 * 1024 * 1024)))  # 80 MB
NO_TEXT_IN_IMAGE = "image contains no readable text"
MAX_OCR_PAGES = int(os.getenv("MAX_OCR_PAGES", "5"))                       # OCR cap
# Vision-capable model for OCR. Defaults to the same Haiku every other
# Claude call uses (single source of truth in ai_service) — a Sonnet
# default here billed silently for weeks before anyone noticed.
from app.services.ai_service import HAIKU as _HAIKU
OCR_MODEL = os.getenv("OCR_MODEL", _HAIKU)

# What the vision API will actually accept, and what it does with the rest.
#
# The limit is 10 MB of BASE64, not of file bytes — base64 inflates by a third,
# so a 7.6 MB screenshot is already over. Nothing here checked either number,
# and the failure arrived as a bare BadRequestError with the reason discarded.
#
# The long edge matters more than the size. The API downscales every image to
# 1568 px on the standard tier before the model ever sees it, so a 4000 px phone
# screenshot is bytes we pay to upload and tokens we pay to encode, for exactly
# zero extra readability. Fitting first is cheaper AND more reliable.
VISION_LONG_EDGE = int(os.getenv("VISION_LONG_EDGE", "1568"))
VISION_B64_MAX   = int(os.getenv("VISION_B64_MAX", str(9 * 1024 * 1024)))  # under 10 MB


def _b64_len(n: int) -> int:
    """Base64 length of n bytes, without encoding them. Pure."""
    return ((n + 2) // 3) * 4


def _fit_for_vision(data: bytes, media_type: str) -> Tuple[bytes, str]:
    """Make one image safe to send to the vision API.

    Returns the original bytes untouched when they are already within both
    limits — the common case, and no student should pay a re-encode for it.

    Never raises. An image that cannot be re-encoded is sent as it is: a
    BadRequestError we can read beats a silent drop, and the caller now reports
    the API's own reason.
    """
    try:
        from PIL import Image
    except ImportError:
        return data, media_type

    try:
        with Image.open(io.BytesIO(data)) as probe:
            wide = max(probe.size)
    except Exception:
        return data, media_type          # not decodable here; let the API judge

    if wide <= VISION_LONG_EDGE and _b64_len(len(data)) <= VISION_B64_MAX:
        return data, media_type

    try:
        img = Image.open(io.BytesIO(data))
        # Animated formats: the API reads frame one anyway.
        img = img.convert("RGB")
        w, h = img.size
        if max(w, h) > VISION_LONG_EDGE:
            scale = VISION_LONG_EDGE / float(max(w, h))
            img = img.resize((max(1, round(w * scale)), max(1, round(h * scale))),
                             Image.LANCZOS)
        # PNG first: screenshots and diagrams are what this cohort submits, and
        # lossless keeps small text legible. JPEG only if PNG is still too big.
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        out = buf.getvalue()
        if _b64_len(len(out)) <= VISION_B64_MAX:
            return out, "image/png"
        for quality in (85, 70, 55, 40):
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=quality, optimize=True)
            out = buf.getvalue()
            if _b64_len(len(out)) <= VISION_B64_MAX:
                return out, "image/jpeg"
        return out, "image/jpeg"
    except Exception as e:
        logger.warning("could not fit image for vision (%s) - sending as is",
                       type(e).__name__)
        return data, media_type


# What learners actually send. The cohort submits AI-generated images from a
# different tool every day and screenshots from whatever device is to hand, so
# the list is deliberately wider than "what the vision API accepts natively":
# anything Pillow can open is converted to PNG on the way in (see
# _to_readable_image), which is the same path HEIC has always used.
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif",
              ".gif", ".bmp", ".tif", ".tiff", ".avif"}

# Sent to the vision API untouched. Everything else in IMAGE_EXTS is converted.
NATIVE_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
TEXT_EXTS  = {".txt", ".md", ".rst", ".log", ".text"}

# OpenDocument — LibreOffice and OpenOffice are what a lot of students have,
# and .odt/.ods/.odp were refused outright as "could not be read as text".
# They are ZIP containers holding content.xml, so they need no new dependency.
ODF_EXTS = {".odt", ".ods", ".odp", ".odg", ".otp", ".ott"}

# Legacy binary Office. Genuinely unreadable without a converter, so say so in
# words a student can act on rather than "this file type could not be read".
LEGACY_OFFICE = {
    ".doc":  "Word",
    ".ppt":  "PowerPoint",
    ".xls":  "Excel",
}
# Audio/video. AiRev reviews WRITTEN work; there is nothing to extract from a
# video. Named explicitly because the unknown-extension fallback used to decode
# them as UTF-8 with errors="ignore": a WhatsApp .mp4 became "272004 words" of
# binary garbage, which then exceeded the notes column and 500'd the submit
# (live, 13 Aug). Reject them by name, with a message the student can act on.
# Audio/video. Routed to app/services/submission_media (ffmpeg -> Whisper for
# speech, sampled frames -> vision for what is on screen). Named explicitly so
# the unknown-extension fallback never decodes them as UTF-8 — that is how a
# WhatsApp .mp4 became "272004 words" of binary garbage and 500'd a live submit
# on 13 Aug.
MEDIA_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp", ".wmv",
              ".flv", ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma",
              ".flac"}
CODE_EXTS  = {".py", ".sql", ".js", ".ts", ".jsx", ".tsx", ".java", ".c",
              ".cpp", ".r", ".json", ".css", ".sh", ".yaml", ".yml"}

# A web page the learner BUILT — Day 02's deliverable (Claude artifacts are
# exported as .html). Read as a page first (what a visitor would see), source
# second (what the learner wrote) — never refused as a failed download.
# .htm was previously in NO set at all, so an .htm upload fell through to the
# unknown-ext sniff, hit the login-page guard, and was refused as NOT_A_FILE.
HTML_EXTS = {".html", ".htm", ".xhtml"}
# A "Web Page, Complete" save from Internet Explorer/Edge: one MIME
# multipart file holding the page and every image it referenced.
# Student 357 submitted one on Day 05 and it read as nothing at all,
# because .mht fell through to the byte sniffer and looked like mail.
MHTML_EXTS = {".mht", ".mhtml"}
SHEET_MAX_ROWS  = 300     # per sheet — enough for any coursework workbook
SHEET_MAX_COLS  = 40
SHEET_MAX_CHARS = 60000   # whole-workbook render cap; truncation is stated
ZIP_MAX_FILES   = 25
# Images inside a zip go through vision, which costs money — so the
# number read is bounded and whatever is skipped is NAMED in the output.
# Eight covers a slide deck exported as PNGs, which is what this is for.
ZIP_MAX_IMAGES  = int(os.getenv("ZIP_MAX_IMAGES", "8"))
ZIP_MAX_TOTAL   = 60 * 1024 * 1024   # unpacked-bytes bomb guard

# Hard ceiling on returned text, enforced ONCE in extract_text_from_bytes().
# A 10 MB file could otherwise return ~10M characters — past the notes column
# (DataError 1406, HTTP 500, submission lost) and an enormous token bill if it
# reached the model. The sheet/zip/code paths have their own tighter bounds;
# PDF, DOCX, RTF and image OCR have none, which is why this must live at the
# shared exit rather than in whichever branch was being debugged that day.
MAX_TEXT_CHARS = int(os.getenv("MAX_TEXT_CHARS", "50000"))


def _cap(text: str) -> str:
    """Trim to MAX_TEXT_CHARS and say so — silent truncation reads as a short
    answer to the reviewer, which would cost the learner marks."""
    if len(text) <= MAX_TEXT_CHARS:
        return text
    return (text[:MAX_TEXT_CHARS] +
            f"\n\n[Note: submission truncated at {MAX_TEXT_CHARS} characters "
            f"for review; the original file was longer.]")


def _looks_like_text(data: bytes, sample: int = 4096) -> bool:
    """True when these bytes are plausibly human-readable text.

    Guards the last-resort decode. Binary containers (video, audio, archives)
    survive errors="ignore" as long strings of control characters that look
    like content to every downstream word count.
    """
    chunk = data[:sample]
    if not chunk:
        return False
    if b"\x00" in chunk:                       # NUL bytes never appear in text
        return False
    printable = sum(1 for b in chunk if 9 <= b <= 13 or 32 <= b <= 126 or b >= 160)
    return printable / len(chunk) >= 0.85


# ---------------------------------------------------------------------------
# A WEB PAGE IS NOT A SUBMISSION.
#
# Live, 16 Aug 2026. Dozens of Day 06 rows came back "1917 words", "1921
# words", "1925 words" — near-identical counts across unrelated learners — and
# every one scored 0.0/10. The log said why on the line above each: 
#
#     invalid pdf header: b'<!DOC'
#
# The download returned HTTP 200 carrying an HTML page: a Cloudinary
# not-found, a login wall, a link-shortener interstitial, or the JS shell of a
# client-rendered site. PDF parsing failed, DOCX failed, it was not an image —
# and the last-resort branch decoded it as text, because markup IS text. So
# ~1900 words of boilerplate went to the marker as the learner's coursework and
# was graded as such. The clustered word counts are the same page each time,
# varying only by the URL embedded in it.
#
# Grading that page is not strictness, it is a false statement about a student.
# Refuse it, and let the unassessable path report the row instead of scoring it.
# ---------------------------------------------------------------------------

_HTML_SIGNATURES = (b"<!doctype html", b"<html", b"<head", b"<body",
                    b"<script", b"<meta ")


def looks_like_web_page(data: bytes, sample: int = 2048) -> bool:
    """True when these bytes are a web page rather than a submitted document.

    Deliberately checks only the HEAD of the payload. A legitimate .html
    upload hits the TEXT_EXTS branch by extension long before the sniff path,
    so this cannot swallow work a learner meant to submit as HTML.
    """
    head = (data or b"")[:sample].lstrip().lower()
    return any(sig in head for sig in _HTML_SIGNATURES)


NOT_A_FILE = ("the link returned a web page, not the file itself — the page "
              "may need a login, or the file may have been removed")


# ---------- public entry ---------------------------------------------------

# LMS rows often store uploads as relative paths (e.g. /uploads/x.pdf) that
# only resolve against the LMS backend. Address checking and path allowlisting
# live in app/utils/url_guard, imported by EVERY caller that fetches a
# learner-supplied URL — see that module for why it is not defined here.
from app.utils.url_guard import (          # noqa: E402  (kept next to its use)
    check_public_url,
    resolve_lms_url as _guarded_resolve,
)


def resolve_lms_url(file_url: str) -> str:
    """Back-compat shim: returns the safe URL, or "" when it is refused.

    Kept because other modules import this name. New code should call
    url_guard.resolve_lms_url() directly and read the refusal reason.
    """
    url, _why = _guarded_resolve(file_url)
    return url


def extract_text_from_url(file_url: str, file_name: str = "") -> Tuple[str, str]:
    """
    Returns (extracted_text, reason).
    On success: ('extracted text...', '')
    On failure: ('', 'short reason for log')

    THE GUARD IS HERE, before any network call. `file_url` arrives straight
    from a learner's request body, so this function is a server-side request
    forgery primitive unless every URL is checked. It used to have no check at
    all: fileUrl="http://169.254.169.254/latest/meta-data/" with
    fileName="x.txt" fetched the cloud metadata endpoint and returned the body
    to the student as their own submission text.
    """
    if not file_url:
        return "", "no file_url provided"

    safe_url, why = _guarded_resolve(file_url)
    if not safe_url:
        logger.warning("refused fetch of %r: %s", file_url[:120], why)
        return "", why

    # The ceiling follows the FILE, not the default: media is allowed
    # its own, larger limit all the way through the download.
    data, why = _download_file(safe_url,
                               size_ceiling_for(file_name or file_url))
    if data is None:
        return "", why

    return extract_text_from_bytes(data, file_name or file_url)


def extract_text_from_base64(b64: str, file_name: str = "") -> Tuple[str, str]:
    """Decode a base64 payload (optionally a data: URL) and extract its text.

    This is the storage-free upload path: the browser sends the file's bytes
    inline, so the agent never depends on Cloudinary or any external store.
    Returns (extracted_text, reason) — same contract as extract_text_from_url.
    """
    if not b64:
        return "", "no file data provided"
    payload = b64.strip()
    # Tolerate data-URL prefixes ("data:application/pdf;base64,....")
    if payload.startswith("data:") and "," in payload:
        payload = payload.split(",", 1)[1]
    try:
        data = base64.b64decode(payload, validate=False)
    except Exception as e:
        return "", f"base64 decode failed: {type(e).__name__}"
    if not data:
        return "", "file data was empty after decode"
    return extract_text_from_bytes(data, file_name)


def extract_upload(file_data: str = None, file_url: str = None,
                   file_name: str = "") -> Tuple[str, str]:
    """Single entry point every submit route uses for an attached file.

    Prefers inline bytes (base64, storage-free) and falls back to a URL when
    one is supplied — so a route works whether the browser sent the file's
    bytes or a Cloudinary/LMS link. Returns (extracted_text, reason).
    """
    if file_data:
        return extract_text_from_base64(file_data, file_name)
    if file_url:
        return extract_text_from_url(file_url, file_name)
    return "", "no file provided"


def upload_bytes(file_data: str = None, file_url: str = None) -> bytes:
    """The raw bytes of an attachment, however it arrived. Never raises.

    Used when something beyond the text is wanted — currently the picture the
    marker looks at. Callers gate on the file EXTENSION before calling, so a
    50 MB PDF is never re-fetched just to discover it is not an image.
    """
    try:
        if file_data:
            payload = file_data.strip()
            if payload.startswith("data:") and "," in payload:
                payload = payload.split(",", 1)[1]
            return base64.b64decode(payload, validate=False)
        if file_url:
            data, _why = _download_file(resolve_lms_url(file_url))
            return data or b""
    except Exception as e:
        logger.warning("upload_bytes failed: %s", e)
    return b""


def extract_text_from_bytes(data: bytes, file_name: str = "") -> Tuple[str, str]:
    """Extract reviewable text from already-in-memory file bytes.

    Shared by both the URL path (download then extract) and the base64 path
    (decode then extract), so every supported format behaves identically no
    matter how the bytes arrived. Returns (extracted_text, reason).

    THE CEILING LIVES HERE, at the one exit every caller uses. It was first
    applied per-branch, which left _extract_pdf, _extract_docx, _extract_rtf
    and _extract_image uncapped — a 10 MB text-based PDF or Word file would
    still have produced millions of characters and reproduced the exact
    DataError 1406 / HTTP 500 that lost a submission on 13 Aug. A guard that
    only covers the branch you happened to debug is not a guard. Capping the
    single exit also means a format added later is bounded by default.
    """
    text, reason = _extract_dispatch(data, file_name)
    return _cap(text), reason


# ---------------------------------------------------------------------------
# THE FORMAT REGISTRY (Phase 4, 23 Aug 2026).
#
# Adding a format used to mean a fresh investigation: find the ladder below,
# work out where in it the new branch had to sit, and hope nothing above it
# claimed the file first. That is how .mht reached a learner as "not a file",
# and how audio spent weeks refused outright — and it is why the agent was
# permanently one tool behind a syllabus that adds one every day.
#
# One table instead. A new format is ONE ENTRY and ONE TEST.
#
# The ladder below still runs for the genuinely conditional cases — a PDF that
# may or may not need OCR, a legacy Office file that gets an explanation
# rather than a reader, the sniffing of a file with no usable extension.
# Those are decisions, not lookups, and flattening them into the table would
# hide them.
#
# supported_formats() exists so the agent can TELL a learner what it reads.
# That sentence was hand-written in three places and all three were wrong the
# day audio landed.
# ---------------------------------------------------------------------------


def _ext_of(name: str) -> str:
    lowered = (name or "").lower().split("?")[0]
    return "." + lowered.rsplit(".", 1)[-1] if "." in lowered else ""


def _read_text(data: bytes, name: str) -> Tuple[str, str]:
    body = _clean(data.decode("utf-8", errors="ignore"))
    return (body, "") if body else ("", "the file was empty")


def _read_code(data: bytes, name: str) -> Tuple[str, str]:
    body = _clean(data.decode("utf-8", errors="ignore"))[:SHEET_MAX_CHARS]
    if not body:
        return "", "code file was empty"
    return f"[Code file: {name.rsplit('/', 1)[-1]}]\n{body}", ""


def _read_media(data: bytes, name: str) -> Tuple[str, str]:
    from app.services.submission_media import transcribe_and_describe
    return transcribe_and_describe(data, name)


READERS: dict = {}          # extension -> (reader, what a person calls it)


def _register(exts, reader, label: str) -> None:
    for ext in exts:
        READERS[ext] = (reader, label)


_register(TEXT_EXTS, _read_text, "text")
_register({".rtf"}, lambda d, n: _extract_rtf(d), "rich text")
_register({".docx"}, lambda d, n: _extract_docx(d), "Word document")
_register(ODF_EXTS, lambda d, n: _extract_odf(d, _ext_of(n)), "OpenDocument")
_register({".xlsx", ".xlsm", ".xltx"}, lambda d, n: _extract_xlsx(d), "Excel workbook")
_register({".csv", ".tsv"}, lambda d, n: _extract_csv(d, _ext_of(n)), "spreadsheet")
_register({".pptx", ".potx"}, lambda d, n: _extract_pptx(d), "slide deck")
_register({".zip"}, lambda d, n: _extract_zip(d), "zip archive")
_register({".ipynb"}, lambda d, n: _extract_ipynb(d), "notebook")
_register(HTML_EXTS, lambda d, n: _extract_html(d), "web page")
_register(MHTML_EXTS, lambda d, n: _extract_mhtml(d), "saved web page")
_register(IMAGE_EXTS, lambda d, n: _extract_image(d, _ext_of(n)), "image")
_register(CODE_EXTS, _read_code, "code")
_register(MEDIA_EXTS, _read_media, "audio or video")


def reader_for(file_name: str):
    """The reader registered for this file, or None. Pure."""
    entry = READERS.get(_ext_of(file_name))
    return entry[0] if entry else None


def supported_formats() -> list:
    """Every format the agent can read, named as a person would. Pure."""
    return sorted({label for _reader, label in READERS.values()})


def _extract_dispatch(data: bytes, file_name: str = "") -> Tuple[str, str]:
    """Format detection and per-format extraction. Callers use
    extract_text_from_bytes(), which applies the length ceiling."""
    if not data:
        return "", "no file bytes provided"

    name = (file_name or "").lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""

    # The size ceiling is PER KIND, and the extension has to be known first.
    # A single 10 MB limit applied before dispatch would have refused the very
    # files this pipeline exists to read: a 10-minute NotebookLM Audio Overview
    # is 10-15 MB, a one-minute Runway clip larger still. Documents stay at the
    # tighter limit, because a 60 MB "PDF" is not a coursework document.
    ceiling = size_ceiling_for(file_name)
    if len(data) > ceiling:
        return "", (f"file too large ({len(data) // (1024 * 1024)} MB > "
                    f"{ceiling // (1024 * 1024)} MB)")

    # A document extension proves nothing about what the server actually sent.
    # These rows arrived as ".pdf" and were HTML; without this the PDF branch
    # fails, OCR fails, and some paths still fall through to a text decode.
    if ext not in TEXT_EXTS and ext not in HTML_EXTS and looks_like_web_page(data):
        return "", NOT_A_FILE

    try:
        # PDFs ------------------------------------------------------------
        if ext == ".pdf":
            text, why = _extract_pdf(data)
            if not pdf_needs_ocr(text, _pdf_has_images(data)):
                return text, ""
            logger.info("PDF has pictures and %d words of text -> OCR as well",
                        len((text or "").split()))
            ocr, ocr_why = _extract_scanned_pdf(data)
            if ocr and text:
                # Both, labelled. The marker needs to know which words the
                # learner typed and which came off the picture.
                return (f"{text}\n\n=== PICTURES IN THIS PDF ===\n{ocr}"), ""
            if ocr:
                return ocr, ""
            return (text, "") if text else ("", ocr_why or why)

        # THE REGISTRY. Everything that is a straight extension -> reader
        # lookup lives in READERS, so a new format is one entry and one test
        # rather than a new rung on this ladder. PDF, legacy Office and the
        # no-extension sniff stay below because they are decisions.
        entry = READERS.get(ext)
        if entry:
            return entry[0](data, file_name)

        # Word ------------------------------------------------------------
        if ext == ".docx":
            return _extract_docx(data)
        if ext in LEGACY_OFFICE:
            app = LEGACY_OFFICE[ext]
            modern = {"Word": ".docx", "PowerPoint": ".pptx", "Excel": ".xlsx"}[app]
            return "", (
                f"The old {ext} format can't be read. Open the file in {app} "
                f"(or Google Docs), choose 'Save As' or 'Download as', pick "
                f"{modern}, and upload that instead."
            )

        # OpenDocument -------------------------------------------------------
        if ext in ODF_EXTS:
            return _extract_odf(data, ext)

        # Audio / video ----------------------------------------------------
        # NOT refused any more. The programme teaches tools whose output IS
        # sound and moving pictures — NotebookLM Audio Overviews, Suno songs,
        # ElevenLabs speech, Runway and HeyGen video — and refusing the medium
        # marked those learners as having submitted nothing.
        #
        # The original guard existed because a WhatsApp .mp4 decoded as 272,000
        # words of binary garbage and 500'd a submit. That danger is real, and
        # the answer is to stop decoding binary AS TEXT, not to reject the
        # format: media now goes to the transcriber, and the last-resort UTF-8
        # decode below is still gated by _looks_like_text().
        if ext in MEDIA_EXTS:
            from app.services.submission_media import transcribe_and_describe
            return transcribe_and_describe(data, file_name)

        # Plain text ------------------------------------------------------
        if ext in TEXT_EXTS:
            return _clean(data.decode("utf-8", errors="ignore")), ""
        if ext == ".rtf":
            return _extract_rtf(data)

        # Spreadsheets (CASA-class assignments demand Excel workbooks) -----
        if ext in {".xlsx", ".xlsm", ".xltx"}:
            return _extract_xlsx(data)
        if ext in {".csv", ".tsv"}:
            return _extract_csv(data, ext)

        # Presentations ----------------------------------------------------
        if ext in {".pptx", ".potx"}:
            return _extract_pptx(data)

        # Archives (capstone UI promises ZIP — honor it) -------------------
        if ext == ".zip":
            return _extract_zip(data)

        # Code / notebooks (capstones: "build a decisioning engine") -------
        if ext == ".ipynb":
            return _extract_ipynb(data)
        if ext in MHTML_EXTS:
            return _extract_mhtml(data)
        if ext in HTML_EXTS:
            return _extract_html(data)
        if ext in CODE_EXTS:
            body = _clean(data.decode("utf-8", errors="ignore"))[:SHEET_MAX_CHARS]
            return (f"[Code file: {name.rsplit('/', 1)[-1]}]\n{body}", "") if body \
                else ("", "code file was empty")

        # Images (handwritten notes) -------------------------------------
        if ext in IMAGE_EXTS:
            return _extract_image(data, ext)

        # Unknown ext -> sniff. Try PDF, DOCX, then image, then bytes-as-text.
        # HTML first: it is text, so the last-resort decode below would happily
        # hand a login page to the marker as the learner's essay.
        # A saved page checked BEFORE looks_like_web_page: an .mht carries
        # HTML inside it, so the web-page check would reject the whole
        # archive as "not a file" — which is what happened to student 357.
        if looks_like_mhtml(data):
            return _extract_mhtml(data)
        if looks_like_web_page(data):
            return "", NOT_A_FILE
        for fn in (_extract_pdf, _extract_docx):
            text, _ = fn(data)
            if text:
                return text, ""
        if _looks_like_image(data):
            return _extract_image(data, ".png")
        if not _looks_like_text(data):
            return "", (
                "this file type could not be read as text. Upload your work as "
                "PDF, Word, Excel, an image, or type it into the answer box."
            )
        return _clean(data.decode("utf-8", errors="ignore")), ""

    except Exception as e:
        logger.exception("extraction crashed")
        return "", f"extraction failed: {type(e).__name__}"


# ---------- download (kept compatible with original) -----------------------

_MAX_REDIRECT_HOPS = 4


class _Fetched:
    """Just enough of a response for the caller: status, headers, capped body."""
    __slots__ = ("status_code", "headers", "content")

    def __init__(self, status_code, headers, content):
        self.status_code, self.headers, self.content = status_code, headers, content


def _get_following_redirects(client, url: str, ceiling: int):
    """Stream a GET, walking redirects by hand so every hop is address-checked
    AND the body is bounded while it arrives.

    THE `ceiling` PARAMETER WAS MISSING (found 24 Aug 2026, by file_check).
    The per-file ceiling was added to the CALLER and used throughout this
    BODY, but never added to this signature. So every call raised

        _get_following_redirects() takes 2 positional arguments but 3 were given

    which _download_file caught as a generic failure, retried four times with
    backoff, and reported as "download failed". EVERY file fetched by URL —
    every screenshot, PDF, deck and recording a learner uploaded — has been
    failing since, silently, while burning nine seconds each in retries.

    On Day 07 that was roughly fifty students told their work could not be
    read. It was read: it was never fetched.

    REQUIRED, not defaulted. A default would have hidden this same mismatch
    for another week, and it would let a caller silently cap an Audio Overview
    at the document limit — the bug size_ceiling_for() was written to end.
    Every caller states the ceiling it means.

    client.stream() — NOT client.get(). A plain get() buffers the entire body
    into memory before returning, so any size check afterwards is decorative:
    a hostile server answering with 10 GB kills the container before the check
    runs. Streaming lets us abandon the transfer the moment it exceeds the
    ceiling.

    Returns (_Fetched, "") or (None, reason). A refused hop is terminal — it is
    an attack signal, not a transient failure — so the caller must not retry.
    """
    current = url
    for _ in range(_MAX_REDIRECT_HOPS):
        ok, why = check_public_url(current)
        if not ok:
            logger.warning("refused fetch/redirect to %r: %s", current[:120], why)
            return None, f"download refused: {why}"

        with client.stream("GET", current) as r:
            if r.status_code in (301, 302, 303, 307, 308):
                nxt = r.headers.get("location")
                if not nxt:
                    return _Fetched(r.status_code, r.headers, b""), ""
                current = str(httpx.URL(current).join(nxt))
                continue

            declared = r.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > ceiling:
                return None, (f"file too large ({int(declared) // 1024} KB > "
                              f"{ceiling // 1024} KB)")

            buf = bytearray()
            for chunk in r.iter_bytes():
                buf.extend(chunk)
                if len(buf) > ceiling:
                    # Content-Length can lie; this is the check that holds.
                    return None, f"file exceeds {ceiling // 1024} KB"
            return _Fetched(r.status_code, r.headers, bytes(buf)), ""

    return None, "download redirected too many times"


def size_ceiling_for(file_name: str) -> int:
    """The download ceiling for THIS file. Pure.

    An Audio Overview or Video Overview is legitimately tens of megabytes,
    and MAX_MEDIA_BYTES exists to allow that — but only the EXTRACTOR ever
    consulted it. The downloader capped every file at MAX_FILE_BYTES, so
    the bytes never arrived and the media ceiling was decorative.

    Day 05 (22 Aug): every .m4a and .mp4 came back unreadable. Those are
    NotebookLM's Audio and Video Overviews — the assignment's own
    deliverable. The students who did the task best were the ones we could
    not read.
    """
    name = (file_name or "").split("?")[0].strip().lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return MAX_MEDIA_BYTES if ext in MEDIA_EXTS else MAX_FILE_BYTES


def _download_file(file_url: str, ceiling: int = MAX_FILE_BYTES) -> Tuple[bytes, str]:
    """Returns (bytes, reason). bytes is None on failure.

    Retries transient failures with backoff. Cloudinary serves PDFs as
    image-type (available instantly) but Word/Excel/other as RAW-type, and a
    freshly-uploaded RAW file takes a few seconds to propagate to the CDN — so
    the very first fetch can 404. Without this retry, the first review of a
    non-PDF upload failed and only the second attempt (seconds later) worked.
    """
    import time as _time
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key    = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")

    # 404/423/425/429/5xx are "not ready yet / transient" → wait and retry.
    TRANSIENT = {404, 408, 423, 425, 429, 500, 502, 503, 504}
    last = "download failed"
    # follow_redirects=False, deliberately. It was True, which meant a URL that
    # passed the address check could 302 straight to 169.254.169.254 — the
    # standard bypass of any check-once guard. Redirects are walked by hand
    # below and EVERY hop is re-checked.
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        for attempt in range(4):          # waits ~0 + 1.5 + 3 + 4.5s across tries
            if attempt:
                _time.sleep(1.5 * attempt)
            try:
                r, hop_err = _get_following_redirects(client, file_url,
                                                      ceiling)
            except Exception as e:
                last = f"download failed: {e}"
                continue
            if r is None:
                return None, hop_err          # refused hop — do NOT retry

            if r.status_code == 200:
                return r.content, ""

            # Authenticated Cloudinary fallback (different URL scheme — no retry)
            if r.status_code in (401, 403) and cloud_name and api_key and api_secret:
                m = re.search(
                    rf"https://res\.cloudinary\.com/{re.escape(cloud_name)}/[^/]+/authenticated/(.+)",
                    file_url,
                )
                if m:
                    public_id = m.group(1)
                    try:
                        import cloudinary, cloudinary.utils
                        cloudinary.config(cloud_name=cloud_name, api_key=api_key,
                                          api_secret=api_secret)
                        signed_url, _ = cloudinary.utils.cloudinary_url(
                            public_id, type="authenticated", sign_url=True)
                        r2 = client.get(signed_url)
                        if r2.status_code == 200:
                            return r2.content, ""
                        return None, f"signed download HTTP {r2.status_code}"
                    except Exception as e:
                        return None, f"cloudinary sign failed: {e}"
                return None, f"download HTTP {r.status_code} (no Cloudinary credentials to retry)"

            if r.status_code in TRANSIENT:
                last = f"download HTTP {r.status_code} (attempt {attempt + 1}/4, retrying)"
                print(f"[EXTRACT] {last} — {file_url[:90]}")
                continue                    # likely CDN propagation — wait and retry

            return None, f"download HTTP {r.status_code}"
    return None, last


# ---------- PDF ------------------------------------------------------------

# ---------------------------------------------------------------------------
# A PDF WITH A PICTURE IN IT.
#
# Learners put the deliverable inside a PDF constantly — the AI image with a
# heading above it, a screenshot pasted into Word and exported, a scan of
# handwritten notes with a typed title.
#
# The old rule was: extract text; if there is NONE, OCR it. So a PDF holding
# ONLY an image was read correctly, and a PDF holding "My 5 Year Plan" plus the
# image returned six words and the picture was never looked at. On Day 01 that
# is the whole submission — the image criterion scores zero for a learner whose
# work is sitting right there on page one.
#
# Now: thin text plus embedded images means OCR as well, and BOTH are sent. The
# threshold is words, not bytes, because a title page is short by nature and a
# real write-up is not.
# ---------------------------------------------------------------------------

PDF_THIN_TEXT_WORDS = int(os.getenv("PDF_THIN_TEXT_WORDS", "150"))


def pdf_needs_ocr(text: str, has_images: bool,
                  thin_words: int = PDF_THIN_TEXT_WORDS) -> bool:
    """Should this PDF be OCR'd as well as read? Pure, so it is testable.

    Conservative on cost: a PDF carrying a real write-up is returned as text
    and never rasterized, however many decorative images it contains.
    """
    if not has_images:
        return not (text or "").strip()
    return len((text or "").split()) < thin_words


def _pdf_has_images(data: bytes) -> bool:
    """Does any of the first few pages embed an image? Never raises."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(data))
        for page in reader.pages[:MAX_OCR_PAGES]:
            try:
                if len(page.images) > 0:
                    return True
            except Exception:
                # Some producers make page.images raise. An unreadable page is
                # not evidence of absence, so assume there IS something to look
                # at — the cost of being wrong is one OCR call, and the cost of
                # the opposite is a learner's deliverable going unseen.
                return True
    except Exception:
        return False
    return False


def _extract_pdf(data: bytes) -> Tuple[str, str]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return "", "pypdf not installed"
    try:
        reader = PdfReader(io.BytesIO(data))
        chunks = []
        for page in reader.pages:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                continue
        text = _clean("\n".join(chunks))
        if not text:
            return "", "PDF parsed but no extractable text (likely scanned image)"
        return text, ""
    except Exception as e:
        return "", f"pdf parse error: {e}"


# Rendered-pixel ceiling per OCR page. 4M px ~ a 2000x2000 image: ample for
# handwriting, and ~16 MB of BGRA rather than gigabytes.
MAX_OCR_PIXELS = int(os.getenv("MAX_OCR_PIXELS", str(4_000_000)))


def _ocr_scale(page, preferred: float = 2.0) -> float:
    """Render scale for one page, reduced until the bitmap fits the ceiling."""
    try:
        width, height = page.get_size()
        area = float(width) * float(height)
    except Exception:
        return preferred                      # unknown geometry — trust default
    if area <= 0:
        return preferred
    import math
    max_scale = math.sqrt(MAX_OCR_PIXELS / area)
    return max(0.2, min(preferred, max_scale))


def _extract_scanned_pdf(data: bytes) -> Tuple[str, str]:
    """Rasterize first N pages and OCR via Claude vision."""
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return "", "scanned PDF detected — install pypdfium2 to enable OCR"

    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as e:
        return "", f"pdf rasterize open failed: {e}"

    # Read the page count BEFORE the finally block closes the document. This
    # used to be re-read after close(), where the handle is NULL — pypdfium2
    # then raised ctypes.ArgumentError and the whole extraction crashed, so a
    # scanned PDF returned "extraction failed" instead of its OCR text.
    try:
        total_pages = len(pdf)
    except Exception as e:
        return "", f"pdf page count failed: {e}"
    n = min(total_pages, MAX_OCR_PAGES)
    if n == 0:
        return "", "PDF has zero pages"

    images_b64: List[Tuple[str, str]] = []  # (media_type, base64)
    try:
        for i in range(n):
            page = pdf[i]
            # Scale is CLAMPED by page area. MAX_FILE_BYTES bounds the input
            # bytes and MAX_OCR_PAGES bounds the page count, but nothing bounded
            # the page SIZE — and a valid 328-byte PDF may declare a 14400x14400
            # point MediaBox (pdfium's maximum). At scale 2.0 that renders a
            # 28800x28800 BGRA bitmap: 3.3 GB, copied again by to_pil(), five
            # times over. One request, container dead, every learner offline.
            bitmap = page.render(scale=_ocr_scale(page))
            pil_img = bitmap.to_pil()
            buf = io.BytesIO()
            pil_img.save(buf, format="PNG", optimize=True)
            images_b64.append(("image/png", base64.b64encode(buf.getvalue()).decode()))
    except Exception as e:
        return "", f"pdf rasterize page failed: {e}"
    finally:
        try:
            pdf.close()
        except Exception:
            pass

    text, why = _ocr_with_claude(images_b64, kind="scanned PDF")
    if not text:
        return "", why
    if total_pages > MAX_OCR_PAGES:
        text += (
            f"\n\n[Note: only the first {MAX_OCR_PAGES} pages were OCR-processed "
            f"out of {total_pages} total. Increase MAX_OCR_PAGES to read more.]"
        )
    return _cap(text), ""


# ---------- DOCX -----------------------------------------------------------

# Office files get the SAME picture treatment as PDFs. A .docx or .pptx is a
# ZIP whose media folder holds every pasted image — and learners paste their
# deliverable into Word constantly (Day 01: student 1315's .docx scored 0.0
# because the plan image inside it was never looked at). Thin typed text plus
# embedded pictures means OCR as well, same threshold, same labelling.

MAX_EMBEDDED_IMAGES = int(os.getenv("MAX_EMBEDDED_IMAGES", "6"))
MAX_EMBEDDED_IMAGE_BYTES = int(os.getenv("MAX_EMBEDDED_IMAGE_BYTES",
                                         str(6 * 1024 * 1024)))


def _embedded_media_images(data: bytes, folder: str) -> List[Tuple[str, str]]:
    """(media_type, b64) for pictures inside an OOXML zip's media folder.

    Never raises — a malformed archive returns [] and the caller falls back
    to text-only, exactly as before this existed. Tiny images (icons, bullet
    glyphs) are skipped by a size floor so six logo decorations cannot crowd
    out the one picture that is the actual deliverable.
    """
    import zipfile
    out: List[Tuple[str, str]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = sorted(n for n in z.namelist()
                           if n.startswith(folder)
                           and os.path.splitext(n)[1].lower() in IMAGE_EXTS)
            for name in names:
                if len(out) >= MAX_EMBEDDED_IMAGES:
                    break
                blob = z.read(name)
                if not (10_000 <= len(blob) <= MAX_EMBEDDED_IMAGE_BYTES):
                    continue                      # icon-sized or oversized
                converted, media_type, why = _to_readable_image(
                    blob, os.path.splitext(name)[1].lower())
                if not why:
                    out.append((media_type,
                                base64.b64encode(converted).decode()))
    except Exception:
        return []
    return out


def _with_embedded_pictures(text: str, data: bytes, folder: str,
                            kind: str, label: str) -> Tuple[str, str]:
    """Combine typed text with OCR of the file's pasted pictures — the PDF
    rule (`pdf_needs_ocr`) applied to Office files: a real write-up is never
    re-billed for decoration, thin text with pictures gets both read."""
    images = _embedded_media_images(data, folder)
    if not images or not pdf_needs_ocr(text, True):
        return (text, "") if text else ("", f"{label} parsed but empty")
    ocr, ocr_why = _ocr_with_claude(images, kind=kind)
    if ocr and text:
        return f"{text}\n\n=== PICTURES IN THIS {label} ===\n{ocr}", ""
    if ocr:
        return ocr, ""
    return (text, "") if text else ("", ocr_why or f"{label} parsed but empty")


def _extract_docx(data: bytes) -> Tuple[str, str]:
    try:
        from docx import Document
    except ImportError:
        return "", "python-docx not installed"
    try:
        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs if p.text]
        # Also pull table cells (case studies often use them)
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    if cell.text:
                        parts.append(cell.text)
        text = _clean("\n".join(parts))
        return _with_embedded_pictures(
            text, data, "word/media/",
            kind="pictures pasted into a Word document", label="DOCUMENT")
    except Exception as e:
        return "", f"docx parse error: {e}"


# ---------- RTF ------------------------------------------------------------

def _extract_odf(data: bytes, ext: str) -> Tuple[str, str]:
    """Text from an OpenDocument file (.odt / .ods / .odp).

    An ODF file is a ZIP whose content.xml holds the document body, so this
    needs no converter and no new dependency. Paragraph and cell boundaries
    become newlines first, THEN tags are stripped — strip them in the other
    order and a spreadsheet collapses into one unreadable run of words.
    """
    import zipfile

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if "content.xml" not in z.namelist():
                return "", f"{ext} file has no content.xml — it may be corrupt"
            xml = z.read("content.xml").decode("utf-8", errors="ignore")
    except Exception as e:
        return "", f"could not open this {ext} file: {type(e).__name__}"

    # Block boundaries -> newlines, before any tag stripping.
    xml = re.sub(r"</(text:p|text:h|table:table-row|draw:frame)>", "\n", xml)
    xml = re.sub(r"</table:table-cell>", "\t", xml)
    text = re.sub(r"<[^>]+>", "", xml)
    text = (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&apos;", "'"))
    text = _clean(text)
    if not text:
        return "", (f"this {ext} file has no readable text — if the work is a "
                    f"picture inside it, export it as a PDF or image and re-upload")
    return text, ""


_DATA_URI_RE = re.compile(r"(data:[a-zA-Z0-9/+.-]+;base64,)[A-Za-z0-9+/=]{80,}")
_HTML_SOURCE_MIN_VISIBLE_WORDS = 40


def _extract_html(data: bytes) -> Tuple[str, str]:
    """A web page the learner BUILT (Day 02: Claude artifacts export as .html).

    Two honest readings, tried in order:

    1. As a PAGE — the text a visitor would see, which is what the rubric
       judges. Uses the same tag-stripper the link path trusts (late import:
       submission_intake imports this module, so a top-level import here
       would be circular).
    2. As SOURCE — a React/JS artifact export renders client-side, so its
       visible text is nothing but the learner still wrote (or generated and
       curated) every line. The source IS the deliverable then; it goes to
       the marker labelled as source, with embedded base64 blobs truncated so
       one background image cannot spend the whole text budget.
    """
    source = data.decode("utf-8", errors="ignore")
    if not source.strip():
        return "", "HTML file was empty"

    from app.utils.submission_intake import html_to_text
    title_m = re.search(r"<title[^>]*>(.*?)</title>", source, re.I | re.S)
    title = re.sub(r"\s+", " ", title_m.group(1)).strip() if title_m else ""
    head = "[WEB PAGE BUILT BY THE LEARNER" + (f" — title: {title}" if title else "") + "]"

    visible = html_to_text(source)
    if len(visible.split()) >= _HTML_SOURCE_MIN_VISIBLE_WORDS:
        return f"{head}\n{visible}", ""

    stripped = _DATA_URI_RE.sub(r"\1[...embedded image data removed...]", source)
    body = _clean(stripped)[:SHEET_MAX_CHARS]
    return (f"{head}\n[The page renders in the browser, so its source code is "
            f"shown — this source is the learner's built deliverable.]\n"
            + (f"Visible text: {visible}\n" if visible else "") + body, "")


def looks_like_mhtml(data: bytes, sample: int = 2048) -> bool:
    """Is this a saved-web-page archive? Pure.

    An .mht is a MIME document, so it opens with mail headers. Checked on the
    head only, and requires the multipart/related content type a browser
    writes — an ordinary email forwarded as a file must not be mistaken for a
    submission, and a page saved without the extension must still be read.
    """
    head = bytes(data[:sample]).lower()
    return (b"mime-version:" in head
            and (b"multipart/related" in head or b"content-location:" in head))


def _mhtml_html_parts(data: bytes) -> list:
    """Every HTML fragment inside the archive, decoded. Pure-ish (no I/O).

    Images and stylesheets are skipped deliberately: a "Web Page, Complete"
    save embeds every asset as base64, and one background image would spend
    the whole text budget before the learner's own words were reached.
    """
    from email import policy
    from email.parser import BytesParser

    msg = BytesParser(policy=policy.default).parsebytes(data)
    parts = []
    for part in msg.walk():
        if part.get_content_type() != "text/html":
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            parts.append(payload.decode(charset, errors="ignore"))
        except LookupError:                      # an encoding Python lacks
            parts.append(payload.decode("utf-8", errors="ignore"))
    return parts


def _extract_mhtml(data: bytes) -> Tuple[str, str]:
    """A saved web page (.mht/.mhtml) — read as the page the learner saw.

    The archive is unwrapped to its HTML and handed to the ordinary HTML
    reader, so a saved page and an uploaded page are judged by exactly the
    same code. No second reading path, no second set of bugs.
    """
    try:
        parts = _mhtml_html_parts(data)
    except Exception as e:
        return "", f"the saved web page could not be unpacked ({type(e).__name__})"
    if not parts:
        return "", "the saved web page contained no readable page inside it"

    text, why = _extract_html("\n".join(parts).encode("utf-8", errors="ignore"))
    if not text:
        return "", why or "the saved web page had no readable content"
    return text.replace("[WEB PAGE BUILT BY THE LEARNER",
                        "[SAVED WEB PAGE SUBMITTED BY THE LEARNER", 1), ""


def _extract_rtf(data: bytes) -> Tuple[str, str]:
    """Strip RTF control words. Good enough for plain answer text."""
    try:
        raw = data.decode("utf-8", errors="ignore")
        # Drop binary blocks
        raw = re.sub(r"\\pict[^}]*\}", "", raw)
        # Drop control words like \rtf1, \par, \fs24, \'e9
        raw = re.sub(r"\\[a-zA-Z]+-?\d*\s?", " ", raw)
        raw = re.sub(r"\\'[0-9a-fA-F]{2}", "", raw)
        # Drop braces
        raw = re.sub(r"[{}]", "", raw)
        text = _clean(raw)
        if not text:
            return "", "RTF parsed but empty"
        return text, ""
    except Exception as e:
        return "", f"rtf parse error: {e}"


# ---------- Image OCR ------------------------------------------------------

def _sniff_media_type(data: bytes) -> str:
    """The media type the BYTES say they are, or "" when they say nothing.

    Live 21 Aug: WhatsApp saves PNG screenshots with a .jpeg name, and the
    vision API refuses the pair outright — 'specified using the image/jpeg
    media type, but the image appears to be a image/png image' — so the file
    the student really did upload scored as unreadable. The filename is the
    student's claim; the magic bytes are the file's own testimony, and only
    the testimony is admissible.
    """
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def _to_readable_image(data: bytes, ext: str) -> Tuple[bytes, str, str]:
    """(bytes, media_type, error) — convert anything the vision API cannot read.

    ONE conversion path for every non-native format instead of a special case
    per extension. HEIC was handled and the rest were not, so a learner who
    submitted a GIF or an AVIF — both ordinary outputs of the tools this course
    teaches — was told their image could not be read.

    The media type is decided by the bytes, never the extension: a PNG named
    .jpeg passes through as image/png, and a native-named file whose bytes are
    some OTHER real image format (a BMP renamed .png) falls through to the
    Pillow conversion below instead of being sent mislabelled and refused.
    """
    if ext in NATIVE_IMAGE_EXTS:
        sniffed = _sniff_media_type(data)
        if sniffed:
            return data, sniffed, ""
        # The extension promised a native format the bytes don't back up —
        # let Pillow identify and normalise whatever this actually is.
    try:
        from PIL import Image
        if ext in {".heic", ".heif"}:
            import pillow_heif
            pillow_heif.register_heif_opener()
        img = Image.open(io.BytesIO(data))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="PNG", optimize=True)
        return buf.getvalue(), "image/png", ""
    except ImportError as e:
        return b"", "", (f"{ext} images need an extra package ({e.name}). Either "
                         f"install it, or convert the picture to JPG or PNG and "
                         f"re-upload.")
    except Exception as e:
        return b"", "", f"could not open this {ext} image: {type(e).__name__}"


def image_for_judge(data: bytes, ext: str) -> Tuple[str, str]:
    """(base64, media_type) of an upload the MARKER should look at. Pure-ish.

    Returns ("", "") for anything that is not an image, or that could not be
    converted into one of the four types the vision model accepts. Reuses
    _to_readable_image, so HEIC from an iPhone and the other awkward formats
    reach the marker exactly as they already reach OCR — one conversion path,
    not two that drift.
    """
    if ext.lower() not in IMAGE_EXTS:
        return "", ""
    try:
        ready, media_type, why = _to_readable_image(data, ext)
    except Exception as e:
        logger.warning("image_for_judge failed: %s", e)
        return "", ""
    if not ready or why:
        return "", ""
    from app.services.ai_service import IMAGE_MEDIA_TYPES
    if media_type not in IMAGE_MEDIA_TYPES:
        return "", ""
    # The marker looks at this image through the same API, under the same
    # limits, so it gets the same fit.
    ready, media_type = _fit_for_vision(ready, media_type)
    return base64.b64encode(ready).decode(), media_type


def _extract_image(data: bytes, ext: str) -> Tuple[str, str]:
    """OCR a single image (handwritten, printed, or AI-generated)."""
    data, media_type, why = _to_readable_image(data, ext)
    if why:
        return "", why

    b64 = base64.b64encode(data).decode()
    return _ocr_with_claude([(media_type, b64)], kind="photographed notes")


def _ocr_with_claude(images: List[Tuple[str, str]], kind: str) -> Tuple[str, str]:
    """Run vision OCR on one or more images. Returns (text, reason)."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return "", "ANTHROPIC_API_KEY not set — cannot OCR"

    try:
        import anthropic
    except ImportError:
        return "", "anthropic SDK not installed"

    # Fit here, not at each caller. Three paths reach this function — a single
    # image, the pictures inside an OOXML file, and up to five rasterised PDF
    # pages in ONE request — and only the first went through a converter. One
    # choke point cannot drift out of step with the others.
    content = []
    for media_type, b64 in images:
        try:
            fitted, media_type = _fit_for_vision(base64.b64decode(b64), media_type)
            b64 = base64.b64encode(fitted).decode()
        except Exception:
            pass                          # send the original; the API will say why
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    content.append({
        "type": "text",
        "text": (
            f"The image(s) above are a student's {kind} submitted as coursework. "
            "Do BOTH of the following.\n"
            "1. TEXT — transcribe ALL handwritten or printed text exactly as written, "
            "preserving paragraph breaks and bullet points. Do not summarize, do not add "
            "commentary, do not correct grammar. Mark unreadable parts [unreadable]. "
            "Write NONE if the image contains no readable text.\n"
            "2. VISUAL — state factually what the image shows (subject, setting, style, "
            "any chart/diagram/screen and what it depicts). Describe only what is "
            "visible; never infer intent or judge quality.\n\n"
            "Answer in exactly this format:\n"
            "TEXT:\n<transcription or NONE>\n\n"
            "VISUAL:\n<one short factual paragraph>"
        ),
    })

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=OCR_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": content}],
        )
        text_parts = [b.text for b in msg.content if getattr(b, "type", "") == "text"]
        raw = _clean("\n".join(text_parts))
        if not raw or raw.strip().upper().strip(" .") == "NO_TEXT":
            # Not an error: an AI-generated picture or a photo legitimately
            # carries no text. Signalled distinctly so the caller can record
            # that an attachment EXISTS instead of concluding that the student
            # submitted nothing — which is what produced the fabricated
            # "your submission lacks an AI-generated image" criticism.
            return "", NO_TEXT_IN_IMAGE
        return raw, ""
    except Exception as e:
        # The class name alone is useless. "BadRequestError" appeared on live
        # submissions for days and nobody could tell whether it meant the image
        # was too large, too big on its long edge, or mislabelled — because the
        # one string that says so was discarded here. The API's own message is
        # short, safe to store and is the whole diagnosis.
        logger.exception("vision OCR failed")
        detail = str(e).strip().replace("\n", " ")
        return "", f"OCR failed: {type(e).__name__}" + (f": {detail[:300]}" if detail else "")


# ---------- Spreadsheets ----------------------------------------------------

def _extract_xlsx(data: bytes) -> Tuple[str, str]:
    """Render a workbook as reviewable text: values AND formulas, sheet by
    sheet. Formulas matter — an assignment asking for computed ratios must be
    judged on whether the student actually computed them, not typed them."""
    try:
        from openpyxl import load_workbook
    except ImportError:
        return "", "openpyxl not installed"
    try:
        wb_formulas = load_workbook(io.BytesIO(data), data_only=False, read_only=True)
        wb_values   = load_workbook(io.BytesIO(data), data_only=True,  read_only=True)
    except Exception as e:
        return "", f"xlsx parse error: {e}"

    out, truncated = [], False
    for sheet_name in wb_formulas.sheetnames:
        wsf, wsv = wb_formulas[sheet_name], wb_values[sheet_name]
        out.append(f"=== SHEET: {sheet_name} ===")
        for r_idx, (row_f, row_v) in enumerate(zip(
                wsf.iter_rows(max_row=SHEET_MAX_ROWS, max_col=SHEET_MAX_COLS),
                wsv.iter_rows(max_row=SHEET_MAX_ROWS, max_col=SHEET_MAX_COLS))):
            cells = []
            for cf, cv in zip(row_f, row_v):
                if cf.value is None and cv.value is None:
                    cells.append("")
                    continue
                formula = str(cf.value) if isinstance(cf.value, str) and str(cf.value).startswith("=") else None
                value = cv.value if cv.value is not None else cf.value
                cells.append(f"{value} [{formula}]" if formula else str(value))
            line = " | ".join(cells).rstrip(" |")
            if line.strip():
                out.append(line)
        if (wsf.max_row or 0) > SHEET_MAX_ROWS or (wsf.max_column or 0) > SHEET_MAX_COLS:
            truncated = True
        if sum(len(x) for x in out) > SHEET_MAX_CHARS:
            truncated = True
            break

    text = _clean("\n".join(out))[:SHEET_MAX_CHARS]
    if truncated:
        text += "\n\n[Note: workbook truncated for review — very large sheets are rendered partially.]"
    if not text or text.startswith("=== SHEET") and len(text) < 40:
        return "", "workbook parsed but contained no data"
    return text, ""


def _extract_csv(data: bytes, ext: str) -> Tuple[str, str]:
    raw = data.decode("utf-8-sig", errors="ignore")
    lines = raw.splitlines()[:SHEET_MAX_ROWS]
    text = _clean("\n".join(lines))[:SHEET_MAX_CHARS]
    if len(raw.splitlines()) > SHEET_MAX_ROWS:
        text += f"\n\n[Note: showing first {SHEET_MAX_ROWS} rows of {len(raw.splitlines())}.]"
    return (text, "") if text else ("", "csv was empty")


# ---------- Presentations ---------------------------------------------------

def _extract_pptx(data: bytes) -> Tuple[str, str]:
    try:
        from pptx import Presentation
    except ImportError:
        return "", "python-pptx not installed"
    try:
        prs = Presentation(io.BytesIO(data))
        out = []
        for i, slide in enumerate(prs.slides, 1):
            parts = [f"--- Slide {i} ---"]
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        t = "".join(run.text for run in para.runs).strip()
                        if t:
                            parts.append(t)
            if slide.has_notes_slide and slide.notes_slide.notes_text_frame:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    parts.append(f"[Speaker notes] {notes}")
            out.append("\n".join(parts))
        text = _clean("\n\n".join(out))[:SHEET_MAX_CHARS]
        return _with_embedded_pictures(
            text, data, "ppt/media/",
            kind="pictures on a presentation's slides", label="PRESENTATION")
    except Exception as e:
        return "", f"pptx parse error: {e}"


# ---------- Archives ---------------------------------------------------------

def _extract_zip(data: bytes) -> Tuple[str, str]:
    """Unpack in memory and extract every supported file inside. The capstone
    UI promises ZIP support — this honors it. Bomb-guarded."""
    import zipfile
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except Exception as e:
        return "", f"zip open failed: {e}"

    infos = [i for i in zf.infolist()
             if not i.is_dir() and not i.filename.startswith("__MACOSX")]
    if not infos:
        return "", "zip was empty"
    if sum(i.file_size for i in infos) > ZIP_MAX_TOTAL:
        return "", f"zip unpacks beyond {ZIP_MAX_TOTAL // (1024*1024)}MB — too large to review"

    # IMAGES INSIDE A ZIP ARE THE DELIVERABLE, NOT AN ATTACHMENT (24 Aug 2026).
    #
    # Student 312, Day 09: eight slides exported as PNGs and zipped —
    # 1_Data-Science.png ... 8_Advantages-and-Challenges.png — and the row read
    # "zip contained no readable files". Their whole deck was in there. A zip
    # of images IS how people hand over a presentation, and refusing it told a
    # learner who did the work that they had submitted nothing.
    #
    # Vision costs money, so it is bounded: only the first ZIP_MAX_IMAGES are
    # read, and what is skipped is named in the output rather than silently
    # dropped. That is the same trade the PDF path makes one line above.
    images_read = 0

    out, skipped = [], []
    for info in infos[:ZIP_MAX_FILES]:
        inner_name = info.filename
        inner_ext = "." + inner_name.rsplit(".", 1)[-1].lower() if "." in inner_name else ""
        if inner_ext == ".zip":
            skipped.append(inner_name)      # no nested archives
            continue
        try:
            inner = zf.read(info)
        except Exception:
            skipped.append(inner_name)
            continue
        if inner_ext in IMAGE_EXTS:
            if images_read >= ZIP_MAX_IMAGES:
                skipped.append(f"{inner_name} (beyond the "
                               f"{ZIP_MAX_IMAGES}-image limit for one zip)")
                continue
            images_read += 1
        text, why = _extract_inner(inner, inner_name, inner_ext)
        if text:
            out.append(f"===== FILE: {inner_name} =====\n{text}")
        else:
            skipped.append(f"{inner_name} ({why})" if why else inner_name)

    if len(infos) > ZIP_MAX_FILES:
        skipped.append(f"... and {len(infos) - ZIP_MAX_FILES} more files beyond the {ZIP_MAX_FILES}-file cap")
    combined = _clean("\n\n".join(out))
    if not combined:
        return "", ("zip contained no readable files; skipped: " + ", ".join(skipped[:8]))
    if skipped:
        combined += "\n\n[Files in the zip that could not be read: " + ", ".join(skipped[:10]) + "]"
    return combined, ""


def _extract_inner(data: bytes, name: str, ext: str) -> Tuple[str, str]:
    """Extract one file from inside a zip using the standard handlers."""
    if ext == ".pdf":
        text, why = _extract_pdf(data)
        return (text, why) if text else ("", why)   # no OCR inside zips — cost guard
    if ext == ".docx":
        return _extract_docx(data)
    if ext in {".xlsx", ".xlsm", ".xltx"}:
        return _extract_xlsx(data)
    if ext in {".csv", ".tsv"}:
        return _extract_csv(data, ext)
    if ext in {".pptx", ".potx"}:
        return _extract_pptx(data)
    if ext == ".ipynb":
        return _extract_ipynb(data)
    if ext in TEXT_EXTS or ext in CODE_EXTS:
        body = _clean(data.decode("utf-8", errors="ignore"))[:SHEET_MAX_CHARS]
        return (body, "") if body else ("", "empty")
    if ext in IMAGE_EXTS:
        return _extract_image(data, ext)
    return "", f"unsupported inside zip ({ext or 'no extension'})"


# ---------- Notebooks --------------------------------------------------------

def _extract_ipynb(data: bytes) -> Tuple[str, str]:
    import json as _json
    try:
        nb = _json.loads(data.decode("utf-8", errors="ignore"))
        out = []
        for cell in nb.get("cells", []):
            kind = cell.get("cell_type")
            src = "".join(cell.get("source", [])).strip()
            if not src:
                continue
            out.append(f"[{kind} cell]\n{src}" if kind else src)
        text = _clean("\n\n".join(out))[:SHEET_MAX_CHARS]
        return (text, "") if text else ("", "notebook had no content cells")
    except Exception as e:
        return "", f"ipynb parse error: {e}"


# ---------- helpers --------------------------------------------------------

def _looks_like_image(data: bytes) -> bool:
    return bool(_sniff_media_type(data))


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()