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
import io
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

    def fake_links(text, limit=6, **kw):
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
    listened to is a claim about work we never read.

    23 Aug: this now takes the PUBLISH-LINK branch rather than the generic
    unassessable one. Suno is one of the course's own tools, so a Suno day is
    a publish-this day even though its task text never says "share a link" —
    the same widening that stopped Day 07 zeroing three learners. The refusal
    is unchanged; only the reason the learner reads is better, because it
    names the fix instead of saying "we could not open it".
    """
    result, _ = _call_regrade(monkeypatch, SUNO, link_body="")
    assert result["success"] is False, result
    assert result["skipped"] in ("unreadable_published_link",
                                 "unassessable_deliverable"), result
    assert result["previousGrade"] is None or result["previousGrade"] is not None
    assert "no mark" in result["detail"].lower() \
        or "describing what they made" in result["detail"]


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


# ── FAULT 7: marks allocated to things the work cannot show ───────────────
#
# Day 06's derived rubric, read off the live agent on 16 Aug:
#
#     Song file or link submitted            35
#     Patriotic India theme                  30
#     ChatGPT lyrics with music style line   20   <- a song carries no
#     Suno Custom mode used                  15   <- authorship signature;
#                                                    a toggle leaves no trace
#
# 35 of 100 marks for facts a finished song cannot carry. Student 312 scored
# 0/20 and 2.25/15 on those two — and so would anyone, however well they did
# the task. The cohort ceiling was 6.5/10 before the song was judged at all.

from app.services import rubric_service as rs


def test_a_tool_setting_is_not_a_criterion():
    for name in ("Suno Custom mode used", "Custom mode was enabled",
                 "Used Custom mode", "Advanced setting was turned on"):
        assert rs.is_offplatform({"name": name}) is True, name


def test_the_setting_filter_does_not_eat_real_criteria():
    """The scope note in rubric_service is emphatic: an earlier over-broad
    pattern dropped nine legitimate criteria. These must all survive."""
    for name in ("Song file or link submitted", "Patriotic India theme",
                 "Mode of address", "Setting and atmosphere",
                 "Creative use of the prompt", "Lyrics are original and on theme",
                 "Custom illustration quality", "Feature comparison table"):
        assert rs.is_offplatform({"name": name}) is False, name


def test_dropping_a_criterion_does_not_cost_the_learner_its_weight():
    """The whole point. Stripping must hand the weight to what remains, or the
    fix would simply move the same 15 marks out of reach."""
    kept, dropped = rs.strip_offplatform([
        {"name": "Song file or link submitted", "maxScore": 35},
        {"name": "Patriotic India theme", "maxScore": 30},
        {"name": "Suno Custom mode used", "maxScore": 15},
    ])
    assert [c["name"] for c in dropped] == ["Suno Custom mode used"]
    assert sum(c["maxScore"] for c in rs.normalise(kept)) == 100


def test_the_rubric_version_forces_cached_rubrics_to_rebuild():
    """Day 06's bad rubric is CACHED in derived_rubrics. Without a version bump
    the fix ships and every assignment keeps grading against the old criteria."""
    # Bumped to 5 when rule 9 (criteria must be INDEPENDENT) landed. 17 and 23
    # had already re-derived under v4, so without the bump that fix would have
    # shipped and changed nothing for the two assignments it was written for.
    # The version is part of the cache key; the derivation INSTRUCTIONS are not.
    # Change the rules, change this number.
    assert rs.RUBRIC_VERSION >= 5
    a = rs.source_hash({"title": "Day 06", "description": "Create a song"})
    assert a != "", "source_hash must fold RUBRIC_VERSION into the cache key"


# ── FAULT 8: the same submission scored differently on identical text ─────
#
# Submission 4804, byte-identical input, no code change between runs 2 and 3:
#
#     run 1  0.9/10      run 2  1.2/10      run 3  0.0/10
#
# and across the batch 4758 went 1.8 -> 5.6, 4064 went 4.4 -> 0.1. call_structured
# set no temperature, so every review ran at the API default of 1.0 — full
# sampling on a task whose whole purpose is a defensible number.

from app.services import ai_service as ais


def _kwargs_from_a_scoring_call(monkeypatch, thinking_budget=0):
    seen = {}

    class _Block:
        type, name, input = "tool_use", "emit_result", {"ok": True}

    class _Resp:
        content = [_Block()]
        usage = None

    def fake_create_message(**kw):
        seen.update(kw)
        return _Resp(), "anthropic"

    monkeypatch.setattr(ais, "create_message", fake_create_message)
    monkeypatch.setattr(ais, "_report_usage", lambda *a, **k: None)
    ais.call_structured(blocks=[{"text": "x", "cache": False}], schema={},
                        thinking_budget=thinking_budget)
    return seen


def test_scoring_is_deterministic(monkeypatch):
    kw = _kwargs_from_a_scoring_call(monkeypatch)
    assert kw.get("temperature") == 0, (
        "no temperature set — reviews run at the API default of 1.0, and the "
        "same submission scores differently on identical text")


def test_the_thinking_path_does_not_send_an_illegal_temperature(monkeypatch):
    """Extended thinking requires temperature 1; sending 0 is an API error, so
    the determinism fix must not break the escalation path."""
    kw = _kwargs_from_a_scoring_call(monkeypatch, thinking_budget=2000)
    assert "temperature" not in kw, kw.get("temperature")
    assert kw["thinking"]["budget_tokens"] == 2000


def test_the_marker_is_told_not_to_score_provenance():
    """The rubric no longer NAMES a provenance criterion, but the marker still
    wrote 'no evidence that ChatGPT was used' inside a legitimate one and gave
    it 15%. Same invariant as the authorship estimate: provenance is advisory."""
    rule = rp._JUDGE_INSTRUCTIONS.lower()
    assert "provenance is advisory, never scored" in rule
    assert "never lower a criterion" in rule


# ── FAULT 9: the length trap, enforced instead of advised ─────────────────
#
# aggregate() takes up to 20 marks off an answer shorter than wordMin.
# Derivation rule 2 tells the model to set word_min 0-40 when the deliverable
# is an image, a link or a file. Day 06 came back submission_kind
# "artifact_or_link" with word_min=100 anyway, so a learner who submitted the
# song and a 40-word caption - exactly what was asked - lost 18 more marks.

from app.services import rubric_service as rs2


def test_a_caption_task_cannot_demand_an_essay():
    assert rs2.cap_word_min(100, "artifact_or_link") == 40
    assert rs2.cap_word_min(100, "image") == 40
    assert rs2.cap_word_min(100, "file_or_workbook") == 40


def test_an_essay_may_still_demand_length():
    """The clamp must be narrow. A written task's minimum is legitimate."""
    assert rs2.cap_word_min(300, "written") == 300
    assert rs2.cap_word_min(100, "mixed") == 100


def test_a_low_minimum_is_left_alone():
    assert rs2.cap_word_min(30, "image") == 30
    assert rs2.cap_word_min(0, "image") == 0


def test_the_clamp_actually_removes_the_penalty():
    """End to end through the real arithmetic: a 40-word caption on an image
    task must take no length penalty at all."""
    gated = {"breakdown": [{"criteria": "Image present", "maxScore": 100,
                            "percentage": 80, "score": 80.0, "status": "good",
                            "evidence": ["x"], "judgment": ""}],
             "total_cap": 100, "error_deduction": 0, "gates_hit": []}
    before = rp.aggregate(gated, 40, 100, 1500)
    after = rp.aggregate(gated, 40, rs2.cap_word_min(100, "image"), 1500)
    assert before["wordCountPenalty"] == 18, before
    assert after["wordCountPenalty"] == 0, after


# ── FAULT 10: images the cohort actually sends were refused ───────────────

def test_every_image_format_a_learner_might_send_is_accepted():
    """Day 01 is an AI-generated image. The course teaches a different image
    tool most weeks and learners screenshot on whatever device is to hand, so
    six formats was too narrow - a GIF or an AVIF was reported unreadable."""
    for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif", ".avif",
                ".bmp", ".tif", ".tiff", ".heic", ".heif"):
        assert ext in fx.IMAGE_EXTS, ext


def test_native_formats_are_not_needlessly_converted():
    """Re-encoding a PNG costs time and quality for nothing."""
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    out, media_type, why = fx._to_readable_image(png, ".png")
    assert out is png and media_type == "image/png" and why == ""
    _, gif_type, _ = fx._to_readable_image(b"GIF89a", ".gif")
    assert gif_type == "image/gif", "GIF is readable natively; do not convert it"


def test_a_non_native_format_is_converted_to_png():
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (200, 30, 30)).save(buf, format="BMP")
    out, media_type, why = fx._to_readable_image(buf.getvalue(), ".bmp")
    assert why == "" and media_type == "image/png", why
    assert out[:8] == b"\x89PNG\r\n\x1a\n", "conversion did not produce a PNG"


def test_a_corrupt_image_reports_a_reason_a_learner_can_act_on():
    out, _, why = fx._to_readable_image(b"not an image at all", ".bmp")
    assert out == b"" and why, "a broken image must explain itself"
    assert "bmp" in why.lower()


# ── FAULT 11: a picture inside a PDF was never looked at ──────────────────
#
# The old rule was: extract text; if there is NONE, OCR. So a PDF holding only
# an image was read, and a PDF holding "My 5 Year Plan" plus the image returned
# six words and the picture was never opened. On Day 01 the picture IS the
# submission — the image criterion scores zero for a learner whose work is
# sitting on page one.

def test_a_pdf_with_a_heading_and_a_picture_is_ocred():
    assert fx.pdf_needs_ocr("My 5 Year Plan", has_images=True) is True


def test_a_pdf_that_is_only_a_picture_is_still_ocred():
    assert fx.pdf_needs_ocr("", has_images=True) is True
    assert fx.pdf_needs_ocr("", has_images=False) is True


def test_a_real_write_up_is_not_rasterized_for_nothing():
    """OCR costs a vision call per page. A PDF carrying an actual write-up is
    returned as text however many decorative images it holds."""
    assert fx.pdf_needs_ocr("word " * 400, has_images=True) is False
    assert fx.pdf_needs_ocr("word " * 400, has_images=False) is False


def test_a_short_text_only_pdf_is_not_ocred():
    """No pictures means nothing for OCR to find — do not pay for the call."""
    assert fx.pdf_needs_ocr("My 5 Year Plan", has_images=False) is False


def test_an_unreadable_page_assumes_there_is_something_to_see():
    """page.images raises on some producers. Absence of evidence is not
    evidence of absence, and the cost of guessing wrong is one OCR call versus
    a learner's deliverable going unseen."""
    assert fx._pdf_has_images(b"%PDF-1.4 not really a pdf") is False


# ── FAULT 12: formats the cohort has but AiRev refused ────────────────────

def test_opendocument_files_are_read():
    """LibreOffice is what a lot of students have. .odt was refused outright."""
    import io as _io
    import zipfile
    content = ('<?xml version="1.0"?><office:document-content><office:body>'
               '<office:text><text:h>My 5 Year Plan</text:h>'
               '<text:p>I want to become a data analyst at a bank.</text:p>'
               '</office:text></office:body></office:document-content>')
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("content.xml", content)
    text, why = fx.extract_text_from_bytes(buf.getvalue(), "plan.odt")
    assert why == "" and "data analyst" in text, (text, why)
    assert "My 5 Year Plan\n" in text, "block boundaries must survive as newlines"


def test_a_spreadsheet_does_not_collapse_into_one_run_of_words():
    """Strip tags before inserting boundaries and every cell runs together."""
    import io as _io
    import zipfile
    rows = ("<table:table-row><table:table-cell>Year</table:table-cell>"
            "<table:table-cell>Goal</table:table-cell></table:table-row>"
            "<table:table-row><table:table-cell>2027</table:table-cell>"
            "<table:table-cell>Analyst</table:table-cell></table:table-row>")
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("content.xml", f"<x>{rows}</x>")
    text, _ = fx.extract_text_from_bytes(buf.getvalue(), "plan.ods")
    assert "YearGoal" not in text, f"cells collapsed together: {text!r}"


def test_legacy_office_says_what_to_do_instead():
    """'This file type could not be read' helps nobody. Name the app, the
    menu, and the format to pick."""
    for name, app, modern in (("essay.doc", "Word", ".docx"),
                              ("deck.ppt", "PowerPoint", ".pptx"),
                              ("sheet.xls", "Excel", ".xlsx")):
        _, why = fx.extract_text_from_bytes(b"\xd0\xcf\x11\xe0junk", name)
        assert app in why and modern in why, (name, why)


def test_plain_text_variants_are_read():
    for name in ("notes.txt", "notes.md", "notes.log", "notes.rst"):
        text, why = fx.extract_text_from_bytes(b"I want to be an analyst", name)
        assert "analyst" in text, (name, why)


# ── FAULT 13: the marker praised the work and punished the score ──────────
#
# Student 405, live 19 Aug. Their image contained the full structured plan.
# The marker's own judgments: "Five distinct steps clearly articulated,
# labeled Year 1-5" (scored 35%), "specific, actionable tasks - 'Start a
# blog', 'Publish first eBook'" (scored 22%), and the image criterion was
# shaved to 25% for "no evidence of AI [generation]". 2.3/10 for a completed
# assignment. Three prompt rules close it: file content IS the submission,
# the score must match the judgment, and unprovable provenance never deducts.

def test_the_judge_is_told_file_content_is_the_submission():
    j = rp._JUDGE_INSTRUCTIONS
    assert "FILE CONTENT IS THE SUBMISSION" in j
    assert "exactly as if it were typed" in j


def test_the_judge_is_told_scores_must_match_judgments():
    j = rp._JUDGE_INSTRUCTIONS
    assert "SCORE MUST MATCH" in j
    assert "score_pct must say the same" in j


def test_the_judge_is_told_ai_generation_cannot_be_demanded():
    j = rp._JUDGE_INSTRUCTIONS
    assert "UNPROVABLE PROVENANCE" in j
    assert "AI-generated" in j
