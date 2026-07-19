# app/services/media_service.py
# The agent's SENSES (brain spec, slice 3): watch a session video ONCE,
# produce a clean pure-content transcript, remember it forever.
#
# Pipeline per video (runs in the background, never blocks a review):
#   1. Resolve the source. YouTube -> existing captions first (free, instant).
#      Anything else (Cloudinary / MP4 / LMS storage) -> download.
#   2. ffmpeg: extract mono 16 kHz audio, segment into 10-minute chunks
#      (keeps each transcription call under API size limits).
#   3. Whisper via an OpenAI-compatible API. Env-swappable provider:
#         TRANSCRIBE_API_KEY   (required — OpenAI or Groq key)
#         TRANSCRIBE_BASE_URL  (default https://api.openai.com/v1;
#                               Groq: https://api.groq.com/openai/v1)
#         TRANSCRIBE_MODEL     (default whisper-1; Groq: whisper-large-v3-turbo)
#      A BFSI glossary rides along as the Whisper prompt so SARFAESI, FOIR,
#      NBFC etc. transcribe correctly.
#   4. Filter to pure content: chunked AI pass that removes introductions,
#      pleasantries, jokes without teaching value, filler words, housekeeping
#      and audience chatter — conservative rule: when in doubt, KEEP.
#   5. Store once in session_transcripts, keyed by source URL hash. The
#      session knowledge pack rebuilds automatically on next touch because
#      its `transcript` source (and therefore its content hash) changed.
#
# Failure honesty: every failure path records status='failed' with the real
# reason. A session with no obtainable transcript reviews from metadata —
# stated, never silently pretended otherwise.

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from typing import Optional

import httpx

from app.database import query, execute
from app.services import ai_service

_TABLE = "session_transcripts"
_table_ready = False

# Domain terms Whisper commonly mangles — passed as the transcription prompt.
BFSI_GLOSSARY = (
    "SARFAESI, FOIR, DSCR, LTV, NBFC, NPA, KYC, e-KYC, RBI, SEBI, NABARD, "
    "UPI, CIBIL, EMI, CASA, MCLR, repo rate, drawing power, cash credit, "
    "FinTech, InsurTech, RegTech, Aadhaar, PAN, demat, ULIP, PMJDY"
)

SEGMENT_SECONDS = 600          # 10-minute audio chunks
MAX_VIDEO_MB = 800             # refuse absurd downloads; logged, not silent
PROCESSING_TIMEOUT_MIN = 45    # a 'processing' row older than this may retry

_CLEAN_INSTRUCTIONS = """You are AiRev's transcript filter. Below is a chunk of a raw industry-session transcript. Rewrite it as PURE TEACHING CONTENT:

REMOVE: introductions and welcomes, thank-yous, housekeeping ("can you see my screen", mic checks), audience logistics, filler words (um, uh, you know, basically, actually when meaningless), verbatim repetitions, small talk and jokes that carry no teaching point.
KEEP (verbatim wherever possible): every concept, explanation, framework, example, case reference, number, quote and piece of career advice. A joke or story that ILLUSTRATES a teaching point stays. When in doubt, KEEP.
Do NOT summarize, do NOT paraphrase substance, do NOT add anything. Output only the filtered transcript text."""


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    execute("""
        CREATE TABLE IF NOT EXISTS session_transcripts (
            session_id       INT PRIMARY KEY,
            source_url       VARCHAR(1024),
            source_hash      CHAR(32),
            status           VARCHAR(16) NOT NULL DEFAULT 'processing',
            raw_chars        INT DEFAULT 0,
            clean_transcript LONGTEXT,
            method           VARCHAR(32),
            error            TEXT,
            started_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at     DATETIME NULL
        )
    """)
    _table_ready = True


# ─── Public API ──────────────────────────────────────────────────────────────

def get_clean_transcript(session_id: int) -> Optional[str]:
    """Ready transcript or None. Never triggers processing."""
    _ensure_table()
    rows = query(
        f"SELECT clean_transcript FROM {_TABLE} "
        f"WHERE session_id=%s AND status='ready' LIMIT 1", (session_id,))
    return rows[0]["clean_transcript"] if rows else None


def ensure_processing(session_id: int, video_url: Optional[str],
                      background_tasks=None) -> str:
    """Fully-automatic trigger. Returns current status:
    'ready' | 'processing' | 'started' | 'failed' | 'no_video' | 'no_runner'.
    Concurrency-guarded: a fresh 'processing' row blocks duplicate starts."""
    if not video_url:
        return "no_video"
    if not os.getenv("TRANSCRIBE_API_KEY") and not _is_youtube(video_url):
        # Honest capability statement — no key, no non-YouTube transcription.
        print(f"ℹ️  session {session_id}: video present but TRANSCRIBE_API_KEY "
              f"not set — captions-only mode")

    _ensure_table()
    rows = query(
        f"SELECT status, source_hash, "
        f"TIMESTAMPDIFF(MINUTE, started_at, NOW()) AS age_min "
        f"FROM {_TABLE} WHERE session_id=%s LIMIT 1", (session_id,))
    url_hash = hashlib.md5(video_url.encode()).hexdigest()

    if rows:
        r = rows[0]
        if r["source_hash"] == url_hash:
            if r["status"] == "ready":
                return "ready"
            if r["status"] == "processing" and (r["age_min"] or 0) < PROCESSING_TIMEOUT_MIN:
                return "processing"
            # failed, or processing timed out -> retry below

    if background_tasks is None:
        return "no_runner"
    execute(
        f"REPLACE INTO {_TABLE} (session_id, source_url, source_hash, status, started_at) "
        f"VALUES (%s, %s, %s, 'processing', NOW())",
        (session_id, video_url[:1024], url_hash))
    background_tasks.add_task(process_video, session_id, video_url)
    print(f"🎬 Session {session_id}: watch started ({video_url[:80]})")
    return "started"


def process_video(session_id: int, video_url: str) -> None:
    """The full watch: resolve → transcribe → filter → store. Background-run."""
    try:
        raw, method = _obtain_raw_transcript(video_url)
        if not raw or len(raw) < 200:
            raise Exception(f"transcript too short ({len(raw or '')} chars) via {method}")
        clean = _filter_to_pure_content(raw)
        execute(
            f"UPDATE {_TABLE} SET status='ready', raw_chars=%s, "
            f"clean_transcript=%s, method=%s, error=NULL, completed_at=NOW() "
            f"WHERE session_id=%s",
            (len(raw), clean, method, session_id))
        print(f"✅ Session {session_id} watched: {len(raw)} raw → "
              f"{len(clean)} clean chars via {method}")
    except Exception as e:
        print(f"❌ Session {session_id} watch FAILED: {e}")
        execute(
            f"UPDATE {_TABLE} SET status='failed', error=%s, completed_at=NOW() "
            f"WHERE session_id=%s", (str(e)[:1000], session_id))


def watch_status(session_id: int) -> dict:
    _ensure_table()
    rows = query(
        f"SELECT status, method, raw_chars, error, started_at, completed_at "
        f"FROM {_TABLE} WHERE session_id=%s LIMIT 1", (session_id,))
    if not rows:
        return {"status": "absent"}
    r = rows[0]
    return {"status": r["status"], "method": r.get("method"),
            "rawChars": r.get("raw_chars"), "error": r.get("error"),
            "startedAt": str(r["started_at"]) if r.get("started_at") else None,
            "completedAt": str(r["completed_at"]) if r.get("completed_at") else None}


# ─── Stage 1: obtain raw transcript ──────────────────────────────────────────

_YT_RE = re.compile(
    r"(?:youtube\.com/(?:watch\?v=|embed/|shorts/)|youtu\.be/)([A-Za-z0-9_-]{11})")


def _is_youtube(url: str) -> bool:
    return bool(_YT_RE.search(url or ""))


def _obtain_raw_transcript(video_url: str) -> tuple[str, str]:
    """Returns (raw_text, method). Order: YouTube captions → download+Whisper."""
    m = _YT_RE.search(video_url)
    if m:
        try:
            return _youtube_captions(m.group(1)), "youtube_captions"
        except Exception as e:
            print(f"ℹ️  YouTube captions unavailable ({e}) — "
                  f"server-side YouTube download is unreliable; marking failed")
            raise Exception(
                "YouTube video without accessible captions. Upload the video "
                "file or transcript to the LMS for this session.")
    return _download_and_whisper(video_url), "whisper"


def _youtube_captions(video_id: str) -> str:
    from youtube_transcript_api import YouTubeTranscriptApi
    segments = YouTubeTranscriptApi().fetch(
        video_id, languages=["en", "en-IN", "hi"])
    return " ".join(s.text for s in segments if s.text).strip()


def _download_and_whisper(video_url: str) -> str:
    if not os.getenv("TRANSCRIBE_API_KEY"):
        raise Exception("TRANSCRIBE_API_KEY not set — cannot transcribe non-YouTube video")

    workdir = tempfile.mkdtemp(prefix="airev_media_")
    try:
        video_path = os.path.join(workdir, "video")
        _download(video_url, video_path)
        chunks = _extract_audio_chunks(video_path, workdir)
        texts = [_whisper_chunk(c) for c in chunks]
        return " ".join(t for t in texts if t).strip()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _download(url: str, dest: str) -> None:
    with httpx.stream("GET", url, follow_redirects=True, timeout=300.0) as resp:
        if resp.status_code >= 400:
            raise Exception(f"video download HTTP {resp.status_code}")
        size = 0
        with open(dest, "wb") as f:
            for part in resp.iter_bytes():
                size += len(part)
                if size > MAX_VIDEO_MB * 1024 * 1024:
                    raise Exception(f"video exceeds {MAX_VIDEO_MB}MB cap")
                f.write(part)
    print(f"   downloaded {size // (1024*1024)}MB")


def _extract_audio_chunks(video_path: str, workdir: str) -> list[str]:
    """Mono 16 kHz mp3, segmented — ffmpeg does both in one pass."""
    pattern = os.path.join(workdir, "chunk_%03d.mp3")
    cmd = ["ffmpeg", "-y", "-i", video_path, "-vn", "-ac", "1", "-ar", "16000",
           "-b:a", "48k", "-f", "segment", "-segment_time", str(SEGMENT_SECONDS),
           pattern]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise Exception(f"ffmpeg failed: {proc.stderr[-400:]}")
    chunks = sorted(
        os.path.join(workdir, f) for f in os.listdir(workdir)
        if f.startswith("chunk_") and f.endswith(".mp3"))
    if not chunks:
        raise Exception("ffmpeg produced no audio — is the URL actually a video?")
    print(f"   {len(chunks)} audio chunks of ≤{SEGMENT_SECONDS//60}min")
    return chunks


def _whisper_chunk(chunk_path: str) -> str:
    base = os.getenv("TRANSCRIBE_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    model = os.getenv("TRANSCRIBE_MODEL", "whisper-1")
    with open(chunk_path, "rb") as f:
        resp = httpx.post(
            f"{base}/audio/transcriptions",
            headers={"Authorization": f"Bearer {os.getenv('TRANSCRIBE_API_KEY')}"},
            files={"file": (os.path.basename(chunk_path), f, "audio/mpeg")},
            data={"model": model, "prompt": BFSI_GLOSSARY, "response_format": "text"},
            timeout=300.0,
        )
    if resp.status_code >= 400:
        raise Exception(f"transcription HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.text.strip()


# ─── Stage 2: filter to pure content ─────────────────────────────────────────

CLEAN_CHUNK_CHARS = 9000   # ~2.2k tokens per cleaning call


def _filter_to_pure_content(raw: str) -> str:
    """Chunked AI cleaning pass. On a chunk failure the RAW chunk is kept —
    losing filler beats losing teaching content."""
    chunks = [raw[i:i + CLEAN_CHUNK_CHARS]
              for i in range(0, len(raw), CLEAN_CHUNK_CHARS)]
    cleaned: list[str] = []
    for idx, chunk in enumerate(chunks):
        try:
            out = ai_service.call_claude(
                f"{_CLEAN_INSTRUCTIONS}\n\n--- RAW TRANSCRIPT CHUNK "
                f"{idx + 1}/{len(chunks)} ---\n{chunk}",
                max_tokens=4000,
                system="You filter transcripts. Output only the filtered text.",
            )
            cleaned.append(out.strip())
        except Exception as e:
            print(f"⚠️ clean pass failed on chunk {idx + 1}/{len(chunks)}: {e} — keeping raw")
            cleaned.append(chunk)
    return "\n\n".join(cleaned)
