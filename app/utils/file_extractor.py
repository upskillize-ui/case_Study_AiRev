# app/utils/file_extractor.py
# Downloads an uploaded file and extracts text from PDF/DOCX/TXT.
# Supports Cloudinary authenticated files via signed URLs.

import io
import os
import re
import httpx


MAX_BYTES = 15 * 1024 * 1024   # 15MB cap
TIMEOUT   = 25.0


def _get_cloudinary_signed_url(file_url: str) -> str | None:
    """
    If the URL is from Cloudinary and we have credentials,
    generate a signed URL that can download authenticated resources.
    """
    cloud_name = os.getenv("CLOUDINARY_CLOUD_NAME", "")
    api_key    = os.getenv("CLOUDINARY_API_KEY", "")
    api_secret = os.getenv("CLOUDINARY_API_SECRET", "")

    if not all([cloud_name, api_key, api_secret]):
        return None

    # Check if this is a Cloudinary URL
    if "cloudinary.com" not in file_url and "cloudinary" not in file_url:
        return None

    try:
        import cloudinary
        import cloudinary.utils

        cloudinary.config(
            cloud_name=cloud_name,
            api_key=api_key,
            api_secret=api_secret,
            secure=True,
        )

        # Parse the public_id and resource_type from the URL
        # URL format:
        #   https://res.cloudinary.com/CLOUD/raw/authenticated/[transformations/]v123/folder/file.pdf
        # Transformations contain ':' (e.g., fl_attachment:false, w_500:h_300)
        # We need to skip them to get the real public_id
        import re as _re

        # Step 1: Extract resource_type and delivery_type
        pattern = rf"cloudinary\.com/{_re.escape(cloud_name)}/(\w+)/(authenticated|upload|private)/(.*)"
        match = _re.search(pattern, file_url)
        if not match:
            pattern = rf"cloudinary\.com/[^/]+/(\w+)/(authenticated|upload|private)/(.*)"
            match = _re.search(pattern, file_url)

        if not match:
            print(f"📄 Cloudinary: could not parse URL structure")
            return None

        resource_type = match.group(1)   # raw, image, video
        delivery_type = match.group(2)   # authenticated, upload, private
        remainder     = match.group(3)   # everything after type/

        # Step 2: Strip transformations (segments containing ':') and version (v followed by digits)
        parts = remainder.split("/")
        clean_parts = []
        found_version = False
        for part in parts:
            if ":" in part:
                continue  # skip transformations like fl_attachment:false
            if _re.match(r"^v\d+$", part):
                found_version = True
                continue  # skip version like v1776406075
            clean_parts.append(part)

        public_id = "/".join(clean_parts)

        if not public_id:
            print(f"📄 Cloudinary: empty public_id after parsing")
            return None

        print(f"📄 Cloudinary: parsed public_id = {public_id}")

        # Generate signed URL
        signed_url, _ = cloudinary.utils.cloudinary_url(
            public_id,
            resource_type=resource_type,
            type=delivery_type,
            sign_url=True,
            secure=True,
        )

        if signed_url:
            print(f"📄 Cloudinary: generated signed URL for {public_id}")
            return signed_url

    except Exception as e:
        print(f"📄 Cloudinary signed URL failed: {e}")

    return None


def _download_file(file_url: str) -> tuple[bytes | None, str]:
    """Download file, with Cloudinary signed URL fallback on 401."""
    urls_to_try = [file_url]

    # First try the original URL
    try:
        with httpx.stream("GET", file_url, timeout=TIMEOUT, follow_redirects=True) as r:
            if r.status_code == 200:
                buf = io.BytesIO()
                total = 0
                for chunk in r.iter_bytes():
                    total += len(chunk)
                    if total > MAX_BYTES:
                        return None, f"file exceeds {MAX_BYTES // (1024*1024)}MB cap"
                    buf.write(chunk)
                return buf.getvalue(), ""
            elif r.status_code in (401, 403):
                # Try Cloudinary signed URL
                signed_url = _get_cloudinary_signed_url(file_url)
                if signed_url and signed_url != file_url:
                    print(f"📄 Retrying with Cloudinary signed URL...")
                    try:
                        with httpx.stream("GET", signed_url, timeout=TIMEOUT, follow_redirects=True) as r2:
                            if r2.status_code == 200:
                                buf = io.BytesIO()
                                total = 0
                                for chunk in r2.iter_bytes():
                                    total += len(chunk)
                                    if total > MAX_BYTES:
                                        return None, f"file exceeds {MAX_BYTES // (1024*1024)}MB cap"
                                    buf.write(chunk)
                                print(f"📄 Cloudinary signed URL download SUCCESS")
                                return buf.getvalue(), ""
                            else:
                                return None, f"signed URL also returned HTTP {r2.status_code}"
                    except Exception as e:
                        return None, f"signed URL download failed: {e}"
                return None, f"download HTTP {r.status_code} (no Cloudinary credentials to retry)"
            else:
                return None, f"download HTTP {r.status_code}"
    except Exception as e:
        return None, f"download failed: {e}"


def extract_text_from_url(file_url: str, file_name: str = "") -> tuple[str, str]:
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
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()