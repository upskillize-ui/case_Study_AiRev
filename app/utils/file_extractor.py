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
MAX_OCR_PAGES = int(os.getenv("MAX_OCR_PAGES", "5"))                       # OCR cap
OCR_MODEL = os.getenv("OCR_MODEL", "claude-sonnet-4-5")                    # vision-capable

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
TEXT_EXTS  = {".txt", ".md"}


# ---------- public entry ---------------------------------------------------

def extract_text_from_url(file_url: str, file_name: str = "") -> Tuple[str, str]:
    """
    Returns (extracted_text, reason).
    On success: ('extracted text...', '')
    On failure: ('', 'short reason for log')
    """
    if not file_url:
        return "", "no file_url provided"

    data, why = _download_file(file_url)
    if data is None:
        return "", why

    if len(data) > MAX_FILE_BYTES:
        return "", f"file too large ({len(data) // 1024} KB > {MAX_FILE_BYTES // 1024} KB)"

    name = (file_name or file_url).lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""

    try:
        # PDFs ------------------------------------------------------------
        if ext == ".pdf":
            text, why = _extract_pdf(data)
            if text:
                return text, ""
            # Empty -> probably scanned. Try OCR.
            logger.info("PDF text empty (%s) -> falling back to vision OCR", why)
            return _extract_scanned_pdf(data)

        # Word ------------------------------------------------------------
        if ext == ".docx":
            return _extract_docx(data)
        if ext == ".doc":
            return "", (
                "Legacy .doc format isn't supported. "
                "Please open the file in Word, choose 'Save As', "
                "select 'Word Document (.docx)', and re-upload."
            )

        # Plain text ------------------------------------------------------
        if ext in TEXT_EXTS:
            return _clean(data.decode("utf-8", errors="ignore")), ""
        if ext == ".rtf":
            return _extract_rtf(data)

        # Images (handwritten notes) -------------------------------------
        if ext in IMAGE_EXTS:
            return _extract_image(data, ext)

        # Unknown ext -> sniff. Try PDF, DOCX, then image, then bytes-as-text.
        for fn in (_extract_pdf, _extract_docx):
            text, _ = fn(data)
            if text:
                return text, ""
        if _looks_like_image(data):
            return _extract_image(data, ".png")
        return _clean(data.decode("utf-8", errors="ignore")), ""

    except Exception as e:
        logger.exception("extraction crashed")
        return "", f"extraction failed: {type(e).__name__}"


# ---------- download (kept compatible with original) -----------------------

def _download_file(file_url: str) -> Tuple[bytes, str]:
    """Returns (bytes, reason). bytes is None on failure."""
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME")
    api_key    = os.getenv("CLOUDINARY_API_KEY")
    api_secret = os.getenv("CLOUDINARY_API_SECRET")

    try:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            r = client.get(file_url)
            if r.status_code == 200:
                return r.content, ""
            # Authenticated Cloudinary fallback
            if r.status_code in (401, 403) and cloud_name and api_key and api_secret:
                m = re.search(
                    rf"https://res\.cloudinary\.com/{re.escape(cloud_name)}/[^/]+/authenticated/(.+)",
                    file_url,
                )
                if m:
                    public_id = m.group(1)
                    try:
                        import cloudinary, cloudinary.utils
                        cloudinary.config(
                            cloud_name=cloud_name,
                            api_key=api_key,
                            api_secret=api_secret,
                        )
                        signed_url, _ = cloudinary.utils.cloudinary_url(
                            public_id, type="authenticated", sign_url=True,
                        )
                        r2 = client.get(signed_url)
                        if r2.status_code == 200:
                            return r2.content, ""
                        return None, f"signed download HTTP {r2.status_code}"
                    except Exception as e:
                        return None, f"cloudinary sign failed: {e}"
                return None, f"download HTTP {r.status_code} (no Cloudinary credentials to retry)"
            return None, f"download HTTP {r.status_code}"
    except Exception as e:
        return None, f"download failed: {e}"


# ---------- PDF ------------------------------------------------------------

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

    n = min(len(pdf), MAX_OCR_PAGES)
    if n == 0:
        return "", "PDF has zero pages"

    images_b64: List[Tuple[str, str]] = []  # (media_type, base64)
    try:
        for i in range(n):
            page = pdf[i]
            # 144 DPI is a good handwriting/print balance
            bitmap = page.render(scale=2.0)
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
    if len(pdf) > MAX_OCR_PAGES:
        text += (
            f"\n\n[Note: only the first {MAX_OCR_PAGES} pages were OCR-processed "
            f"out of {len(pdf)} total. Increase MAX_OCR_PAGES to read more.]"
        )
    return text, ""


# ---------- DOCX -----------------------------------------------------------

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
        if not text:
            return "", "DOCX parsed but empty"
        return text, ""
    except Exception as e:
        return "", f"docx parse error: {e}"


# ---------- RTF ------------------------------------------------------------

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

def _extract_image(data: bytes, ext: str) -> Tuple[str, str]:
    """OCR a single image (handwritten or printed)."""
    media_type = _media_type_for_ext(ext)

    # Convert HEIC/HEIF -> PNG so Claude can read it
    if ext in {".heic", ".heif"}:
        try:
            import pillow_heif
            from PIL import Image
            pillow_heif.register_heif_opener()
            img = Image.open(io.BytesIO(data))
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG", optimize=True)
            data = buf.getvalue()
            media_type = "image/png"
        except ImportError:
            return "", (
                "HEIC images need pillow-heif. Either install it, "
                "or convert the photo to JPG/PNG and re-upload."
            )
        except Exception as e:
            return "", f"heic conversion failed: {e}"

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

    content = []
    for media_type, b64 in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": media_type, "data": b64},
        })
    content.append({
        "type": "text",
        "text": (
            f"The image(s) above are a student's {kind} for a case-study answer. "
            "Transcribe ALL handwritten or printed text you can read, exactly as written. "
            "Preserve paragraph breaks and bullet points. Do not summarize, do not add "
            "commentary, do not correct grammar. If multiple pages are shown, separate them "
            "with a blank line. If a section is unreadable, write [unreadable] in its place. "
            "Output only the transcription — no preamble."
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
        text = _clean("\n".join(text_parts))
        if not text:
            return "", "OCR returned empty text"
        return text, ""
    except Exception as e:
        logger.exception("vision OCR failed")
        return "", f"OCR failed: {type(e).__name__}"


# ---------- helpers --------------------------------------------------------

def _media_type_for_ext(ext: str) -> str:
    return {
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png":  "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(ext, "image/png")


def _looks_like_image(data: bytes) -> bool:
    if len(data) < 12:
        return False
    return (
        data[:3] == b"\xff\xd8\xff"            # JPEG
        or data[:8] == b"\x89PNG\r\n\x1a\n"    # PNG
        or data[:4] == b"RIFF" and data[8:12] == b"WEBP"  # WEBP
    )


def _clean(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()