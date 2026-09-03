# app/services/grade_guard.py
# ---------------------------------------------------------------------------
# ONE RULE: IF THE AGENT DID NOT READ IT, THE AGENT DOES NOT MARK IT.
#
# Every wrong mark this system has produced shares a shape. Something failed
# on OUR side — a link that would not open, a file that would not parse, a
# model call that came back empty or truncated — and a number was written
# anyway. The learner then reads a score that measures our reach, not their
# work, and no amount of careful wording in the feedback repairs that.
#
# Live cases this module exists for:
#   - 21 Day-04 rows marked 0.0-4.7 while their own feedback said the Notion
#     page could not be read (22 Aug).
#   - 17 Day-07 links that returned Google's signed-out shell and were about
#     to be marked as dashboards (23 Aug).
#   - 11 Day-04 rows carrying a bare "You scored 0 out of 10." with no reasons
#     at all — a mark with nothing behind it (still undiagnosed; this guard
#     refuses the shape whatever produced it).
#
# The rule is deliberately asymmetric. A withheld mark can become a real score
# the same evening, once the learner resubmits or we fix our end. A recorded
# wrong mark is already in front of them.
#
# Everything here is pure: no I/O, no DB, no model. The routes decide what to
# DO with a refusal; this module only decides whether a grade may exist.
# ---------------------------------------------------------------------------

import os
from typing import Optional, Tuple

# Exception text that means "the model never answered", as opposed to "the
# model answered something we could not use". The first must never fall
# through to a second, weaker marker; the second may.
_TRANSPORT_MARKERS = (
    "all ai models are currently unavailable",
    "returned no structured result",
    "model returned empty content",
    "timeout", "timed out", "connection", "read operation",
    "rate limit", "rate_limit", "429", "500", "502", "503", "504",
    "overloaded", "service unavailable", "api key", "authentication",
    "insufficient_quota", "credit balance",
    # BILLING AND QUOTA (03 Sep 2026). Live on the Space: the Anthropic monthly
    # spend cap tripped, every OCR call answered HTTP 400 "You have reached your
    # specified API usage limits. You will regain access on 2026-10-01", and
    # because none of the markers above matched, a learner was told on her own
    # card to re-attach a file that was perfectly fine — with the raw API error
    # pasted in beside it. Our account running out is ours.
    "usage limit", "spend limit", "regain access", "billing", "quota",
    "insufficient", "exceeded",
    # RAW ERROR SIGNATURES. Whatever the words, text that quotes an exception
    # class, an HTTP error envelope or a request id came from our side of the
    # wire. A learner's file cannot produce a request_id.
    "error code:", "request_id", "invalid_request_error", "badrequesterror",
    "apistatuserror", "apiconnectionerror", "internalservererror",
    "ocr failed on", "transcription failed", "could not process this recording",
)


def reads_as_our_outage(text: str) -> bool:
    """Does this failure text describe OUR side going down? Pure.

    Extracted 02 Sep 2026 because a second caller needed it. The intake layer
    records WHY a file could not be read, and that string is often our own
    provider refusing: "OCR failed on startupapi: Error code: 503 - provider
    capacity is temporarily unavailable". Telling a learner to re-attach a file
    that is perfectly fine — because our OCR was down — is blaming them for our
    outage, in writing, on their own review card.

    Since 03 Sep the list is deliberately generous: a raw provider error of ANY
    shape is ours. The cost of a false positive is one row waiting for the next
    sweep; the cost of a false negative is a learner blamed in writing.
    """
    blob = str(text or "").lower()
    return any(marker in blob for marker in _TRANSPORT_MARKERS)


# What a learner may be TOLD when a read fails and it is genuinely their file.
# Anything that looks like machinery — an exception class, a JSON envelope, a
# request id, a stack — is replaced. The reason is kept on the row for staff
# (the route logs it); it is never the learner's to decode.
_MACHINERY = ("error code", "request_id", "{'type'", '{"type"', "traceback",
              "exception", "error:", "errno", "http ", "status_code")


def learner_facing(why: str) -> str:
    """The reason a learner sees, or a plain sentence when the real one is
    machine noise. Pure."""
    text = str(why or "").strip()
    if not text:
        return "the file could not be read"
    low = text.lower()
    if any(m in low for m in _MACHINERY) or reads_as_our_outage(text):
        return "the file could not be read"
    # Even a clean reason should be short on a card.
    return text if len(text) <= 120 else text[:117].rstrip() + "…"


def is_transport_failure(error: BaseException) -> bool:
    """Did the model never answer? Pure.

    A transport failure is OUR outage. Falling back to a weaker marker and
    writing its number would turn our downtime into the learner's grade —
    quietly, because the fallback succeeds and nothing looks broken.
    """
    return reads_as_our_outage(f"{type(error).__name__}: {error}")


# What the pipeline writes into a criterion when the model gave it nothing.
# A row saying "No assessment available" beside evidence: [] is the ABSENCE of
# a judgement wearing the shape of one — and it was passing the evidence check
# because it carried percentage: 0.
#
# Live case (Day 07, student 220, 23 Aug): 1,057 words read, one criterion,
# judgment "No assessment available.", evidence [], every feedback field empty,
# authorship "signals unavailable" — and a stored 0.00/10. That is the bare-
# zero shape from Day 04, caught in the act.
_PLACEHOLDER_JUDGMENTS = ("no assessment available", "not assessed",
                          "unavailable", "n/a", "none")


def _is_real_judgement(c: dict) -> bool:
    """Does this criterion row carry an actual judgement? Pure."""
    if c.get("score_pct") is None and c.get("percentage") is None:
        return False
    judgment = str(c.get("judgment") or c.get("note") or "").strip().lower()
    if not judgment:
        # Some writers store no per-row prose at all; the score alone counts,
        # provided the row is not one of the placeholder shapes below.
        return True
    return not any(judgment.startswith(p) or judgment.rstrip(".") == p
                   for p in _PLACEHOLDER_JUDGMENTS)


def has_model_evidence(criteria: Optional[list]) -> bool:
    """Did the marker actually judge anything? Pure.

    True when at least one criterion carries a REAL judgement. An empty list,
    rows with no score, or rows whose only judgement is a placeholder all mean
    the number below them was computed from nothing.
    """
    return any(_is_real_judgement(c) for c in (criteria or [])
               if isinstance(c, dict))


# How much of the task must carry a real verdict before a number is honest.
# Below this, "the score" is an extrapolation from a minority of the brief,
# multiplied up to look like a whole mark — which is precisely the shape that
# produced 0.00 and 0.70 for two submissions described identically.
MIN_JUDGED_SHARE = float(os.getenv("MIN_JUDGED_SHARE", "0.5"))


def judged_share(criteria: Optional[list]) -> float:
    """Fraction of the task's weight that received a verdict. Pure.

    Rows written before requirement-level judging carry no `unjudged` key and
    count as judged, so nothing about older reviews changes.
    """
    total = judged = 0.0
    for c in criteria or []:
        if not isinstance(c, dict):
            continue
        try:
            weight = float(c.get("outOf") or c.get("maxScore") or 0) or 1.0
        except (TypeError, ValueError):
            weight = 1.0
        total += weight
        if not c.get("unjudged"):
            judged += weight
    return (judged / total) if total else 0.0


def review_is_empty(result: Optional[dict]) -> bool:
    """A review with no feedback of any kind did not happen. Pure.

    Student 220's row: strengths [], improvements [], feedbackPoints [],
    detailedFeedback "", missingConcepts [], encouragement "". A learner
    cannot act on that, and neither can a mentor defend it.
    """
    if not result:
        return True
    prose = (str(result.get("detailedFeedback") or "").strip()
             + str(result.get("hardTruth") or "").strip()
             + str(result.get("summary_body") or "").strip())
    lists = (list(result.get("strengths") or [])
             + list(result.get("improvements") or [])
             + list(result.get("feedbackPoints") or []))
    return not prose and not lists


def read_failed(manifest: str) -> bool:
    """Does the intake manifest record something we could not read? Pure."""
    blob = (manifest or "").lower()
    return ("could not be read" in blob
            or "could not be retrieved" in blob
            or "not rendered" in blob)


def may_write_grade(*, criteria: Optional[list], manifest: str = "",
                    proposed_score: Optional[float] = None,
                    words_read: int = 0,
                    review: Optional[dict] = None) -> Tuple[bool, str]:
    """(allowed, reason_if_not) — the single gate before any mark is stored.

    Refuses three shapes, each one a live incident:

    1. NO EVIDENCE AT ALL. No criterion carries a model score, so whatever
       number was computed came from the fallbacks, not from a reading of the
       work. (The bare "0 out of 10 with no reasons" rows.)

    2. A ZERO ON WORK WE COULD NOT READ. A failed read plus a zero is our
       failure wearing the learner's grade. A zero is only honest when we
       held the work and found nothing in it.

    3. A MARK WITH NOTHING BEHIND IT. Zero words reached the marker, yet a
       score exists. There is no reading of an empty input that yields a
       defensible number.

    A LOW score on work we DID read is left alone — that is a judgement, and
    judgements are what this system is for.
    """
    if review is not None and review_is_empty(review):
        return False, ("the reviewer produced no feedback at all — no "
                       "strengths, no improvements, nothing written — so "
                       "there is no review behind this number")
    if not has_model_evidence(criteria):
        return False, ("the reviewer returned no judgement for any part of "
                       "this task, so any score would be computed from "
                       "nothing")
    share = judged_share(criteria)
    if criteria and share < MIN_JUDGED_SHARE:
        return False, (f"only {round(share * 100)}% of what this task asks for "
                       f"received a verdict — a score built from that much of "
                       f"the brief is an extrapolation, not a mark")
    if words_read <= 0:
        return False, ("nothing readable reached the reviewer, so there is "
                       "no work to put a number on")
    try:
        score = float(proposed_score) if proposed_score is not None else None
    except (TypeError, ValueError):
        score = None
    if score is not None and score <= 0 and read_failed(manifest):
        return False, ("part of this submission could not be read, and a zero "
                       "on unread work measures our reach rather than the "
                       "learner's effort")
    return True, ""
