# app/utils/file_extractor.py
# Downloads an uploaded file (Cloudinary, S3, anywhere public) and extracts
# text from PDF/DOCX/TXT. Returns ("", reason) on any failure so the caller
# can fall back to whatever short text it already had.

import io
import re
import httpx


MAX_BYTES = 15 * 1024 * 1024   # 15MB cap to be safe
TIMEOUT   = 20.0


def extract_text_from_url(file_url: str, file_name: str = "") -> tuple[str, str]:
    """
    Returns (extracted_text, reason).
    On success: ('extracted text...', '')
    On failure: ('', 'short reason for log')
    """
    if not file_url:
        return "", "no file_url provided"

    try:
        with httpx.stream("GET", file_url, timeout=TIMEOUT, follow_redirects=True) as r:
            if r.status_code != 200:
                return "", f"download HTTP {r.status_code}"
            buf = io.BytesIO()
            total = 0
            for chunk in r.iter_bytes():
                total += len(chunk)
                if total > MAX_BYTES:
                    return "", f"file exceeds {MAX_BYTES // (1024*1024)}MB cap"
                buf.write(chunk)
            data = buf.getvalue()
    except Exception as e:
        return "", f"download failed: {e}"

    name = (file_name or file_url).lower()
    try:
        if name.endswith(".pdf"):
            return _extract_pdf(data)
        if name.endswith(".docx"):
            return _extract_docx(data)
        if name.endswith((".txt", ".md")):
            return _clean(data.decode("utf-8", errors="ignore")), ""
        # Fallback: try PDF first, then DOCX, then bytes-as-text
        for fn in (_extract_pdf, _extract_docx):
            text, why = fn(data)
            if text:
                return text, ""
        return _clean(data.decode("utf-8", errors="ignore")), ""
    except Exception as e:
        return "", f"extraction failed: {e}"


def _extract_pdf(data: bytes) -> tuple[str, str]:
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


def _extract_docx(data: bytes) -> tuple[str, str]:
    try:
        from docx import Document
    except ImportError:
        return "", "python-docx not installed"
    try:
        doc = Document(io.BytesIO(data))
        text = _clean("\n".join(p.text for p in doc.paragraphs if p.text))
        if not text:
            return "", "DOCX parsed but empty"
        return text, ""
    except Exception as e:
        return "", f"docx parse error: {e}"


def _clean(text: str) -> str:
    # Collapse excessive whitespace and strip
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()