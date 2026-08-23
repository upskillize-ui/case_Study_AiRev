"""Why a stored submission file could not be read.

Day 07: forty-odd rows came back "no_readable_content" and nobody could say
why. TWO completely different faults were hiding under one label —

    student 217 | 0 chars + Link submission            <- nothing was stored
    student 317 | 0 chars + tata_motors_sales_report.pdf  <- a real file

— and telling them apart by guessing has already cost a week. One of them is
an LMS bug no agent change can fix; the other is ours.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from file_check import (DOWNLOAD_FAILED, EMPTY_ROW, NO_LINK, READS_FINE,
                        UNREADABLE, is_label_not_a_url, verdict_for)

CLOUDINARY = "https://res.cloudinary.com/dirgd2vmv/image/upload/v178741004"


# ── the label that is not a link ────────────────────────────────────────────

@pytest.mark.parametrize("label", [
    "Link submission", "link submission", "LINK SUBMISSION",
    "Link", "URL", "submission link",
])
def test_the_lms_caption_is_recognised(label):
    assert is_label_not_a_url(label, label) is True


def test_a_real_url_is_not_a_label():
    assert is_label_not_a_url(CLOUDINARY, "dashboard.png") is False


def test_a_server_path_is_not_a_label():
    assert is_label_not_a_url("/uploads/2026/dashboard.pdf", "dashboard.pdf") is False


def test_a_real_file_whose_NAME_contains_the_word_link_is_not_swept_up():
    """'Link submission notes.pdf' is a document, not the LMS caption. Matched
    exactly, never loosely — a false positive here would tell a learner their
    work was never stored when it was."""
    assert is_label_not_a_url(CLOUDINARY, "Link submission notes.pdf") is False


def test_nothing_stored_is_not_a_label():
    assert is_label_not_a_url("", "") is False


# ── the four verdicts ───────────────────────────────────────────────────────

def test_a_caption_is_reported_as_an_LMS_problem_not_a_download_failure():
    """Ordering matters: a label will of course fail to download, and calling
    that a download failure sends everyone hunting the wrong fault."""
    verdict, detail = verdict_for("Link submission", "Link submission",
                                  0, "", "HTTP 404")
    assert verdict == NO_LINK
    assert "LMS side" in detail


def test_a_row_with_nothing_at_all_is_its_own_verdict():
    assert verdict_for("", "", 0, "", "")[0] == EMPTY_ROW


def test_typed_text_alone_is_not_an_empty_row():
    """They wrote an answer; the file is simply absent."""
    assert verdict_for("", "", 1200, "", "")[0] != EMPTY_ROW


def test_a_file_that_reads_is_reported_as_fine():
    verdict, detail = verdict_for(CLOUDINARY, "dashboard.png", 0,
                                  "Q3 revenue 6.8 crore across three charts", "")
    assert verdict == READS_FINE and "7 words read" in detail


@pytest.mark.parametrize("why", [
    "HTTP 403: forbidden", "HTTP 404: not found",
    "download failed (ReadTimeout)", "the file could not be retrieved",
])
def test_a_fetch_failure_is_reported_as_a_download_problem(why):
    assert verdict_for(CLOUDINARY, "report.pdf", 0, "", why)[0] == DOWNLOAD_FAILED


def test_a_file_that_arrived_and_said_nothing_is_the_learners_to_fix():
    verdict, detail = verdict_for(CLOUDINARY, "scan.pdf", 0, "",
                                  "no text layer and OCR found nothing")
    assert verdict == UNREADABLE
    assert "no text layer" in detail


def test_an_unexplained_empty_read_still_gets_an_honest_verdict():
    verdict, detail = verdict_for(CLOUDINARY, "x.png", 0, "", "")
    assert verdict == UNREADABLE and detail


# ── the Day 07 shape, end to end ────────────────────────────────────────────

def test_the_two_faults_are_separated():
    rows = [
        ("Link submission", "Link submission", 0, "", "HTTP 404"),
        ("Link submission", "Link submission", 0, "", "HTTP 404"),
        (CLOUDINARY, "tata_motors_sales_report.pdf", 0, "", "HTTP 403"),
        (CLOUDINARY, "dashboard.png", 0, "a revenue dashboard", ""),
    ]
    verdicts = [verdict_for(*r)[0] for r in rows]
    assert verdicts == [NO_LINK, NO_LINK, DOWNLOAD_FAILED, READS_FINE]


# ── read-only ───────────────────────────────────────────────────────────────

def test_the_tool_writes_nothing_to_the_database():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "file_check.py"), encoding="utf-8").read()
    for forbidden in ("UPDATE ", "INSERT ", "DELETE ", "REPLACE INTO"):
        assert forbidden not in src.upper(), f"a read-only tool must not {forbidden}"


def test_one_bad_row_cannot_stop_the_sweep():
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "file_check.py"), encoding="utf-8").read()
    assert "except Exception as e:" in src


def test_the_tool_can_find_the_agents_own_reader():
    """It died on first use with "No module named 'app'": tools/ was on the
    path but the repo root was not, and this is the only tool that imports
    the agent's reader. A diagnostic that cannot start diagnoses nothing."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "tools",
                            "file_check.py"), encoding="utf-8").read()
    setup = src[:src.index("NO_LINK =")]
    assert "sys.path.insert(0, os.path.dirname(_HERE))" in setup
    assert setup.index("sys.path.insert(0, os.path.dirname(_HERE))") \
        < src.index("from app.utils.file_extractor import")


def test_it_runs_from_any_working_directory():
    """Ranjana runs it from the repo root; a scheduled job would not.

    tempfile.gettempdir(), not "/tmp": this test was written on Linux and
    hardcoded a path Windows does not have, so it failed on the one machine
    that actually runs these tools. A test that only passes on the author's
    computer is worse than no test — it fails in the middle of a push and
    costs the time it was meant to save.
    """
    import subprocess
    import sys as _sys
    import tempfile
    tool = os.path.join(os.path.dirname(__file__), "..", "tools", "file_check.py")
    out = subprocess.run([_sys.executable, os.path.abspath(tool),
                          "--assignment-id", "24"],
                         cwd=tempfile.gettempdir(), capture_output=True,
                         text=True, env={**os.environ, "AIREV_DB_URL": ""})
    assert "No module named" not in out.stderr, out.stderr[-300:]
    assert "AIREV_DB_URL" in (out.stdout + out.stderr)
