#!/usr/bin/env python
"""Can this machine — and the Space — actually read a recording? (read-only)

Eight Day-05 learners submitted Audio Overviews that were never read, and the
cause was never established: the pipeline exists, ffmpeg is in the image, the
dispatch is wired. One of the links in that chain is missing at RUNTIME, and
guessing which has already cost a week.

    python tools/media_check.py                  # check the pieces
    python tools/media_check.py path/to/a.m4a    # and actually read one

Nothing is written, nothing is graded, no student row is touched.
"""

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

OK, BAD = "  ok  ", " FAIL "


def _row(label: str, good: bool, detail: str = "") -> bool:
    print(f"[{OK if good else BAD}] {label}" + (f" — {detail}" if detail else ""))
    return good


def check_tools() -> bool:
    """ffmpeg and ffprobe do the work; without them nothing else matters."""
    fine = True
    for exe in ("ffmpeg", "ffprobe"):
        path = shutil.which(exe)
        fine &= _row(f"{exe} installed", bool(path), path or "not on PATH")
    return fine


def check_key() -> bool:
    key = os.getenv("TRANSCRIBE_API_KEY", "")
    return _row("TRANSCRIBE_API_KEY set", bool(key),
                f"{len(key)} characters" if key
                else "unset — every recording will come back 'no speech'")


def check_wiring() -> bool:
    """Is the dispatch actually routing media to the transcriber?"""
    try:
        from app.services.submission_media import MEDIA_EXTS, is_media
        from app.utils.file_extractor import _extract_dispatch      # noqa: F401
    except Exception as e:
        return _row("media pipeline importable", False, str(e)[:80])
    fine = _row("media pipeline importable", True,
                f"{len(MEDIA_EXTS)} extensions recognised")
    fine &= _row(".m4a recognised as media", is_media("overview.m4a"))
    fine &= _row(".mp4 recognised as media", is_media("overview.mp4"))
    return fine


def read_one(path: str) -> bool:
    """End to end on a real file — the only check that proves the whole chain."""
    if not os.path.exists(path):
        return _row(f"read {path}", False, "no such file")
    from app.services.submission_media import frames_for, transcribe_and_describe

    with open(path, "rb") as fh:
        data = fh.read()
    print(f"\nReading {path} ({len(data) / 1e6:.1f} MB)...")
    text, why = transcribe_and_describe(data, os.path.basename(path))
    if not text:
        return _row("recording read", False, why)
    _row("recording read", True, f"{len(text.split())} words")
    frames = frames_for(data)
    if frames:
        print(f"        {len(frames)} frame(s) kept for the marker to look at")
    print("\n--- what the marker would receive (first 600 characters) ---")
    print(text[:600])
    return True


def main() -> None:
    print("AiRev media check — nothing is written.\n")
    fine = check_tools()
    fine &= check_key()
    fine &= check_wiring()

    for path in sys.argv[1:]:
        fine &= read_one(path)

    print()
    if fine:
        print("All checks passed. If a learner's recording still comes back "
              "unread, the fault is in that FILE, not in the pipeline.")
    else:
        print("Something above is missing. Fix the FAIL lines before blaming "
              "the submissions — every one of them makes every recording "
              "unreadable, for every student, silently.")
    print("\nRun this on the Space too (Settings -> the same environment), "
          "not only here:\n  the key is set per-environment, and this machine "
          "having it proves nothing about the Space.")
    sys.exit(0 if fine else 1)


if __name__ == "__main__":
    main()
