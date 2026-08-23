# app/services/submission_media.py
# ---------------------------------------------------------------------------
# Audio and video submitted BY A LEARNER, turned into something reviewable.
#
# WHY. The programme is "30 Days, 30 AI Tools", and a growing share of those
# tools produce sound and moving pictures, not prose: NotebookLM's Studio
# Output is an mp3 Audio Overview, Suno makes songs, ElevenLabs makes speech,
# Runway and HeyGen make video. Until now file_extractor refused all of it —
#
#     "mp3 is a video or audio file, and AiRev reviews written work"
#
# — which was the right guard for the wrong scope. It was added because a
# WhatsApp .mp4 once decoded as 272,000 words of binary garbage and 500'd a
# submit. The fix for that is to STOP DECODING BINARY AS TEXT, not to refuse
# the medium. Observed live on 14 Aug: Day 05 (NotebookLM) logging
# "file(unread)", Day 06 (Suno) logging "link(unread)" — learners marked as
# having submitted nothing, for doing exactly what the task asked.
#
# WHAT CLAUDE CAN ACTUALLY DO. There is no video input on the API. Claude
# cannot watch a video, and no amount of wiring changes that. What it can do is
# read text and see still images. So a media file becomes reviewable as two
# streams:
#
#   audio track  -> ffmpeg -> Whisper  -> the narration, verbatim
#   sampled frames -> ffmpeg -> vision -> factual descriptions of what is shown
#
# That answers most of a rubric honestly: was it produced, how long is it, what
# is said, is it on topic, what appears on screen. It CANNOT judge motion,
# pacing, transitions or edit quality — and the header written into the output
# says so, so the marker declares that rather than inventing a view on it.
#
# Everything here reuses media_service's ffmpeg and Whisper helpers. That
# module already downloads, extracts mono 16 kHz audio, segments it and
# transcribes; duplicating any of it would mean two pipelines drifting apart,
# which is exactly the mistake that left `fileUrl` unguarded while links were
# protected.
# ---------------------------------------------------------------------------

from __future__ import annotations

import base64
import logging
import os
import shutil
import subprocess
import tempfile
from typing import List, Tuple

logger = logging.getLogger(__name__)

AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".flac"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".3gp", ".wmv", ".flv"}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS

# Bounds. Coursework media is minutes, not hours; anything past this is either
# a mistake or an attack, and either way the learner gets a message they can
# act on instead of a silent timeout.
MAX_MEDIA_SECONDS = int(os.getenv("MAX_MEDIA_SECONDS", "900"))        # 15 min
MAX_VIDEO_FRAMES = int(os.getenv("MAX_VIDEO_FRAMES", "6"))
FFPROBE_TIMEOUT = 30
FFMPEG_TIMEOUT = int(os.getenv("MEDIA_FFMPEG_TIMEOUT", "300"))


# ---------------------------------------------------------------------------
# THE FRAMES THEMSELVES (Phase 3, 23 Aug 2026).
#
# _describe_frames turns sampled frames into prose so the marker can read
# them. That was the only option while the judge took text alone. It is no
# longer: the judge sees pictures now, and a described frame is a lossy
# retelling of one. A HeyGen presenter's slide, a Runway shot, a NotebookLM
# Video Overview — the marker should look at those, not read about them.
#
# Frames are stashed here during extraction, keyed by the submission's own
# bytes, because the extraction happens several layers below the code that
# assembles what the marker receives and threading them back would mean
# changing four signatures for one optional extra. Bounded and locked, the
# same shape as the renderer's screenshot store, for the same reason: reviews
# run concurrently and a nice-to-have must never take a review down.
# ---------------------------------------------------------------------------

import hashlib
import threading

_FRAME_KEEP = int(os.getenv("MEDIA_FRAME_KEEP", "4"))
_frames_lock = threading.Lock()
_last_frames: dict = {}          # content hash -> [(media_type, b64)]


def media_key(data: bytes) -> str:
    """Identify a submission by its own bytes. Pure."""
    return hashlib.sha1(data or b"").hexdigest()


def _keep_frames(key: str, frames: list) -> None:
    if not frames:
        return
    with _frames_lock:
        while len(_last_frames) >= _FRAME_KEEP:
            try:
                _last_frames.pop(next(iter(_last_frames)))
            except StopIteration:
                break
        _last_frames[key] = frames


def frames_for(data: bytes) -> list:
    """[{'image': b64, 'media_type': ...}] for media already extracted, else []."""
    with _frames_lock:
        frames = _last_frames.get(media_key(data), [])
    return [{"image": b64, "media_type": mt} for mt, b64 in frames]


def forget_frames() -> None:
    with _frames_lock:
        _last_frames.clear()


def is_media(file_name: str) -> bool:
    return _ext(file_name) in MEDIA_EXTS


def _ext(file_name: str) -> str:
    name = (file_name or "").lower().split("?")[0]
    return "." + name.rsplit(".", 1)[-1] if "." in name else ""


def transcribe_and_describe(data: bytes, file_name: str) -> Tuple[str, str]:
    """Turn submitted media bytes into reviewable text. Returns (text, reason).

    Audio yields a transcript. Video yields a transcript plus descriptions of
    sampled frames. Both carry a header stating what was and was not assessed,
    because a marker told only "here is a transcript" cannot know whether
    silence means no narration or a failed extraction.
    """
    ext = _ext(file_name)
    if ext not in MEDIA_EXTS:
        return "", f"{ext or 'this file'} is not audio or video"

    workdir = tempfile.mkdtemp(prefix="airev_submission_")
    try:
        src = os.path.join(workdir, "input" + ext)
        with open(src, "wb") as fh:
            fh.write(data)

        duration = _duration_seconds(src)
        if duration and duration > MAX_MEDIA_SECONDS:
            mins = MAX_MEDIA_SECONDS // 60
            return "", (f"this recording is {int(duration // 60)} minutes long; "
                        f"AiRev reviews up to {mins} minutes. Trim it and re-upload.")

        is_video = ext in VIDEO_EXTS
        parts: List[str] = [_header(is_video, duration)]

        transcript, why = _transcribe(src, workdir)
        if transcript:
            parts.append("WHAT IS SAID (transcribed from the audio):\n" + transcript)
        else:
            # Silence is a legitimate finding for a music or visual piece, and
            # a failed transcription is not. Never conflate them.
            parts.append(f"WHAT IS SAID: no speech could be transcribed ({why}).")

        if is_video:
            shots, why_frames = _describe_frames(src, workdir, key=media_key(data))
            if shots:
                parts.append("WHAT IS SHOWN (still frames sampled across the "
                             "video, described factually):\n" + shots)
            else:
                parts.append(f"WHAT IS SHOWN: frames could not be read ({why_frames}).")

        text = "\n\n".join(parts).strip()
        # A header alone is not content — it would tell the marker a
        # deliverable was read when nothing was.
        if not transcript and (not is_video or "WHAT IS SHOWN: frames could not" in text):
            return "", "nothing could be read from this recording"
        return text, ""

    except Exception as e:
        logger.exception("submission media extraction failed")
        return "", f"could not process this recording ({type(e).__name__})"
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _header(is_video: bool, duration: float | None) -> str:
    length = f" ({int(duration // 60)}m {int(duration % 60)}s)" if duration else ""
    kind = "video" if is_video else "audio recording"
    limits = ("Motion, pacing, editing and transitions were NOT assessed — only "
              "still frames and the spoken audio were available. Say so if a "
              "criterion depends on them; do not infer them."
              if is_video else
              "Only the audio was available. Judge what is said and how it is "
              "structured; do not infer visuals.")
    return (f"[The learner submitted a {kind}{length}. It cannot be played "
            f"here, so it was converted: {limits}]")


def _duration_seconds(path: str) -> float | None:
    """ffprobe the real duration. Returns None when it cannot be determined —
    unknown length must not be treated as zero, which would skip the cap."""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
        value = (proc.stdout or "").strip()
        return float(value) if value and value != "N/A" else None
    except Exception:
        return None


def _transcribe(src: str, workdir: str) -> Tuple[str, str]:
    """Audio -> text, via media_service's existing chunk-and-Whisper path."""
    from app.services import media_service

    if not os.getenv("TRANSCRIBE_API_KEY"):
        return "", "TRANSCRIBE_API_KEY is not set on the Space"
    try:
        chunks = media_service._extract_audio_chunks(src, workdir)
    except Exception as e:
        return "", f"no audio track ({type(e).__name__})"
    try:
        texts = [media_service._whisper_chunk(c) for c in chunks]
    except Exception as e:
        return "", f"transcription failed ({type(e).__name__})"
    joined = " ".join(t for t in texts if t).strip()
    return (joined, "") if joined else ("", "the recording carries no speech")


def _describe_frames(src: str, workdir: str, key: str = "") -> Tuple[str, str]:
    """Sample frames evenly and have vision describe them.

    Evenly spaced, not the first N seconds — a title card repeated six times
    tells the marker nothing about the body of the video.
    """
    frames_dir = os.path.join(workdir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    duration = _duration_seconds(src) or 0
    n = max(1, min(MAX_VIDEO_FRAMES, 6))
    # fps filter that lands ~n frames across the whole clip; scale keeps each
    # frame small enough to be cheap to send.
    rate = max(n / duration, 0.05) if duration > 0 else 0.2
    cmd = ["ffmpeg", "-y", "-i", src,
           "-vf", f"fps={rate:.4f},scale='min(768,iw)':-2",
           "-frames:v", str(n), os.path.join(frames_dir, "f_%02d.jpg")]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=FFMPEG_TIMEOUT)
        if proc.returncode != 0:
            return "", f"ffmpeg: {proc.stderr[-160:]}"
    except Exception as e:
        return "", f"frame extraction failed ({type(e).__name__})"

    files = sorted(os.path.join(frames_dir, f) for f in os.listdir(frames_dir)
                   if f.endswith(".jpg"))
    if not files:
        return "", "no frames could be extracted"

    images = []
    for path in files[:n]:
        try:
            with open(path, "rb") as fh:
                images.append(("image/jpeg", base64.b64encode(fh.read()).decode()))
        except Exception:
            continue
    # Keep the frames themselves for the marker to LOOK at. The described
    # version below is still produced: a description names things a still
    # cannot show on its own, and the two together are what a person gets.
    if key:
        _keep_frames(key, images)
    if not images:
        return "", "frames could not be read"

    from app.utils.file_extractor import _ocr_with_claude
    text, why = _ocr_with_claude(
        images, kind=f"{len(images)} still frames sampled across a video the "
                     f"learner submitted as coursework")
    return (text, "") if text else ("", why or "frames carried nothing readable")
