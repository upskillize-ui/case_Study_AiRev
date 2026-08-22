"""A recorded failure is a reason to read the source again — never a result
to reuse.

The 21 Aug probe: of 145 ungraded rows, ~110 'unreadable' files EXISTED on
the LMS server and served bytes on demand. The failures were transient
(fetch errors, the media-type bug) — but the regrade path reuses stored
assemblies whole, so a manifest that says 'could not be read' was replayed
on every sweep, forever, while the file sat there readable. These tests pin
the fix: a stored assembly recording a failed read of a file still on record
is discarded and the source re-extracted; only the learner's own TYPED TEXT
carries over (the 1126 nesting rule).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.routes import assignment_review as ar
from app.utils import submission_intake as intake
from app.utils.submission_intake import Artefact


def _assembled(file_readable: bool) -> str:
    """Stored notes as the submit path really writes them: manifest + items."""
    file_art = (Artefact(kind="image", label="plan.jpg", text="FIVE STEPS: one two three four five six seven eight nine ten")
                if file_readable else
                Artefact(kind="image", label="plan.jpg", text="",
                         note="download HTTP 503", confirmed=True))
    typed = Artefact(kind="typed text", label="answer box",
                     text="My own caption about my plan.")
    manifest, content = intake.render([file_art, typed])
    return f"{manifest}\n{content}".strip()


# ── the helpers, pure ─────────────────────────────────────────────────────

def test_a_failed_read_is_recognised_in_both_wordings():
    assert intake.records_failed_read(_assembled(file_readable=False))
    unretrieved = intake.render([Artefact(kind="document", label="plan.pdf",
                                          note="download failed", confirmed=False)])[0]
    assert intake.records_failed_read(unretrieved)


def test_a_clean_assembly_records_no_failure():
    assert not intake.records_failed_read(_assembled(file_readable=True))
    assert not intake.records_failed_read("")


def test_typed_text_comes_back_and_nothing_else_does():
    notes = _assembled(file_readable=False)
    _, content = intake.split_manifest(notes)
    recovered = intake.typed_text_from(content)
    assert recovered == "My own caption about my plan."
    assert "MANIFEST" not in recovered and "ITEM" not in recovered


def test_typed_text_from_handles_rows_with_no_typing():
    assert intake.typed_text_from("") == ""
    only_file = intake.render([Artefact(kind="image", label="a.jpg", text="OCR words here")])[1]
    assert intake.typed_text_from(only_file) == ""


# ── the route honours it ──────────────────────────────────────────────────

def _regrade(monkeypatch, notes, file_path, fresh_ocr="FRESH READ " * 30):
    calls = {"re_extracted": False}

    def fake_from_stored_file(url, name=""):
        calls["re_extracted"] = True
        return Artefact(kind="image", label=name or url, text=fresh_ocr)

    monkeypatch.setattr(ar.intake, "from_stored_file", fake_from_stored_file)
    monkeypatch.setattr(ar.ai_service, "begin_run_billing", lambda k="": True)
    monkeypatch.setattr(
        ar.assignment_db_service, "get_submission_for_regrade",
        lambda tenant, sid: {
            "id": sid, "student_id": 405, "assignment_id": 17,
            "grade": None, "feedback": json.dumps({"wordCount": 0}),
            "notes": notes, "file_path": file_path,
            "file_name": "plan.jpg", "attempt_number": 1})
    monkeypatch.setattr(ar.assignment_db_service, "get_assignment_by_id",
                        lambda tenant, aid: {"id": aid, "title": "Day 01",
                                             "maxScore": 10})
    out = ar.re_review_assignment(submission_id=1, dryRun=True, force=False,
                                  tenant=object(), x_admin_key="k")
    return out, calls


def test_a_stored_failure_with_the_file_on_record_is_re_extracted(monkeypatch):
    out, calls = _regrade(monkeypatch, _assembled(file_readable=False),
                          "/uploads/plan.jpg")
    assert calls["re_extracted"] is True, "the source file was never re-read"
    assert out.get("dryRun") and out.get("success")
    assert out["wordCount"] >= 30           # the fresh OCR, not the old failure


def test_a_clean_stored_assembly_is_still_reused_never_re_extracted(monkeypatch):
    """The 1126 protection stands: re-extracting a GOOD assembly double-reads
    the image and nests manifests. Only recorded failures trigger a re-read."""
    out, calls = _regrade(monkeypatch, _assembled(file_readable=True),
                          "/uploads/plan.jpg")
    assert calls["re_extracted"] is False, "a good assembly was re-extracted"
    assert out.get("success")


def test_a_stored_failure_with_no_file_left_changes_nothing(monkeypatch):
    """Nothing on record to re-read — the old behaviour (reuse, then the
    unassessable/empty guards) must hold exactly."""
    out, calls = _regrade(monkeypatch, _assembled(file_readable=False),
                          file_path="")
    assert calls["re_extracted"] is False
