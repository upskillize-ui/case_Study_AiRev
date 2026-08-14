"""Intake must carry EVERY submitted format to the marker — and nothing else.

Pinned to the 13 Aug 2026 batch, where learners who produced exactly what the
task asked for were scored 0:

    the task     — "generate an AI image of your five-year future self"
    the learner  — attached the image, typed a 12-word caption
    the marker   — saw the caption only, reported the image "missing", and
                   zeroed a criterion worth 40% of the rubric

The attachment never entered the answer text. These tests hold that shut, and
hold the link fetcher shut against the server-side request forgery it would
otherwise be.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.utils import submission_intake as intake
from app.utils import url_guard
from app.utils.submission_intake import Artefact

IMAGE = Artefact(kind="image", label="future_self.png",
                 text="TEXT:\nNEXT 5 YEARS\n\nVISUAL:\nA woman at a desk.")
CAPTION = Artefact(kind="typed text", label="answer box",
                   text="Here is my five year vision image.")


# ── the defect ────────────────────────────────────────────────────────────

def test_an_attachment_reaches_the_marker():
    """The whole bug in one assertion: the image's content is in the answer."""
    manifest, content = intake.render([IMAGE, CAPTION])
    assert "NEXT 5 YEARS" in content
    assert "A woman at a desk" in content


def test_the_manifest_declares_the_image_exists():
    manifest, _ = intake.render([IMAGE, CAPTION])
    assert "IMAGE" in manifest and "future_self.png" in manifest


def test_a_produced_deliverable_releases_the_length_gate():
    """12 words + an image is a COMPLETE image-first submission."""
    assert intake.has_deliverable([IMAGE, CAPTION]) is True
    assert intake.has_deliverable([CAPTION]) is False


def test_word_count_excludes_the_manifest():
    """Provenance must not inflate the length the gates measure — that would
    invert the bug and pass thin answers instead of failing complete ones."""
    manifest, content = intake.render([IMAGE, CAPTION])
    assert "SUBMISSION MANIFEST" not in content


def test_typed_only_submissions_are_untouched():
    """A plain written answer must travel exactly as it did before."""
    manifest, content = intake.render([CAPTION])
    assert manifest == ""
    assert content == CAPTION.text


# ── an unreadable attachment is not an absent one ─────────────────────────

def test_an_unreadable_but_present_file_is_recorded_as_existing():
    a = intake._finish_file("sketch.png", "", "image contains no readable text",
                            had_bytes=True)
    assert a.confirmed is True and a.readable is False
    manifest, _ = intake.render([a, CAPTION])
    assert "could not be read" in manifest
    assert "It exists" in manifest


def test_a_file_we_could_not_download_is_not_evidence():
    """Fabricating a deliverable from a failed download is exactly the
    hallucination the project forbids."""
    a = intake._finish_file("essay.pdf", "", "download HTTP 404", had_bytes=False)
    assert a.confirmed is False
    assert intake.has_deliverable([a]) is False
    manifest, _ = intake.render([a])
    assert "NOT evidence" in manifest


def test_the_manifest_never_suggests_a_score():
    manifest, _ = intake.render([IMAGE, CAPTION])
    lowered = manifest.lower()
    for word in ("credit the", "award", "at least", "minimum", "full marks",
                 "should score", "bonus"):
        assert word not in lowered, word


# ── links ─────────────────────────────────────────────────────────────────

def test_urls_are_found_and_deduped_in_order():
    text = ("See https://claude.ai/public/artifacts/abc and "
            "https://example.com/report.pdf. Again https://claude.ai/public/artifacts/abc")
    assert intake.find_urls(text) == ["https://claude.ai/public/artifacts/abc",
                                      "https://example.com/report.pdf"]


def test_trailing_sentence_punctuation_is_not_part_of_the_url():
    assert intake.find_urls("my work: https://example.com/a.")[0] == "https://example.com/a"
    assert intake.find_urls("(https://example.com/b)")[0] == "https://example.com/b"


def test_html_becomes_readable_text():
    html = ("<html><head><style>p{color:red}</style>"
            "<script>var x=1</script></head><body><h1>My Plan</h1>"
            "<p>Build a startup&nbsp;&amp; scale it</p></body></html>")
    text = intake.html_to_text(html)
    assert "My Plan" in text and "Build a startup & scale it" in text
    assert "color:red" not in text and "var x" not in text


def test_a_client_rendered_page_is_reported_honestly():
    """A JavaScript shell is not the learner's work. Saying 'could not be read'
    is honest; passing the loader off as their submission is not."""
    shell = b"<html><body><div id='root'></div></body></html>"
    text, why = intake._read_response(shell, "text/html", "https://example.com/x")
    assert text == ""
    assert "browser" in why


# ── SSRF guard: learner text drives this fetcher ──────────────────────────

@pytest.mark.parametrize("url", [
    "http://169.254.169.254/latest/meta-data/",   # cloud metadata endpoint
    "http://127.0.0.1:8000/admin",
    "http://localhost/health",
    "http://[::1]/",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://example.com/",
    "http://example.com:22/",                     # non-web port
])
def test_unsafe_targets_are_refused(url):
    ok, why = intake._safe_target(url)
    assert ok is False, f"{url} was allowed ({why})"


def test_private_ranges_are_refused(monkeypatch):
    import socket
    for addr in ("10.0.0.5", "192.168.1.1", "172.16.0.1", "169.254.1.1", "127.0.0.1"):
        monkeypatch.setattr(
            url_guard.socket, "getaddrinfo",
            lambda *a, _a=addr, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_a, 443))])
        ok, why = intake._safe_target("https://evil.example.com/")
        assert ok is False, f"{addr} was allowed"
        assert "non-public" in why


def test_a_public_host_is_allowed(monkeypatch):
    import socket
    monkeypatch.setattr(
        url_guard.socket, "getaddrinfo",
        lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))])
    ok, why = intake._safe_target("https://example.com/artifact")
    assert ok is True, why


def test_every_redirect_hop_is_revalidated(monkeypatch):
    """A public URL that 302s to the metadata endpoint is the classic bypass of
    a one-shot check. follow_redirects=True would walk straight into it."""
    checked = []

    def fake_safe(url):
        checked.append(url)
        return ("169.254" not in url), "link points to a non-public address"

    class FakeResp:
        status_code = 302
        headers = {"location": "http://169.254.169.254/latest/meta-data/"}
        content = b""

    class FakeClient:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def get(self, url): return FakeResp()

    monkeypatch.setattr(intake, "_safe_target", fake_safe)
    monkeypatch.setattr(intake.httpx, "Client", lambda **k: FakeClient())

    text, why = intake.fetch_link("https://example.com/start")
    assert text == ""
    assert "non-public" in why
    assert any("169.254" in u for u in checked), "the redirect target was never checked"


def test_redirect_chains_terminate():
    """No infinite loop on a self-redirecting page."""
    assert intake.LINK_MAX_REDIRECTS <= 6


# ── kind labelling ────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,kind", [
    ("a.png", "image"), ("a.JPEG", "image"), ("b.pdf", "document"),
    ("c.docx", "document"), ("d.xlsx", "spreadsheet"), ("e.csv", "spreadsheet"),
    ("f.pptx", "slide deck"), ("g.ipynb", "notebook"), ("h.zip", "archive"),
    ("noextension", "file"), ("i.wat", "file"),
])
def test_kinds_are_named_from_the_extension(name, kind):
    assert intake.kind_for(name) == kind


def test_unknown_types_are_not_given_a_type_we_did_not_verify():
    assert intake.kind_for("mystery.bin") == "file"


# ── storage ceiling ───────────────────────────────────────────────────────

def test_assembled_text_stays_inside_the_storage_ceiling():
    """Intake can now assemble SEVERAL capped artefacts, and their SUM is what
    reaches the notes column. Overflowing it is DataError 1406 and a lost
    submission — the 13 Aug failure, one level up."""
    from app.utils.file_extractor import MAX_TEXT_CHARS
    big = [Artefact(kind="document", label=f"f{i}.pdf", text="word " * 30000)
           for i in range(4)]
    manifest, content = intake.render(big)
    assert len(manifest) + len(content) <= MAX_TEXT_CHARS


def test_truncation_is_announced_not_silent():
    """A silently-clipped answer reads to the marker as a thin one."""
    big = [Artefact(kind="document", label="huge.pdf", text="word " * 40000),
           Artefact(kind="image", label="x.png", text="a picture")]
    _, content = intake.render(big)
    assert "more material than fits" in content


def test_short_submissions_are_never_truncated():
    _, content = intake.render([IMAGE, CAPTION])
    assert "more material than fits" not in content


# ── latency budget ────────────────────────────────────────────────────────

def test_link_reading_cannot_hold_the_request_open_indefinitely():
    """Fetching runs inside submit, so its worst case is the learner's wait."""
    assert intake.MAX_LINKS * intake.LINK_TIMEOUT <= 60
    assert intake.LINK_TOTAL_BUDGET <= 30


def test_links_beyond_the_budget_are_recorded_not_dropped(monkeypatch):
    import time as real_time
    monkeypatch.setattr(intake, "LINK_TOTAL_BUDGET", -1)   # budget already spent
    called = []
    monkeypatch.setattr(intake, "fetch_link", lambda u: called.append(u) or ("", "x"))
    arts = intake.from_links_in("see https://example.com/a and https://example.com/b")
    assert called == [], "no link should have been fetched"
    assert len(arts) == 2, "both links must still appear on the record"
    assert all("budget" in a.note for a in arts)
