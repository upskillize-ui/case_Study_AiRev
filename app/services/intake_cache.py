# app/services/intake_cache.py
# ---------------------------------------------------------------------------
# READ A FILE ONCE, EVER.
#
# Why this exists (03 Sep 2026). Every re-review re-ran intake from scratch:
# OCR on every image and PDF page, Whisper on every recording, a vision call
# per sampled video frame, a browser render for every link. The submission had
# not changed. The rules had, or the provider had come back, or an admin had
# pressed a button — and the most expensive part of the whole pipeline ran
# again to produce byte-identical text.
#
# The submit route already had half an answer: it writes the assembled intake
# into `notes`, and from_stored_submission() reuses it. But rows that arrive
# through the LMS queue never pass that route — their notes stay raw, and
# every re-review pays full price. This cache is the other half, and it does
# not touch `notes`, which stays the learner's own words for the admin to read.
#
# WHAT IS CACHED. The full Artefact list, pictures included, so a cached
# re-review is byte-for-byte what a fresh one would have seen. Text alone
# would make the marker blind on picture work the second time round — which
# is the exact failure v9 fixed for the first time round.
#
# WHEN IT IS INVALID. The key is a fingerprint of everything the learner
# controls: notes, file path, file name. A resubmit changes at least one and
# misses. INTAKE_VERSION is folded in too, so a change to how we READ (a new
# renderer, a fixed extractor) invalidates every entry at once instead of
# serving yesterday's mistakes for ever.
#
# NEVER RAISES. A cache that can fail a review is a liability, not a saving:
# every public function returns a harmless default on any error.
# ---------------------------------------------------------------------------

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from typing import List, Optional

from app.database import tquery, texecute
from app.utils.submission_intake import Artefact

TABLE = "airev_intake_cache"

# Bump when intake itself changes shape or behaviour — a better OCR, a fixed
# link path — so stale reads are re-done rather than served.
INTAKE_VERSION = 3       # 3 = partial reads no longer cached; every v2 entry
                         #     may hold an unopened link, so all must miss (04 Sep)

_tables_ready: set = set()


def ensure_table(tenant) -> None:
    key = getattr(tenant, "id", str(tenant))
    if key in _tables_ready:
        return
    texecute(tenant, f"""
        CREATE TABLE IF NOT EXISTS {TABLE} (
            submission_id INT          NOT NULL PRIMARY KEY,
            fingerprint   CHAR(64)     NOT NULL,
            artefacts     MEDIUMTEXT   NOT NULL,
            items         SMALLINT     NOT NULL DEFAULT 0,
            bytes         INT          NOT NULL DEFAULT 0,
            created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
            updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP
                                       ON UPDATE CURRENT_TIMESTAMP
        )
    """)
    _tables_ready.add(key)


def fingerprint(notes: str, file_path: str, file_name: str) -> str:
    """What the learner controls, plus how we read it. Pure."""
    raw = "\x1f".join([
        f"v{INTAKE_VERSION}",
        (notes or "").strip(),
        (file_path or "").strip(),
        (file_name or "").strip(),
    ])
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


def _dump(artefacts: List[Artefact]) -> str:
    return json.dumps([asdict(a) for a in artefacts], ensure_ascii=False)


def _load(blob: str) -> List[Artefact]:
    out = []
    for d in json.loads(blob or "[]"):
        out.append(Artefact(
            kind=d.get("kind", "file"), label=d.get("label", ""),
            text=d.get("text", ""), note=d.get("note", ""),
            confirmed=bool(d.get("confirmed", True)),
            image_b64=d.get("image_b64", ""), media_type=d.get("media_type", ""),
            extra_images=list(d.get("extra_images") or []),
        ))
    return out


def recall(tenant, submission_id: int, fp: str) -> Optional[List[Artefact]]:
    """The artefacts a previous review assembled for exactly this input, or
    None. A miss on a changed fingerprint is the point; a miss on an error is
    the safe default."""
    try:
        ensure_table(tenant)
        rows = tquery(tenant,
                      f"SELECT fingerprint, artefacts FROM {TABLE} WHERE submission_id = %s",
                      (int(submission_id),)) or []
        if not rows or rows[0]["fingerprint"] != fp:
            return None
        arts = _load(rows[0]["artefacts"])
        return arts or None
    except Exception as e:
        print(f"[INTAKE CACHE] recall failed for {submission_id}: {e}")
        return None


def remember(tenant, submission_id: int, fp: str, artefacts: List[Artefact]) -> bool:
    """Store what intake produced. Only worth storing when something was
    actually READ: caching a row of nothing would make a transient failure
    permanent. Returns True on write."""
    if not artefacts or not any(a.readable or a.image_b64 for a in artefacts):
        return False
    # A PARTIAL READ IS A FAILED READ (04 Sep 2026). "Something was read" let a
    # typed sentence beside an UNOPENED link into the cache — and every
    # re-review then served the unopened link back from here, whatever the
    # renderer could do by then. Only a read with every item open is worth
    # keeping; a row with one unread item is re-read next time, at the price
    # of one extraction, which is the cheap side of that trade.
    if any(not (a.readable or a.image_b64) for a in artefacts):
        return False
    try:
        ensure_table(tenant)
        blob = _dump(artefacts)
        texecute(tenant, f"""
            INSERT INTO {TABLE} (submission_id, fingerprint, artefacts, items, bytes)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                fingerprint = VALUES(fingerprint),
                artefacts   = VALUES(artefacts),
                items       = VALUES(items),
                bytes       = VALUES(bytes)
        """, (int(submission_id), fp, blob, len(artefacts), len(blob)))
        return True
    except Exception as e:
        print(f"[INTAKE CACHE] remember failed for {submission_id}: {e}")
        return False


def forget(tenant, submission_id: int) -> None:
    """Drop one entry. Used when a row is deleted or an admin wants a clean read."""
    try:
        ensure_table(tenant)
        texecute(tenant, f"DELETE FROM {TABLE} WHERE submission_id = %s", (int(submission_id),))
    except Exception as e:
        print(f"[INTAKE CACHE] forget failed for {submission_id}: {e}")
