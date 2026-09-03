# tests/test_stale_assembly.py
# ---------------------------------------------------------------------------
# THE STALE ASSEMBLY (04 Sep 2026).
#
# A review writes its manifest + content into `notes`. A re-review reuses that
# stored assembly whole (right: re-extracting nests one manifest inside
# another). But an assembly whose manifest says an item "could not be read" is
# a record of a FAILED read, and reusing it re-refuses the row with the same
# words for ever, whatever was fixed in between. 157 requeued rows were about
# to walk into that loop.
#
# These pin: a complete assembly is reused; an incomplete one is re-read from
# source; and only the learner's own TYPED TEXT survives from the old copy —
# never the manifest, never our own OCR.
#
# records_failed_read / typed_text_from existed since 21 Aug and were never
# called; these pin them now that the regrade route uses them.
#
# Pure. render() itself produces the stored shape, so the test cannot drift
# from the format the route writes.
# ---------------------------------------------------------------------------

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

from app.utils import submission_intake as intake
from app.utils.submission_intake import Artefact


def _stored(artefacts):
    """What the route writes to `notes`: manifest + content, as one blob."""
    manifest, content = intake.render(artefacts)
    return manifest + "\n\n" + content


URL = "https://www.canva.com/design/DAF12/view"


def test_a_complete_assembly_is_not_stale():
    stored = _stored([
        Artefact(kind="link", label=URL, text="word " * 300, confirmed=True),
        Artefact(kind="typed text", label="typed answer", text="I built this for Day 6.", confirmed=True),
    ])
    manifest, content = intake.from_stored_submission(stored)
    assert manifest
    assert not intake.records_failed_read(manifest)


def test_an_unread_link_makes_the_assembly_stale():
    stored = _stored([
        Artefact(kind="link", label=URL, confirmed=True,
                 note="the link returned a web page, not the file itself"),
        Artefact(kind="typed text", label="typed answer", text=f"Here is my design {URL}", confirmed=True),
    ])
    manifest, content = intake.from_stored_submission(stored)
    assert intake.records_failed_read(manifest)


def test_an_unretrievable_file_makes_the_assembly_stale():
    stored = _stored([
        Artefact(kind="image", label="cw_1.jpg", confirmed=False, note="download HTTP 404"),
        Artefact(kind="typed text", label="typed answer", text="see attached", confirmed=True),
    ])
    manifest, _ = intake.from_stored_submission(stored)
    assert intake.records_failed_read(manifest)


def test_only_the_learners_typed_words_are_carried_forward():
    stored = _stored([
        Artefact(kind="image", label="cw_1.jpg", text="OCR TEXT THAT IS OURS", confirmed=True),
        Artefact(kind="link", label=URL, confirmed=True, note="could not be read"),
        Artefact(kind="typed text", label="typed answer", text=f"My balance sheet notes. {URL}", confirmed=True),
    ])
    manifest, content = intake.from_stored_submission(stored)
    typed = intake.typed_text_from(content)
    assert typed == f"My balance sheet notes. {URL}"
    assert "OCR TEXT" not in typed
    assert "MANIFEST" not in typed
    # The URL the learner pasted is still findable, so from_links_in re-opens it.
    assert intake.find_urls(typed) == [URL]


def test_typed_text_survives_clean_text_newline_collapse():
    # Stored rows come back through clean_text(), which folds newline runs.
    stored = _stored([
        Artefact(kind="link", label=URL, confirmed=True, note="could not be read"),
        Artefact(kind="typed text", label="typed answer", text="line one\n\nline two", confirmed=True),
    ])
    flattened = " ".join(stored.split("\n"))
    manifest, content = intake.from_stored_submission(flattened)
    assert intake.records_failed_read(manifest)
    assert "line one" in intake.typed_text_from(content)
    assert "line two" in intake.typed_text_from(content)


def test_no_typed_block_yields_empty_not_the_manifest():
    stored = _stored([
        Artefact(kind="link", label=URL, confirmed=True, note="could not be read"),
    ])
    manifest, content = intake.from_stored_submission(stored)
    assert intake.records_failed_read(manifest)
    assert intake.typed_text_from(content) == ""


def test_raw_learner_notes_are_untouched():
    # Plain typed notes carry no manifest: not stale, nothing to strip.
    assert intake.from_stored_submission("Just my answer, nothing else.") is None
    assert not intake.records_failed_read("")
    assert intake.typed_text_from("Just my answer") == ""
