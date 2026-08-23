"""Day 02: the deliverable is a Claude artifact — a link, an exported HTML
file, or a deck. Every reading path a learner can arrive by is pinned here.

Verified live 21 Aug before writing these: claude.ai and claude.site artifact
pages serve every server-side reader the IDENTICAL boilerplate ("Title:
Claude Artifact" + a marketing description), no artifact content, and their
API is behind Cloudflare. So the honest capacity is: read HTML files fully
(page first, source second), read decks fully, treat an artifact LINK as a
confirmed published deliverable — and when nothing beside the link is
readable, refuse to score and tell the learner exactly what to add, at
SUBMIT time, not days later from a sweep.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.utils import file_extractor as fx
from app.utils import submission_intake as intake


# ── uploaded HTML: read as a page first, as source second ─────────────────

PAGE = (b"<html><head><title>My BFSI Career Coach</title></head><body>"
        b"<h1>Loan eligibility checker</h1>"
        + b"<p>Enter your monthly income and existing EMIs to see FOIR. </p>" * 20
        + b"</body></html>")


def test_an_html_page_reads_as_what_a_visitor_would_see():
    text, why = fx.extract_text_from_bytes(PAGE, "coach.html")
    assert why == ""
    assert "My BFSI Career Coach" in text          # the title travels
    assert "Loan eligibility checker" in text      # the content travels
    assert "<h1>" not in text                      # tags do not


def test_dot_htm_is_no_longer_refused_as_a_failed_download():
    """The pinned bug: .htm was in NO extension set, fell to the unknown-ext
    sniff, and the login-page guard refused the learner's own built page."""
    text, why = fx.extract_text_from_bytes(PAGE, "coach.htm")
    assert why == "" and "Loan eligibility checker" in text


def test_a_react_artifact_export_reads_as_source_not_as_nothing():
    """Claude artifacts exported as HTML are client-rendered: almost no
    visible text, all the work in the script. The source IS the deliverable."""
    app_page = (b"<html><head><title>EMI Calculator</title></head>"
                b"<body><div id='root'></div>"
                b"<script>const emi=(p,r,n)=>p*r*Math.pow(1+r,n)/(Math.pow(1+r,n)-1);"
                b"document.getElementById('root').innerHTML='calc';</script>"
                b"</body></html>")
    text, why = fx.extract_text_from_bytes(app_page, "emi.html")
    assert why == ""
    assert "source" in text.lower()
    assert "const emi=" in text                    # the learner's logic, judgeable
    assert "EMI Calculator" in text


def test_embedded_base64_images_cannot_spend_the_text_budget():
    blob = b"A" * 5000
    page = (b"<html><body><div id='root'></div>"
            b"<img src='data:image/png;base64," + blob + b"'>"
            b"<script>let x=1;</script></body></html>")
    text, why = fx.extract_text_from_bytes(page, "page.html")
    assert why == ""
    assert b"A" * 200 not in text.encode()
    assert "embedded image data removed" in text


def test_an_empty_html_file_says_so():
    text, why = fx.extract_text_from_bytes(b"   ", "blank.html")
    assert text == "" and "empty" in why.lower()


# ── artifact links: boilerplate is not the learner's work ─────────────────

CLAUDE_SHELL = (b"<html><head>"
                b"<meta property='og:title' content='Claude Artifact'>"
                b"<meta property='og:description' content='Try Claude'>"
                b"</head><body><div id='root'></div></body></html>")


def test_claude_boilerplate_meta_is_not_passed_off_as_content():
    """Every artifact page serves the same two tags. 20 generic words per
    learner = the whole cohort pinned at the no-evidence cap. Boilerplate
    must read as could-not-open, which routes to the honest ask-for-more."""
    text, why = intake._read_response(
        CLAUDE_SHELL, "text/html",
        "https://claude.ai/public/artifacts/8b6ba33e-3487-4e10-8f79-871f11b7e6c7")
    assert text == ""
    assert "browser" in why


def test_a_real_titled_artifact_page_would_still_be_read():
    """Future-proof: if Claude ever serves the artifact's own title, that IS
    evidence of what the learner made and must come through."""
    page = (b"<html><head>"
            b"<meta property='og:title' content='Loan Eligibility Coach by Priya'>"
            b"</head><body><div id='root'></div></body></html>")
    text, why = intake._read_response(
        page, "text/html", "https://claude.ai/public/artifacts/xyz")
    assert "Loan Eligibility Coach by Priya" in text


def test_other_platforms_keep_their_metadata_fallback():
    """The Suno fix must not regress: a platform whose meta carries the real
    title/author still describes the deliverable."""
    page = (b"<html><head>"
            b"<meta property='og:title' content='Monsoon Dreams - AI song'>"
            b"<meta property='og:site_name' content='Suno'>"
            b"</head><body><div id='root'></div></body></html>")
    text, why = intake._read_response(page, "text/html", "https://suno.com/song/abc")
    assert "Monsoon Dreams" in text


def test_an_unread_artifact_link_alone_is_unassessable_not_a_low_score():
    art = intake.Artefact(kind="link",
                          label="https://claude.ai/public/artifacts/abc",
                          note="the page loads its content in the browser",
                          confirmed=True)
    manifest, content = intake.render(
        [intake.from_typed("https://claude.ai/public/artifacts/abc"), art])
    assert intake.is_unassessable(manifest, content) is True


def test_a_link_plus_a_real_description_is_fully_assessable():
    description = ("I built a loan eligibility coach in Claude. It asks for "
                   "income, EMIs and tenure, computes FOIR and shows whether "
                   "a cooperative bank would sanction the loan, with three "
                   "improvement tips. I iterated the prompt four times to "
                   "get the FOIR formula right and added a reset button.") * 2
    art = intake.Artefact(kind="link", label="https://claude.ai/x",
                          note="browser-only", confirmed=True)
    manifest, content = intake.render([intake.from_typed(description), art])
    assert intake.is_unassessable(manifest, content) is False


# ── the SUBMIT path refuses to score an unopenable deliverable ────────────

def test_submit_time_guard_asks_for_more_instead_of_scoring_a_url(monkeypatch):
    """Day 06's failure arrived through the LIVE submit path, which had no
    unassessable guard (only the regrade path did). Day 02 is a cohort of
    browser-only links — the guard must fire at submit time, before any row
    is written, with instructions the learner can act on in the same sitting."""
    from app.routes import assignment_review as ar

    monkeypatch.setattr(ar.ai_service, "begin_run_billing", lambda k="": False)
    monkeypatch.setattr(ar.assignment_db_service, "get_assignment_by_id",
                        lambda tenant, aid: {"id": aid, "title": "Day 02",
                                             "maxScore": 10, "status": "active"})
    monkeypatch.setattr(ar.assignment_db_service, "get_attempt_state",
                        lambda tenant, aid, sid: {"reviewedAttempts": 0,
                                                  "latestAnswerText": ""})
    import app.database
    monkeypatch.setattr(app.database, "canonical_student_id",
                        lambda sid, space=None: sid)
    monkeypatch.setattr(ar.intake, "from_links_in", lambda text, limit=5, **kw: [
        intake.Artefact(kind="link", label="https://claude.ai/public/artifacts/abc",
                        note="the page loads its content in the browser",
                        confirmed=True)])
    monkeypatch.setattr(
        ar.assignment_db_service, "get_latest_assignment_submission",
        lambda tenant, aid, sid: None)
    stored = []
    monkeypatch.setattr(ar.assignment_db_service, "upsert_submission",
                        lambda *a, **k: stored.append(a) or {"submissionId": 1,
                                                             "attemptNumber": 1},
                        raising=False)

    class T:
        id = "lms"

    req = ar.SubmitAssignmentRequest(
        assignmentId=18, studentId=501,
        answerText="https://claude.ai/public/artifacts/abc")
    out = ar.submit_and_review_assignment(req, tenant=T(), x_admin_key="")

    assert out["status"] == "needs_input", out
    msg = out["feedback"]["detailedFeedback"] if "detailedFeedback" in out["feedback"] else str(out["feedback"])
    assert "browser" in msg and "screenshot" in msg.lower()
    assert stored == [], "a row was written for an unassessable submission"


# ── the judge is told how to score built artifacts ────────────────────────

def test_rule_14_built_artifacts_and_published_links_exists():
    from app.services import review_pipeline as rp
    rules = rp._JUDGE_INSTRUCTIONS.lower()
    assert "built artifacts and published links" in rules
    assert "source code of a client-rendered page is that page" in rules
    assert "never rule wrong_task from a link you could not read" in rules
    assert "created, published, shared or linked" in rules


def test_rule_12_is_task_relative_not_day01_specific():
    """Baked-in Day 01 language ('is not a personal future-self plan AT ALL')
    read literally on Day 02 would let the judge un-grade every artifact —
    nothing on Day 02 is a personal plan. The rule must bind to THIS task's
    brief, with Day 01 kept as the worked example."""
    from app.services import review_pipeline as rp
    rules = rp._JUDGE_INSTRUCTIONS.lower()
    assert "makes no attempt at this task's brief" in rules
    assert "own choices within the brief are never grounds" in rules
    # the tested Day 01 phrases survive as the worked example
    assert "any career counts" in rules
    assert "career choice is never grounds" in rules
