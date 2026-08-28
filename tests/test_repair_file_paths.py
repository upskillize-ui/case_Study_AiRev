"""The 384 rows that hold a file NAME with no file PATH (28 Aug 2026).

314 of them still carry a non-zero file_size, so the upload finished and only
the URL was lost — those files are still in Cloudinary under
`upskillize/coursework/cw_<users.id>_<epoch_ms>`. These tests pin the matching
rules, because a WRONG match would attach one learner's work to another's
submission, which is worse than leaving the row unrepaired.
"""
from datetime import datetime, timezone

import pytest

from tools.repair_file_paths import best_match, MAX_SKEW_SECONDS


def asset(bytes_, created, fmt="png", url="https://res.cloudinary.com/x.png"):
    return {"bytes": bytes_, "created_at": created, "format": fmt,
            "secure_url": url}


SUBMITTED = "2026-08-27 10:00:00"


def _at(offset_seconds: int) -> str:
    base = datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    return datetime.fromtimestamp(base + offset_seconds,
                                  tz=timezone.utc).isoformat().replace("+00:00", "Z")


def test_no_assets_is_never_a_guess():
    got, why = best_match([], 1234, SUBMITTED, "shot.png")
    assert got is None and "no assets" in why


def test_a_unique_byte_count_settles_it():
    right = asset(50_000, _at(-30))
    wrong = asset(90_000, _at(-20))
    got, why = best_match([wrong, right], 50_000, SUBMITTED, "shot.png")
    assert got is right and why == "exact size"


def test_two_files_of_the_same_size_break_by_upload_time():
    near = asset(50_000, _at(-40))
    far = asset(50_000, _at(-3000))
    got, why = best_match([far, near], 50_000, SUBMITTED, "shot.png")
    assert got is near and "nearest in time" in why


def test_without_a_size_the_extension_must_agree():
    pdf = asset(0, _at(-30), fmt="pdf")
    png = asset(0, _at(-90), fmt="png")
    got, why = best_match([pdf, png], 0, SUBMITTED, "design.png")
    assert got is png, "matched a PDF to a .png submission"
    assert "same extension" in why


def test_an_asset_from_another_day_is_refused():
    """The same learner's NEXT day's upload must never be attached here."""
    yesterday = asset(0, _at(-MAX_SKEW_SECONDS - 60), fmt="png")
    got, why = best_match([yesterday], 0, SUBMITTED, "design.png")
    assert got is None and "within an hour" in why


def test_a_size_mismatch_far_from_the_time_is_refused():
    got, why = best_match([asset(11, _at(-99999), fmt="png")],
                          50_000, SUBMITTED, "design.png")
    assert got is None


def test_size_beats_time():
    """A byte-for-byte match is proof; proximity is only ever a tiebreak."""
    exact_but_older = asset(50_000, _at(-900))
    wrong_size_closer = asset(1, _at(-1))
    got, _ = best_match([wrong_size_closer, exact_but_older],
                        50_000, SUBMITTED, "shot.png")
    assert got is exact_but_older


@pytest.mark.parametrize("stamp", [
    "2026-08-27T10:00:00Z",
    "2026-08-27 10:00:00",
    datetime(2026, 8, 27, 10, 0, 0, tzinfo=timezone.utc),
])
def test_every_timestamp_shape_the_two_systems_emit(stamp):
    """Cloudinary sends ISO-8601 Z, MySQL sends a datetime. A parse failure
    would silently score every gap as identical and match by luck."""
    got, _ = best_match([asset(50_000, _at(-10))], 50_000, stamp, "shot.png")
    assert got is not None
