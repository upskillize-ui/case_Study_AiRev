"""WHY A COHORT SCORED 0-3.8/10 — and the three faults behind it.

Ranjana ran the whole "30 Days 30 AI Tools" batch and every learner on
assignments 14 and 23 came back an F. The answer was not the marker being
harsh. It was three structural ceilings, none of which the model could see
past however good the work was:

  1. THE REGRADE PATH NEVER OPENED LINKS. The submit route calls
     from_links_in(); the regrade route — which is what bulk_review drives —
     did not. Day 06 asks for a song made in Suno. Learners submitted a
     suno.com link, exactly as asked. The marker was handed the URL as if it
     were their prose. Nothing to quote, so the no-evidence cap pinned every
     criterion at 20%: a flawless submission scores 2.0/10.

  2. AN UNREADABLE DELIVERABLE WAS SCORED ANYWAY. When a link or file cannot
     be opened, any mark is a claim about work nobody read. That is the
     fabrication rule pointed the other way, and it is what produced the wall
     of 2/10s.

  3. THE CASE-SPECIFICITY GATE FIRED ON TASKS WITH NO CASE. It caps
     'application'/'depth'/'practical' criteria at 40% when the answer does not
     engage the material's own facts and figures. On "build an agent and
     describe it" there is no such material, so the marker honestly says
     case_specific=false for everyone and the gate fires on everyone. Strong,
     specific work was capped at 6.2/10 before any other penalty.

Each test below fails if its fault is reintroduced. That was checked by
reintroducing each one.
"""
import os
import sys

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, ROOT)
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from app.services import review_pipeline as rp
from app.utils import submission_intake as intake


# ── the shape of a hands-on tool task ─────────────────────────────────────

TOOL_RUBRIC = {"criteria": [
    {"name": "Agent built and demonstrated",        "maxScore": 35},
    {"name": "Practical application of the tool",   "maxScore": 25},
    {"name": "Depth of reflection",                 "maxScore": 25},
    {"name": "Communication",                       "maxScore": 15},
]}

# A hands-on task has no case material, so the pack carries no specificity
# markers. That absence is the signal the gate should read.
TOOL_PACK = {"summary": "Day 14: build an agent with n8n and describe what it does."}

CASE_PACK = {"summary": "A retail case study.",
             "specificity_markers": ["18% margin", "Q3 stockout", "Meesho"]}


def model_says(pct, case_specific, quotes=("I built a resume screening agent",)):
    return {
        "is_garbage": False, "garbage_reason": "",
        "criteria": [
            {"name": c["name"], "evidence_quotes": list(quotes),
             "case_specific": case_specific,
             "judgment": "Judged from the quoted evidence.",
             "score_pct": pct, "confidence": "high"}
            for c in TOOL_RUBRIC["criteria"]
        ],
        "concepts_covered": ["a", "b", "c"], "concepts_missing": [],
        "factual_errors": [],
        "strengths": ["You named what the agent does."],
        "improvements": ["Say which step failed first."],
        "feedback_points": ["Your write-up names the tool but not the trigger."],
        "hard_truth": "Show the agent running, not just what it is for.",
        "language_report": {"grammar_issues": [], "spelling_examples": [],
                            "redundancy_note": "", "clarity_note": ""},
        "authorship": {"ai_likelihood_percent": 20, "reason": "personal detail"},
    }


@pytest.fixture
def captured(monkeypatch):
    box = {"blocks": None, "answer": None}

    def fake_call_structured(blocks, schema, **kw):
        box["blocks"] = blocks
        return box["answer"]

    monkeypatch.setattr(rp.ai_service, "call_structured", fake_call_structured)
    monkeypatch.setattr(rp.ai_service, "set_student_context", lambda *a, **k: None)
    return box


def run(captured, answer, pack, text="My agent write-up. " * 40, words=200,
        scope_type="assignment"):
    captured["answer"] = answer
    return rp.run_review(
        scope_type=scope_type, scope_id=14, pack=pack, pack_version=1,
        rubric=TOOL_RUBRIC, student_answer=text, word_count=words,
        word_limit_min=50, word_limit_max=800, student_id=1,
    )


def gates_of(out):
    return [g["gate"] for g in out["scores"]["gatesHit"]]


# ── FAULT 3: the case-specificity gate on a task with no case ─────────────

def test_specificity_gate_is_off_when_there_is_no_case_material():
    # A case study: shared material, markers present -> the gate is meaningful.
    assert rp.case_specificity_applies(CASE_PACK, "casestudy") is True
    # Same material, but an assignment is the learner's OWN project. There is no
    # shared case for every learner to reference, so the question is unfair and
    # the answer is false for all of them.
    assert rp.case_specificity_applies(CASE_PACK, "assignment") is False
    # No markers: nothing to check specificity against, whatever the type.
    assert rp.case_specificity_applies(TOOL_PACK, "casestudy") is False
    assert rp.case_specificity_applies({}, "casestudy") is False


def test_strong_hands_on_work_is_not_capped_at_four_out_of_ten(captured):
    """THE ceiling. Marker gives 85% across the board and honestly reports
    case_specific=false, because there are no case facts to engage.

    Before the fix: 'Practical application' and 'Depth of reflection' both hit
    _SPECIFICITY_BOUND and were cut 85 -> 40, taking a 6.2/10 off the top of
    every learner on the assignment.
    """
    out = run(captured, model_says(85, case_specific=False), TOOL_PACK)
    assert "generic_answer" not in gates_of(out), (
        "the case-specificity cap fired on a task with no case material — every "
        f"learner is ceilinged regardless of quality: {out['scores']['gatesHit']}")
    assert out["scores"]["totalScore"] >= 80, out["scores"]


def test_the_gate_still_fires_on_real_case_material(captured):
    """The fix must narrow the gate, not remove it. A case study whose answer
    never touches the case's own figures still gets capped."""
    out = run(captured, model_says(85, case_specific=False), CASE_PACK,
              scope_type="casestudy")
    assert "generic_answer" in gates_of(out), (
        "generic answers to a real case study must still be capped: "
        f"{out['scores']['gatesHit']}")
    assert out["scores"]["totalScore"] < 80, out["scores"]


# ── FAULT 1+2: a deliverable we could not read ────────────────────────────

def test_a_bare_link_is_not_counted_as_a_written_answer():
    assert intake.substantive_words("https://suno.com/song/6f2a-91bb") == 0
    # URL and the .mp3 filename token both drop out. "मेरा" is a separate word
    # and stays — a filename with a space in it only loses its last token, which
    # is the honest reading: we cannot tell prose from filename at a space.
    assert intake.substantive_words("my song https://suno.com/song/6f2a  मेरा भारत.mp3") == 3
    long_answer = ("I used Suno to write a song about my village. It took four "
                   "prompts before the chorus scanned properly.")
    assert intake.substantive_words(long_answer) >= intake.MIN_GRADABLE_WORDS


def test_link_only_submission_is_unassessable_not_a_fail():
    assert intake.is_unassessable("", "https://suno.com/song/6f2a-91bb") is True


def test_an_unreadable_file_is_unassessable():
    manifest, content = intake.render([
        intake.Artefact(kind="audio", label="मेरा भारत.mp3", text="",
                        note="no transcript available", confirmed=True),
    ])
    assert intake.unreadable_deliverable(manifest) is True
    assert intake.is_unassessable(manifest, content) is True


def test_a_thin_typed_answer_is_still_graded_not_skipped():
    """The narrowness matters. Judging a weak written answer is the marker's
    job — skipping it would hide real underperformance behind a process
    message, which is its own kind of dishonesty."""
    assert intake.is_unassessable("", "This assignment was about making a song "
                                      "and I made one that I liked a lot") is False


def test_a_readable_transcript_is_graded_not_skipped():
    """The mp3s that DID transcribe must not be swept up by the same rule."""
    manifest, content = intake.render([
        intake.Artefact(kind="audio", label="भारत माझा.mp3", confirmed=True,
                        text="This song is about the rivers of my state and I "
                             "wrote the chorus first before the verses came."),
    ])
    assert intake.is_unassessable(manifest, content) is False


# ── FAULT 1: the regrade path never opened the learner's link ─────────────
#
# Behavioural, driven through the real route with dryRun=True — which runs the
# whole intake and returns before any AI call. Not source-inspecting: an
# earlier test in this repo read a function's source and a mutation run walked
# straight through it, because `if False:` leaves the text unchanged.

def _call_regrade(monkeypatch, notes, file_path=None, link_body=""):
    from app.routes import assignment_review as ar

    opened = []

    def fake_links(text, limit=6):
        for url in intake.find_urls(text):
            opened.append(url)
        return [intake.Artefact(kind="link", label=u, text=link_body, confirmed=True)
                for u in opened]

    monkeypatch.setattr(ar.intake, "from_links_in", fake_links)
    monkeypatch.setattr(ar.ai_service, "begin_run_billing", lambda k="": True)
    monkeypatch.setattr(ar.assignment_db_service, "get_submission_for_regrade",
                        lambda tenant, sid: {"id": sid, "student_id": 345,
                                             "assignment_id": 23, "grade": None,
                                             "feedback": None, "notes": notes,
                                             "file_path": file_path,
                                             "file_name": "", "attempt_number": 1})
    monkeypatch.setattr(ar.assignment_db_service, "get_assignment_by_id",
                        lambda tenant, aid: {"id": aid, "title": "Day 06 — Suno",
                                             "maxScore": 10})
    result = ar.re_review_assignment(submission_id=1, dryRun=True, force=False,
                                     tenant=object(), x_admin_key="k")
    return result, opened


SUNO = "https://suno.com/song/6f2a-91bb"


def test_the_regrade_route_opens_the_link_the_learner_submitted(monkeypatch):
    """Day 06 asked for a Suno song. Learners submitted the link, as asked.
    bulk_review drives THIS route, and it never opened them."""
    _, opened = _call_regrade(monkeypatch, SUNO, link_body="A four-minute folk "
                              "song in Marathi about rivers, generated from the "
                              "prompt 'village morning, tabla, hopeful'.")
    assert opened == [SUNO], (
        "the regrade path did not open the learner's link — the URL is marked "
        "as if it were their prose, and a correct submission scores 2/10")


def test_a_link_that_opens_is_graded_normally(monkeypatch):
    result, _ = _call_regrade(monkeypatch, SUNO, link_body="A four-minute folk "
                              "song in Marathi about the rivers of my state, "
                              "generated from a prompt about village mornings.")
    assert result["success"] is True, result
    assert result.get("skipped") is None


def test_a_link_that_will_not_open_is_skipped_not_failed(monkeypatch):
    """The row keeps whatever grade it has. Awarding 2/10 for a song nobody
    listened to is a claim about work we never read."""
    result, _ = _call_regrade(monkeypatch, SUNO, link_body="")
    assert result["success"] is False, result
    assert result["skipped"] == "unassessable_deliverable", result
    assert "describing what they made" in result["detail"]


# ── FAULT 4: a web page graded as the learner's coursework ────────────────
#
# Live, 16 Aug 2026: dozens of Day 06 rows reported "1917 words", "1921
# words", "1925 words" — near-identical counts across unrelated learners — and
# every one scored 0.0/10. Above each, the log said `invalid pdf header:
# b'<!DOC'`. The download had returned HTTP 200 carrying an HTML page, PDF
# parsing failed, and the last-resort branch decoded the markup as text.

from app.utils import file_extractor as fx

WEB_PAGE = (b"<!DOCTYPE html><html><head><title>Not Found</title></head>"
            b"<body>" + b"navigation footer cookie policy " * 300 + b"</body></html>")


def test_a_web_page_is_not_read_as_a_submission():
    assert fx.looks_like_web_page(WEB_PAGE) is True
    text, why = fx.extract_text_from_bytes(WEB_PAGE, "Link submission")
    assert text == "", (
        f"{len(text.split())} words of web page were returned as the learner's "
        "work — this is what produced the wall of identical ~1920-word rows")
    assert "web page" in why


def test_html_served_under_a_pdf_name_is_also_refused():
    """The exact live case: the file was named .pdf and the server sent HTML."""
    text, why = fx.extract_text_from_bytes(WEB_PAGE, "India_My_Pride_Song.pdf")
    assert text == "", f"HTML was extracted from a .pdf-named response: {text[:80]}"
    assert "web page" in why


def test_a_real_document_is_still_read():
    """The refusal must be narrow — ordinary work must pass straight through."""
    body = b"I made my song in Suno using a prompt about village mornings."
    text, why = fx.extract_text_from_bytes(body, "notes.txt")
    assert "village mornings" in text and why == ""


def test_an_html_file_the_learner_meant_to_submit_still_reads():
    """A deliberate .html upload is coursework, not a failed download."""
    page = b"<html><body><h1>My portfolio</h1><p>Built with Lovable.</p></body></html>"
    text, why = fx.extract_text_from_bytes(page, "portfolio.html")
    assert "portfolio" in text.lower(), (text, why)


# ── FAULTS 5 & 6: malformed model output returned HTTP 500 ────────────────

def test_criteria_returned_as_strings_does_not_crash(captured):
    """AttributeError: 'str' object has no attribute 'get' — live 500."""
    answer = model_says(80, case_specific=False)
    answer["criteria"] = [c["name"] for c in TOOL_RUBRIC["criteria"]]
    out = run(captured, answer, TOOL_PACK)
    assert out["scores"]["totalScore"] == 0, out["scores"]


def test_a_missing_criteria_key_does_not_crash(captured):
    """KeyError: 'criteria' — live 500. The learner's row must not be skipped
    because of a malformed response they had no part in."""
    answer = model_says(80, case_specific=False)
    del answer["criteria"]
    out = run(captured, answer, TOOL_PACK)
    assert out["scores"]["totalScore"] == 0, out["scores"]


def test_normalise_never_invents_a_score():
    r = rp.normalise_review({"criteria": ["Agent built and demonstrated"]})
    assert r["criteria"][0]["score_pct"] == 0
    assert r["criteria"][0]["evidence_quotes"] == []
