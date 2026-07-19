# tests/test_media_service.py
# Pure-function tests for the media pipeline — no network, no ffmpeg, no DB.

from app.services.media_service import _is_youtube, _YT_RE, BFSI_GLOSSARY, CLEAN_CHUNK_CHARS


def test_youtube_url_detection():
    assert _is_youtube("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    assert _is_youtube("https://youtu.be/dQw4w9WgXcQ")
    assert _is_youtube("https://www.youtube.com/embed/dQw4w9WgXcQ")
    assert _is_youtube("https://youtube.com/shorts/dQw4w9WgXcQ")
    assert not _is_youtube("https://res.cloudinary.com/demo/video/upload/session.mp4")
    assert not _is_youtube("https://upskillize-lms-backend.onrender.com/uploads/s1.mp4")
    assert not _is_youtube("")
    assert not _is_youtube(None)


def test_youtube_id_extraction():
    m = _YT_RE.search("https://www.youtube.com/watch?v=Ab3_x9Yz012&t=42s")
    assert m and m.group(1) == "Ab3_x9Yz012"


def test_glossary_covers_key_jargon():
    for term in ("SARFAESI", "FOIR", "NBFC", "drawing power", "CIBIL"):
        assert term in BFSI_GLOSSARY


def test_clean_chunking_boundaries():
    raw = "x" * (CLEAN_CHUNK_CHARS * 2 + 100)
    chunks = [raw[i:i + CLEAN_CHUNK_CHARS] for i in range(0, len(raw), CLEAN_CHUNK_CHARS)]
    assert len(chunks) == 3
    assert "".join(chunks) == raw  # nothing lost at boundaries


if __name__ == "__main__":
    import sys, inspect
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and inspect.isfunction(fn):
            try:
                fn()
                print(f"  PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"  FAIL {name}: {e}")
    sys.exit(1 if failures else 0)
