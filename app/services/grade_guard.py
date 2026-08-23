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

from typing import Optional, Tuple

# Exception text that means "the model never answered", as opposed to "the
# model answered something we could not use". The first must never fall
# through to a second, weaker marker; the second may.
_TRANSPORT_MARKERS = (
    "all ai models are currently unavailable",
    "returned no structured result",
    "model returned empty content",
    "timeout", "timed out", "connection", "read operation",
    "rate limit", "429", "500", "502", "503", "504",
    "overloaded", "service unavailable", "api key", "authentication",
    "insufficient_quota", "credit balance",
)


def is_transport_failure(error: BaseException) -> bool:
    """Did the model never answer? Pure.

    A transport failure is OUR outage. Falling back to a weaker marker and
    writing its number would turn our downtime into the learner's grade —
    quietly, because the fallback succeeds and nothing looks broken.
    """
    text = f"{type(error).__name__}: {error}".lower()
    return any(marker in text for marker in _TRANSPORT_MARKERS)


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
