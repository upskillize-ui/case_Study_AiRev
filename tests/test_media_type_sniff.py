"""The media type is decided by the bytes, never by the filename.

Live 21 Aug, submission 301 (WA_1785939666840.jpeg): WhatsApp and several
phone galleries save PNG screenshots under a .jpeg name. The old path trusted
the extension, told the vision API "image/jpeg", and the API refused the pair
outright — BadRequestError 400: "The image was specified using the image/jpeg
media type, but the image appears to be a image/png image" (seen in BOTH
directions in the sweep log). The student's perfectly good picture then scored
as unreadable — an agent-side failure billed to the learner.

Rule now: the filename is the student's claim; the magic bytes are the file's
own testimony, and only the testimony is admissible.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.utils import file_extractor as fe

PNG  = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 100
GIF  = b"GIF89a" + b"\x00" * 100
WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 100


def test_the_whatsapp_case_png_bytes_named_jpeg():
    """Submission 301's exact shape: .jpeg name, PNG bytes. Must go to the
    API as image/png, bytes untouched."""
    out, media_type, why = fe._to_readable_image(PNG, ".jpeg")
    assert why == ""
    assert media_type == "image/png"
    assert out == PNG


def test_the_reverse_case_jpeg_bytes_named_png():
    """The sweep log showed the mirror error too."""
    out, media_type, why = fe._to_readable_image(JPEG, ".png")
    assert why == "" and media_type == "image/jpeg" and out == JPEG


def test_an_honestly_named_file_is_unchanged():
    for blob, ext, mt in ((PNG, ".png", "image/png"),
                          (JPEG, ".jpg", "image/jpeg"),
                          (GIF, ".gif", "image/gif"),
                          (WEBP, ".webp", "image/webp")):
        out, media_type, why = fe._to_readable_image(blob, ext)
        assert (out, media_type, why) == (blob, mt, "")


def test_a_native_name_over_foreign_bytes_falls_to_pillow_conversion():
    """A BMP renamed .png: the bytes back up NO native format, so the file
    must be normalised through Pillow instead of being sent mislabelled."""
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (200, 60, 60)).save(buf, format="BMP")
    out, media_type, why = fe._to_readable_image(buf.getvalue(), ".png")
    assert why == ""
    assert media_type == "image/png"
    assert out[:8] == b"\x89PNG\r\n\x1a\n"      # really converted, not passed


def test_garbage_with_a_native_name_errors_instead_of_hitting_the_api():
    """Random bytes named .jpg used to be shipped straight to the vision API
    (a guaranteed 400 spent from the OCR budget). Now they fail locally with
    the ordinary could-not-open message."""
    out, media_type, why = fe._to_readable_image(b"\x00\x01" * 200, ".jpg")
    assert why != "" and out == b""


def test_sniff_answers_for_every_native_format_and_nothing_else():
    assert fe._sniff_media_type(PNG) == "image/png"
    assert fe._sniff_media_type(JPEG) == "image/jpeg"
    assert fe._sniff_media_type(GIF) == "image/gif"
    assert fe._sniff_media_type(WEBP) == "image/webp"
    assert fe._sniff_media_type(b"plain text here") == ""
    assert fe._sniff_media_type(b"") == ""


def test_embedded_office_pictures_ride_the_same_rule():
    """word/media/image1.jpeg holding PNG bytes — the docx path feeds
    _to_readable_image, so the label must come out right there too."""
    import base64
    import io
    import zipfile
    big_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20_000
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/media/image1.jpeg", big_png)   # lying name
    images = fe._embedded_media_images(buf.getvalue(), "word/media/")
    assert len(images) == 1
    media_type, b64 = images[0]
    assert media_type == "image/png"
    assert base64.b64decode(b64) == big_png
