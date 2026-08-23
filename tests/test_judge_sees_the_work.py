"""PHASE 1: the marker looks at the work instead of reading a description of it.

Until 23 Aug 2026 every submission was flattened to text before it was judged.
A poster became OCR'd words. A Lovable site became its visible headings. A mind
map became a list. The judge call carried text blocks and nothing else, so
"score it on quality, like a human" was impossible by construction — the human
in that metaphor never saw anything.

These tests hold the door open: images reach the model, unusable ones are
refused loudly, and the forced-tool instruction still lands where the model
will read it.
"""

import pytest

from app.services import ai_service as ai


PNG = "iVBORw0KGgoAAAANSUhEUg=="


# ── blocks ──────────────────────────────────────────────────────────────────

def test_a_text_block_is_unchanged():
    assert ai._content_block({"text": "the task"}) == {"type": "text",
                                                       "text": "the task"}


def test_a_cached_text_block_keeps_its_marker():
    b = ai._content_block({"text": "requirements", "cache": True})
    assert b["cache_control"] == {"type": "ephemeral"}


def test_an_image_block_reaches_the_model_as_an_image():
    b = ai._content_block({"image": PNG, "media_type": "image/png"})
    assert b["type"] == "image"
    assert b["source"] == {"type": "base64", "media_type": "image/png",
                           "data": PNG}


def test_png_is_assumed_when_the_type_is_missing():
    assert ai._content_block({"image": PNG})["source"]["media_type"] == "image/png"


@pytest.mark.parametrize("mt", ai.IMAGE_MEDIA_TYPES)
def test_every_accepted_type_passes(mt):
    assert ai._content_block({"image": PNG, "media_type": mt})["type"] == "image"


@pytest.mark.parametrize("mt", ["image/tiff", "image/bmp", "application/pdf",
                                "audio/mpeg", "video/mp4"])
def test_an_unusable_image_is_refused_loudly_not_dropped(mt):
    """Silently dropping it would leave the marker believing it had seen the
    work. That is the exact failure this system keeps making."""
    with pytest.raises(ValueError):
        ai._content_block({"image": PNG, "media_type": mt})


def test_an_image_is_never_cached():
    """Images differ per learner; a cache write on one costs more than it
    can ever save."""
    b = ai._content_block({"image": PNG, "cache": True})
    assert "cache_control" not in b


# ── the request the judge actually receives ─────────────────────────────────

def _content(blocks, monkeypatch):
    """Capture the content list call_structured would send, without calling
    anything."""
    captured = {}

    class _Stop(Exception):
        pass

    def fake_provider(*a, **k):
        raise _Stop()

    monkeypatch.setattr(ai, "MODEL_TIERS",
                        {"default": lambda: "claude-haiku-4-5"}, raising=False)
    import app.services.ai_service as mod
    real = mod._content_block

    def spy(b):
        out = real(b)
        captured.setdefault("blocks", []).append(out)
        return out

    monkeypatch.setattr(mod, "_content_block", spy)
    try:
        ai.call_structured(blocks, {"type": "object"}, tier="default")
    except Exception:
        pass
    return captured.get("blocks", [])


def test_a_mixed_request_keeps_both_kinds_in_order(monkeypatch):
    parts = _content([{"text": "what the task asked for", "cache": True},
                      {"image": PNG},
                      {"text": "the learner's own words"}], monkeypatch)
    assert [p["type"] for p in parts] == ["text", "image", "text"]


def test_a_request_with_no_text_at_all_is_refused():
    """An image with no task beside it is not a review."""
    with pytest.raises(ValueError):
        ai.call_structured([{"image": PNG}], {"type": "object"})


def test_the_tool_instruction_lands_on_a_text_block_not_an_image():
    """If it were appended to a trailing image the model would answer in
    prose and every review on that path would fail schema validation."""
    src = open("app/services/ai_service.py", encoding="utf-8").read()
    body = src[src.index("def call_structured("):]
    body = body[:body.index("\n    kwargs = {")]
    assert 'p.get("type") == "text"' in body
    assert "content[-1] = {**content[-1]" not in body, \
        "the old unconditional append would land on an image"
