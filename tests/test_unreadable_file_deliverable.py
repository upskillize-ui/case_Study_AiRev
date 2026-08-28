"""A8 — the file WAS the work, and it never opened (28 Aug 2026).

The link half of this has been guarded since 19 Aug: a publish-this task whose
link never opened gets no mark, because a mark there measures our reach rather
than the learner's work. The FILE half was never guarded, and
is_unassessable() stops applying the moment the typed answer passes
MIN_GRADABLE_WORDS — so a learner who exported a .fig, uploaded it, and wrote
300 careful words about their design was scored on the WORDS and charged for
the design nobody could see.

These tests hold both halves of the rule:
  * a caption never rescues an unopened deliverable
  * a readable file, or a readable link, always does
"""
import pytest

from app.services import student_notices as sn
from app.utils import submission_intake as intake


def art(kind, label, text="", note=""):
    return intake.Artefact(kind=kind, label=label, text=text, note=note)


CAPTION = art("typed text", "answer box",
              "I designed three screens in Figma: a login, a dashboard and a "
              "payment confirmation, using a dark blue and white palette "
              "throughout, with consistent spacing and type. " * 6)


# ── the rule ───────────────────────────────────────────────────────────────

def test_a_caption_does_not_rescue_a_file_that_never_opened():
    """The live shape: good work, unopenable export, thorough description."""
    assert intake.file_deliverable_unseen([art("file", "screens.fig"), CAPTION])


def test_a_readable_file_means_the_work_arrived():
    assert not intake.file_deliverable_unseen(
        [art("image", "screens.png", "VISUAL: three banking screens"), CAPTION])


def test_one_readable_file_among_several_is_enough():
    assert not intake.file_deliverable_unseen([
        art("file", "screens.fig"),
        art("image", "export.png", "VISUAL: the same three screens"),
    ])


def test_a_readable_link_rescues_the_row():
    """A published page IS the work in another form — unlike prose about it."""
    assert not intake.file_deliverable_unseen([
        art("file", "screens.fig"),
        art("link", "https://x.figma.com/p", "the rendered prototype text"),
    ])


def test_an_unreadable_link_does_not_rescue_it():
    assert intake.file_deliverable_unseen([
        art("file", "screens.fig"),
        art("link", "https://x.figma.com/p"),
    ])


def test_typed_text_alone_is_not_this_rule():
    """No file was submitted, so there is nothing unseen — that row is judged
    on what it is, which is the marker's job, not this guard's."""
    assert not intake.file_deliverable_unseen([CAPTION])


def test_no_artefacts_at_all():
    assert not intake.file_deliverable_unseen([])


# ── the narrowing: a written task keeps its essay ──────────────────────────

def test_only_produce_tasks_are_gated():
    """On a written task an unopenable attachment is a supporting extra. The
    essay in the answer box is still the deliverable and still earns marks."""
    assert "written" not in intake.PRODUCED_KINDS
    assert "mixed" not in intake.PRODUCED_KINDS
    assert intake.PRODUCED_KINDS == {"image", "artifact_or_link",
                                     "file_or_workbook"}


# ── what the learner is told ───────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("screens.fig", "Export your screens from Figma as PNG or JPG"),
    ("art.psd", "Export as PNG or JPG"),
    ("work.rar", "Upload a ZIP"),
    ("page.mht", "Save the page as a PDF"),
])
def test_the_notice_names_the_export_that_will_actually_work(name, expected):
    """"Re-attach it" is useless advice for a .fig — the same file fails
    again. Name the one step that fixes it."""
    msg = sn.file_unreadable("", name)
    assert expected in msg
    assert sn.NO_MARK in msg


def test_a_readable_type_that_simply_failed_keeps_the_original_advice():
    msg = sn.file_unreadable("password protected", "report.pdf")
    assert "password protected" in msg and "re-attach" in msg.lower()


def test_the_notice_never_blames_the_learner():
    msg = sn.file_unreadable("", "screens.fig")
    for punitive in ("you failed", "invalid", "rejected", "your fault"):
        assert punitive not in msg.lower()
    assert "safe" in msg.lower()
