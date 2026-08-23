"""End of PHASE 1: the picture reaches the marker.

The plumbing test (test_judge_sees_the_work.py) proves an image block CAN be
built. This one proves one actually travels: from the learner's upload and from
the page we rendered, through intake, into the judge call, on BOTH the submit
and the regrade path.

Why it matters on Lovable day: a built site's text tells you what it says. Only
the picture tells you whether it looks finished, whether the layout holds, or
whether the template's placeholder blocks are still sitting in it.
"""

import base64
import io

import pytest

from app.utils import submission_intake as intake
from app.utils.submission_intake import Artefact


def _png(colour="navy", size=(40, 30)) -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ── the collector ───────────────────────────────────────────────────────────

def test_an_uploaded_picture_is_offered_to_the_marker():
    art = intake.from_upload(_png(), None, "dashboard.png")
    assert art.image_b64 and art.media_type == "image/png"
    assert intake.images_for_judge([art])[0]["media_type"] == "image/png"


def test_a_pdf_upload_carries_no_picture():
    """Only things you LOOK at. A PDF is read, not viewed."""
    art = intake.from_upload(_png(), None, "notes.pdf")
    assert art.image_b64 == ""


def test_a_submission_with_no_pictures_sends_none():
    typed = Artefact(kind="typed text", label="answer box", text="my answer")
    assert intake.images_for_judge([typed]) == []


def test_the_learners_own_upload_comes_before_our_screenshot():
    """When the cap bites, the proxy should fall off, never the deliverable."""
    theirs = Artefact(kind="image", label="shot.png", image_b64="AAA",
                      media_type="image/png")
    ours = Artefact(kind="link", label="https://x.lovable.app",
                    image_b64="BBB", media_type="image/jpeg")
    assert [i["image"] for i in intake.images_for_judge([ours, theirs])] \
        == ["AAA", "BBB"]


def test_the_number_of_pictures_is_capped():
    many = [Artefact(kind="image", label=f"{i}.png", image_b64=str(i),
                     media_type="image/png") for i in range(10)]
    assert len(intake.images_for_judge(many)) == intake.MAX_JUDGE_IMAGES
    assert len(intake.images_for_judge(many, limit=2)) == 2


def test_a_zero_cap_sends_nothing_rather_than_everything():
    many = [Artefact(kind="image", label="a.png", image_b64="A",
                     media_type="image/png")]
    assert intake.images_for_judge(many, limit=0) == []


def test_a_missing_media_type_defaults_to_png():
    art = Artefact(kind="image", label="a.png", image_b64="A")
    assert intake.images_for_judge([art])[0]["media_type"] == "image/png"


# ── the pictures reach the judge call ───────────────────────────────────────

def test_the_pipeline_puts_pictures_between_the_task_and_the_learners_words():
    """Order matters: after the requirements so the marker knows what it is
    looking for, before the prose so a caption cannot colour what it sees."""
    src = open("app/services/review_pipeline.py", encoding="utf-8").read()
    block = src[src.index("judge_blocks = ("):src.index("review = normalise_review")]
    assert 'static_block' in block
    assert block.index("static_block") < block.index("picture_blocks")
    assert block.index("picture_blocks") < block.index("student_block")


def test_the_escalation_pass_sees_the_same_pictures():
    """A low-confidence review escalates to a stronger pass. Sending it text
    alone would make the second opinion worse-informed than the first."""
    src = open("app/services/review_pipeline.py", encoding="utf-8").read()
    strong = src[src.index('tier="strong"') - 400:src.index('tier="strong"')]
    assert "judge_blocks" in strong


def test_both_review_paths_show_the_marker_the_work():
    src = open("app/routes/assignment_review.py", encoding="utf-8").read()
    assert src.count("images=intake.images_for_judge(artefacts)") == 2, \
        "submit AND regrade — one alone means a re-review sees less than the first"


def test_a_text_only_review_is_unchanged():
    """No pictures must mean byte-identical behaviour to before Phase 1."""
    src = open("app/services/review_pipeline.py", encoding="utf-8").read()
    assert "picture_blocks = list(images or [])" in src
    assert "+ picture_blocks" in src


# ── the renderer keeps the picture, and lets it go ──────────────────────────

def test_the_renderer_exposes_the_page_picture():
    from app.services import link_renderer as lr
    lr.forget_screenshots()
    lr._keep_screenshot("https://x.lovable.app", "SHOT")
    assert lr._last_screenshot["https://x.lovable.app"] == "SHOT"


def test_the_screenshot_store_is_bounded():
    """A cohort sweep opens hundreds of pages and a JPEG is not small."""
    from app.services import link_renderer as lr
    lr.forget_screenshots()
    for i in range(lr._SCREENSHOT_KEEP + 5):
        lr._keep_screenshot(f"https://x/{i}", "S")
    assert len(lr._last_screenshot) <= lr._SCREENSHOT_KEEP


def test_an_empty_screenshot_is_not_stored():
    from app.services import link_renderer as lr
    lr.forget_screenshots()
    lr._keep_screenshot("https://x/1", "")
    assert lr._last_screenshot == {}


def test_a_refused_page_hands_back_no_picture():
    """A sign-in wall must never reach the marker as 'the learner's design'."""
    from app.services import link_renderer as lr
    src = open("app/services/link_renderer.py", encoding="utf-8").read()
    body = src[src.index("def read_rendered_page("):]
    body = body[:body.index("\n# The most recent screenshot")]
    assert 'if why:' in body and 'return text, why, ""' in body


# ── the screenshot store under concurrent reviews ───────────────────────────
#
# Found in self-review, 23 Aug: `len() >= KEEP` followed by
# `pop(next(iter(...)))` is a race. Another worker can empty the dict in
# between, next() raises StopIteration, and a review that had already
# SUCCEEDED dies for the sake of a screenshot. Reviews run concurrently, so
# this was reachable in every cohort sweep.

def test_the_screenshot_store_survives_concurrent_reviews():
    import threading
    from app.services import link_renderer as lr

    lr.forget_screenshots()
    errors = []

    def hammer(worker):
        try:
            for i in range(300):
                lr._keep_screenshot(f"https://x/{worker}-{i}", "S")
                lr._last_screenshot.get(f"https://x/{worker}-{i}")
        except Exception as e:                      # pragma: no cover
            errors.append(repr(e))

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], errors
    assert len(lr._last_screenshot) <= lr._SCREENSHOT_KEEP


def test_eviction_is_guarded_by_a_lock():
    src = open("app/services/link_renderer.py", encoding="utf-8").read()
    body = src[src.index("def _keep_screenshot("):src.index("def forget_screenshots(")]
    assert "with _screenshot_lock:" in body
    assert "except StopIteration" in body, \
        "the lock alone is not enough if forget_screenshots() runs mid-evict"


def test_a_picture_never_leaks_from_one_link_to_the_next():
    """`picture` must be re-initialised per URL inside the loop, or student A's
    page reaches the marker attached to student A's SECOND link."""
    src = open("app/utils/submission_intake.py", encoding="utf-8").read()
    body = src[src.index("def from_links_in("):]
    body = body[:body.index("\ndef _is_thin_body")]
    loop = body.index("for url in find_urls")
    init = body.index('picture = ""')
    assert init > loop, "picture is initialised outside the loop — it will leak"
